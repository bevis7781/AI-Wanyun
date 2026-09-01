"""Fault containment tests for downstream LiveTalking audio transport errors."""

from __future__ import annotations

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
from backend.session import ConversationSession

from tests.test_core import _make_config


def _voice_frame() -> bytes:
    return b"".join(struct.pack("<h", int(0.5 * 0x7FFF)) for _ in range(320))


class FaultInjectingLiveTalkingAdapter(DummyLiveTalkingAdapter):
    """Raise an equivalent downstream closed-stream error once per target turn."""

    def __init__(self, fail_generations: set[int] | None = None) -> None:
        super().__init__()
        self.fail_generations = set(fail_generations or {1})
        self.failed_generations: list[int] = []
        self.stream_ids: list[int] = []

    async def start_audio_stream(self, generation_id: int) -> bool:
        ok = await super().start_audio_stream(generation_id)
        if ok:
            self.stream_ids.append(generation_id)
        return ok

    async def send_pcm(self, pcm: bytes) -> None:
        if self.generation in self.fail_generations:
            generation = self.generation
            self.fail_generations.remove(generation)
            self.failed_generations.append(generation)
            raise RuntimeError(
                "received 4008 (private use) stream timeout; "
                "then sent 4008 (private use) stream timeout"
            )
        await super().send_pcm(pcm)


class TestLiveTalkingFailureRecovery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        _make_config(self.tmp)
        self.lt = FaultInjectingLiveTalkingAdapter()
        self.asr_instances: list[DummyASRAdapter] = []
        self.replies = iter(("第一轮回复。", "恢复后的文字。", "恢复后的语音。"))

        def asr_factory(cb):
            adapter = DummyASRAdapter(final_text="语音输入", on_final=cb)
            self.asr_instances.append(adapter)
            return adapter

        def llm_factory(cb):
            return DummyLLMAdapter(reply=next(self.replies), on_token=cb)

        self.session = ConversationSession(
            asr_factory=asr_factory,
            llm_factory=llm_factory,
            tts_factory=lambda cb: DummyTTSAdapter(duration_seconds=0.06, on_pcm=cb),
            livetalking_factory=lambda: self.lt,
        )
        await self.session.set_session_id("fault-recovery")
        await self.session.start_tts_consumer()
        await self.session.start_pcm_sender()

    async def asyncTearDown(self) -> None:
        await self.session.shutdown()
        self.tmp_dir.cleanup()

    async def _wait_for(self, predicate, timeout: float = 6.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.02)
        self.fail(f"condition not met: state={self.session.state}")

    async def _feed_voice_turn(self) -> None:
        await self.session.start()
        for _ in range(15):
            await self.session.handle_pcm(_voice_frame())

    async def test_downstream_fault_returns_to_listening_and_next_text_is_admitted(self):
        await self._feed_voice_turn()
        await self._wait_for(
            lambda: bool(self.lt.failed_generations) and self.session.state == "listening"
        )
        recent = self.session._storage.get_recent_turns(1)[0]
        self.assertEqual(recent["status"], "failed")
        self.assertIn("发送音频失败", recent["error"])
        self.assertIsNone(self.session._current_turn)
        self.assertIsNone(self.session._current_audio_turn)
        self.assertIsNone(self.lt.generation)
        self.assertEqual(self.session._tts_queue.qsize(), 0)
        self.assertEqual(self.session._raw_pcm_queue.qsize(), 0)

        accepted, reason = await self.session.submit_text("after-fault", "你好")
        self.assertEqual((accepted, reason), (True, None))
        await self._wait_for(
            lambda: self.session.state == "listening"
            and bool(self.session._storage.get_completed_turns())
        )
        self.assertEqual(
            self.session._storage.get_completed_turns()[0]["assistant"], "恢复后的文字。"
        )
        self.assertGreaterEqual(len(self.lt.stream_ids), 2)
        self.assertNotEqual(self.lt.stream_ids[0], self.lt.stream_ids[1])

    async def test_next_voice_uses_new_stream_without_error_state(self):
        await self._feed_voice_turn()
        await self._wait_for(
            lambda: bool(self.lt.failed_generations) and self.session.state == "listening"
        )
        old_stream = self.lt.stream_ids[0]
        await self._feed_voice_turn()
        await self._wait_for(
            lambda: len(self.lt.stream_ids) >= 2 and self.session.state == "listening"
        )
        self.assertEqual(self.session.state, "listening")
        self.assertEqual(len(self.asr_instances), 2)
        self.assertNotEqual(old_stream, self.lt.stream_ids[-1])
        self.assertIsNone(self.lt.generation)

    async def test_twenty_turn_stress_keeps_transport_ownership_clean(self):
        self.lt.fail_generations = {1, 6, 11, 16}
        self.replies = iter((f"第{i}轮。" for i in range(1, 21)))
        for i in range(20):
            accepted, reason = await self.session.submit_text(f"stress-{i}", "继续")
            self.assertEqual((accepted, reason), (True, None))
            await self._wait_for(lambda: self.session.state == "paused")
            self.assertIsNone(self.session._current_audio_turn)
            self.assertIsNone(self.lt.generation)
            self.assertEqual(self.session._tts_queue.qsize(), 0)
            self.assertEqual(self.session._raw_pcm_queue.qsize(), 0)
            latest = self.session._storage.get_recent_turns(1)[0]
            self.assertIn(latest["status"], {"completed", "failed"})
            if i + 1 in {1, 6, 11, 16}:
                self.assertEqual(latest["status"], "failed")

        recent = self.session._storage.get_recent_turns(25)
        self.assertEqual(len(recent), 20)
        self.assertEqual(sum(row["status"] == "failed" for row in recent), 4)
        self.assertEqual(len(self.lt.failed_generations), 4)


if __name__ == "__main__":
    unittest.main()
