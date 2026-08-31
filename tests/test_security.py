"""安全相关回归测试：日志脱敏、会话关闭错误隔离、数据文件隔离。"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config as config_module
from backend import main as main_module
from backend.config import Config
from backend.logger import SecretRedactFilter, redact, redact_exc


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
