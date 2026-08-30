from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        raise RuntimeError(f"Failed to load {path}: {exc}") from exc


def _load_secrets(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: str(v).strip() for k, v in (data or {}).items() if isinstance(v, str)}
    except Exception as exc:
        raise RuntimeError(f"Failed to load {path}: {exc}") from exc


class Config:
    def __init__(self) -> None:
        self.config_path = PROJECT_ROOT / "config.yaml"
        self.secrets_path = PROJECT_ROOT / "secrets.json"
        self._raw = _load_yaml(self.config_path)
        self._secrets = _load_secrets(self.secrets_path)

    @property
    def app(self) -> dict[str, Any]:
        return self._raw.get("app", {})

    @property
    def asr(self) -> dict[str, Any]:
        return self._raw.get("asr", {})

    @property
    def llm(self) -> dict[str, Any]:
        return self._raw.get("llm", {})

    @property
    def tts(self) -> dict[str, Any]:
        return self._raw.get("tts", {})

    @property
    def audio(self) -> dict[str, Any]:
        return self._raw.get("audio", {})

    @property
    def livetalking(self) -> dict[str, Any]:
        return self._raw.get("livetalking", {})

    @property
    def storage(self) -> dict[str, Any]:
        return self._raw.get("storage", {})

    @property
    def logging(self) -> dict[str, Any]:
        return self._raw.get("logging", {})

    def secret(self, name: str) -> str:
        # 环境变量优先级高于 secrets.json
        value = os.environ.get(name) or self._secrets.get(name) or ""
        if not value or not value.strip():
            raise ValueError(f"Missing secret: {name}")
        return value.strip()

    def has_secret(self, name: str) -> bool:
        value = os.environ.get(name) or self._secrets.get(name) or ""
        return bool(value and value.strip())

    def require(self, *keys: str) -> Any:
        value = self._raw
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                raise KeyError(f"Missing config key: {'.'.join(keys)}")
            value = value[key]
        return value

    def readiness(self) -> dict[str, Any]:
        """返回非敏感的就绪检查信息，不调用任何付费接口。"""
        result: dict[str, Any] = {
            "secrets_present": {
                "DASHSCOPE_API_KEY": self.has_secret("DASHSCOPE_API_KEY"),
                "DEEPSEEK_API_KEY": self.has_secret("DEEPSEEK_API_KEY"),
                "HUOSHAN_APPID": self.has_secret("HUOSHAN_APPID"),
                "HUOSHAN_ACCESS_TOKEN": self.has_secret("HUOSHAN_ACCESS_TOKEN"),
            },
            "config_present": {
                "asr.ws_url": bool(self.asr.get("ws_url")),
                "asr.model": bool(self.asr.get("model")),
                "llm.model": bool(self.llm.get("model")),
                "llm.base_url": bool(self.llm.get("base_url")),
                "tts.ws_url": bool(self.tts.get("ws_url")),
                "tts.resource_id": bool(self.tts.get("resource_id")),
                "tts.speaker": bool(self.tts.get("speaker")),
                "livetalking.http_url": bool(self.livetalking.get("http_url")),
            },
        }
        result["all_secrets_present"] = all(result["secrets_present"].values())
        result["all_config_present"] = all(result["config_present"].values())
        return result


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config


def reload_config() -> Config:
    global _config
    _config = Config()
    return _config
