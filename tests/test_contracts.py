"""契约级回归测试：火山 TTS V3、LiveTalking、PCM/状态机。"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import uvicorn
import websockets
from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.main as main_module
from backend.adapters.deepseek_llm import DummyLLMAdapter
from backend.adapters.huoshan_tts import (
    AUDIO_ONLY_RESPONSE,
    EVENT_FinishSession,
    EVENT_SessionFinished,
    EVENT_SessionStarted,
    EVENT_SessionFailed,
    EVENT_StartSession,
    EVENT_TaskRequest,
    EVENT_TTSResponse,
    FULL_CLIENT_REQUEST,
    FULL_SERVER_RESPONSE,
    HuoshanTTSAdapter,
    MsgTypeFlagWithEvent,
    _Header,
    _Optional,
)
from backend.adapters.livetalking import LiveTalkingAdapter, PCM_FRAME_BYTES
from backend.adapters.qwen_asr import DummyASRAdapter, QwenASRAdapter
from backend import config as config_module
from backend.config import Config
from backend.session import ConversationSession


def _silence_frame():
    return bytes(640)


def _voice_frame(amplitude=0.5):
    samples = [int(amplitude * 0x7FFF)] * 320
    return b"".join(struct.pack("<h", s) for s in samples)


def _make_config(tmp_path):
    cfg = Config()
    cfg._raw = {
        "app": {"host": "127.0.0.1", "port": 7870},
        "asr": {"model": "dummy", "ws_url": "", "format": "pcm", "sample_rate": 16000},
        "llm": {"model": "dummy", "base_url": "", "max_context_turns": 20},
        "tts": {
            "pcm_queue_limit": 50,
            "ws_url": "wss://openspeech.bytedance.com/api/v3/tts/bidirection",
            "resource_id": "seed-tts-2.0",
            "speaker": "zh_female_jiaochuannv_uranus_bigtts",
            "audio_params": {"speech_rate": 0, "loudness_rate": 0},
            "additions": {"post_process": {"pitch": 0}},
        },
        "audio": {
            "sample_rate": 16000,
            "channels": 1,
            "frame_duration_ms": 20,
            "rms_threshold": 0.1,
            "rms_consecutive_frames": 2,
            "prebuffer_ms": 500,
        },
        "livetalking": {"http_url": "http://127.0.0.1:8010"},
        "storage": {"db_path": str(tmp_path / "test.db")},
        "logging": {"directory": str(tmp_path / "logs")},
    }
    cfg._secrets = {
        "DASHSCOPE_API_KEY": "fake-dashscope-key",
        "DEEPSEEK_API_KEY": "fake-deepseek-key",
        "HUOSHAN_APPID": "fake-appid",
        "HUOSHAN_ACCESS_TOKEN": "fake-token",
    }
    config_module._config = cfg
    return cfg


# ---------------------------------------------------------------------------
# 火山 V3 伪服务器
# ---------------------------------------------------------------------------

def _parse_client_request(data: bytes) -> tuple[int, str | None, bytes]:
    header = _Header()
    header.protocol_version = (data[0] >> 4) & 0x0F
    header.header_size = data[0] & 0x0F
    header.message_type = (data[1] >> 4) & 0x0F
    header.message_type_specific_flags = data[1] & 0x0F
    header.serial_method = (data[2] >> 4) & 0x0F
    header.compression_type = data[2] & 0x0F
    header.reserved_data = data[3]

    offset = 4
    event = int.from_bytes(data[offset:offset + 4], "big", signed=True)
    offset += 4
    session_id = None
    if header.message_type == FULL_CLIENT_REQUEST and event != 0:
        sid_len = int.from_bytes(data[offset:offset + 4], "big", signed=True)
        offset += 4
        session_id = data[offset:offset + sid_len].decode("utf-8")
        offset += sid_len
    payload = b""
    if offset + 4 <= len(data):
        payload_len = int.from_bytes(data[offset:offset + 4], "big", signed=True)
        offset += 4
        payload = data[offset:offset + payload_len]
    return event, session_id, payload


def _build_server_response(event: int, session_id: str | None = None, payload: bytes = b"") -> bytes:
    header = _Header(
        message_type=FULL_SERVER_RESPONSE if payload else FULL_SERVER_RESPONSE,
        message_type_specific_flags=MsgTypeFlagWithEvent,
    )
    optional = _Optional(event=event, session_id=session_id)
    buf = bytearray(header.as_bytes())
    buf.extend(optional.as_bytes())
    if payload:
        buf.extend(len(payload).to_bytes(4, "big", signed=True))
        buf.extend(payload)
    return bytes(buf)


def _build_audio_response(session_id: str, audio: bytes) -> bytes:
    header = _Header(
        message_type=AUDIO_ONLY_RESPONSE,
        message_type_specific_flags=MsgTypeFlagWithEvent,
    )
    optional = _Optional(event=EVENT_TTSResponse, session_id=session_id)
    buf = bytearray(header.as_bytes())
    buf.extend(optional.as_bytes())
    buf.extend(len(audio).to_bytes(4, "big", signed=True))
    buf.extend(audio)
    return bytes(buf)


async def _huoshan_happy_server(websocket):
    """标准 V3 会话：StartSession -> SessionStarted -> TaskRequest -> audio -> FinishSession -> trailing audio -> SessionFinished."""
    received: list[tuple[int, str | None]] = []
    session_id: str | None = None
    text_before_started = False

    try:
        while True:
            msg = await websocket.recv()
            event, sid, payload = _parse_client_request(msg)
            received.append((event, sid))

            if event == EVENT_StartSession:
                session_id = sid
                # 在 SessionStarted 之前收到正文即为违规
                if text_before_started:
                    await websocket.send(_build_server_response(EVENT_SessionFailed, session_id, b'{"error":"text before start"}'))
                    return
                await websocket.send(_build_server_response(EVENT_SessionStarted, session_id, b'{"message":"started"}'))
            elif event == EVENT_TaskRequest:
                if session_id is None:
                    return
                body = json.loads(payload)
                # 验证字段存在
                assert "req_params" in body
                assert body["req_params"].get("speaker")
                # 顺序交付多个音频事件
                for i in range(3):
                    await websocket.send(_build_audio_response(session_id, bytes([i] * 640)))
            elif event == EVENT_FinishSession:
                if session_id is None:
                    return
                # FinishSession 后仍能接收尾部音频
                await websocket.send(_build_audio_response(session_id, bytes([9] * 640)))
                await websocket.send(_build_server_response(EVENT_SessionFinished, session_id, b'{"message":"finished"}'))
                break
    except websockets.ConnectionClosed:
        pass


async def _huoshan_error_server(websocket):
    """鉴权/服务端错误：返回 SessionFailed。"""
    msg = await websocket.recv()
    event, session_id, _ = _parse_client_request(msg)
    if event == EVENT_StartSession:
        await websocket.send(_build_server_response(EVENT_SessionFailed, session_id, b'{"error_code":401}'))


async def _huoshan_timeout_server(websocket):
    """不返回 SessionStarted，导致客户端超时。"""
    await websocket.recv()
    try:
        await websocket.wait_closed()
    except Exception:
        pass


class TestHuoshanV3Contract(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        _make_config(self.tmp)
        self.server = None
        self.uri = ""

    async def asyncTearDown(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        self.tmp_dir.cleanup()

    async def _start_server(self, handler):
        self.server = await serve(handler, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.uri = f"ws://127.0.0.1:{port}"

    async def test_start_session_fields_and_audio_order(self):
        await self._start_server(_huoshan_happy_server)
        cfg = config_module._config
        cfg._raw["tts"]["ws_url"] = self.uri

        received_audio: list[bytes] = []
        adapter = HuoshanTTSAdapter(on_pcm=received_audio.append)
        started = await adapter.start()
        self.assertTrue(started)
        await adapter.send_text("你好")
        await adapter.finish()
        self.assertEqual(len(received_audio), 4)
        self.assertEqual(received_audio[0], bytes([0] * 640))
        self.assertEqual(received_audio[1], bytes([1] * 640))
        self.assertEqual(received_audio[2], bytes([2] * 640))
        self.assertEqual(received_audio[3], bytes([9] * 640))

    async def test_session_failed_reported(self):
        await self._start_server(_huoshan_error_server)
        cfg = config_module._config
        cfg._raw["tts"]["ws_url"] = self.uri

        errors: list[str] = []
        adapter = HuoshanTTSAdapter(on_error=errors.append)
        started = await adapter.start()
        self.assertFalse(started)
        self.assertTrue(errors)

    async def test_start_timeout_cleans_up(self):
        await self._start_server(_huoshan_timeout_server)
        cfg = config_module._config
        cfg._raw["tts"]["ws_url"] = self.uri

        adapter = HuoshanTTSAdapter()
        started = await adapter.start()
        self.assertFalse(started)
        self.assertIsNone(adapter.ws)
        self.assertTrue(adapter._closed)


# ---------------------------------------------------------------------------
# LiveTalking 伪服务器
# ---------------------------------------------------------------------------

async def _livetalking_happy_server(websocket):
    generation: int | None = None
    pcm_count = 0
    try:
        while True:
            msg = await websocket.recv()
            if isinstance(msg, bytes):
                pcm_count += 1
                continue
            data = json.loads(msg)
            t = data.get("type")
            if t == "start":
                generation = data.get("generationId")
                # 验证必要字段
                assert data.get("sessionId")
                assert data.get("sampleRate") == 16000
                assert data.get("channels") == 1
                assert data.get("format") == "pcm_s16le"
                await websocket.send(json.dumps({"type": "started", "code": 0}))
            elif t == "end":
                assert data.get("generationId") == generation
                await websocket.send(json.dumps({"type": "ended", "code": 0}))
                break
            elif t == "abort":
                await websocket.send(json.dumps({"type": "aborted", "code": 0}))
                break
    except websockets.ConnectionClosed:
        pass


async def _livetalking_error_ack_server(websocket):
    msg = await websocket.recv()
    data = json.loads(msg)
    if data.get("type") == "start":
        await websocket.send(json.dumps({"type": "error", "code": 1, "message": "busy"}))


class TestLiveTalkingContract(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        _make_config(self.tmp)
        self.server = None
        self.uri = ""

    async def asyncTearDown(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        self.tmp_dir.cleanup()

    async def _start_server(self, handler):
        self.server = await serve(handler, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.uri = f"ws://127.0.0.1:{port}"

    async def test_full_stream_lifecycle(self):
        await self._start_server(_livetalking_happy_server)
        cfg = config_module._config
        cfg._raw["livetalking"]["http_url"] = self.uri.replace("ws://", "http://")

        adapter = LiveTalkingAdapter()
        adapter.session_id = "test-session"
        ok = await adapter.start_audio_stream(1)
        self.assertTrue(ok)
        await adapter.send_pcm(bytes(640))
        await adapter.send_pcm(bytes(640))
        ok = await adapter.end_audio_stream(1)
        self.assertTrue(ok)
        self.assertIsNone(adapter.ws)

    async def test_error_ack_rejected(self):
        await self._start_server(_livetalking_error_ack_server)
        cfg = config_module._config
        cfg._raw["livetalking"]["http_url"] = self.uri.replace("ws://", "http://")

        adapter = LiveTalkingAdapter()
        adapter.session_id = "test-session"
        ok = await adapter.start_audio_stream(1)
        self.assertFalse(ok)
        self.assertIsNone(adapter.ws)


class TestAckStrictness(unittest.TestCase):
    """_ack_ok 必须只接受明确成功格式，拒绝一切模糊/错误响应。"""

    # (ack JSON, expected_type, 应当接受)
    _CASES = [
        # 真实容器格式（允许携带 sessionId/generationId 等附加字段）
        ('{"type": "ack", "event": "start", "sessionId": "s1", "generationId": 1}', "started", True),
        ('{"type": "ack", "event": "end", "framesReceived": 270}', "ended", True),
        ('{"type": "ack", "event": "abort"}', "aborted", True),
        # 旧版/伪服务器格式
        ('{"type": "started", "code": 0}', "started", True),
        ('{"type": "ended", "code": 0}', "ended", True),
        ('{"type": "aborted", "code": 0}', "aborted", True),
        # 必须拒绝：旧格式缺少 code（无明确成功标识）
        ('{"type": "started"}', "started", False),
        ('{"type": "ended"}', "ended", False),
        ('{"type": "aborted"}', "aborted", False),
        # 必须拒绝：空对象 / 未知字段
        ("{}", "started", False),
        ('{"foo": 1}', "started", False),
        # 必须拒绝：错误 event
        ('{"type": "ack", "event": "end"}', "started", False),
        ('{"type": "ack", "event": "start"}', "ended", False),
        ('{"type": "ack"}', "started", False),
        # 必须拒绝：非零 code
        ('{"type": "started", "code": 1}', "started", False),
        ('{"type": "ended", "code": -1}', "ended", False),
        # 必须拒绝：success=false
        ('{"type": "ack", "event": "start", "success": false}', "started", False),
        ('{"type": "started", "success": false}', "started", False),
        # 必须拒绝：type=error
        ('{"type": "error", "message": "boom"}', "started", False),
        # 必须拒绝：非 JSON / 非 dict
        ("not json", "started", False),
        ("[1, 2]", "started", False),
    ]

    def test_ack_cases(self):
        from backend.adapters.livetalking import _ack_ok

        for ack, expected, should_pass in self._CASES:
            with self.subTest(ack=ack, expected=expected):
                self.assertEqual(_ack_ok(ack, expected), should_pass)


# ---------------------------------------------------------------------------
# PCM 缓冲与状态机
# ---------------------------------------------------------------------------

class TestPCMBufferAndStateMachine(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        _make_config(self.tmp)
        self.session = ConversationSession(
            asr_factory=lambda cb: DummyASRAdapter(final_text="你好测试", on_final=cb),
            llm_factory=lambda cb: DummyLLMAdapter(
                reply="你好，我是小唯。很高兴认识你。", on_token=cb
            ),
            tts_factory=lambda cb: _FragmentTTSAdapter(on_pcm=cb),
            livetalking_factory=_StrictLiveTalkingAdapter,
        )
        await self.session.start_tts_consumer()
        await self.session.start_pcm_sender()

    async def asyncTearDown(self):
        await self.session.shutdown()
        self.tmp_dir.cleanup()

    async def test_random_fragments_no_loss_or_misorder(self):
        await self.session.start()
        for _ in range(20):
            await self.session.handle_pcm(_voice_frame())
        await asyncio.sleep(2.5)

        lt = self.session.livetalking
        self.assertIsInstance(lt, _StrictLiveTalkingAdapter)
        # 验证每帧都是 640 字节
        self.assertTrue(all(len(f) == 640 for f in lt.frames))
        # 验证字节内容顺序：FragmentTTSAdapter 发送的是递增字节序列
        combined = b"".join(lt.frames)
        expected_prefix = bytes(range(128)) * 10  # 每个 fragment 128 字节，共 10 次
        # 允许末尾补零，因此只校验有效前缀
        valid_len = (len(combined) // 640) * 640
        self.assertGreater(len(lt.frames), 0)
        self.assertEqual(combined[: len(expected_prefix)], expected_prefix)

    async def test_multi_sentence_single_generation(self):
        await self.session.start()
        for _ in range(20):
            await self.session.handle_pcm(_voice_frame())
        await asyncio.sleep(2.5)

        lt = self.session.livetalking
        self.assertIsInstance(lt, _StrictLiveTalkingAdapter)
        # 多句回答只应出现一次 start/end
        starts = [c for c in lt.calls if c["type"] == "start"]
        ends = [c for c in lt.calls if c["type"] == "end"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(ends), 1)
        self.assertEqual(starts[0]["generation_id"], ends[0]["generation_id"])

    async def test_interrupt_excludes_from_history(self):
        await self.session.start()
        for _ in range(3):
            await self.session.handle_pcm(_voice_frame())
        await asyncio.sleep(0.1)
        await self.session.interrupt()
        await asyncio.sleep(0.5)
        completed = self.session._storage.get_completed_turns()
        self.assertEqual(len(completed), 0)
        # 诊断记录应标记为 interrupted
        recent = self.session._storage.get_recent_turns()
        self.assertTrue(any(r["status"] == "interrupted" for r in recent))

    async def test_speaking_blocks_microphone(self):
        await self.session.start()
        self.session._set_state("speaking")
        # speaking 期间 handle_pcm 不得触发 ASR
        await self.session.handle_pcm(_voice_frame())
        self.assertFalse(self.session._asr_connected)
        self.assertEqual(self.session.state, "speaking")


class _FragmentTTSAdapter:
    """TTS 适配器，发送随机尺寸 PCM 碎片，用于验证缓冲拼接。"""

    def __init__(self, on_pcm=None, **kwargs):
        self.on_pcm = on_pcm
        self._active = False
        self._sequence = 0

    async def start(self) -> bool:
        self._active = True
        return True

    async def send_text(self, text: str) -> None:
        if not self._active:
            return
        # 发送 10 个 128 字节的碎片，内容为递增字节
        for _ in range(10):
            fragment = bytes(range(128))
            self._sequence = (self._sequence + 1) % 256
            if self.on_pcm:
                await self._ensure_coro(self.on_pcm(fragment))

    async def finish(self) -> None:
        self._active = False

    async def cancel(self) -> None:
        self._active = False

    async def _ensure_coro(self, maybe_coro):
        if asyncio.iscoroutine(maybe_coro):
            await maybe_coro


class _StrictLiveTalkingAdapter:
    """严格记录协议调用，拒绝重复 generation。"""

    def __init__(self):
        self.session_id = "test-session"
        self.generation: int | None = None
        self.calls: list[dict] = []
        self.frames: list[bytes] = []
        self.ws = None
        self.close_session_calls = 0

    async def health(self):
        return {"ok": True}

    async def create_offer(self, sdp, type_="offer"):
        return {"sessionid": self.session_id}

    async def interrupt(self):
        return {"ok": True}

    async def close_session(self):
        self.close_session_calls += 1
        self.calls.append({"type": "close_session"})
        return {"ok": True}

    async def start_audio_stream(self, generation_id: int) -> bool:
        if self.generation is not None:
            return False
        self.generation = generation_id
        self.calls.append({"type": "start", "generation_id": generation_id})
        return True

    async def send_pcm(self, pcm: bytes) -> None:
        if self.generation is None:
            return
        if len(pcm) != PCM_FRAME_BYTES:
            raise ValueError("non-640 frame")
        self.frames.append(pcm)

    async def end_audio_stream(self, generation_id=None) -> bool:
        if self.generation is None or generation_id != self.generation:
            return False
        self.calls.append({"type": "end", "generation_id": self.generation})
        self.generation = None
        return True

    async def abort_audio_stream(self, generation_id: int) -> bool:
        if self.generation is None or generation_id != self.generation:
            return False
        self.calls.append({"type": "abort", "generation_id": generation_id})
        self.generation = None
        return True

    async def close(self):
        pass


class TestSessionRecoveryAndBackpressure(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        _make_config(self.tmp)
        self.session = ConversationSession(
            asr_factory=lambda cb: _DisconnectASRAdapter(
                on_final=cb,
                on_close=lambda: asyncio.create_task(self.session._on_asr_close()),
                on_error=lambda msg: asyncio.create_task(self.session._enter_error_state(msg)),
            ),
            llm_factory=lambda cb: DummyLLMAdapter(reply="", on_token=cb),
            tts_factory=lambda cb: _FragmentTTSAdapter(on_pcm=cb),
            livetalking_factory=_StrictLiveTalkingAdapter,
        )
        await self.session.start_tts_consumer()
        await self.session.start_pcm_sender()

    async def asyncTearDown(self):
        await self.session.shutdown()
        self.tmp_dir.cleanup()

    async def test_asr_disconnect_resets_and_allows_restart(self):
        await self.session.start()
        for _ in range(20):
            await self.session.handle_pcm(_voice_frame())
        await asyncio.sleep(0.2)
        # 模拟 ASR 连接断开，应标记当前 turn 并恢复 listening
        self.assertEqual(self.session.state, "listening")
        self.assertFalse(self.session._asr_connected)
        recent = self.session._storage.get_recent_turns()
        self.assertTrue(any(r["status"] == "interrupted" for r in recent))

    async def test_stop_current_streams_cleans_up(self):
        await self.session.start()
        for _ in range(20):
            await self.session.handle_pcm(_voice_frame())
        await asyncio.sleep(0.5)
        lt = self.session.livetalking
        self.assertIsInstance(lt, _StrictLiveTalkingAdapter)
        # 断线场景下 turn 已被清理；构造一个活跃音频流验证 stop 必须发送 abort
        from backend.session import TurnGuard

        turn = TurnGuard(self.session._next_turn_id())
        turn.storage_id = self.session._storage.save_turn(turn.turn_id, status="active")
        turn.generation_id = self.session._next_generation_id()
        self.session._current_turn = turn
        await lt.start_audio_stream(turn.generation_id)
        turn.audio_started = True
        self.session._current_audio_turn = turn
        await self.session.stop_current_streams()
        self.assertEqual(self.session.state, "paused")
        self.assertIsNone(self.session._current_turn)
        self.assertIsNone(self.session._current_audio_turn)
        self.assertTrue(any(c["type"] == "abort" for c in lt.calls))


class TestConsecutiveTurns(unittest.IsolatedAsyncioTestCase):
    """连续两轮回归：第一轮结束后 RMS 门控必须复位，第二轮无需重新点击开始。"""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        _make_config(self.tmp)
        self.asr_instances: list[DummyASRAdapter] = []

        def asr_factory(on_final):
            asr = DummyASRAdapter(final_text="你好", on_final=on_final)
            self.asr_instances.append(asr)
            return asr

        self.session = ConversationSession(
            asr_factory=asr_factory,
            llm_factory=lambda cb: DummyLLMAdapter(reply="好的。", on_token=cb),
            tts_factory=lambda cb: _FragmentTTSAdapter(on_pcm=cb),
            livetalking_factory=_StrictLiveTalkingAdapter,
        )
        await self.session.start_tts_consumer()
        await self.session.start_pcm_sender()

    async def asyncTearDown(self):
        await self.session.shutdown()
        self.tmp_dir.cleanup()

    async def _wait_completed(self, count: int, timeout: float = 10.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if len(self.session._storage.get_completed_turns()) >= count:
                return True
            await asyncio.sleep(0.1)
        return False

    async def test_two_consecutive_turns_without_restart(self):
        # 连续模式：只点击一次开始
        await self.session.start()
        self.assertEqual(self.session.state, "listening")

        # 第一轮：有效 PCM 从 handle_pcm 进入，走真实 RMS 门控
        for _ in range(20):
            await self.session.handle_pcm(_voice_frame())
        self.assertTrue(await self._wait_completed(1), "first turn did not complete")
        self.assertEqual(self.session.state, "listening")

        # 第一轮结束后：ASR 适配器已关闭且门控状态必须复位（否则第二轮 PCM 被静默丢弃）
        self.assertIsNone(self.session.asr)
        self.assertFalse(self.session._asr_connected)
        self.assertEqual(self.session._rms_hits, 0)
        self.assertEqual(len(self.session._prebuffer), 0)
        turn_id_after_first = self.session._turn_id
        generation_id_after_first = self.session._generation_id
        self.assertEqual(self.session._asr_started_count, 1)

        # 第二轮：不再次调用 start()，直接继续说话
        for _ in range(20):
            await self.session.handle_pcm(_voice_frame())
        self.assertTrue(await self._wait_completed(2), "second turn did not complete")
        self.assertEqual(self.session.state, "listening")

        # 第二轮必须重新经过 RMS 门控并创建新的 ASR 实例
        self.assertEqual(len(self.asr_instances), 2)
        self.assertIsNot(self.asr_instances[0], self.asr_instances[1])
        self.assertEqual(self.session._asr_started_count, 2)
        # 第二轮 PCM 确实到达第二个 ASR 实例（未被丢弃）
        self.assertGreater(self.asr_instances[1]._bytes_received, 6400)
        # 每轮恰好一次 final
        for asr in self.asr_instances[:2]:
            self.assertTrue(asr.submitted)
            self.assertIsNotNone(asr.final_text)
        # turn_id / generation_id 单调递增
        self.assertGreater(self.session._turn_id, turn_id_after_first)
        self.assertGreater(self.session._generation_id, generation_id_after_first)
        # 两轮都完整落库
        completed = self.session._storage.get_completed_turns()
        self.assertEqual(len(completed), 2)


class _SentenceASRAdapter:
    """可编程分句 ASR：由测试手动触发分句/活动事件，验证多分句聚合与回合结束判定。"""

    def __init__(self, on_final=None, on_sentence=None, **kwargs):
        self.on_final = on_final
        self.on_sentence = on_sentence  # 会被 session 按轮次重新绑定
        self.on_activity = None  # 非空中间结果活动信号，会被 session 按轮次重新绑定
        self.submitted = False
        self.stopping = False
        self.final_text = None
        self.bytes_received = 0

    async def start(self) -> bool:
        return True

    async def send_pcm(self, pcm: bytes) -> None:
        self.bytes_received += len(pcm)

    async def finish(self) -> None:
        self.stopping = True

    async def stop(self) -> None:
        self.stopping = True

    def emit_sentence(self, key: str, text: str, begin_time=0, end_time=0) -> None:
        if self.on_sentence:
            self.on_sentence(
                {"sentence_key": key, "begin_time": begin_time, "end_time": end_time, "text": text}
            )

    def emit_activity(self) -> None:
        """模拟 Qwen 非空中间结果（低于本地 RMS 阈值的轻声）触发的活动信号。"""
        if self.on_activity:
            self.on_activity()


class TestLongSpeechAggregation(unittest.IsolatedAsyncioTestCase):
    """第三关：长句多分句聚合 + 应用层回合结束判定。"""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        cfg = _make_config(self.tmp)
        # 缩短回合结束静音阈值，加快测试（默认 1800ms=90 帧）
        cfg._raw["audio"]["turn_end_silence_ms"] = 200  # 10 帧
        self.asr_instances: list[_SentenceASRAdapter] = []

        def asr_factory(on_final):
            asr = _SentenceASRAdapter(on_final=on_final)
            self.asr_instances.append(asr)
            return asr

        self.session = ConversationSession(
            asr_factory=asr_factory,
            llm_factory=lambda cb: DummyLLMAdapter(reply="好的，我明白了。", on_token=cb),
            tts_factory=lambda cb: _FragmentTTSAdapter(on_pcm=cb),
            livetalking_factory=_StrictLiveTalkingAdapter,
        )
        await self.session.start_tts_consumer()
        await self.session.start_pcm_sender()

    async def asyncTearDown(self):
        await self.session.shutdown()
        self.tmp_dir.cleanup()

    async def _wait_completed(self, count: int, timeout: float = 10.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if len(self.session._storage.get_completed_turns()) >= count:
                return True
            await asyncio.sleep(0.05)
        return False

    async def _feed_async(self, frames: int, voice: bool = True) -> None:
        for _ in range(frames):
            await self.session.handle_pcm(_voice_frame() if voice else _silence_frame())

    async def _start_turn(self) -> _SentenceASRAdapter:
        await self.session.start()
        await self._feed_async(3, voice=True)  # rms_consecutive_frames=2，触发 ASR
        self.assertTrue(self.session._asr_connected)
        return self.asr_instances[-1]

    async def test_two_sentences_merged_single_submit(self):
        """两个不同 sentence_id 依次到达，最终只提交一次合并文本。"""
        asr = await self._start_turn()
        asr.emit_sentence("s1", "第一句话说完了。", begin_time=0, end_time=1000)
        asr.emit_sentence("s2", "第二句话也说完了。", begin_time=1200, end_time=2200)
        # 静音达阈值（10 帧）触发提交
        await self._feed_async(12, voice=False)
        self.assertTrue(await self._wait_completed(1), "turn did not complete")
        completed = self.session._storage.get_completed_turns()
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["user"], "第一句话说完了。第二句话也说完了。")
        # 完成后：回合已提交（保留 guard 用于迟到事件丢弃，启动下轮时替换）
        turn = self.session._current_turn
        self.assertIsNotNone(turn)
        self.assertIsNotNone(turn.asr_final)
        self.assertEqual(turn.final_call_count, 1)
        self.assertEqual(self.session.state, "listening")

    async def test_duplicate_sentence_key_not_concatenated(self):
        """同一 sentence_key 重复到达不得重复拼接。"""
        asr = await self._start_turn()
        asr.emit_sentence("s1", "重复分句。", begin_time=0, end_time=1000)
        asr.emit_sentence("s1", "重复分句。", begin_time=0, end_time=1000)  # 重复
        await self._feed_async(12, voice=False)
        self.assertTrue(await self._wait_completed(1))
        completed = self.session._storage.get_completed_turns()
        self.assertEqual(completed[0]["user"], "重复分句。")

    async def test_voice_after_sentence_keeps_listening(self):
        """第一段结束后仍有人声：视为长句内部切段，不进入 thinking。"""
        asr = await self._start_turn()
        asr.emit_sentence("s1", "我还没说完。", begin_time=0, end_time=1000)
        # 持续人声 30 帧（远超静音阈值）
        await self._feed_async(30, voice=True)
        self.assertEqual(self.session.state, "listening")
        turn = self.session._current_turn
        self.assertIsNotNone(turn)
        self.assertIsNone(turn.asr_final)
        self.assertEqual(len(turn.pending_sentences), 1)
        self.assertEqual(len(self.session._storage.get_completed_turns()), 0)

    async def test_silence_submits_exactly_once(self):
        """最终静音后只调用一次提交（单次 final、单次 generation）。"""
        asr = await self._start_turn()
        asr.emit_sentence("s1", "说完了。", begin_time=0, end_time=800)
        asr.emit_sentence("s2", "真的说完了。", begin_time=900, end_time=1600)
        await self._feed_async(12, voice=False)
        self.assertTrue(await self._wait_completed(1))
        # 再喂更多静音帧：不得产生第二次提交
        await self._feed_async(30, voice=False)
        self.assertEqual(len(self.session._storage.get_completed_turns()), 1)
        lt = self.session.livetalking
        starts = [c for c in lt.calls if c["type"] == "start"]
        self.assertEqual(len(starts), 1)

    async def test_second_turn_after_aggregated_submit(self):
        """提交后第二轮语音仍能正常启动（新 ASR 实例 + 新 turn）。"""
        asr1 = await self._start_turn()
        asr1.emit_sentence("s1", "第一轮。", begin_time=0, end_time=500)
        await self._feed_async(12, voice=False)
        self.assertTrue(await self._wait_completed(1))

        # 第二轮：不重新 start，直接说话
        await self._feed_async(3, voice=True)
        self.assertTrue(self.session._asr_connected)
        asr2 = self.asr_instances[-1]
        self.assertIsNot(asr1, asr2)
        asr2.emit_sentence("t1", "第二轮。", begin_time=0, end_time=500)
        await self._feed_async(12, voice=False)
        self.assertTrue(await self._wait_completed(2))
        completed = self.session._storage.get_completed_turns()
        self.assertEqual(len(completed), 2)
        self.assertEqual(completed[1]["user"], "第二轮。")

    async def test_pause_interrupt_cancel_pending_submit(self):
        """pause / interrupt 在静音阈值达成前取消待提交任务。"""
        # pause 场景
        asr = await self._start_turn()
        asr.emit_sentence("s1", "暂停前的分句。", begin_time=0, end_time=500)
        await self._feed_async(5, voice=False)  # 5 帧 < 10 帧阈值
        await self.session.pause()
        await self._feed_async(30, voice=False)  # 超阈值但已暂停
        self.assertEqual(self.session.state, "paused")
        self.assertEqual(len(self.session._storage.get_completed_turns()), 0)

        # interrupt 场景
        await self.session.start()
        await self._feed_async(3, voice=True)
        asr2 = self.asr_instances[-1]
        asr2.emit_sentence("s1", "打断前的分句。", begin_time=0, end_time=500)
        await self._feed_async(3, voice=False)
        await self.session.interrupt()
        await self._feed_async(30, voice=False)
        self.assertEqual(len(self.session._storage.get_completed_turns()), 0)
        self.assertEqual(self.session.state, "listening")

    async def test_disconnect_cancels_pending_submit(self):
        """ASR 断线取消待提交任务并恢复 listening（不提交 pending 分句）。"""
        asr = await self._start_turn()
        asr.emit_sentence("s1", "断线前的分句。", begin_time=0, end_time=500)
        await self.session._on_asr_close()
        await self._feed_async(30, voice=False)
        self.assertEqual(len(self.session._storage.get_completed_turns()), 0)
        self.assertEqual(self.session.state, "listening")
        recent = self.session._storage.get_recent_turns()
        self.assertTrue(any(r["status"] == "interrupted" for r in recent))

    async def test_late_sentence_does_not_pollute_next_turn(self):
        """迟到分句（旧 turn）不得污染下一轮提交文本。"""
        asr1 = await self._start_turn()
        asr1.emit_sentence("s1", "第一轮内容。", begin_time=0, end_time=500)
        await self._feed_async(12, voice=False)
        self.assertTrue(await self._wait_completed(1))

        # 第二轮开始后，旧适配器补发迟到分句
        await self._feed_async(3, voice=True)
        asr2 = self.asr_instances[-1]
        asr1.emit_sentence("s2", "迟到的内容。", begin_time=600, end_time=900)  # 应被丢弃
        asr2.emit_sentence("t1", "第二轮内容。", begin_time=0, end_time=500)
        await self._feed_async(12, voice=False)
        self.assertTrue(await self._wait_completed(2))
        completed = self.session._storage.get_completed_turns()
        self.assertEqual(len(completed), 2)
        self.assertEqual(completed[1]["user"], "第二轮内容。")
        self.assertNotIn("迟到", completed[1]["user"])

    async def test_max_turn_limit_submits_with_reason(self):
        """达到安全上限时完整提交已有分句并记录原因（非静默截断）。"""
        # 覆盖 max_turn_ms 为极小值（测试专用，仅验证机制；生产默认 120s）
        self.session.max_turn_frames = 8
        asr = await self._start_turn()
        asr.emit_sentence("s1", "上限前内容。", begin_time=0, end_time=500)
        # 持续人声永不静音，但帧数达到上限
        await self._feed_async(10, voice=True)
        self.assertTrue(await self._wait_completed(1))
        completed = self.session._storage.get_completed_turns()
        self.assertEqual(completed[0]["user"], "上限前内容。")


class TestBackpressure(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        cfg = _make_config(self.tmp)
        cfg._raw["tts"]["pcm_queue_limit"] = 3
        self.session = ConversationSession(
            asr_factory=lambda cb: DummyASRAdapter(final_text="", on_final=cb),
            llm_factory=lambda cb: DummyLLMAdapter(reply="", on_token=cb),
            tts_factory=lambda cb: _SlowTTSAdapter(on_pcm=cb),
            livetalking_factory=_StrictLiveTalkingAdapter,
        )
        await self.session.start_tts_consumer()
        await self.session.start_pcm_sender()
        # 手动建立一个 turn，让 TTS 消费者真正处理队列
        from backend.session import TurnGuard

        self.session._next_turn_id()
        turn = TurnGuard(self.session._turn_id)
        turn.generation_id = self.session._next_generation_id()
        self.session._current_turn = turn
        self.session.livetalking = self.session.livetalking_factory()
        self.session.livetalking.session_id = "test-session"

    async def asyncTearDown(self):
        await self.session.shutdown()
        self.tmp_dir.cleanup()

    async def test_queue_preserves_order_under_backpressure(self):
        # 快速放入 5 个句子，队列容量只有 3，应阻塞而非丢数据
        received_order: list[str] = []
        producer = asyncio.create_task(self._produce_sentences(received_order))
        await asyncio.sleep(1.5)
        producer.cancel()
        try:
            await producer
        except asyncio.CancelledError:
            pass
        # 消费者应处理所有成功放入的句子，且顺序不变
        tts = self.session.tts
        self.assertIsInstance(tts, _SlowTTSAdapter)
        self.assertGreaterEqual(len(tts.received), 3)
        # 校验顺序
        for i, text in enumerate(tts.received):
            self.assertEqual(text, f"sentence {i}")

    async def _produce_sentences(self, out: list[str]):
        for i in range(5):
            await self.session._tts_queue.put((1, f"sentence {i}"))
            out.append(f"sentence {i}")


class _DisconnectASRAdapter(DummyASRAdapter):
    """接收若干 PCM 后调用 on_close 模拟断线。"""

    def __init__(self, on_final=None, on_close=None, on_error=None, **kwargs):
        super().__init__(final_text="", on_final=on_final, **kwargs)
        self.on_close = on_close
        self.on_error = on_error
        self._bytes_received = 0

    async def send_pcm(self, pcm: bytes) -> None:
        self._bytes_received += len(pcm)
        if self._bytes_received > 6400 and self.on_close and not self.submitted:
            self.on_close()


class _SlowTTSAdapter:
    """慢速 TTS，用于制造背压。"""

    def __init__(self, on_pcm=None, **kwargs):
        self.on_pcm = on_pcm
        self.received: list[str] = []

    async def start(self) -> bool:
        return True

    async def send_text(self, text: str) -> None:
        self.received.append(text)
        await asyncio.sleep(0.3)

    async def finish(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 第三关定向补考：静音从真实人声计算 + ASR 活动信号阻止提前提交
# ---------------------------------------------------------------------------

class TestSilenceFromRealVoice(unittest.IsolatedAsyncioTestCase):
    """最终分句不得重置本地静音计数；低于 RMS 阈值的中间结果活动阻止提前提交。"""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        cfg = _make_config(self.tmp)
        cfg._raw["audio"]["turn_end_silence_ms"] = 200  # 10 帧
        self.asr_instances: list[_SentenceASRAdapter] = []

        def asr_factory(on_final):
            asr = _SentenceASRAdapter(on_final=on_final)
            self.asr_instances.append(asr)
            return asr

        self.session = ConversationSession(
            asr_factory=asr_factory,
            llm_factory=lambda cb: DummyLLMAdapter(reply="好的，我明白了。", on_token=cb),
            tts_factory=lambda cb: _FragmentTTSAdapter(on_pcm=cb),
            livetalking_factory=_StrictLiveTalkingAdapter,
        )
        await self.session.start_tts_consumer()
        await self.session.start_pcm_sender()

    async def asyncTearDown(self):
        await self.session.shutdown()
        self.tmp_dir.cleanup()

    async def _feed_async(self, frames: int, voice: bool = True) -> None:
        for _ in range(frames):
            await self.session.handle_pcm(_voice_frame() if voice else _silence_frame())

    async def _start_turn(self) -> _SentenceASRAdapter:
        await self.session.start()
        await self._feed_async(3, voice=True)
        self.assertTrue(self.session._asr_connected)
        return self.asr_instances[-1]

    async def _wait_completed(self, count: int, timeout: float = 10.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if len(self.session._storage.get_completed_turns()) >= count:
                return True
            await asyncio.sleep(0.05)
        return False

    async def test_final_sentence_does_not_reset_silence(self):
        """最终分句到达不得重置已累计的本地静音（静音从真实最后人声计算）。"""
        asr = await self._start_turn()
        asr.emit_sentence("s1", "第一句说完了。", begin_time=0, end_time=1000)
        # 静音累计 9 帧（阈值 10 帧）
        await self._feed_async(9, voice=False)
        self.assertEqual(self.session._frames_since_voice, 9)
        # 若第二分句最终结果此刻到达，不得清零静音计数
        asr.emit_sentence("s2", "第二句也说完了。", begin_time=1200, end_time=2200)
        self.assertEqual(self.session._frames_since_voice, 9)
        # 再 1 帧静音即达阈值 → 单次提交
        await self._feed_async(1, voice=False)
        self.assertTrue(await self._wait_completed(1))
        completed = self.session._storage.get_completed_turns()
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["user"], "第一句说完了。第二句也说完了。")

    async def test_subthreshold_activity_blocks_early_submit(self):
        """低于 RMS 阈值、但产生 ASR 中间结果的新分句必须阻止上一分句被提前提交。"""
        asr = await self._start_turn()
        asr.emit_sentence("s1", "前半句包含东风。", begin_time=0, end_time=1000)
        # 停顿 9 帧（160ms×9=1.44s < 1.8s 阈值，模拟 1.0-1.3s 真人停顿场景）
        await self._feed_async(9, voice=False)
        self.assertEqual(self.session._frames_since_voice, 9)
        # 后半句开始：轻声低于 RMS 阈值，但 Qwen 非空中间结果到达（活动信号）
        asr.emit_activity()
        self.assertEqual(self.session._frames_since_voice, 0)
        # 活动后仅 5 帧静音：不得提交
        await self._feed_async(5, voice=False)
        self.assertEqual(self.session.state, "listening")
        turn = self.session._current_turn
        self.assertIsNotNone(turn)
        self.assertIsNone(turn.asr_final)
        # 后半句最终分句到达，完整静音后合并提交一次
        asr.emit_sentence("s2", "后半句包含西岳。", begin_time=1600, end_time=2600)
        await self._feed_async(10, voice=False)
        self.assertTrue(await self._wait_completed(1))
        completed = self.session._storage.get_completed_turns()
        self.assertEqual(len(completed), 1)
        self.assertIn("东风", completed[0]["user"])
        self.assertIn("西岳", completed[0]["user"])


# ---------------------------------------------------------------------------
# 第三关定向补考：pause 完整取消当前 turn
# ---------------------------------------------------------------------------

class TestPauseFullCleanup(unittest.IsolatedAsyncioTestCase):
    """pause 必须清除 turn/pending/门控/队列，DB 无 active 孤儿，不关 WebRTC 会话。"""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        cfg = _make_config(self.tmp)
        cfg._raw["audio"]["turn_end_silence_ms"] = 200
        self.asr_instances: list[_SentenceASRAdapter] = []

        def asr_factory(on_final):
            asr = _SentenceASRAdapter(on_final=on_final)
            self.asr_instances.append(asr)
            return asr

        self.session = ConversationSession(
            asr_factory=asr_factory,
            llm_factory=lambda cb: DummyLLMAdapter(reply="好的，我明白了。", on_token=cb),
            tts_factory=lambda cb: _FragmentTTSAdapter(on_pcm=cb),
            livetalking_factory=_StrictLiveTalkingAdapter,
        )
        await self.session.start_tts_consumer()
        await self.session.start_pcm_sender()

    async def asyncTearDown(self):
        await self.session.shutdown()
        self.tmp_dir.cleanup()

    async def _feed_async(self, frames: int, voice: bool = True) -> None:
        for _ in range(frames):
            await self.session.handle_pcm(_voice_frame() if voice else _silence_frame())

    async def _start_turn(self) -> _SentenceASRAdapter:
        await self.session.start()
        await self._feed_async(3, voice=True)
        self.assertTrue(self.session._asr_connected)
        return self.asr_instances[-1]

    async def _wait_completed(self, count: int, timeout: float = 10.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if len(self.session._storage.get_completed_turns()) >= count:
                return True
            await asyncio.sleep(0.05)
        return False

    async def test_pause_clears_turn_pending_and_gating(self):
        asr = await self._start_turn()
        asr.emit_sentence("s1", "暂停前的内容。", begin_time=0, end_time=500)
        await self._feed_async(5, voice=False)  # 静音未达阈值
        self.assertIsNotNone(self.session._current_turn)
        self.assertEqual(len(self.session._current_turn.pending_sentences), 1)

        await self.session.pause()
        self.assertEqual(self.session.state, "paused")
        self.assertIsNone(self.session._current_turn)
        self.assertIsNone(self.session.asr)
        self.assertFalse(self.session._asr_connected)
        self.assertEqual(self.session._rms_hits, 0)
        self.assertEqual(len(self.session._prebuffer), 0)
        self.assertEqual(self.session._frames_since_voice, 0)
        self.assertEqual(self.session._turn_frames, 0)
        self.assertTrue(self.session._tts_queue.empty())
        self.assertTrue(self.session._raw_pcm_queue.empty())
        # 数据库不留 active 孤立记录
        recent = self.session._storage.get_recent_turns()
        self.assertFalse(any(r["status"] == "active" for r in recent))
        self.assertTrue(any(r["status"] == "interrupted" for r in recent))
        # 暂停后静音帧不再触发提交
        await self._feed_async(30, voice=False)
        self.assertEqual(len(self.session._storage.get_completed_turns()), 0)

    async def test_pause_then_restart_new_turn_isolated(self):
        """pause 后重新开始：新 turn 不混入暂停前文本。"""
        asr1 = await self._start_turn()
        asr1.emit_sentence("s1", "暂停前旧文本。", begin_time=0, end_time=500)
        await self.session.pause()

        await self.session.start()
        await self._feed_async(3, voice=True)
        asr2 = self.asr_instances[-1]
        self.assertIsNot(asr1, asr2)
        asr2.emit_sentence("t1", "恢复后的新文本。", begin_time=0, end_time=400)
        await self._feed_async(12, voice=False)
        self.assertTrue(await self._wait_completed(1))
        completed = self.session._storage.get_completed_turns()
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["user"], "恢复后的新文本。")
        self.assertNotIn("旧文本", completed[0]["user"])

    async def test_pause_keeps_livetalking_session(self):
        """pause 不得销毁页面仍在使用的 WebRTC 会话（仅浏览器断开/应用关闭才关闭）。"""
        await self.session.set_session_id("pause-keep-session")
        await self._start_turn()
        await self.session.pause()
        lt = self.session.livetalking
        self.assertIsInstance(lt, _StrictLiveTalkingAdapter)
        self.assertEqual(lt.close_session_calls, 0)
        self.assertEqual(lt.session_id, "pause-keep-session")


# ---------------------------------------------------------------------------
# 第三关定向补考：max_turn_ms >= 120000 强制校正
# ---------------------------------------------------------------------------

class TestMaxTurnFloorConfig(unittest.IsolatedAsyncioTestCase):
    """低于 120000 的配置必须被校正为 120000（禁止隐性缩短安全上限）。"""

    async def _make_session(self) -> ConversationSession:
        return ConversationSession(
            asr_factory=lambda cb: _SentenceASRAdapter(on_final=cb),
            llm_factory=lambda cb: DummyLLMAdapter(reply="", on_token=cb),
            tts_factory=lambda cb: _FragmentTTSAdapter(on_pcm=cb),
            livetalking_factory=_StrictLiveTalkingAdapter,
        )

    async def test_below_floor_corrected(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        cfg = _make_config(Path(tmp_dir.name))
        for bad in (1000, 20000, 60000, 119999):
            cfg._raw["audio"]["max_turn_ms"] = bad
            session = await self._make_session()
            self.assertEqual(session.max_turn_ms, 120000, f"max_turn_ms={bad} not corrected")
            self.assertEqual(session.max_turn_frames, 120000 // 20)
            await session.shutdown()

    async def test_at_or_above_floor_kept(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        cfg = _make_config(Path(tmp_dir.name))
        cfg._raw["audio"]["max_turn_ms"] = 120000
        session = await self._make_session()
        self.assertEqual(session.max_turn_ms, 120000)
        await session.shutdown()
        cfg._raw["audio"]["max_turn_ms"] = 300000
        session = await self._make_session()
        self.assertEqual(session.max_turn_ms, 300000)
        await session.shutdown()

    async def test_missing_uses_floor_default(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        cfg = _make_config(Path(tmp_dir.name))
        cfg._raw["audio"].pop("max_turn_ms", None)
        session = await self._make_session()
        self.assertEqual(session.max_turn_ms, 120000)
        await session.shutdown()


# ---------------------------------------------------------------------------
# 第三关定向补考：会话标识日志脱敏
# ---------------------------------------------------------------------------

class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


async def _qwen_started_server(websocket):
    """伪 Qwen 服务：回 task-started 后保持连接。"""
    msg = await websocket.recv()
    data = json.loads(msg)
    task_id = (data.get("header") or {}).get("task_id")
    await websocket.send(
        json.dumps({"header": {"event": "task-started", "task_id": task_id}, "payload": {}})
    )
    try:
        await websocket.wait_closed()
    except Exception:
        pass


async def _tts_started_server(websocket):
    """伪火山服务：回 SessionStarted 后保持连接。"""
    msg = await websocket.recv()
    event, sid, _ = _parse_client_request(msg)
    if event == EVENT_StartSession:
        await websocket.send(_build_server_response(EVENT_SessionStarted, sid, b'{"message":"started"}'))
    try:
        await websocket.wait_closed()
    except Exception:
        pass


class TestSessionIdMasking(unittest.IsolatedAsyncioTestCase):
    """Qwen task ID / LiveTalking session ID / 火山 TTS session ID 日志必须脱敏。"""

    async def asyncSetUp(self):
        self.handler = _ListHandler()
        self.logger = logging.getLogger("voice_client")
        self.logger.addHandler(self.handler)

    async def asyncTearDown(self):
        self.logger.removeHandler(self.handler)

    def _assert_masked(self, full_value: str, marker: str) -> None:
        msgs = self.handler.messages
        self.assertTrue(any(marker in m for m in msgs), f"no log line contains marker {marker!r}")
        for m in msgs:
            self.assertNotIn(full_value, m, "full session id leaked to logs")

    async def test_mask_id_unit(self):
        from backend.logger import mask_id

        self.assertEqual(mask_id(None), "none")
        self.assertEqual(mask_id(""), "none")
        self.assertEqual(mask_id("short123"), "short1***")  # 短值仅保留头部
        long_id = "0123456789abcdefghij"
        self.assertEqual(mask_id(long_id), "012345***efghij")

    async def test_livetalking_session_id_masked(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        _make_config(Path(tmp_dir.name))
        session = ConversationSession(
            asr_factory=lambda cb: _SentenceASRAdapter(on_final=cb),
            llm_factory=lambda cb: DummyLLMAdapter(reply="", on_token=cb),
            tts_factory=lambda cb: _FragmentTTSAdapter(on_pcm=cb),
            livetalking_factory=_StrictLiveTalkingAdapter,
        )
        try:
            full = "lt-session-" + "x" * 30
            await session.set_session_id(full)
            self._assert_masked(full, "livetalking session_id=")
        finally:
            await session.shutdown()

    async def test_qwen_task_id_masked(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        cfg = _make_config(Path(tmp_dir.name))
        server = await serve(_qwen_started_server, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        cfg._raw["asr"]["ws_url"] = f"ws://127.0.0.1:{port}"
        adapter = QwenASRAdapter()
        try:
            started = await adapter.start()
            self.assertTrue(started)
            self.assertIsNotNone(adapter.task_id)
            self._assert_masked(adapter.task_id, "session started task=")
        finally:
            await adapter.stop()
            server.close()
            await server.wait_closed()
            tmp_dir.cleanup()

    async def test_huoshan_session_id_masked(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        cfg = _make_config(Path(tmp_dir.name))
        server = await serve(_tts_started_server, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        cfg._raw["tts"]["ws_url"] = f"ws://127.0.0.1:{port}"
        adapter = HuoshanTTSAdapter()
        try:
            started = await adapter.start()
            self.assertTrue(started)
            self.assertIsNotNone(adapter._session_id)
            self._assert_masked(adapter._session_id, "session started session_id=")
        finally:
            try:
                await adapter.cancel()
            except Exception:
                pass
            server.close()
            await server.wait_closed()
            tmp_dir.cleanup()


# ---------------------------------------------------------------------------
# 第三关定向补考：真实 /ws 端点浏览器断开清理
# ---------------------------------------------------------------------------

class TestBrowserDisconnectCleanup(unittest.IsolatedAsyncioTestCase):
    """真实浏览器断开（经 FastAPI /ws + uvicorn）：取消 turn、关闭 LT 会话一次、可重连。"""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        cfg = _make_config(self.tmp)
        cfg._raw["audio"]["turn_end_silence_ms"] = 200
        self.asr_instances: list[_SentenceASRAdapter] = []

        def asr_factory(on_final):
            asr = _SentenceASRAdapter(on_final=on_final)
            self.asr_instances.append(asr)
            return asr

        self.session = ConversationSession(
            asr_factory=asr_factory,
            llm_factory=lambda cb: DummyLLMAdapter(reply="好的，我明白了。", on_token=cb),
            tts_factory=lambda cb: _FragmentTTSAdapter(on_pcm=cb),
            livetalking_factory=_StrictLiveTalkingAdapter,
        )
        await self.session.start_tts_consumer()
        await self.session.start_pcm_sender()
        # 注入全局 session：get_session() 直接复用（uvicorn 与测试共享同一事件循环）
        main_module._session = self.session
        main_module._active_websockets.clear()
        self.server = uvicorn.Server(
            uvicorn.Config(main_module.app, host="127.0.0.1", port=0, log_level="error")
        )
        self.server_task = asyncio.create_task(self.server.serve())
        for _ in range(200):
            if self.server.started:
                break
            await asyncio.sleep(0.05)
        self.assertTrue(self.server.started, "uvicorn test server failed to start")
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.should_exit = True
        try:
            await asyncio.wait_for(self.server_task, timeout=10)
        except Exception:
            self.server_task.cancel()
        main_module._session = None
        main_module._active_websockets.clear()
        try:
            await self.session.shutdown()
        except Exception:
            pass
        self.tmp_dir.cleanup()

    async def _wait_state(self, state: str, timeout: float = 10.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self.session.state == state:
                return True
            await asyncio.sleep(0.05)
        return False

    async def _connect(self):
        return await websockets.connect(f"ws://127.0.0.1:{self.port}/ws")

    async def _begin_turn(self, ws, sentence_key: str, text: str) -> _SentenceASRAdapter:
        await ws.send(json.dumps({"type": "sessionid", "sessionId": "ws-disconnect-test-session"}))
        await ws.send(json.dumps({"type": "start"}))
        await asyncio.sleep(0.2)
        for _ in range(3):
            await ws.send(_voice_frame())
        await asyncio.sleep(0.2)
        asr = self.asr_instances[-1]
        asr.emit_sentence(sentence_key, text, begin_time=0, end_time=500)
        return asr

    async def _wait_completed(self, count: int, timeout: float = 10.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if len(self.session._storage.get_completed_turns()) >= count:
                return True
            await asyncio.sleep(0.1)
        return False

    async def test_last_disconnect_cancels_turn_and_closes_livetalking_once(self):
        """最后一个浏览器断开：取消 pending turn，close_session 恰好一次，状态回 paused。"""
        ws = await self._connect()
        try:
            asr = await self._begin_turn(ws, "s1", "断开前的分句。")
            for _ in range(5):
                await ws.send(_silence_frame())
            await asyncio.sleep(0.2)
            self.assertIsNotNone(self.session._current_turn)
        finally:
            await ws.close()
        # 断开后 10 秒内完成清理
        self.assertTrue(await self._wait_state("paused", timeout=10))
        self.assertIsNone(self.session._current_turn)
        self.assertIsNone(self.session.asr)
        self.assertFalse(self.session._asr_connected)
        lt = self.session.livetalking
        self.assertIsInstance(lt, _StrictLiveTalkingAdapter)
        self.assertEqual(lt.close_session_calls, 1)
        recent = self.session._storage.get_recent_turns()
        self.assertFalse(any(r["status"] == "active" for r in recent))
        self.assertTrue(any(r["status"] == "interrupted" for r in recent))
        self.assertEqual(len(self.session._storage.get_completed_turns()), 0)

    async def test_one_of_two_connections_close_keeps_session(self):
        """多连接：一个断开不得误杀仍在使用的其他连接。"""
        ws1 = await self._connect()
        ws2 = await self._connect()
        try:
            await self._begin_turn(ws1, "s1", "多连接场景分句。")
        finally:
            await ws1.close()
        await asyncio.sleep(0.5)
        # ws2 仍在使用：不得清理
        self.assertEqual(self.session.state, "listening")
        self.assertIsNotNone(self.session._current_turn)
        self.assertEqual(self.session.livetalking.close_session_calls, 0)
        await ws2.close()
        self.assertTrue(await self._wait_state("paused", timeout=10))
        self.assertEqual(self.session.livetalking.close_session_calls, 1)

    async def test_reconnect_completes_new_turn(self):
        """断开清理后重新连接：可重建绑定并完成新一轮完整对话。"""
        # 第一次连接：pending turn 被断开取消
        ws = await self._connect()
        try:
            asr1 = await self._begin_turn(ws, "s1", "断开前旧内容。")
            for _ in range(5):
                await ws.send(_silence_frame())
        finally:
            await ws.close()
        self.assertTrue(await self._wait_state("paused", timeout=10))
        self.assertEqual(len(self.session._storage.get_completed_turns()), 0)

        # 重新打开浏览器：完成新一轮对话（ASR→LLM→TTS→音频流→落库）
        ws2 = await self._connect()
        try:
            asr2 = await self._begin_turn(ws2, "t1", "重连后的新内容。")
            self.assertIsNot(asr1, asr2)
            for _ in range(12):
                await ws2.send(_silence_frame())
            self.assertTrue(await self._wait_completed(1), "reconnected turn did not complete")
        finally:
            await ws2.close()
        completed = self.session._storage.get_completed_turns()
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["user"], "重连后的新内容。")
        self.assertNotIn("旧内容", completed[0]["user"])


if __name__ == "__main__":
    unittest.main()
