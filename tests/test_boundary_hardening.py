"""The app→Composer boundary on the runtime volume, the executor socket and the Logs view.

`web` and `worker` can write the runtime volume. A FIFO, a symlink or a huge file
at a request name must never stall the agent or the executor, or make them act on
somebody else's file; a project page must never publish a link's target; the
Logs view keeps the newest lines and shows them as plain text.
"""

import io
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from composer import executor_protocol as proto
from composer import maintenance_page as mp
from composer import ops, relay
from composer.agent_protocol import redact_text
from composer.executor import Executor
from composer.output_utils import strip_terminal_controls
from composer.ops import OpsResponder
from composer.relay import RelayResponder, operation_digest
from composer.watcher import WatchRuntime

#: A call that blocks on a FIFO would hang the test run; each runs in a thread
#: and must return within this many seconds.
DEADLINE = 5.0


def _within(call, seconds=DEADLINE):
    """``call()``'s value, or AssertionError when it did not return in time."""
    box = {}

    def run():
        try:
            box["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise AssertionError(f"blocked for more than {seconds} s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _args(trigger, **overrides):
    base = dict(
        trigger_file=str(trigger), package_trigger_file=None, status_file=None,
        log_file=None, interval=2, dev=False, file=None, check_image=[],
        availability_file=None, check_interval=900, once=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _quiet():
    return redirect_stdout(io.StringIO())


class StateDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)
        self.trigger = self.state / "image-update-request.json"

    def runtime(self, **overrides):
        return WatchRuntime(_args(self.trigger, **overrides))

    def write(self, name, payload):
        temporary = self.state / f".{name}.tmp"
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, self.state / name)


class RequestFileTests(StateDirTestCase):
    """R2-004: FIFOs, symlinks and oversized files at request names."""

    def test_a_fifo_at_ops_request_does_not_stall_the_loop(self):
        os.mkfifo(self.state / "ops-request.json")
        runtime = self.runtime()
        with _quiet():
            _within(runtime.maybe_answer_ops_request)
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.state / "ops-request.json.refused").st_mode),
                        "the FIFO is set aside")

    def test_a_check_request_is_answered_while_a_fifo_sits_at_ops_request(self):
        os.mkfifo(self.state / "ops-request.json")
        self.write("check-request.json", {"schema_version": 1, "token": "check-1"})
        runtime = self.runtime()
        with _quiet():
            _within(runtime.maybe_answer_ops_request)
            _within(runtime.maybe_answer_check_request)
        ack = json.loads((self.state / "check-request.json.ack").read_text(encoding="utf-8"))
        self.assertEqual(ack["token"], "check-1")

    def test_a_fifo_at_the_image_trigger_is_refused_and_the_next_real_request_runs(self):
        os.mkfifo(self.trigger)
        runtime = self.runtime()
        with _quiet():
            self.assertIsNone(_within(runtime.pending_request))
        self.write(self.trigger.name, {"token": "update-1"})
        request = _within(runtime.pending_request)
        self.assertEqual(request["token"], "update-1")

    def test_a_symlinked_ops_request_is_never_executed(self):
        elsewhere = self.state / "elsewhere.json"
        elsewhere.write_text(json.dumps({"schema_version": 1, "token": "t-link", "operation": "services"}))
        os.symlink(elsewhere, self.state / "ops-request.json")
        runtime = self.runtime()
        with patch.dict("composer.ops.HANDLERS", {"services": lambda r, q: self.fail("a linked request ran")}), _quiet():
            runtime.maybe_answer_ops_request()
        self.assertFalse((self.state / "ops-request.json.ack").exists())
        self.assertTrue(elsewhere.is_file(), "the link's target is left alone")

    def test_an_oversized_request_is_not_read(self):
        big = self.state / "big"
        with open(big, "wb") as stream:
            stream.truncate(400 * 1024 * 1024)
        os.symlink(big, self.state / "ops-request.json")
        os.symlink(big, self.trigger)
        runtime = self.runtime()
        with _quiet():
            self.assertIsNone(_within(runtime.pending_request))
            _within(runtime.maybe_answer_ops_request)
        (self.state / "real.json").write_bytes(b"{" + b" " * (70 * 1024) + b"}")
        os.replace(self.state / "real.json", self.trigger)
        with _quiet():
            self.assertIsNone(_within(runtime.pending_request))

    def test_a_fifo_at_the_deploy_log_does_not_block_a_failure_note(self):
        from composer.watcher import _append_terminal_failure

        log = self.state / "deploy-log.txt"
        os.mkfifo(log)
        with _quiet():
            _within(lambda: _append_terminal_failure(str(log), "boom"))

    def test_the_relay_skips_a_fifo_and_a_dangling_link(self):
        responder = _relay(self.state, Path(self._tmp.name))
        responder.requests.mkdir(parents=True, exist_ok=True)
        os.mkfifo(responder.requests / f"{uuid.uuid4()}.json")
        os.symlink("/nonexistent", responder.requests / f"{uuid.uuid4()}.json")
        oid = _submit(responder)
        output = io.StringIO()
        with redirect_stdout(output):
            _within(responder.answer)
        self.assertNotIn("relay failed", output.getvalue())
        result = json.loads((responder.results / f"{oid}.json").read_text())
        self.assertEqual(result["status"], "ok")


def _relay(state, base):
    declared = base / "declared"
    declared.mkdir(exist_ok=True)
    page = {"name": "finance.rates_page", "url": "https://rates.example.com/exchange/",
            "response": {"type": "text", "max_bytes": 4096}}
    (declared / "operations.json").write_text(json.dumps({"schema_version": 1, "operations": [page]}))
    (declared / "operations.lock").write_text(json.dumps(
        {"schema_version": 1, "operations": {page["name"]: operation_digest(page)}}))
    agent = base / "agent"
    agent.mkdir(exist_ok=True)

    def fake(operation, params, secret=None):
        return {"http_status": 200, "content_type": "text/html", "bytes": 5, "data": "hello"}

    with patch.object(relay, "BUILTIN_OPERATIONS", []):
        responder = RelayResponder(state, agent, declared, perform_call=fake, composer_version="test")
    responder._clock = time.time
    return responder


def _submit(responder, *, created=None, lifetime=60):
    oid = str(uuid.uuid4())
    created = created or datetime.now(timezone.utc)
    body = {"schema_version": 1, "operation_id": oid, "op": "finance.rates_page", "params": {}, "sealed": None,
            "created_at": created.isoformat(), "expires_at": (created + timedelta(seconds=lifetime)).isoformat()}
    responder.requests.mkdir(parents=True, exist_ok=True)
    (responder.requests / f"{oid}.json").write_text(json.dumps(body))
    return oid


class RelayClockTests(unittest.TestCase):
    """R2-077 (b): request lifetime is bounded by the agent's clock."""

    def test_a_request_stamped_an_hour_ahead_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(relay, "BUILTIN_OPERATIONS", []):
            responder = _relay(Path(tmp) / "state", Path(tmp))
            oid = _submit(responder, created=datetime.now(timezone.utc) + timedelta(hours=1))
            with _quiet():
                responder.answer()
            result = json.loads((responder.results / f"{oid}.json").read_text())
        self.assertEqual((result["status"], result["error"]), ("rejected", "invalid"))


class ReplayTests(StateDirTestCase):
    """R2-081: a replay of an older token does not run the request again."""

    def _ops(self, token):
        self.write("ops-request.json", {"schema_version": 1, "token": token, "operation": "services"})

    def test_ops_tokens_a_b_a_run_a_once(self):
        runs = []
        handlers = {"services": lambda runtime, request: runs.append(request["token"]) or {"exit_code": 0, "findings": []}}
        with patch.dict("composer.ops.HANDLERS", handlers), _quiet():
            responder = OpsResponder(self.runtime())
            for token in ("A", "B", "A"):
                self._ops(token)
                responder.answer()
            restarted = OpsResponder(self.runtime())
            self._ops("A")
            restarted.answer()
        self.assertEqual(runs, ["A", "B"])

    def test_image_trigger_tokens_a_b_a_run_a_once(self):
        runtime = self.runtime()
        with patch("composer.watcher.subprocess.run", return_value=SimpleNamespace(returncode=0)) as child, _quiet():
            for token in ("A", "B", "A"):
                self.write(self.trigger.name, {"token": token})
                request = runtime.pending_request()
                if request:
                    runtime.process(request)
            self.write(self.trigger.name, {"token": "A"})
            self.assertIsNone(self.runtime().pending_request(), "a restart remembers recent tokens")
        self.assertEqual(child.call_count, 2)


class OpsOffTheLoopTests(StateDirTestCase):
    """R2-075: a hung operation does not hold check requests behind it."""

    def test_a_slow_operation_does_not_hold_a_check_request(self):
        release = threading.Event()
        handlers = {"services": lambda runtime, request: release.wait(10) and {"exit_code": 0, "findings": []}}
        runtime = self.runtime(once=False)
        self.write("ops-request.json", {"schema_version": 1, "token": "slow", "operation": "services"})
        self.write("check-request.json", {"schema_version": 1, "token": "check-2"})
        with patch.dict("composer.ops.HANDLERS", handlers), _quiet():
            try:
                _within(runtime.maybe_answer_ops_request, 2)
                _within(runtime.maybe_answer_check_request, 2)
                self.assertTrue((self.state / "check-request.json.ack").is_file())
            finally:
                release.set()
                if runtime._ops_thread is not None:
                    runtime._ops_thread.join(5)
        ack = json.loads((self.state / "ops-request.json.ack").read_text(encoding="utf-8"))
        self.assertEqual(ack["token"], "slow")


class ExecutorSocketTests(unittest.TestCase):
    """R2-076: idle peers, deeply nested frames and loose version checks."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bh", dir="/tmp")
        self.socket_path = os.path.join(self.tmp, "x.sock")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def _serve(self):
        def handler(request):
            return proto.build_result(request["operation_id"], "succeeded", exit_code=0, detail="ok")

        executor = Executor(self.socket_path, handler)
        threading.Thread(target=executor.serve_forever, daemon=True).start()
        self.addCleanup(executor.stop)
        self.assertTrue(executor.wait_until_listening(5))
        return executor

    def _exchange(self, frame, timeout=5):
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout)
        conn.connect(self.socket_path)
        try:
            conn.sendall(frame)
            data = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            return json.loads(data[4:].decode("utf-8")) if len(data) > 4 else {}
        finally:
            conn.close()

    def _request(self):
        return proto.encode_frame({"protocol_version": 1, "operation_id": str(uuid.uuid4()),
                                   "op": "restart", "payload": {"service": "web"}})

    def test_idle_peers_do_not_hold_every_slot(self):
        with patch("composer.executor._REQUEST_IDLE_TIMEOUT", 0.5):
            self._serve()
            idle = []
            for _ in range(4):
                peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                peer.connect(self.socket_path)
                idle.append(peer)
            self.addCleanup(lambda: [peer.close() for peer in idle])
            time.sleep(1.0)
            result = self._exchange(self._request())
        self.assertEqual(result.get("state"), "succeeded")

    def test_a_deeply_nested_frame_gets_a_typed_answer(self):
        body = b"[" * 100000 + b"]" * 100000
        body = body[: proto.MAX_EXECUTOR_MESSAGE_BYTES]
        with self.assertRaises(proto.ProtocolError):
            proto.read_frame(lambda n, data=[len(body).to_bytes(4, "big") + body]: _take(data, n))

    def test_only_the_integer_one_is_protocol_version_one(self):
        for value in (True, 1.0):
            request = {"protocol_version": value, "operation_id": str(uuid.uuid4()), "op": "restart",
                       "payload": {"service": "web"}}
            with self.assertRaises(proto.ProtocolError, msg=repr(value)):
                proto.validate_executor_request(request)

    def test_the_watch_loop_logs_what_it_swallows(self):
        from composer.executor import _run_watch_loop

        stop = threading.Event()

        class Broken:
            interval = 2

            def pending_request(self):
                stop.set()
                raise RuntimeError("trigger unreadable")

        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            _run_watch_loop(Broken(), threading.Lock(), stop)
        self.assertIn("trigger unreadable", stderr.getvalue())


def _take(data, n):
    chunk, data[0] = data[0][:n], data[0][n:]
    return chunk


class LogsTests(unittest.TestCase):
    """R2-026, R2-027, R2-084."""

    def _launcher(self, out):
        return SimpleNamespace(run_docker_compose=lambda args, timeout=None: (True, out, ""))

    def test_a_long_tail_keeps_the_newest_lines(self):
        lines = [f"2026-11-27T07:{i // 60:02d}:{i % 60:02d}.000Z line {i:04d} " + "x" * 300 for i in range(200)]
        out = "\n".join(f"web  | {line}" for line in lines)
        text = ops._read_logs(self._launcher(out), "", 200)
        self.assertTrue(text.rstrip().endswith("line 0199 " + "x" * 300), text[-80:])
        self.assertLessEqual(len(text), ops.MAX_LOG_CHARS)
        self.assertTrue(text.splitlines()[0].startswith("web | "), "the cut falls on a line boundary")

    def test_tail_redaction_covers_the_whole_text_and_leaks_no_partial_secret(self):
        secret = "s3cr3t-value-ABCDEFGHIJ"
        with patch.dict(os.environ, {"COMPOSER_INHERITED_SECRET_KEYS": "K", "K": secret}):
            text = ("a" * 100 + "\n") * 400 + f"token in log {secret}\n" + "tail line\n"
            kept = redact_text(text, 2000, keep="tail")
            self.assertIn("[REDACTED]", kept)
            self.assertTrue(kept.endswith("tail line"))
            straddling = "x" * 50 + secret + "\n" + "b" * 1990
            self.assertNotIn(secret[-6:], redact_text(straddling, 2000, keep="tail"))

    def test_invalid_utf8_in_a_log_does_not_blind_the_view(self):
        from composer.subprocess_runner import SubprocessRunnerMixin

        runner = SubprocessRunnerMixin()
        runner._prepare_command = lambda cmd: cmd
        runner._detached_child_kwargs = lambda: {}
        ok, out, _err = runner.run_command(["/bin/sh", "-c", r"printf 'x \300\257 y\n'"], timeout=10)
        self.assertTrue(ok)
        self.assertIn("x", out)
        self.assertIn("�", out)

    def test_bidi_controls_are_shown_as_code_points(self):
        shown = strip_terminal_controls("user=admin ‮gnp.exe‬ and ⁦x⁩")
        self.assertEqual(shown, "user=admin <U+202E>gnp.exe<U+202C> and <U+2066>x<U+2069>")
        self.assertEqual(strip_terminal_controls("مرحبا ‏ ok"), "مرحبا ‏ ok")


class MaintenancePageTests(unittest.TestCase):
    """R2-005, R2-022, R2-080, R2-087."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.project = self.base / "project"
        self.override = self.project / mp.OVERRIDE
        self.override.mkdir(parents=True)
        (self.override / "index.html").write_text("<p>ours</p>", encoding="utf-8")
        self.runtime = self.base / "runtime"
        (self.runtime / "state").mkdir(parents=True)
        (self.project / ".secrets").mkdir()
        (self.project / ".secrets" / ".env").write_text("SECRET_KEY=decoy-not-a-secret\n")
        self.served = mp.published_dir(self.runtime)

    def _served_bytes(self):
        return b"".join(path.read_bytes() for path in self.served.iterdir() if path.is_file())

    def test_a_symlink_in_the_override_is_refused_and_nothing_is_published(self):
        os.symlink("../../.secrets/.env", self.override / "brand.css")
        with self.assertRaises(mp.OverrideRefused) as caught:
            mp.publish_override(self.override, self.runtime)
        self.assertIn("brand.css is a symbolic link", str(caught.exception))
        self.assertFalse(self.served.exists() and any(self.served.iterdir()))

    def test_the_agent_tick_never_serves_a_link_target(self):
        os.symlink("../../.secrets/.env", self.override / "brand.css")
        with _quiet():
            mp.publish(self.runtime, project_root=self.project)
        self.assertNotIn(b"decoy-not-a-secret", self._served_bytes())
        self.assertEqual((self.served / "index.html").read_bytes(), (mp.BUNDLE / "index.html").read_bytes())

    def test_fifos_dotfiles_folders_and_other_types_are_not_published(self):
        os.mkfifo(self.override / "pipe.css")
        with self.assertRaises(mp.OverrideRefused):
            _within(lambda: mp.publish_override(self.override, self.runtime))
        (self.override / "pipe.css").rename(self.base / "pipe")
        (self.override / ".env").write_text("X=1")
        (self.override / "notes.txt").write_text("internal")
        (self.override / "img").mkdir()
        count, skipped = mp.publish_override(self.override, self.runtime)
        self.assertEqual(count, 1)
        self.assertEqual(sorted(skipped), [".env", "img/", "notes.txt"])
        self.assertEqual(sorted(path.name for path in self.served.iterdir()), [mp.CUSTOM_MARKER, "index.html"])

    def test_an_override_folder_outside_the_project_is_refused(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "index.html").write_text("<p>outside</p>")
        os.symlink(outside, self.project / ".proxy" / "elsewhere")
        with self.assertRaises(mp.OverrideRefused):
            mp.read_override(self.project / ".proxy" / "elsewhere", self.project)

    def test_the_page_comes_from_the_project_not_from_a_marker_on_the_volume(self):
        with _quiet():
            mp.publish(self.runtime, project_root=self.project)
        self.assertEqual((self.served / "index.html").read_text(), "<p>ours</p>")
        # The app plants its own page and marker; the next tick puts the project page back.
        os.chmod(self.served, 0o777)
        (self.served / "index.html").unlink()
        (self.served / "index.html").write_text("<script>alert(1)</script>")
        (self.served / "evil.html").write_text("<script>alert(2)</script>")
        with _quiet():
            self.assertTrue(mp.publish(self.runtime, project_root=self.project))
        self.assertEqual((self.served / "index.html").read_text(), "<p>ours</p>")
        self.assertFalse((self.served / "evil.html").exists())
        self.assertEqual(stat.S_IMODE(os.lstat(self.served).st_mode), mp.PUBLISHED_DIR_MODE)
        self.assertEqual(stat.S_IMODE(os.lstat(self.served / "index.html").st_mode), mp.PUBLISHED_FILE_MODE)

    def test_without_an_override_a_planted_marker_does_not_keep_a_planted_page(self):
        (self.override / "index.html").unlink()
        self.override.rmdir()
        self.served.mkdir(parents=True)
        (self.served / "index.html").write_text("<script>alert(1)</script>")
        (self.served / mp.CUSTOM_MARKER).write_text("project override\n")
        with _quiet():
            mp.publish(self.runtime, project_root=self.project)
        self.assertEqual((self.served / "index.html").read_bytes(), (mp.BUNDLE / "index.html").read_bytes())
        self.assertFalse((self.served / mp.CUSTOM_MARKER).exists())

    def test_a_swapped_folder_is_replaced(self):
        with _quiet():
            mp.publish(self.runtime, project_root=self.project)
        self.served.rename(self.served.with_name("taken"))
        os.symlink(self.base, self.served)
        with _quiet():
            mp.publish(self.runtime, project_root=self.project)
        self.assertTrue(stat.S_ISDIR(os.lstat(self.served).st_mode))
        self.assertEqual((self.served / "index.html").read_text(), "<p>ours</p>")

    def test_the_wrapper_refuses_a_runtime_root_that_is_not_the_volume(self):
        from composer.maintenance_cli import run_maintenance

        stderr = io.StringIO()
        with patch("sys.stderr", stderr), _quiet():
            code = run_maintenance("publish", ["--runtime-root", str(self.base / "absent"),
                                               "--override", str(self.override)])
        self.assertEqual(code, 2)
        self.assertIn("not the stack's runtime volume", stderr.getvalue())
        self.assertFalse((self.base / "absent").exists())

    def test_the_bundled_page_is_neutral_until_an_update_reports_progress(self):
        html = (mp.BUNDLE / "index.html").read_text(encoding="utf-8")
        script = (mp.BUNDLE / "maintenance.js").read_text(encoding="utf-8")
        self.assertNotIn("applying the verified image update", html)
        self.assertIn("temporarily unavailable", html)
        self.assertIn('ar: {', script)
        self.assertIn('dir: "rtl"', script)
        self.assertIn("isActiveUpdate", script)


_PAGE_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [script, docJson, lang] = process.argv.slice(-3);
const elements = {};
const element = () => ({ textContent: "", hidden: false, style: {}, scrollTop: 0, scrollHeight: 0,
  classList: { remove() {}, add() {} }, setAttribute() {} });
const document = { title: "", documentElement: {},
  getElementById: (id) => elements[id] || (elements[id] = element()) };
const respond = (body) => Promise.resolve({ ok: true, json: () => Promise.resolve(body),
  text: () => Promise.resolve("") });
const context = { document, navigator: { languages: [lang] }, Date, String, isNaN, Promise,
  fetch: (url) => url.endsWith("status.json") ? respond(JSON.parse(docJson)) : respond(""),
  window: { location: { href: "https://site.test/" }, setInterval() {}, setTimeout() { return 1; } } };
vm.runInNewContext(fs.readFileSync(script, "utf8"), context);
setImmediate(() => setImmediate(() => process.stdout.write(JSON.stringify({
  title: document.title, heading: elements.heading.textContent, meta_hidden: elements.meta.hidden }))));
"""


@unittest.skipUnless(shutil.which("node"), "node runs the maintenance page script")
class MaintenancePageWordingTests(unittest.TestCase):
    """R2-086(c): a failed or ended run gets the neutral page, never "update in progress"."""

    def render(self, doc, lang="en"):
        out = subprocess.run(
            ["node", "-e", _PAGE_HARNESS, "--", str(mp.BUNDLE / "maintenance.js"), json.dumps(doc), lang],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return json.loads(out.stdout)

    def now(self, minutes=0):
        return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()

    def test_a_running_update_shows_the_update_wording(self):
        page = self.render({"status": "recreating", "updated_at": self.now()})
        self.assertEqual(page["title"], "Update in progress")
        self.assertFalse(page["meta_hidden"])

    def test_a_failed_or_ended_run_is_neutral_in_english_and_arabic(self):
        failed = [
            {"status": "failed", "updated_at": self.now(), "error": "Failed to start containers"},
            {"status": "rolled_back", "updated_at": self.now()},
            {"status": "recreating", "updated_at": self.now(), "error": "pre_start[0] exited with code 1"},
            {"status": "rollback_failed", "updated_at": self.now()},
            {"status": "error", "updated_at": self.now()},
            {"status": "recreating", "updated_at": self.now(minutes=20)},
        ]
        for doc in failed:
            with self.subTest(doc=doc):
                english = self.render(doc)
                self.assertEqual(english["title"], "Temporarily unavailable")
                self.assertEqual(english["heading"], "This site is temporarily unavailable")
                self.assertTrue(english["meta_hidden"])
                arabic = self.render(doc, "ar")
                self.assertEqual(arabic["title"], "الموقع غير متاح مؤقتًا")
                self.assertTrue(arabic["meta_hidden"])


if __name__ == "__main__":
    unittest.main()
