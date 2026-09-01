"""安全相关回归测试：日志脱敏、会话关闭错误隔离、数据文件隔离。"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config as config_module
from backend import main as main_module
from backend.config import Config
from backend.logger import SecretRedactFilter, redact, redact_exc


class _FakeWebSocket:
    def __init__(self, origin, messages):
        self.headers = {"origin": origin} if origin is not None else {}
        self._messages = list(messages)
        self.accepted = False
        self.closed_code = None
        self.sent = []

    async def accept(self):
        self.accepted = True

    async def close(self, code=None):
        self.closed_code = code

    async def send_json(self, message):
        self.sent.append(message)

    async def receive(self):
        if self._messages:
            return self._messages.pop(0)
        raise main_module.WebSocketDisconnect()


class _FakeWebSocketSession:
    state = "listening"

    def __init__(self):
        self.handle_pcm = AsyncMock()
        self.stop_current_streams = AsyncMock()


class TestSessionClose(unittest.IsolatedAsyncioTestCase):
    async def test_internal_error_is_redacted_and_not_returned(self):
        raw_secret = "SYNTHETIC_CLOSE_SECRET_12345"

        async def failing_get_session():
            raise RuntimeError(f"downstream auth failed: {raw_secret}")

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": raw_secret}), \
                patch.object(main_module, "get_session", failing_get_session), \
                self.assertLogs(main_module.logger, level="ERROR") as captured:
            response = await main_module.close_session()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.body, b'{"ok":false,"error":"internal_error"}')
        self.assertNotIn(raw_secret, "\n".join(captured.output))

    async def test_diagnostics_masks_livetalking_session_id(self):
        full_id = "live-session-secret-123456"

        class FakeSession:
            def diagnostics(self):
                return {"state": "listening", "session_id": full_id}

        async def fake_get_session():
            return FakeSession()

        with patch.object(main_module, "get_session", fake_get_session):
            response = await main_module.diagnostics_data()

        payload = json.loads(response.body)
        self.assertNotEqual(payload["session_id"], full_id)
        self.assertIn("***", payload["session_id"])


class TestWebSocketSecurity(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main_module._active_websockets.clear()
        self.session = _FakeWebSocketSession()

    async def asyncTearDown(self):
        main_module._active_websockets.clear()

    async def test_loopback_origin_accepts_and_handles_valid_pcm(self):
        ws = _FakeWebSocket("http://localhost:7870", [{"type": "websocket.receive", "bytes": bytes(640)}])

        async def fake_get_session():
            return self.session

        with patch.object(main_module, "get_session", fake_get_session):
            await main_module.websocket_endpoint(ws)

        self.assertTrue(ws.accepted)
        self.assertIsNone(ws.closed_code)
        self.session.handle_pcm.assert_awaited_once_with(bytes(640))

    async def test_non_loopback_origin_rejected_before_accept_or_session(self):
        ws = _FakeWebSocket("http://192.168.1.5:7870", [])
        get_session = AsyncMock(side_effect=AssertionError("session must not be created"))

        with patch.object(main_module, "get_session", get_session):
            await main_module.websocket_endpoint(ws)

        self.assertFalse(ws.accepted)
        self.assertEqual(ws.closed_code, 1008)
        get_session.assert_not_awaited()

    async def test_invalid_pcm_frame_is_dropped_before_session(self):
        ws = _FakeWebSocket("https://127.0.0.1:7870", [{"type": "websocket.receive", "bytes": bytes(638)}])

        async def fake_get_session():
            return self.session

        with patch.object(main_module, "get_session", fake_get_session):
            await main_module.websocket_endpoint(ws)

        self.assertTrue(ws.accepted)
        self.session.handle_pcm.assert_not_awaited()


class TestLogRedaction(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        cfg = Config()
        cfg._raw = {
            "app": {"host": "127.0.0.1", "port": 7870},
            "asr": {"model": "dummy"},
            "llm": {"model": "dummy", "base_url": ""},
            "tts": {},
            "audio": {},
            "livetalking": {},
            "storage": {"db_path": str(self.tmp / "test.db")},
            "logging": {"directory": str(self.tmp / "logs"), "level": "DEBUG"},
        }
        cfg._secrets = {
            "DASHSCOPE_API_KEY": "SYNTHETIC_NOT_A_SECRET_DASHSCOPE_12345",
            "DEEPSEEK_API_KEY": "SYNTHETIC_NOT_A_SECRET_DEEPSEEK_67890",
            "HUOSHAN_APPID": "SYNTHETIC_NOT_A_SECRET_APPID_ABCDE",
            "HUOSHAN_ACCESS_TOKEN": "SYNTHETIC_NOT_A_SECRET_TOKEN_XYZ",
        }
        config_module._config = cfg

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_secret_filter_replaces_values(self):
        filt = SecretRedactFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
             msg="Authorization: Bearer SYNTHETIC_NOT_A_SECRET_DASHSCOPE_12345",
            args=(),
            exc_info=None,
        )
        filt.filter(record)
        self.assertNotIn("SYNTHETIC_NOT_A_SECRET_DASHSCOPE_12345", record.msg)
        self.assertIn("***", record.msg)

    def test_redact_exc_hides_secret(self):
        fake_secret = "SYNTHETIC_NOT_A_SECRET_DEEPSEEK_67890"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": fake_secret}):
            exc = ValueError(f"auth failed with {fake_secret}")
            text = redact_exc(exc)
        self.assertNotIn(fake_secret, text)
        self.assertIn("auth failed with", text)
        self.assertIn("***", text)

    def test_redact_dict_masks_sensitive_keys(self):
        data = {
            "api_key": "should-be-masked",
            "access_token": "should-be-masked",
            "appid": "should-be-masked",
            "safe_key": "visible-value",
        }
        redacted = redact(data)
        self.assertEqual(redacted["api_key"], "***")
        self.assertEqual(redacted["access_token"], "***")
        self.assertEqual(redacted["appid"], "***")
        self.assertEqual(redacted["safe_key"], "visible-value")

    def test_environment_secret_also_redacted(self):
        fake_secret = "SYNTHETIC_NOT_A_SECRET_ENV_42"
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": fake_secret}):
            filt = SecretRedactFilter()
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg=f"error {fake_secret}",
                args=(),
                exc_info=None,
            )
            filt.filter(record)
            self.assertNotIn(fake_secret, record.msg)

    def test_filter_preserves_numeric_args_and_formats_them(self):
        fake_secret = "SYNTHETIC_NOT_A_SECRET_FORMAT_99"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": fake_secret}):
            filt = SecretRedactFilter()
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg="count=%d secret=%s ratio=%.2f",
                args=(7, fake_secret, 1.25),
                exc_info=None,
            )
            filt.filter(record)
        self.assertIsInstance(record.args[0], int)
        self.assertIsInstance(record.args[2], float)
        self.assertEqual(record.getMessage(), "count=7 secret=*** ratio=1.25")

    def test_origin_and_pcm_contract_helpers(self):
        self.assertTrue(main_module._is_allowed_browser_origin("http://localhost:7870"))
        self.assertTrue(main_module._is_allowed_browser_origin("https://127.0.0.1"))
        self.assertTrue(main_module._is_allowed_browser_origin(None))
        self.assertFalse(main_module._is_allowed_browser_origin("http://192.168.1.5:7870"))
        self.assertFalse(main_module._is_allowed_browser_origin("https://localhost.evil"))
        self.assertTrue(main_module._is_valid_pcm_frame(bytes(640)))
        self.assertFalse(main_module._is_valid_pcm_frame(bytes(638)))
        self.assertFalse(main_module._is_valid_pcm_frame(bytes(642)))


class TestStorageIsolation(unittest.TestCase):
    def test_db_created_in_temp_directory(self):
        tmp_dir = tempfile.TemporaryDirectory()
        tmp = Path(tmp_dir.name)
        cfg = Config()
        cfg._raw = {
            "app": {"host": "127.0.0.1", "port": 7870},
            "asr": {"model": "dummy"},
            "llm": {"model": "dummy", "base_url": ""},
            "tts": {},
            "audio": {},
            "livetalking": {},
            "storage": {"db_path": str(tmp / "test.db")},
            "logging": {"directory": str(tmp / "logs")},
        }
        cfg._secrets = {}
        config_module._config = cfg

        from backend.storage import Storage

        storage = Storage()
        storage.save_turn(1, "completed", "u", "a", completed=True)
        self.assertTrue((tmp / "test.db").exists())
        storage.close()
        tmp_dir.cleanup()
        self.assertFalse((tmp / "test.db").exists())


if __name__ == "__main__":
    unittest.main()
