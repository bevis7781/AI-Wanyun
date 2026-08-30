from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import websockets

from backend.config import get_config
from backend.logger import get_logger, mask_id

logger = get_logger()
TAG = "huoshan_tts"

PROTOCOL_VERSION = 0b0001
DEFAULT_HEADER_SIZE = 0b0001
FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_RESPONSE = 0b1011
FULL_SERVER_RESPONSE = 0b1001
ERROR_INFORMATION = 0b1111
NO_SERIALIZATION = 0b0000
JSON_SERIAL = 0b0001
NO_COMPRESSION = 0b0000

EVENT_NONE = 0
EVENT_StartConnection = 1
EVENT_FinishConnection = 2
EVENT_ConnectionStarted = 50
EVENT_ConnectionFailed = 51
EVENT_ConnectionFinished = 52
EVENT_StartSession = 100
EVENT_CancelSession = 101
EVENT_FinishSession = 102
EVENT_SessionStarted = 150
EVENT_SessionCanceled = 151
EVENT_SessionFinished = 152
EVENT_SessionFailed = 153
EVENT_TaskRequest = 200
EVENT_TTSSentenceStart = 350
EVENT_TTSSentenceEnd = 351
EVENT_TTSResponse = 352

MsgTypeFlagWithEvent = 0b0100

DEFAULT_WS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
DEFAULT_RESOURCE_ID = "seed-tts-2.0"
DEFAULT_SPEAKER = "zh_female_qingxinginxia_moon_bigtts"


@dataclass
class _Header:
    protocol_version: int = PROTOCOL_VERSION
    header_size: int = DEFAULT_HEADER_SIZE
    message_type: int = 0
    message_type_specific_flags: int = 0
    serial_method: int = NO_SERIALIZATION
    compression_type: int = NO_COMPRESSION
    reserved_data: int = 0

    def as_bytes(self) -> bytes:
        return bytes([
            (self.protocol_version << 4) | self.header_size,
            (self.message_type << 4) | self.message_type_specific_flags,
            (self.serial_method << 4) | self.compression_type,
            self.reserved_data,
        ])


@dataclass
class _Optional:
    event: int = EVENT_NONE
    session_id: str | None = None
    sequence: int | None = None
    connection_id: str | None = None
    response_meta_json: str | None = None
    payload: bytes | None = None
    error_code: int | None = None

    def as_bytes(self) -> bytes:
        buf = bytearray()
        if self.event != EVENT_NONE:
            buf.extend(self.event.to_bytes(4, "big", signed=True))
        if self.session_id is not None:
            sid = self.session_id.encode("utf-8")
            buf.extend(len(sid).to_bytes(4, "big", signed=True))
            buf.extend(sid)
        if self.sequence is not None:
            buf.extend(self.sequence.to_bytes(4, "big", signed=True))
        return bytes(buf)


@dataclass
class _Response:
    header: _Header = field(default_factory=_Header)
    optional: _Optional = field(default_factory=_Optional)


class HuoshanTTSAdapter:
    supports_first_pcm_watchdog = True

    def __init__(
        self,
        on_pcm: Callable[[bytes], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        cfg = get_config()
        self.appid = cfg.secret("HUOSHAN_APPID")
        self.access_token = cfg.secret("HUOSHAN_ACCESS_TOKEN")
        self.ws_url = cfg.tts.get("ws_url", DEFAULT_WS_URL)
        self.resource_id = cfg.tts.get("resource_id", DEFAULT_RESOURCE_ID)
        self.speaker = cfg.tts.get("speaker", DEFAULT_SPEAKER)
        self.audio_params = dict(cfg.tts.get("audio_params", {}))
        self.additions = dict(cfg.tts.get("additions", {}))
        self.on_pcm = on_pcm
        self.on_error = on_error

        self.ws: websockets.WebSocketClientProtocol | None = None
        self._monitor_task: asyncio.Task | None = None
        self._session_id: str | None = None
        self._session_started_event = asyncio.Event()
        self._session_finished_event = asyncio.Event()
        self._session_started_ok: bool | None = None
        self._error_message: str | None = None
        self._active = False
        self._closed = False
        self._closing_lock = asyncio.Lock()

    def _headers(self) -> dict[str, str]:
        return {
            "X-Api-App-Key": self.appid,
            "X-Api-Access-Key": self.access_token,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

    def _build_payload(self, event: int, text: str = "", speaker: str = "") -> bytes:
        audio_params = {**self.audio_params, "format": "pcm"}
        audio_params.setdefault("sample_rate", 16000)
        req_params = {
            "text": text,
            "speaker": speaker or self.speaker,
            "audio_params": audio_params,
            "additions": json.dumps(self.additions),
        }
        body = {
            "user": {"uid": "voice_client"},
            "event": event,
            "namespace": "BidirectionalTTS",
            "req_params": req_params,
        }
        return json.dumps(body).encode("utf-8")

    async def _send_event(
        self,
        event: int,
        session_id: str | None = None,
        payload: bytes | None = None,
    ) -> None:
        if not self.ws:
            raise RuntimeError("TTS WebSocket not connected")
        header = _Header(
            message_type=FULL_CLIENT_REQUEST,
            message_type_specific_flags=MsgTypeFlagWithEvent,
            serial_method=JSON_SERIAL,
        )
        optional = _Optional(event=event, session_id=session_id)
        buf = bytearray(header.as_bytes())
        buf.extend(optional.as_bytes())
        if payload is not None:
            buf.extend(len(payload).to_bytes(4, "big", signed=True))
            buf.extend(payload)
        await self.ws.send(bytes(buf))

    @property
    def is_active(self) -> bool:
        """会话是否仍可用（预热会话可能因长时间空闲被服务端关闭）。"""
        return self._active and not self._closed and self.ws is not None

    async def start(self) -> bool:
        if self._closed:
            return False
        self._session_id = uuid.uuid4().hex
        self._session_started_event.clear()
        self._session_finished_event.clear()
        self._session_started_ok = None
        self._error_message = None
        self._active = True
        try:
            self.ws = await websockets.connect(
                self.ws_url,
                additional_headers=self._headers(),
                max_size=100_000_000,
                ping_interval=None,
                open_timeout=10.0,
                close_timeout=5.0,
            )
            self._monitor_task = asyncio.create_task(self._monitor())
            await self._send_event(
                EVENT_StartSession,
                session_id=self._session_id,
                payload=self._build_payload(EVENT_StartSession, speaker=self.speaker),
            )
            await asyncio.wait_for(
                self._session_started_event.wait(),
                timeout=10.0,
            )
            if self._session_started_ok is not True or self._closed:
                logger.error(f"[{TAG}] session failed to start session_id={mask_id(self._session_id)}")
                return False
            logger.info(f"[{TAG}] session started session_id={mask_id(self._session_id)}")
            return True
        except asyncio.TimeoutError:
            logger.error(f"[{TAG}] start timeout waiting for SessionStarted")
            await self._close(error="TTS 会话启动超时")
            return False
        except Exception as exc:
            logger.error(f"[{TAG}] start failed error={exc}")
            await self._close(error=f"TTS 连接失败: {exc}")
            return False

    async def send_text(self, text: str) -> None:
        if not self._active or not self.ws or not self._session_id:
            return
        await self._send_event(
            EVENT_TaskRequest,
            session_id=self._session_id,
            payload=self._build_payload(EVENT_TaskRequest, text=text, speaker=self.speaker),
        )

    async def finish(self) -> None:
        if not self._active or not self.ws or not self._session_id:
            await self._close()
            return
        try:
            await self._send_event(
                EVENT_FinishSession,
                session_id=self._session_id,
                payload=b"{}",
            )
            await asyncio.wait_for(
                self._session_finished_event.wait(),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.error(f"[{TAG}] finish timeout waiting for SessionFinished")
            await self._close(error="TTS 会话结束超时")
            return
        except Exception as exc:
            logger.warning(f"[{TAG}] finish error={exc}")
        finally:
            await self._close()

    async def cancel(self) -> None:
        if not self._active or not self.ws or not self._session_id:
            await self._close()
            return
        try:
            await self._send_event(
                EVENT_CancelSession,
                session_id=self._session_id,
                payload=b"{}",
            )
        except Exception as exc:
            logger.warning(f"[{TAG}] cancel error={exc}")
        finally:
            await self._close()

    async def _close(self, error: str | None = None) -> None:
        async with self._closing_lock:
            if self._closed:
                return
            self._closed = True
            self._active = False
            if error:
                self._error_message = error
                if self.on_error:
                    try:
                        self.on_error(error)
                    except Exception:
                        pass
            current = asyncio.current_task()
            if self._monitor_task and not self._monitor_task.done() and self._monitor_task is not current:
                self._monitor_task.cancel()
                try:
                    await self._monitor_task
                except BaseException:
                    pass
            self._monitor_task = None
            if self.ws:
                try:
                    await self.ws.close()
                except Exception:
                    pass
                self.ws = None

    async def _monitor(self) -> None:
        try:
            while self.ws and not self._closed:
                msg = await self.ws.recv()
                res = self._parse_response(msg)
                event = res.optional.event

                if event == EVENT_ConnectionStarted:
                    continue
                if event == EVENT_ConnectionFailed:
                    error = res.optional.response_meta_json or "TTS 连接鉴权失败"
                    logger.error(f"[{TAG}] connection failed")
                    self._error_message = error
                    self._session_started_ok = False
                    self._session_started_event.set()
                    self._session_finished_event.set()
                    await self._close(error=error)
                    break
                if event == ERROR_INFORMATION:
                    error = f"TTS 服务端错误: code={res.optional.error_code}"
                    logger.error(f"[{TAG}] server error code={res.optional.error_code}")
                    self._error_message = error
                    self._session_started_ok = False
                    await self._close(error=error)
                    break

                if res.optional.session_id and res.optional.session_id != self._session_id:
                    if event in (EVENT_SessionCanceled, EVENT_SessionFailed, EVENT_SessionFinished):
                        logger.warning(f"[{TAG}] stale session event={event}")
                    continue

                if event == EVENT_SessionStarted:
                    self._session_started_ok = True
                    self._session_started_event.set()
                elif event == EVENT_SessionFailed:
                    error = res.optional.response_meta_json or "TTS 会话失败"
                    logger.error(f"[{TAG}] session failed")
                    self._error_message = error
                    self._session_started_ok = False
                    self._session_started_event.set()
                    self._session_finished_event.set()
                    await self._close(error=error)
                    break
                elif event == EVENT_SessionFinished:
                    self._session_finished_event.set()
                    break
                elif event == EVENT_TTSResponse and res.header.message_type == AUDIO_ONLY_RESPONSE:
                    if res.optional.payload and self.on_pcm:
                        maybe_coro = self.on_pcm(res.optional.payload)
                        if asyncio.iscoroutine(maybe_coro):
                            await maybe_coro
        except websockets.ConnectionClosed:
            logger.warning(f"[{TAG}] connection closed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"[{TAG}] monitor error={exc}")
            self._error_message = str(exc)
            if self.on_error:
                try:
                    self.on_error(str(exc))
                except Exception:
                    pass
        finally:
            self._active = False
            self._session_started_event.set()
            self._session_finished_event.set()

    def _parse_response(self, data: bytes) -> _Response:
        header = _Header()
        header.protocol_version = (data[0] >> 4) & 0x0F
        header.header_size = data[0] & 0x0F
        header.message_type = (data[1] >> 4) & 0x0F
        header.message_type_specific_flags = data[1] & 0x0F
        header.serial_method = (data[2] >> 4) & 0x0F
        header.compression_type = data[2] & 0x0F
        header.reserved_data = data[3]

        optional = _Optional()
        offset = 4
        if header.message_type in (FULL_SERVER_RESPONSE, AUDIO_ONLY_RESPONSE):
            if header.message_type_specific_flags == MsgTypeFlagWithEvent:
                optional.event = int.from_bytes(data[offset:offset + 4], "big", signed=True)
                offset += 4
                if optional.event == EVENT_NONE:
                    return _Response(header, optional)
                if optional.event in (EVENT_ConnectionStarted, EVENT_ConnectionFailed):
                    optional.connection_id, offset = self._read_string(data, offset)
                    if optional.event == EVENT_ConnectionFailed:
                        meta, offset = self._read_string(data, offset)
                        optional.response_meta_json = meta
                elif optional.event in (
                    EVENT_SessionStarted,
                    EVENT_SessionFailed,
                    EVENT_SessionFinished,
                ):
                    optional.session_id, offset = self._read_string(data, offset)
                    meta, offset = self._read_string(data, offset)
                    optional.response_meta_json = meta
                else:
                    optional.session_id, offset = self._read_string(data, offset)
                    payload_size = int.from_bytes(data[offset:offset + 4], "big", signed=True)
                    offset += 4
                    optional.payload = data[offset:offset + payload_size]
        elif header.message_type == ERROR_INFORMATION:
            optional.error_code = int.from_bytes(data[offset:offset + 4], "big", signed=True)
            offset += 4
            payload_size = int.from_bytes(data[offset:offset + 4], "big", signed=True)
            offset += 4
            optional.payload = data[offset:offset + payload_size]

        return _Response(header, optional)

    def _read_string(self, data: bytes, offset: int) -> tuple[str, int]:
        size = int.from_bytes(data[offset:offset + 4], "big", signed=True)
        offset += 4
        text = data[offset:offset + size].decode("utf-8")
        offset += size
        return text, offset


class DummyTTSAdapter:
    """Test adapter that yields silence PCM frames for a fixed duration."""

    def __init__(self, duration_seconds: float = 1.0, **kwargs: Any) -> None:
        self.on_pcm = kwargs.get("on_pcm")
        self._duration = duration_seconds
        self._active = False
        self._task: asyncio.Task | None = None
        self.ws = None

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> bool:
        self._active = True
        self._task = asyncio.create_task(self._emit_silence())
        return True

    async def _emit_silence(self) -> None:
        frames = int(self._duration * 50)  # 50 frames / second (20 ms each)
        for _ in range(frames):
            if not self._active:
                break
            if self.on_pcm:
                maybe_coro = self.on_pcm(bytes(640))
                if asyncio.iscoroutine(maybe_coro):
                    await maybe_coro
            await asyncio.sleep(0.02)

    async def send_text(self, text: str) -> None:
        # 预热（start）时的排放窗口可能早已结束：每次送句（重）启排放，
        # 以模拟真实适配器“送句后产出 PCM”的行为
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._emit_silence())

    async def finish(self) -> None:
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=self._duration + 0.5)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except BaseException:
                    pass
            except BaseException:
                pass
        self._active = False

    async def cancel(self) -> None:
        await self._close()

    async def _close(self) -> None:
        self._active = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except BaseException:
                pass
