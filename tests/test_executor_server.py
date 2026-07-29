import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from composer import executor_protocol as proto
from composer.executor import Executor


def _valid_restart():
    return {
        "protocol_version": proto.EXECUTOR_PROTOCOL_VERSION,
        "operation_id": str(uuid.uuid4()),
        "op": "restart",
        "payload": {"service": "web"},
    }


def _client_request(socket_path, obj, timeout=5):
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    conn.connect(socket_path)
    try:
        conn.sendall(proto.encode_frame(obj))

        def recv_exactly(n):
            chunks = []
            remaining = n
            while remaining > 0:
                chunk = conn.recv(remaining)
                if not chunk:
                    raise AssertionError("connection closed before a full result frame")
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

        return proto.read_frame(recv_exactly)
    finally:
        conn.close()


class ExecutorServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.socket_path = os.path.join(self.tmp, "exec.sock")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _serve(self, handler, **kwargs):
        executor = Executor(self.socket_path, handler, **kwargs)
        thread = threading.Thread(target=executor.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(executor.stop)
        for _ in range(100):
            if os.path.exists(self.socket_path):
                break
            time.sleep(0.02)
        self.assertTrue(os.path.exists(self.socket_path), "executor socket never appeared")
        return executor

    def test_valid_request_reaches_handler_and_returns_result(self):
        seen = {}

        def handler(request):
            seen.update(request)
            return proto.build_result(request["operation_id"], "succeeded", exit_code=0, detail="ok")

        self._serve(handler)
        result = _client_request(self.socket_path, _valid_restart())
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(seen["op"], "restart")
        self.assertEqual(seen["payload"], {"service": "web"})

    def test_unknown_op_rejected_without_invoking_handler(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return proto.build_result(request["operation_id"], "succeeded")

        self._serve(handler)
        bad = {"protocol_version": 1, "operation_id": str(uuid.uuid4()), "op": "image_update", "payload": {}}
        result = _client_request(self.socket_path, bad)
        self.assertEqual(result["state"], "rejected")
        self.assertEqual(calls["n"], 0)

    def test_protocol_version_mismatch_rejected(self):
        self._serve(lambda r: proto.build_result(r["operation_id"], "succeeded"))
        skewed = _valid_restart()
        skewed["protocol_version"] = 999
        result = _client_request(self.socket_path, skewed)
        self.assertEqual(result["state"], "rejected")

    def test_second_operation_is_rejected_as_busy_while_one_is_in_flight(self):
        started = threading.Event()
        release = threading.Event()

        def handler(request):
            started.set()
            release.wait(3)
            return proto.build_result(request["operation_id"], "succeeded")

        self._serve(handler)
        first = {}

        def run_first():
            first["result"] = _client_request(self.socket_path, _valid_restart(), timeout=6)

        t = threading.Thread(target=run_first, daemon=True)
        t.start()
        self.assertTrue(started.wait(3), "first op never started")

        second = _client_request(self.socket_path, _valid_restart(), timeout=6)
        self.assertEqual(second["state"], "rejected")
        self.assertIn("busy", second["detail"].lower())

        release.set()
        t.join(3)
        self.assertEqual(first["result"]["state"], "succeeded")

    def test_handler_exception_becomes_failed_result(self):
        def handler(request):
            raise RuntimeError("kaboom")

        self._serve(handler)
        result = _client_request(self.socket_path, _valid_restart())
        self.assertEqual(result["state"], "failed")

    def test_bind_refuses_symlinked_socket_path(self):
        target = os.path.join(self.tmp, "real")
        link = os.path.join(self.tmp, "link.sock")
        open(target, "w").close()
        os.symlink(target, link)
        executor = Executor(link, lambda r: proto.build_result(r["operation_id"], "succeeded"))
        with self.assertRaises(proto.ProtocolError):
            executor._bind()


class ExecutorAuthTests(unittest.TestCase):
    def _executor(self, expected):
        return Executor("/unused.sock", lambda r: r, expected_peer_uid=expected)

    def test_no_expected_uid_allows(self):
        self.assertTrue(self._executor(None)._authorized(object()))

    def test_matching_uid_allows(self):
        ex = self._executor(4242)
        with patch("composer.executor._peer_uid", return_value=4242):
            self.assertTrue(ex._authorized(object()))

    def test_wrong_uid_rejected(self):
        ex = self._executor(4242)
        with patch("composer.executor._peer_uid", return_value=9999):
            self.assertFalse(ex._authorized(object()))

    def test_unavailable_peercred_degrades_to_filesystem_perms(self):
        ex = self._executor(4242)
        with patch("composer.executor._peer_uid", return_value=None):
            self.assertTrue(ex._authorized(object()))


if __name__ == "__main__":
    unittest.main()
