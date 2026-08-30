"""安全相关回归测试：配置导入、日志脱敏、数据文件隔离。"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config as config_module
from backend.config import Config
from backend.logger import SecretRedactFilter, redact, redact_exc


@unittest.skip("legacy private-config importer is intentionally excluded from the public candidate")
class TestImportConfig(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        self.source = self.tmp / "source" / ".config.yaml"
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.secrets = self.tmp / "secrets.json"
        self.config = self.tmp / "config.yaml"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _write_source(self, data: dict) -> None:
        with self.source.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True)

    def _run_import(self) -> int:
        """在临时目录上执行导入脚本，返回退出码。"""
        import scripts.import_config as importer

        # 导入脚本需要本地 config.yaml 存在以便合并
        if not self.config.exists():
            self.config.write_text("app:\n", encoding="utf-8")

        original_source = importer.SOURCE_PATH
        original_secrets = importer.SECRETS_PATH
        original_config = importer.CONFIG_PATH
        try:
            importer.SOURCE_PATH = self.source
            importer.SECRETS_PATH = self.secrets
            importer.CONFIG_PATH = self.config
            return importer.main()
        finally:
            importer.SOURCE_PATH = original_source
            importer.SECRETS_PATH = original_secrets
            importer.CONFIG_PATH = original_config

    def test_extracts_all_secrets_and_tts_params(self):
        self._write_source(
            {
                "ASR": {"QwenAudio3ASRStream": {"api_key": "fake-dashscope"}},
                "LLM": {"DeepSeekLLM": {"api_key": "fake-deepseek"}},
                "TTS": {
                    "HuoshanDoubleStreamTTSV2": {
                        "appid": "fake-appid",
                        "access_token": "fake-token",
                        "ws_url": "wss://example.com/tts",
                        "resource_id": "seed-tts-2.0",
                        "speaker": "test-speaker",
                        "audio_params": {"sample_rate": 16000, "speech_rate": 0},
                        "additions": {"post_process": {"pitch": 0}},
                    }
                },
            }
        )
        rc = self._run_import()
        self.assertEqual(rc, 0)

        secrets = json.loads(self.secrets.read_text(encoding="utf-8"))
        self.assertEqual(secrets["DASHSCOPE_API_KEY"], "fake-dashscope")
        self.assertEqual(secrets["DEEPSEEK_API_KEY"], "fake-deepseek")
        self.assertEqual(secrets["HUOSHAN_APPID"], "fake-appid")
        self.assertEqual(secrets["HUOSHAN_ACCESS_TOKEN"], "fake-token")

        cfg = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        self.assertEqual(cfg["tts"]["ws_url"], "wss://example.com/tts")
        self.assertEqual(cfg["tts"]["resource_id"], "seed-tts-2.0")
        self.assertEqual(cfg["tts"]["speaker"], "test-speaker")

    def test_dashscope_fallback_to_memory_key(self):
        self._write_source(
            {
                "ASR": {"QwenAudio3ASRStream": {"api_key": ""}},
                "Memory": {"powermem": {"embedder": {"config": {"api_key": "memory-key"}}}},
                "LLM": {"DeepSeekLLM": {"api_key": "llm-key"}},
                "TTS": {
                    "HuoshanDoubleStreamTTSV2": {
                        "appid": "app",
                        "access_token": "token",
                    }
                },
            }
        )
        rc = self._run_import()
        self.assertEqual(rc, 0)
        secrets = json.loads(self.secrets.read_text(encoding="utf-8"))
        self.assertEqual(secrets["DASHSCOPE_API_KEY"], "memory-key")

    def test_existing_secret_not_overwritten_by_empty(self):
        self.secrets.write_text(
            json.dumps(
                {
                    "DASHSCOPE_API_KEY": "old-dashscope",
                    "DEEPSEEK_API_KEY": "old-deepseek",
                    "HUOSHAN_APPID": "old-appid",
                    "HUOSHAN_ACCESS_TOKEN": "old-token",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self._write_source(
            {
                "ASR": {"QwenAudio3ASRStream": {"api_key": ""}},
                "LLM": {"DeepSeekLLM": {"api_key": ""}},
                "TTS": {
                    "HuoshanDoubleStreamTTSV2": {
                        "appid": "",
                        "access_token": "",
                    }
                },
            }
        )
        rc = self._run_import()
        # 旧密钥全部被保留，因此不应返回缺失
        self.assertEqual(rc, 0)
        secrets = json.loads(self.secrets.read_text(encoding="utf-8"))
        # 已有非空本地密钥不得被空值覆盖
        self.assertEqual(secrets["DASHSCOPE_API_KEY"], "old-dashscope")
        self.assertEqual(secrets["DEEPSEEK_API_KEY"], "old-deepseek")
        self.assertEqual(secrets["HUOSHAN_APPID"], "old-appid")
        self.assertEqual(secrets["HUOSHAN_ACCESS_TOKEN"], "old-token")

    def test_missing_source_error(self):
        self.source.unlink(missing_ok=True)
        rc = self._run_import()
        self.assertEqual(rc, 1)


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
