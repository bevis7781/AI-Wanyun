import asyncio
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.adapters.deepseek_llm import DummyLLMAdapter
from backend.adapters.huoshan_tts import DummyTTSAdapter
from backend.adapters.livetalking import DummyLiveTalkingAdapter
from backend.adapters.qwen_asr import DummyASRAdapter
from backend import config as config_module
from backend.config import Config
from backend.session import ConversationSession


def _silence_frame():
    return bytes(640)


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


class TestDummyAdapters(unittest.IsolatedAsyncioTestCase):
    async def test_asr_submits_once(self):
        finals = []
        asr = DummyASRAdapter(final_text="一次", on_final=lambda t: finals.append(t))
        await asr.start()
        for _ in range(20):
            await asr.send_pcm(bytes(640))
        await asyncio.sleep(1.5)
        await asr.finish()
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0], "一次")

    async def test_asr_retry_cooldown(self):
        asr = DummyASRAdapter(final_text="")
        asr.max_retries = 1
        asr.retry_cooldown = 2.0
        # 模拟所有重试耗尽后进入冷却
        asr._retry_not_before = __import__("time").monotonic() + 10.0
        started = await asr.start()
        self.assertFalse(started)

    async def test_llm_stream(self):
        tokens = []
        llm = DummyLLMAdapter(reply="你好。", on_token=lambda t: tokens.append(t))
        async for _ in llm.chat("", [], ""):
            pass
        self.assertEqual("".join(tokens), "你好。")

    async def test_tts_pcm_frames(self):
        frames = []
        tts = DummyTTSAdapter(duration_seconds=0.1, on_pcm=lambda b: frames.append(b))
        await tts.start()
        await tts.send_text("hello")
        await tts.finish()
        await asyncio.sleep(0.3)
        self.assertGreaterEqual(len(frames), 4)
        self.assertTrue(all(len(f) == 640 for f in frames))

    async def test_livetalking_protocol_order(self):
        lt = DummyLiveTalkingAdapter()
        await lt.start_audio_stream(1)
        await lt.send_pcm(bytes(640))
        await lt.send_pcm(bytes(640))
        await lt.end_audio_stream(1)
        await lt.start_audio_stream(2)
        await lt.abort_audio_stream(2)
        types = [c["type"] for c in lt.calls]
        self.assertEqual(types, ["start", "end", "start", "abort"])
        self.assertEqual(len(lt.pcm_frames), 2)

    async def test_livetalking_rejects_stale_generation(self):
        lt = DummyLiveTalkingAdapter()
        await lt.start_audio_stream(1)
        await lt.end_audio_stream(1)
        await lt.start_audio_stream(2)
        ok = await lt.abort_audio_stream(1)
        self.assertFalse(ok)

    async def test_livetalking_rejects_non_640_frame(self):
        lt = DummyLiveTalkingAdapter()
        await lt.start_audio_stream(1)
        await lt.send_pcm(bytes(300))
        await lt.send_pcm(bytes(640))
        self.assertEqual(len(lt.pcm_frames), 1)


class TestSession(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        _make_config(self.tmp)
        self.session = ConversationSession(
            asr_factory=lambda cb: DummyASRAdapter(final_text="你好测试", on_final=cb),
            llm_factory=lambda cb: DummyLLMAdapter(
                reply="你好，我是小唯。很高兴认识你。", on_token=cb
            ),
            tts_factory=lambda cb: DummyTTSAdapter(duration_seconds=0.2, on_pcm=cb),
            livetalking_factory=DummyLiveTalkingAdapter,
        )
        await self.session.start_tts_consumer()
        await self.session.start_pcm_sender()

    async def asyncTearDown(self):
        await self.session.shutdown()
        self.tmp_dir.cleanup()

    async def test_state_machine_and_asr_final(self):
        states = []
        self.session.on_state_change = lambda s: states.append(s)
        await self.session.start()
        self.assertEqual(self.session.state, "listening")
        for _ in range(5):
            await self.session.handle_pcm(_silence_frame())
        for _ in range(20):
            await self.session.handle_pcm(_voice_frame())
        for _ in range(60):
            await asyncio.sleep(0.05)
            if self.session.state == "speaking":
                break
        self.assertIsNotNone(self.session._current_turn)
        self.assertEqual(self.session._current_turn.asr_final, "你好测试")
        self.assertIn("speaking", states)

    async def test_interrupt_increments_turn(self):
        await self.session.start()
        for _ in range(3):
            await self.session.handle_pcm(_voice_frame())
        await asyncio.sleep(0.1)
        old_turn = self.session._current_turn.turn_id if self.session._current_turn else 0
        await self.session.interrupt()
        self.assertIsNone(self.session._current_turn)
        self.assertEqual(self.session.state, "listening")
        self.assertGreater(self.session._turn_id, old_turn)

    async def test_completed_turns_persist(self):
        await self.session.start()
        for _ in range(20):
            await self.session.handle_pcm(_voice_frame())
        await asyncio.sleep(3.0)
        completed = self.session._storage.get_completed_turns()
        self.assertGreaterEqual(len(completed), 1)
        self.assertEqual(completed[0]["user"], "你好测试")
        self.assertIn("小唯", completed[0]["assistant"])

    async def test_interrupt_drops_late_asr(self):
        await self.session.start()
        for _ in range(20):
            await self.session.handle_pcm(_voice_frame())
        old_turn = self.session._current_turn.turn_id if self.session._current_turn else 0
        await self.session.interrupt()
        self.assertIsNone(self.session._current_turn)
        self.assertEqual(self.session.state, "listening")
        await asyncio.sleep(2.0)
        self.assertIsNone(self.session._current_turn)
        self.assertGreater(self.session._turn_id, old_turn)

    async def test_clear_history(self):
        await self.session.start()
        for _ in range(20):
            await self.session.handle_pcm(_voice_frame())
        await asyncio.sleep(3.0)
        self.assertGreaterEqual(len(self.session._storage.get_completed_turns()), 1)
        await self.session.clear_history()
        self.assertEqual(len(self.session._storage.get_completed_turns()), 0)

    async def test_history_limit_20(self):
        for i in range(25):
            self.session._storage.save_turn(i + 1, "completed", f"u{i}", f"a{i}", completed=True)
        completed = self.session._storage.get_completed_turns()
        self.assertEqual(len(completed), 20)

    async def test_speaking_ignores_pcm(self):
        await self.session.start()
        self.session._set_state("speaking")
        received = []
        original = self.session.handle_pcm
        # handle_pcm should return immediately and not start ASR
        await self.session.handle_pcm(_voice_frame())
        self.assertEqual(self.session.state, "speaking")
        self.assertFalse(self.session._asr_connected)


if __name__ == "__main__":
    unittest.main()
