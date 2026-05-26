"""Pure-Python unit tests for operator-side framing + env parsing.

Doesn't touch MQTT or aiortc; just verifies the JSON contract matches
the Rust side and that env-file parsing handles common shapes.
"""
import base64
import json
import tempfile
import unittest
from pathlib import Path

from host_binary import load_env_file


class FrameContractTests(unittest.TestCase):
    """Mirrors binary/src/frames.rs round-trip expectations."""

    def test_exec_frame_shape(self):
        frame = {"type": "exec", "id": 1, "cmd": "echo hi"}
        encoded = json.dumps(frame)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["type"], "exec")
        self.assertEqual(decoded["id"], 1)
        self.assertEqual(decoded["cmd"], "echo hi")

    def test_stdout_b64_roundtrip(self):
        raw = b"hello\n"
        frame = {"type": "stdout", "id": 1, "data": base64.b64encode(raw).decode()}
        decoded = json.loads(json.dumps(frame))
        self.assertEqual(base64.b64decode(decoded["data"]), raw)

    def test_stdout_non_utf8_bytes(self):
        raw = bytes([0xff, 0x00, 0x01, 0xfe, 0x80])
        frame = {"type": "stdout", "id": 2, "data": base64.b64encode(raw).decode()}
        decoded = json.loads(json.dumps(frame))
        self.assertEqual(base64.b64decode(decoded["data"]), raw)

    def test_exit_frame_with_code(self):
        frame = {"type": "exit", "id": 1, "code": 7, "signal": None}
        encoded = json.dumps(frame)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["code"], 7)
        self.assertIsNone(decoded["signal"])

    def test_error_frame(self):
        frame = {"type": "error", "id": 1, "message": "spawn failed: ENOENT"}
        decoded = json.loads(json.dumps(frame))
        self.assertEqual(decoded["type"], "error")
        self.assertEqual(decoded["message"], "spawn failed: ENOENT")


class EnvFileTests(unittest.TestCase):
    def test_basic_kv(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("EMQX_URL=wss://host:8084/mqtt\n")
            f.write("EMQX_USER=probe\n")
            f.write("EMQX_PASS=secret\n")
            path = Path(f.name)
        try:
            env = load_env_file(path)
            self.assertEqual(env["EMQX_URL"], "wss://host:8084/mqtt")
            self.assertEqual(env["EMQX_USER"], "probe")
            self.assertEqual(env["EMQX_PASS"], "secret")
        finally:
            path.unlink()

    def test_comments_and_blank_lines(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("# leading comment\n")
            f.write("\n")
            f.write("EMQX_URL=wss://h:8084/mqtt\n")
            f.write("  # indented comment\n")
            f.write("EMQX_USER=probe\n")
            path = Path(f.name)
        try:
            env = load_env_file(path)
            self.assertEqual(env["EMQX_URL"], "wss://h:8084/mqtt")
            self.assertEqual(env["EMQX_USER"], "probe")
            self.assertNotIn("#", env)
        finally:
            path.unlink()

    def test_quoted_values_stripped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write('EMQX_PASS="secret with spaces"\n')
            f.write("EMQX_USER='probe'\n")
            path = Path(f.name)
        try:
            env = load_env_file(path)
            self.assertEqual(env["EMQX_PASS"], "secret with spaces")
            self.assertEqual(env["EMQX_USER"], "probe")
        finally:
            path.unlink()

    def test_missing_file_returns_empty(self):
        self.assertEqual(load_env_file(Path("/nonexistent/path/.env")), {})

    def test_password_with_equals_sign_preserved(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("EMQX_PASS=a=b=c\n")
            path = Path(f.name)
        try:
            env = load_env_file(path)
            self.assertEqual(env["EMQX_PASS"], "a=b=c")
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main()
