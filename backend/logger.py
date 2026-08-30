from __future__ import annotations

import logging
import logging.handlers
import os
import re
from pathlib import Path
from typing import Any

from backend.config import get_config


# 仅作为兜底模式：按字段名脱敏（对未知键生效）
_SENSITIVE_KEY_RE = re.compile(r"(api[_-]?key|access[_-]?token|appid|accesskey|secret|authorization|token|password|passwd)", re.I)

# 不能出现在日志中的敏感键名（值级替换兜底）
_SENSITIVE_FIELD_NAMES = {
    "api_key", "apikey", "access_token", "accesstoken", "appid", "app_id",
    "accesskey", "secret", "authorization", "token", "password",
}


class SecretRedactFilter(logging.Filter):
    """对所有日志记录做值级脱敏过滤。"""

    def __init__(self, name: str = "") -> None:
        super().__init__(name)
        self._secrets: set[str] = set()
        self._load_secrets()

    def _load_secrets(self) -> None:
        try:
            cfg = get_config()
            for name in (
                "DASHSCOPE_API_KEY",
                "DEEPSEEK_API_KEY",
                "HUOSHAN_APPID",
                "HUOSHAN_ACCESS_TOKEN",
            ):
                try:
                    value = cfg.secret(name)
                    if value:
                        self._secrets.add(value)
                except ValueError:
                    pass
        except Exception:
            pass

    def _redact_text(self, text: str) -> str:
        for secret in self._secrets:
            if len(secret) >= 4:
                text = text.replace(secret, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact_text(str(record.msg))
        if record.args:
            record.args = tuple(self._redact_text(str(arg)) for arg in record.args)
        return True


def _redact_value(value: Any, known_secrets: set[str]) -> Any:
    if isinstance(value, str):
        for secret in known_secrets:
            if len(secret) >= 4:
                value = value.replace(secret, "***")
        return value
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8", errors="replace")
        except Exception:
            text = "<bytes>"
        return _redact_value(text, known_secrets)
    if isinstance(value, dict):
        return {
            k: "***" if k.lower() in _SENSITIVE_FIELD_NAMES else _redact_value(v, known_secrets)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(v, known_secrets) for v in value]
    return value


def _known_secrets() -> set[str]:
    secrets: set[str] = set()
    for env_name in (
        "DASHSCOPE_API_KEY",
        "DEEPSEEK_API_KEY",
        "HUOSHAN_APPID",
        "HUOSHAN_ACCESS_TOKEN",
    ):
        value = os.environ.get(env_name, "")
        if value:
            secrets.add(value)
    try:
        cfg = get_config()
        for name in (
            "DASHSCOPE_API_KEY",
            "DEEPSEEK_API_KEY",
            "HUOSHAN_APPID",
            "HUOSHAN_ACCESS_TOKEN",
        ):
            try:
                v = cfg.secret(name)
                if v:
                    secrets.add(v)
            except ValueError:
                pass
    except Exception:
        pass
    return secrets


def redact(value: Any) -> Any:
    """对任意值进行脱敏，支持已知密钥在字符串中的值级替换。"""
    return _redact_value(value, _known_secrets())


def safe_extra(extra: dict[str, Any] | None) -> dict[str, Any]:
    """用于结构化日志 extra 的脱敏。"""
    if not extra:
        return {}
    return redact(extra)


def format_extra(extra: dict[str, Any] | None) -> str:
    redacted = safe_extra(extra)
    parts = [f"{k}={v}" for k, v in redacted.items()]
    return " | ".join(parts)


def setup_logging() -> logging.Logger:
    cfg = get_config().logging
    level_name = cfg.get("level", "INFO")
    directory = Path(cfg.get("directory", "data/logs"))
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / "app.log"

    logger = logging.getLogger("voice_client")
    logger.setLevel(getattr(logging, level_name.upper(), logging.INFO))

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler = logging.handlers.RotatingFileHandler(
        str(log_path),
        maxBytes=int(cfg.get("max_bytes", 10 * 1024 * 1024)),
        backupCount=int(cfg.get("backup_count", 5)),
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    handler.addFilter(SecretRedactFilter())
    logger.addHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(SecretRedactFilter())
    logger.addHandler(console)

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("voice_client")


def safe_log(msg: str, extra: dict[str, Any] | None = None) -> str:
    if extra:
        return f"{msg} | {format_extra(extra)}"
    return msg


def redact_exc(exc: BaseException) -> str:
    """将异常信息脱敏后返回，避免密钥出现在日志或响应中。"""
    return redact(str(exc))


def mask_id(value: str | None, head: int = 6, tail: int = 6) -> str:
    """会话标识脱敏：保留前 head 位 + *** + 末尾 tail 位。

    用于 Qwen task_id / LiveTalking sessionid / 火山 TTS session_id 等可复用标识的日志输出。
    过短时仅保留头部 + ***，不输出完整值。
    """
    if not value:
        return "none"
    if len(value) <= head + tail:
        return value[:head] + "***"
    return f"{value[:head]}***{value[-tail:]}"
