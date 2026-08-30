from __future__ import annotations

import json
from typing import Any, AsyncIterator, Callable

import httpx

from backend.config import get_config
from backend.logger import get_logger

logger = get_logger()
TAG = "deepseek_llm"


class DeepSeekLLMAdapter:
    def __init__(self, on_token: Callable[[str], None] | None = None) -> None:
        cfg = get_config()
        self.model = cfg.llm.get("model", "deepseek-chat")
        self.base_url = cfg.llm.get("base_url", "https://api.deepseek.com/v1")
        self.api_key = cfg.secret("DEEPSEEK_API_KEY")
        self.temperature = float(cfg.llm.get("temperature", 0.7))
        self.max_tokens = int(cfg.llm.get("max_tokens", 512))
        self.on_token = on_token
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=60.0,
        )

    async def chat(
        self,
        system_prompt: str,
        history: list[dict[str, str]],
        user_input: str,
    ) -> AsyncIterator[str]:
        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_input})

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        try:
            async with self._client.stream(
                "POST",
                "/chat/completions",
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    token = delta.get("content", "")
                    if token:
                        if self.on_token:
                            self.on_token(token)
                        yield token
        except httpx.HTTPStatusError as exc:
            logger.error(f"[{TAG}] http error status={exc.response.status_code}")
            raise
        except Exception as exc:
            logger.error(f"[{TAG}] chat error={exc}")
            raise

    async def close(self) -> None:
        await self._client.aclose()


class DummyLLMAdapter(DeepSeekLLMAdapter):
    """Test adapter that yields a fixed sentence with punctuation."""

    def __init__(self, reply: str = "你好，我是小唯。今天有什么可以帮你的吗？", **kwargs: Any) -> None:
        # bypass parent init so we don't need secrets in tests
        self.model = "dummy"
        self.base_url = ""
        self.api_key = ""
        self.temperature = 0.7
        self.max_tokens = 512
        self.on_token = kwargs.get("on_token")
        self._client = None
        self._reply = reply

    async def chat(
        self,
        system_prompt: str,
        history: list[dict[str, str]],
        user_input: str,
    ) -> AsyncIterator[str]:
        for char in self._reply:
            await __import__("asyncio").sleep(0.02)
            if self.on_token:
                self.on_token(char)
            yield char

    async def close(self) -> None:
        pass
