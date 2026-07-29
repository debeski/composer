import unittest
import uuid

from composer import executor_protocol as proto
from composer.agent_protocol import ProtocolError


def _req(op="restart", payload=None, **over):
    base = {
        "protocol_version": proto.EXECUTOR_PROTOCOL_VERSION,
        "operation_id": str(uuid.uuid4()),
        "op": op,
        "payload": {"service": "web"} if payload is None else payload,
    }
    base.update(over)
    return base


class ExecutorProtocolValidationTests(unittest.TestCase):
    def test_valid_restart(self):
        out = proto.validate_executor_request(_req("restart", {"service": "web"}))
        self.assertEqual(out["op"], "restart")
        self.assertEqual(out["payload"], {"service": "web"})

    def test_valid_restart_empty_service_allowed(self):
        out = proto.validate_executor_request(_req("restart", {"service": ""}))
        self.assertEqual(out["payload"], {"service": ""})

    def test_valid_recovery_requires_reason(self):
        out = proto.validate_executor_request(
            _req("recovery_deploy", {"force": False, "reason": "disk full"})
        )
        self.assertEqual(out["payload"]["reason"], "disk full")
        with self.assertRaises(ProtocolError):
            proto.validate_executor_request(_req("recovery_deploy", {"force": False, "reason": ""}))

    def test_unknown_op_rejected(self):
        with self.assertRaises(ProtocolError):
            proto.validate_executor_request(_req("image_update", {}))  # not on the socket surface
        with self.assertRaises(ProtocolError):
            proto.validate_executor_request(_req("nuke", {}))

    def test_protocol_version_mismatch_rejected(self):
        with self.assertRaises(ProtocolError):
            proto.validate_executor_request(_req(protocol_version=2))
        with self.assertRaises(ProtocolError):
            proto.validate_executor_request(_req(protocol_version=None))

    def test_bad_operation_id_rejected(self):
        with self.assertRaises(ProtocolError):
            proto.validate_executor_request(_req(operation_id="not-a-uuid"))

    def test_unexpected_top_level_field_rejected(self):
        with self.assertRaises(ProtocolError):
            proto.validate_executor_request(_req(extra="x"))

    def test_unexpected_payload_field_rejected(self):
        with self.assertRaises(ProtocolError):
            proto.validate_executor_request(_req("restart", {"service": "web", "cmd": "rm"}))

    def test_invalid_service_name_rejected(self):
        with self.assertRaises(ProtocolError):
            proto.validate_executor_request(_req("restart", {"service": "web; rm -rf /"}))

    def test_recovery_force_must_be_bool(self):
        with self.assertRaises(ProtocolError):
            proto.validate_executor_request(_req("recovery_deploy", {"force": "yes", "reason": "x"}))

    def test_oversize_request_rejected(self):
        big = _req("recovery_deploy", {"force": False, "reason": "x"})
        big["payload"]["reason"] = "a" * (proto.MAX_EXECUTOR_MESSAGE_BYTES + 10)
        with self.assertRaises(ProtocolError):
            proto.validate_executor_request(big)

    def test_non_dict_rejected(self):
        with self.assertRaises(ProtocolError):
            proto.validate_executor_request(["not", "a", "dict"])


class ExecutorResultTests(unittest.TestCase):
    def test_build_result_redacts_and_shapes(self):
        res = proto.build_result("op-1", "failed", exit_code=2, detail="token=abcd1234 boom")
        self.assertEqual(res["state"], "failed")
        self.assertEqual(res["exit_code"], 2)
        self.assertNotIn("abcd1234", res["detail"])
        self.assertEqual(res["protocol_version"], proto.EXECUTOR_PROTOCOL_VERSION)

    def test_invalid_state_rejected(self):
        with self.assertRaises(ProtocolError):
            proto.build_result("op-1", "weird")


class ExecutorFramingTests(unittest.TestCase):
    def _reader_for(self, data):
        buf = {"pos": 0}

        def recv_exactly(n):
            start = buf["pos"]
            end = start + n
            if end > len(data):
                raise ProtocolError("short read")
            buf["pos"] = end
            return data[start:end]

        return recv_exactly

    def test_encode_read_roundtrip(self):
        obj = _req("restart", {"service": "web"})
        frame = proto.encode_frame(obj)
        got = proto.read_frame(self._reader_for(frame))
        self.assertEqual(got["op"], "restart")

    def test_read_frame_rejects_oversize_length(self):
        header = (proto.MAX_EXECUTOR_MESSAGE_BYTES + 1).to_bytes(4, "big")
        with self.assertRaises(ProtocolError):
            proto.read_frame(self._reader_for(header))

    def test_read_frame_rejects_zero_length(self):
        with self.assertRaises(ProtocolError):
            proto.read_frame(self._reader_for((0).to_bytes(4, "big")))

    def test_read_frame_rejects_bad_json(self):
        body = b"{not json"
        frame = len(body).to_bytes(4, "big") + body
        with self.assertRaises(ProtocolError):
            proto.read_frame(self._reader_for(frame))


if __name__ == "__main__":
    unittest.main()
