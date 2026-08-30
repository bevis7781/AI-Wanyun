from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import websockets

from backend.config import get_config
from backend.logger import get_logger, mask_id

logger = get_logger()
TAG = "livetalking"

PCM_FRAME_BYTES = 640
SAMPLE_RATE = 16000
CHANNELS = 1
FORMAT = "pcm_s16le"


_ACK_EVENT_BY_EXPECTED = {
    "started": "start",
    "ended": "end",
    "aborted": "abort",
}


def _mask_ack(ack: str) -> str:
    """ACK 日志脱敏：真实容器 ACK 携带完整 sessionId，必须掩码后输出。"""
    try:
        data = json.loads(ack)
    except json.JSONDecodeError:
        return "<non-json>"
    if not isinstance(data, dict):
        return "<non-dict>"
    for key in ("sessionId", "sessionid", "session_id"):
        if key in data:
            data[key] = mask_id(str(data[key]))
    return json.dumps(data, ensure_ascii=False)


def _ack_ok(ack: str, expected_type: str) -> bool:
    """验证 LiveTalking 音频流 ACK（严格模式）。

    仅接受两种明确成功格式：
    - 真实容器：{"type": "ack", "event": "start"|"end"|"abort", ...}
    - 旧版/伪服务器：{"type": "started"|"ended"|"aborted", "code": 0}

    拒绝空对象、未知格式、错误 event、非零 code、success=false、type=error。
    """
    try:
        data = json.loads(ack)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("type") == "error":
        return False
    if data.get("success") is False:
        return False
    code = data.get("code")
    if code is not None and code != 0:
        return False
    if data.get("type") == "ack":
        return data.get("event") == _ACK_EVENT_BY_EXPECTED.get(expected_type)
    if data.get("type") == expected_type:
        # 旧格式必须显式 code==0；缺少 code 一律拒绝
        return data.get("code") == 0
    return False


class LiveTalkingAdapter:
    def __init__(self) -> None:
        cfg = get_config()
        self.http_url = cfg.livetalking.get("http_url", "http://127.0.0.1:8110")
        self.avatar_id = cfg.livetalking.get("avatar_id", "public_demo_avatar")
        self.audio_stream_path = cfg.livetalking.get("audio_stream_path", "/audio-stream")
        self.interrupt_path = cfg.livetalking.get("interrupt_path", "/interrupt_talk")
        self.offer_path = cfg.livetalking.get("offer_path", "/offer")
        self.session_close_path = cfg.livetalking.get("session_close_path", "/api/admin/sessions/close")
        self.client = httpx.AsyncClient(base_url=self.http_url, timeout=10.0)
        self.ws: websockets.WebSocketClientProtocol | None = None
        self.ws_lock = asyncio.Lock()
        self.session_id: str | None = None
        self.generation: int | None = None
        self.last_ack: str | None = None
        self._closed = False

    async def health(self) -> dict[str, Any]:
        try:
            resp = await self.client.get("/api/admin/sessions")
            resp.raise_for_status()
            return {"ok": True, "data": resp.json()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def create_offer(self, sdp: str, type_: str = "offer") -> dict[str, Any]:
        payload = {"sdp": sdp, "type": type_, "avatar": self.avatar_id}
        resp = await self.client.post(self.offer_path, json=payload)
        resp.raise_for_status()
        data = resp.json()
        self.session_id = str(data.get("sessionid") or data.get("sessionId") or "")
        return data

    async def interrupt(self) -> dict[str, Any]:
        if not self.session_id:
            return {"ok": False, "error": "no session"}
        try:
            resp = await self.client.post(
                self.interrupt_path,
                json={"sessionid": self.session_id},
            )
            resp.raise_for_status()
            return {"ok": True, "data": resp.json()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def close_session(self) -> dict[str, Any]:
        if not self.session_id:
            return {"ok": False, "error": "no session"}
        try:
            resp = await self.client.post(
                self.session_close_path,
                json={"sessionid": self.session_id},
            )
            resp.raise_for_status()
            return {"ok": True, "data": resp.json()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def start_audio_stream(self, generation_id: int) -> bool:
        if not self.session_id:
            logger.error(f"[{TAG}] cannot start stream without session_id")
            return False
        async with self.ws_lock:
            if self._closed:
                return False
            await self._close_ws_nolock()
            try:
                ws_url = self.http_url.replace("http://", "ws://").replace("https://", "wss://")
                self.ws = await websockets.connect(
                    f"{ws_url}{self.audio_stream_path}",
                    ping_interval=None,
                    close_timeout=2,
                )
                self.generation = generation_id
                start_msg = {
                    "type": "start",
                    "sessionId": self.session_id,
                    "generationId": generation_id,
                    "sampleRate": SAMPLE_RATE,
                    "channels": CHANNELS,
                    "format": FORMAT,
                }
                await self.ws.send(json.dumps(start_msg))
                ack = await asyncio.wait_for(self.ws.recv(), timeout=5.0)
                self.last_ack = _mask_ack(str(ack))
                if not _ack_ok(ack, "started"):
                    logger.error(f"[{TAG}] stream start rejected ack={_mask_ack(str(ack))}")
                    await self._close_ws_nolock()
                    return False
                logger.info(f"[{TAG}] stream started generation={generation_id}")
                return True
            except Exception as exc:
                logger.error(f"[{TAG}] start_audio_stream error={exc}")
                self.ws = None
                self.generation = None
                return False

    async def send_pcm(self, pcm: bytes) -> None:
        if len(pcm) != PCM_FRAME_BYTES:
            logger.warning(f"[{TAG}] dropping non-640 byte frame size={len(pcm)}")
            raise ValueError(f"PCM frame must be {PCM_FRAME_BYTES} bytes")
        async with self.ws_lock:
            if self.ws is None or self.generation is None:
                return
            try:
                await self.ws.send(pcm)
            except Exception as exc:
                logger.warning(f"[{TAG}] send_pcm error={exc}")
                raise

    async def end_audio_stream(self, generation_id: int | None = None) -> bool:
        async with self.ws_lock:
            if self.ws is None or self.session_id is None or self.generation is None:
                return False
            if generation_id is not None and self.generation != generation_id:
                logger.warning(f"[{TAG}] end generation mismatch current={self.generation} got={generation_id}")
                return False
            try:
                end_msg = {
                    "type": "end",
                    "sessionId": self.session_id,
                    "generationId": self.generation,
                }
                await self.ws.send(json.dumps(end_msg))
                ack = await asyncio.wait_for(self.ws.recv(), timeout=5.0)
                self.last_ack = _mask_ack(str(ack))
                ok = _ack_ok(ack, "ended")
                if not ok:
                    logger.warning(f"[{TAG}] stream end rejected ack={_mask_ack(str(ack))}")
                return ok
            except Exception as exc:
                logger.warning(f"[{TAG}] end_audio_stream error={exc}")
                return False
            finally:
                await self._close_ws_nolock()

    async def abort_audio_stream(self, generation_id: int) -> bool:
        async with self.ws_lock:
            if self.ws is None or self.session_id is None:
                return False
            if self.generation is not None and self.generation != generation_id:
                logger.warning(f"[{TAG}] abort generation mismatch current={self.generation} got={generation_id}")
                return False
            try:
                abort_msg = {
                    "type": "abort",
                    "sessionId": self.session_id,
                    "generationId": generation_id,
                }
                await self.ws.send(json.dumps(abort_msg))
                ack = await asyncio.wait_for(self.ws.recv(), timeout=5.0)
                self.last_ack = _mask_ack(str(ack))
                ok = _ack_ok(ack, "aborted")
                if not ok:
                    logger.warning(f"[{TAG}] stream abort rejected ack={_mask_ack(str(ack))}")
                return ok
            except Exception as exc:
                logger.warning(f"[{TAG}] abort_audio_stream error={exc}")
                return False
            finally:
                await self._close_ws_nolock()

    async def _close_ws_nolock(self) -> None:
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
        self.generation = None

    async def close(self) -> None:
        async with self.ws_lock:
            self._closed = True
            await self._close_ws_nolock()
        await self.close_session()
        await self.client.aclose()


class DummyLiveTalkingAdapter:
    """Strict test adapter that records protocol calls and enforces generation rules."""

    def __init__(self) -> None:
        self.session_id = "dummy-session"
        self.generation: int | None = None
        self.calls: list[dict[str, Any]] = []
        self.pcm_frames: list[bytes] = []
        self.ws = None
        self.ws_lock = asyncio.Lock()
        self._ack_ok = True

    async def health(self) -> dict[str, Any]:
        return {"ok": True, "data": {"sessions": []}}

    async def create_offer(self, sdp: str, type_: str = "offer") -> dict[str, Any]:
        self.calls.append({"type": "create_offer", "sdp": sdp[:80], "type_": type_})
        return {"sessionid": self.session_id, "sdp": "v=0\n...", "type": "answer"}

    async def interrupt(self) -> dict[str, Any]:
        self.calls.append({"type": "interrupt"})
        return {"ok": True, "data": {"code": 0}}

    async def close_session(self) -> dict[str, Any]:
        self.calls.append({"type": "close_session"})
        return {"ok": True, "data": {"code": 0}}

    async def start_audio_stream(self, generation_id: int) -> bool:
        async with self.ws_lock:
            if self.generation is not None:
                logger.warning(f"[{TAG}] dummy: start rejected, generation={self.generation} still active")
                return False
            self.calls.append({"type": "start", "generation_id": generation_id})
            self.generation = generation_id
        return True

    async def send_pcm(self, pcm: bytes) -> None:
        if len(pcm) != PCM_FRAME_BYTES:
            return
        async with self.ws_lock:
            if self.generation is None:
                return
            self.pcm_frames.append(pcm)

    async def end_audio_stream(self, generation_id: int | None = None) -> bool:
        async with self.ws_lock:
            if self.generation is None:
                return False
            if generation_id is not None and self.generation != generation_id:
                logger.warning(f"[{TAG}] dummy: end generation mismatch current={self.generation} got={generation_id}")
                return False
            self.calls.append({"type": "end", "generation": self.generation})
            self.generation = None
        return True

    async def abort_audio_stream(self, generation_id: int) -> bool:
        async with self.ws_lock:
            if self.generation is None:
                return False
            if self.generation != generation_id:
                logger.warning(f"[{TAG}] dummy: abort generation mismatch current={self.generation} got={generation_id}")
                return False
            self.calls.append({"type": "abort", "generation_id": generation_id})
            self.generation = None
        return True

    async def close(self) -> None:
        pass
