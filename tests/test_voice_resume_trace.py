"""R1-INSTRUMENT-VOICE-RESUME 后端 instrumentation 测试。

只验证 instrumentation 的 metadata 观测（事件/计数/关联 id）与既有行为未被改变：
- 完整链路事件顺序：voice/start → pcm/first → vad/hit → asr/start_attempt → asr/started；
- ASR 失败路径 asr/failed 且状态回 error（既有行为）；
- pause 结束 resume 并输出 resume/end 汇总；
- trace 行不包含语音文本/secret/PCM 内容；
- 未改 VAD/ASR/pause-resume 判定逻辑（只读观测）。
"""

import asyncio
import json
import logging
import re
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn
import websockets

import backend.main as main_module
from backend import config as config_module
from backend.adapters.deepseek_llm import DummyLLMAdapter
from backend.adapters.huoshan_tts import DummyTTSAdapter
from backend.adapters.livetalking import DummyLiveTalkingAdapter
from backend.adapters.qwen_asr import DummyASRAdapter
from backend.config import Config
from backend.session import ConversationSession
# 复用 test_contracts 的真实 uvicorn + websockets 集成夹具辅助（避免重复定义）
from tests.test_contracts import _make_config as contracts_make_config
from tests.test_contracts import _voice_frame as contracts_voice_frame


def _voice_frame(amplitude=0.5):
    samples = [int(amplitude * 0x7fff)] * 320
    return b"".join(struct.pack("<h", s) for s in samples)


def _make_config(tmp_path):
    cfg = Config()
    cfg._raw = {
        "app": {"host": "127.0.0.1", "port": 7870},
        "asr": {"model": "dummy", "ws_url": "", "format": "pcm", "sample_rate": 16000},
        "llm": {"model": "dummy", "base_url": "", "max_context_turns": 20},
        "tts": {"pcm_queue_limit": 50},
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
    cfg._secrets = {}
    config_module._config = cfg
    return cfg


class _TraceCapture(logging.Handler):
    """只收集 [voice-trace] 行，便于断言事件与敏感数据。"""

    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        msg = record.getMessage()
        if "[voice-trace]" in msg:
            self.lines.append(msg)


def _events(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        for part in line.split(" "):
            if part.startswith("event="):
                out.append(part.split("=", 1)[1])
    return out


def _trace_records(lines: list[str]) -> list[dict[str, str]]:
    records = []
    for line in lines:
        if not line.startswith("[voice-trace]"):
            continue
        records.append(
            {
                key: value
                for token in line.split()
                if "=" in token
                for key, value in [token.split("=", 1)]
            }
        )
    return records


class TestVoiceResumeTrace(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        _make_config(self.tmp)
        logging.getLogger("voice_client").setLevel(logging.INFO)
        self.session = ConversationSession(
            asr_factory=lambda cb: DummyASRAdapter(final_text="你好测试", on_final=cb),
            llm_factory=lambda cb: DummyLLMAdapter(
                reply="你好，我是小唯。很高兴认识你。", on_token=cb
            ),
            tts_factory=lambda cb: DummyTTSAdapter(duration_seconds=0.1, on_pcm=cb),
            livetalking_factory=DummyLiveTalkingAdapter,
        )
        await self.session.start_tts_consumer()
        await self.session.start_pcm_sender()
        self.capture = _TraceCapture()
        logging.getLogger("voice_client").addHandler(self.capture)

    async def asyncTearDown(self):
        logging.getLogger("voice_client").removeHandler(self.capture)
        await self.session.shutdown()
        self.tmp_dir.cleanup()

    async def test_full_chain_events_and_behavior_unchanged(self):
        await self.session.start(resume_id="probe-1")
        self.assertEqual(self.session.state, "listening")
        self.assertEqual(self.session._resume_id, "probe-1")

        for _ in range(3):
            await self.session.handle_pcm(_voice_frame())

        # instrumentation 计数（rms_consecutive_frames=2，2 帧后 VAD 命中）
        self.assertGreaterEqual(self.session._resume_frames, 3)
        self.assertGreaterEqual(self.session._resume_vad_hits, 1)
        self.assertEqual(self.session._resume_asr_attempts, 1)
        self.assertEqual(self.session._resume_asr_started, 1)
        self.assertEqual(self.session._resume_asr_failed, 0)
        self.assertGreater(self.session._resume_last_rms, 0.0)

        diag = self.session.diagnostics()
        self.assertEqual(diag["resume_id"], "probe-1")
        self.assertGreaterEqual(diag["resume_frames"], 3)
        self.assertGreaterEqual(diag["resume_vad_hits"], 1)
        self.assertEqual(diag["resume_asr_started"], 1)

        # 事件顺序：voice/start < pcm/first < vad/hit < asr/start_attempt < asr/started
        events = _events(self.capture.lines)
        for required in ("voice/start", "pcm/first", "vad/hit", "asr/start_attempt", "asr/started"):
            self.assertIn(required, events)
        self.assertLess(events.index("voice/start"), events.index("pcm/first"))
        self.assertLess(events.index("pcm/first"), events.index("vad/hit"))
        self.assertLess(events.index("vad/hit"), events.index("asr/start_attempt"))
        self.assertLess(events.index("asr/start_attempt"), events.index("asr/started"))

        # trace 行格式：resume= / event= / t_ms=，且不含语音文本或 secret 字段
        for line in self.capture.lines:
            self.assertTrue(line.startswith("[voice-trace] resume="), line)
            self.assertIn(" t_ms=", line)
            self.assertNotIn("你好测试", line)
            self.assertNotIn("api_key", line)
            self.assertNotIn("token", line)

        # 既有行为未变：ASR final 正常到达并提交（与 test_core 相同模式）
        for _ in range(12):
            await self.session.handle_pcm(_voice_frame())
        await asyncio.sleep(2.5)
        self.assertIsNotNone(self.session._current_turn)
        self.assertEqual(self.session._current_turn.asr_final, "你好测试")

    async def test_vad_frame_trace_records_counter_transition_and_chain(self):
        await self.session.start(resume_id="probe-vad-sequence")
        self.session.rms_consecutive_frames = 3
        for amplitude in (0.2, 0.0, 0.2, 0.2, 0.2):
            await self.session.handle_pcm(_voice_frame(amplitude))

        records = _trace_records(self.capture.lines)
        frames = [record for record in records if record.get("event") == "vad/frame"]
        self.assertEqual([int(record["frame_idx"]) for record in frames], [1, 2, 3, 4, 5])
        self.assertEqual([record["above"] for record in frames], ["True", "False", "True", "True", "True"])
        self.assertEqual([int(record["consecutive"]) for record in frames], [1, 0, 1, 2, 3])
        self.assertEqual([int(record["previous_consecutive"]) for record in frames], [0, 1, 0, 1, 2])
        self.assertEqual(
            [record["counter_reset"] for record in frames],
            ["False", "True", "False", "False", "False"],
        )
        self.assertEqual({float(record["threshold"]) for record in frames}, {0.1})
        self.assertTrue(all(float(record["rms"]) >= 0.0 for record in frames))
        self.assertAlmostEqual(float(frames[0]["rms"]), 0.2, places=4)
        self.assertAlmostEqual(float(frames[1]["rms"]), 0.0, places=4)
        self.assertEqual({int(record["required"]) for record in frames}, {3})

        events = [record["event"] for record in records]
        frame4 = next(
            index
            for index, record in enumerate(records)
            if record.get("event") == "vad/frame" and record.get("frame_idx") == "4"
        )
        frame5 = next(
            index
            for index, record in enumerate(records)
            if record.get("event") == "vad/frame" and record.get("frame_idx") == "5"
        )
        vad_hit = events.index("vad/hit")
        asr_attempt = events.index("asr/start_attempt")
        asr_started = events.index("asr/started")
        self.assertLess(frame4, vad_hit)
        self.assertNotIn("vad/hit", events[:frame5])
        self.assertLess(frame5, vad_hit)
        self.assertLess(vad_hit, asr_attempt)
        self.assertLess(asr_attempt, asr_started)
        self.assertEqual(
            {record["resume"] for record in records if record["event"] in {
                "vad/frame", "vad/hit", "asr/start_attempt", "asr/started"
            }},
            {"probe-vad-sequence"},
        )

    async def test_alternating_high_low_never_hits_vad_or_starts_asr(self):
        await self.session.start(resume_id="probe-vad-alternating")
        self.session.rms_consecutive_frames = 3
        for amplitude in (0.2, 0.0, 0.2, 0.0, 0.2, 0.0, 0.0):
            await self.session.handle_pcm(_voice_frame(amplitude))

        records = _trace_records(self.capture.lines)
        frames = [record for record in records if record.get("event") == "vad/frame"]
        self.assertEqual(len(frames), 7)
        self.assertEqual([int(record["consecutive"]) for record in frames], [1, 0, 1, 0, 1, 0, 0])
        self.assertEqual(
            [record["counter_reset"] for record in frames],
            ["False", "True", "False", "True", "False", "True", "False"],
        )
        self.assertEqual({int(record["required"]) for record in frames}, {3})
        events = [record["event"] for record in records]
        self.assertNotIn("vad/hit", events)
        self.assertNotIn("asr/start_attempt", events)
        self.assertEqual(self.session._resume_vad_hits, 0)
        self.assertEqual(self.session._resume_asr_attempts, 0)
        self.assertFalse(self.session._asr_connected)

    async def test_first_receive_observable_before_listening_gate(self):
        """真实首包 receive 观测必须先于任何状态门控：
        帧在状态非 listening 时被门控丢弃，但 receive 必须仍然可观测。"""
        await self.session.start(resume_id="probe-gate")
        self.session._set_state("speaking")  # 模拟首包到达时状态已离开 listening
        await self.session.handle_pcm(_voice_frame())

        # 观测已发生：帧计数 / 首包标记 / pcm/first 事件
        self.assertEqual(self.session._resume_frames, 1)
        self.assertTrue(self.session._resume_first_pcm_seen)
        self.assertIsNotNone(self.session._resume_first_pcm_mono)
        events = _events(self.capture.lines)
        self.assertIn("pcm/first", events)

        # 门控行为未变：非 listening 帧被丢弃，不触发 RMS 记录 / VAD / ASR
        self.assertEqual(self.session._resume_last_rms, 0.0)
        self.assertEqual(self.session._resume_vad_hits, 0)
        self.assertEqual(self.session._resume_asr_attempts, 0)
        self.assertFalse(self.session._asr_connected)
        self.assertNotIn("vad/hit", events)
        self.assertNotIn("asr/start_attempt", events)

    async def test_resume_id_sanitization(self):
        """客户端 correlation 值必须经安全约束：非法/超长/注入型不得原样进入日志或 diagnostics。"""
        # 合法：原样作为 correlation
        await self.session.start(resume_id="probe-ok_1")
        self.assertEqual(self.session._resume_id, "probe-ok_1")
        await self.session.pause()

        # 注入型：拒绝并回退服务端安全 id；原文绝不进入日志 / diagnostics
        evil = "<script>alert(1)</script>\n[voice-trace] fake=1"
        await self.session.start(resume_id=evil)
        self.assertTrue(str(self.session._resume_id).startswith("R"))
        self.assertNotEqual(self.session._resume_id, evil)
        for line in self.capture.lines:
            self.assertNotIn("script", line)
            self.assertNotIn("fake=1", line)
        self.assertNotIn(evil, str(self.session.diagnostics()["resume_id"]))
        await self.session.pause()

        # 超长：拒绝
        await self.session.start(resume_id="x" * 100)
        self.assertTrue(str(self.session._resume_id).startswith("R"))
        await self.session.pause()

        # 非字符串：拒绝
        await self.session.start(resume_id=12345)  # type: ignore[arg-type]
        self.assertTrue(str(self.session._resume_id).startswith("R"))
        await self.session.pause()

    async def test_legitimate_correlation_id_flows_through(self):
        """同一个合法 correlation id 贯穿后端关键事件与 diagnostics。"""
        frontend_rid = "r3_m5xk2z_ab12cd"  # 与 app.js nextResumeId 同构
        await self.session.start(resume_id=frontend_rid)
        self.assertEqual(self.session._resume_id, frontend_rid)
        await self.session.handle_pcm(_voice_frame())
        await self.session.handle_pcm(_voice_frame())

        diag = self.session.diagnostics()
        self.assertEqual(diag["resume_id"], frontend_rid)
        for line in self.capture.lines:
            if line.startswith("[voice-trace]"):
                self.assertIn(f"resume={frontend_rid}", line)
        await self.session.pause()

    async def test_asr_failure_trace_and_error_state(self):
        def failing_factory(cb):
            asr = DummyASRAdapter(final_text="", on_final=cb)

            async def _fail_start():
                return False

            asr.start = _fail_start
            return asr

        self.session.asr_factory = failing_factory
        errors = []
        self.session.on_error = errors.append
        await self.session.start(resume_id="probe-2")
        # 旧测试直接调用内部 startup，绕过了潜在死锁的真实持锁路径。
        # 这里从 start → RMS/VAD → handle_pcm 真实驱动 startup failure。
        await self.session.handle_pcm(_voice_frame())
        failed_task = asyncio.create_task(self.session.handle_pcm(_voice_frame()))
        await asyncio.wait_for(failed_task, timeout=0.25)

        self.assertEqual(self.session._resume_asr_attempts, 1)
        self.assertEqual(self.session._resume_asr_started, 0)
        self.assertEqual(self.session._resume_asr_failed, 1)
        self.assertTrue(failed_task.done())
        self.assertFalse(self.session._lock.locked())
        self.assertFalse(self.session._lock._waiters)
        self.assertIsNone(self.session._current_turn)
        self.assertFalse(self.session._asr_connected)
        self.assertEqual(self.session._rms_hits, 0)
        self.assertFalse(self.session._prebuffer)
        self.assertEqual(self.session._tts_queue.qsize(), 0)
        self.assertEqual(self.session._raw_pcm_queue.qsize(), 0)
        self.assertEqual(self.session._storage.get_recent_turns(1)[0]["status"], "failed")
        self.assertEqual(errors, ["ASR 连接失败"])
        # ASR 连接失败仍然对外可观测，且不留下锁内 waiter。
        self.assertEqual(self.session.state, "error")
        events = _events(self.capture.lines)
        self.assertIn("asr/start_attempt", events)
        self.assertIn("asr/failed", events)

    async def test_asr_startup_failure_recovers_same_session_to_speaking(self):
        attempts = 0

        def sequence_factory(cb):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                asr = DummyASRAdapter(final_text="", on_final=cb)

                async def _fail_start():
                    return False

                asr.start = _fail_start
                return asr
            return DummyASRAdapter(final_text="恢复后的识别", on_final=cb)

        states = []
        self.session.asr_factory = sequence_factory
        self.session.on_state_change = states.append

        await self.session.start(resume_id="lock-recovery")
        await self.session.handle_pcm(_voice_frame())
        failed_task = asyncio.create_task(self.session.handle_pcm(_voice_frame()))
        await asyncio.wait_for(failed_task, timeout=0.25)
        self.assertEqual(self.session.state, "error")
        self.assertFalse(self.session._lock.locked())

        # Recovery is an ordinary next start in the same process.  No
        # cancellation/restart workaround is used to release the lock.
        await self.session.start(resume_id="lock-recovery-2")
        self.assertEqual(self.session.state, "listening")
        for _ in range(15):
            await self.session.handle_pcm(_voice_frame())
        await asyncio.sleep(2.5)

        self.assertEqual(attempts, 2)
        self.assertIn("thinking", states)
        self.assertIn("speaking", states)
        self.assertEqual(self.session.state, "listening")
        self.assertFalse(self.session._lock.locked())
        self.assertFalse(self.session._lock._waiters)
        self.assertGreaterEqual(self.session._resume_asr_started, 1)
        self.assertEqual(
            sum("event=asr/failed" in line for line in self.capture.lines), 1
        )

    async def test_two_startup_failures_then_third_attempt_completes(self):
        attempts = 0

        def sequence_factory(cb):
            nonlocal attempts
            attempts += 1
            asr = DummyASRAdapter(
                final_text="第三次成功", on_final=cb
            )
            if attempts <= 2:
                async def _fail_start():
                    return False

                asr.start = _fail_start
            return asr

        self.session.asr_factory = sequence_factory
        for failure_index in range(2):
            await self.session.start(resume_id=f"retry-{failure_index}")
            await self.session.handle_pcm(_voice_frame())
            task = asyncio.create_task(self.session.handle_pcm(_voice_frame()))
            await asyncio.wait_for(task, timeout=0.25)
            self.assertTrue(task.done())
            self.assertEqual(self.session.state, "error")
            self.assertFalse(self.session._lock.locked())
            self.assertFalse(self.session._lock._waiters)
            self.assertIsNone(self.session._current_turn)
            self.assertFalse(self.session._asr_connected)

        await self.session.start(resume_id="retry-success")
        for _ in range(15):
            await self.session.handle_pcm(_voice_frame())
        await asyncio.sleep(2.5)

        self.assertEqual(attempts, 3)
        self.assertEqual(
            sum("event=asr/failed" in line for line in self.capture.lines), 2
        )
        self.assertEqual(self.session._resume_asr_started, 1)
        self.assertEqual(self.session.state, "listening")
        self.assertFalse(self.session._lock.locked())
        self.assertFalse(self.session._lock._waiters)
        self.assertIsNotNone(self.session._current_turn)
        self.assertEqual(self.session._current_turn.asr_final, "第三次成功")

    async def test_default_qwen_error_callback_cannot_poison_next_adapter(self):
        """旧 Qwen startup on_error waiter 不得把恢复中的会话改回 error。"""
        errors = []
        self.session.on_error = errors.append
        await self.session.start(resume_id="qwen-error-race")
        # 仅为构造真实 Qwen adapter 的回调语义提供临时测试密钥；不触发网络。
        config_module._config._secrets["DASHSCOPE_API_KEY"] = "test-key"

        first = self.session._default_asr_factory(lambda text: None)
        second = self.session._default_asr_factory(lambda text: None)
        self.session.asr = first
        # 模拟 startup failure 已完成清理并立即换入下一适配器；旧回调
        # 仍在事件循环中排队，但应通过 adapter identity gate 被丢弃。
        first.on_error("旧适配器错误")
        self.session.asr = second
        await asyncio.sleep(0)

        self.assertEqual(self.session.state, "listening")
        self.assertEqual(errors, [])
        self.assertFalse(self.session._lock.locked())
        self.assertFalse(self.session._lock._waiters)

    async def test_pause_ends_resume_with_summary(self):
        await self.session.start(resume_id="probe-3")
        await self.session.handle_pcm(_voice_frame())
        await self.session.pause()
        self.assertIsNone(self.session._resume_id)
        events = _events(self.capture.lines)
        self.assertIn("resume/end", events)
        end_line = next(l for l in self.capture.lines if "event=resume/end" in l)
        self.assertIn("reason=pause", end_line)
        self.assertIn("frames=", end_line)

    async def test_start_generates_resume_id_when_absent(self):
        await self.session.start()
        self.assertTrue(self.session._resume_id)
        self.assertTrue(str(self.session._resume_id).startswith("R"))
        await self.session.pause()
        self.assertIsNone(self.session._resume_id)


class TestVoiceResumeCorrelationIntegration(unittest.IsolatedAsyncioTestCase):
    """REV 3：真实跨前后端边界的 correlation 集成测试。

    前端 start payload（含合法 resume_id）→ 真实 WebSocket → FastAPI /ws
    → _handle_command → session.start(resume_id=...) → 模拟 PCM → pcm/first
    → VAD → ASR → diagnostics，全程保持同一 correlation identity。
    真实经过后端命令入口：不 mock _handle_command、不手工给 session 塞 resume_id、
    不是两个独立单测拼接；仅对 ASR/LLM/TTS/LiveTalking 使用 Dummy 适配器。
    """

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        cfg = contracts_make_config(self.tmp)
        logging.getLogger("voice_client").setLevel(logging.INFO)
        self.capture = _TraceCapture()
        logging.getLogger("voice_client").addHandler(self.capture)
        self.session = ConversationSession(
            asr_factory=lambda cb: DummyASRAdapter(final_text="你好测试", on_final=cb),
            llm_factory=lambda cb: DummyLLMAdapter(reply="好的。", on_token=cb),
            tts_factory=lambda cb: DummyTTSAdapter(duration_seconds=0.1, on_pcm=cb),
            livetalking_factory=DummyLiveTalkingAdapter,
        )
        await self.session.start_tts_consumer()
        await self.session.start_pcm_sender()
        # 注入全局 session：真实 /ws 端点经 get_session() 复用（uvicorn 与测试共享事件循环）
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
        logging.getLogger("voice_client").removeHandler(self.capture)
        try:
            await self.session.shutdown()
        except Exception:
            pass
        self.tmp_dir.cleanup()

    async def test_same_correlation_identity_across_frontend_backend_boundary(self):
        # 前端合法 resumeId（与 app.js nextResumeId 同构；node 行为测试已证 app.js
        # 以 {type:'start', resume_id} 发送，此处经真实边界投递同一 payload 形状）
        rid = "r1_m5xk2z_ab12cd"
        self.assertTrue(re.fullmatch(r"^[A-Za-z0-9_-]{1,32}$", rid), "rid 必须是合法 correlation")

        ws = await websockets.connect(f"ws://127.0.0.1:{self.port}/ws")
        try:
            # 基线：连接建立后、命令到达前，session 尚无 correlation
            self.assertIsNone(self.session._resume_id)

            # 1) start 消息包含合法 resume_id；2) 真实进入 backend command handler
            await ws.send(json.dumps({"type": "start", "resume_id": rid}))
            await asyncio.sleep(0.3)

            # 3) backend 接受的是同一 correlation identity
            #   （_resume_id 只能经 _handle_command → session.start(resume_id=...) 获得）
            self.assertEqual(self.session._resume_id, rid)

            # 4) 模拟 PCM：pcm/first → VAD → ASR 均关联同一 identity
            for _ in range(3):
                await ws.send(contracts_voice_frame())
            await asyncio.sleep(0.3)

            self.assertGreaterEqual(self.session._resume_frames, 3)
            self.assertGreaterEqual(self.session._resume_vad_hits, 1)
            self.assertEqual(self.session._resume_asr_started, 1)

            diag = self.session.diagnostics()
            self.assertEqual(diag["resume_id"], rid)
            self.assertGreaterEqual(diag["resume_vad_hits"], 1)
            self.assertEqual(diag["resume_asr_started"], 1)

            # 全部关键 trace 事件携带同一 correlation identity 且顺序正确
            events = _events(self.capture.lines)
            for required in ("voice/start", "pcm/first", "vad/hit", "asr/start_attempt", "asr/started"):
                self.assertIn(required, events)
            self.assertLess(events.index("voice/start"), events.index("pcm/first"))
            self.assertLess(events.index("pcm/first"), events.index("vad/hit"))
            self.assertLess(events.index("vad/hit"), events.index("asr/start_attempt"))
            self.assertLess(events.index("asr/start_attempt"), events.index("asr/started"))
            for line in self.capture.lines:
                if line.startswith("[voice-trace]"):
                    self.assertIn(f"resume={rid}", line)
        finally:
            await ws.close()


if __name__ == "__main__":
    unittest.main()
