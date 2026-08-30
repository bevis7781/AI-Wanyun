"""Focused regression tests for the split TTS/display response path."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.adapters.deepseek_llm import DummyLLMAdapter
from backend.adapters.huoshan_tts import DummyTTSAdapter
from backend.adapters.livetalking import DummyLiveTalkingAdapter
from backend.adapters.qwen_asr import DummyASRAdapter
from backend.session import (
    ConversationSession,
    _find_sentence_end,
    strip_performance_cues,
    strip_unapproved_tts_cues,
)

from test_core import _make_config


class RecordingTTSAdapter(DummyTTSAdapter):
    def __init__(self, sent_texts: list[str], **kwargs):
        super().__init__(duration_seconds=0.05, **kwargs)
        self.sent_texts = sent_texts

    async def send_text(self, text: str) -> None:
        self.sent_texts.append(text)
        await super().send_text(text)


class TestTTSDisplaySplit(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        _make_config(Path(self.tmp_dir.name))
        self.sent_texts: list[str] = []
        self.livetalking = DummyLiveTalkingAdapter()
        self.replies = iter((
            "（轻笑一声）你今日怎么这样会哄人。",
            "(叹了口气)算了。",
            "普通回复。",
        ))

        def llm_factory(cb):
            return DummyLLMAdapter(reply=next(self.replies), on_token=cb)

        self.session = ConversationSession(
            asr_factory=lambda cb: DummyASRAdapter(final_text="用户输入", on_final=cb),
            llm_factory=llm_factory,
            tts_factory=lambda cb: RecordingTTSAdapter(self.sent_texts, on_pcm=cb),
            livetalking_factory=lambda: self.livetalking,
        )
        await self.session.set_session_id("display-test")
        await self.session.start_tts_consumer()
        await self.session.start_pcm_sender()
        self.captions: list[dict[str, str]] = []
        self.session.on_caption = self.captions.append

    async def asyncTearDown(self):
        await self.session.shutdown()
        self.tmp_dir.cleanup()

    async def _wait_state(self, state: str, timeout: float = 8.0) -> None:
        for _ in range(int(timeout / 0.05)):
            if self.session.state == state:
                return
            await asyncio.sleep(0.05)
        self.fail(f"state did not reach {state}: {self.session.state}")

    async def test_cues_are_only_removed_from_display_and_history(self):
        for index in range(3):
            accepted, reason = await self.session.submit_text(f"display-{index}", "测试")
            self.assertEqual((accepted, reason), (True, None))
            await self._wait_state("paused")

        ai_captions = [c["text"] for c in self.captions if c["kind"] == "ai"]
        self.assertEqual(
            ai_captions,
            ["你今日怎么这样会哄人。", "算了。", "普通回复。"],
        )
        self.assertEqual(
            self.sent_texts,
            ["你今日怎么这样会哄人。", "算了。", "普通回复。"],
        )
        history = self.session._storage.get_completed_turns()
        self.assertEqual([row["assistant"] for row in history], [
            "你今日怎么这样会哄人。", "算了。", "普通回复。",
        ])

    def test_cleaner_and_boundary_handle_complete_pairs(self):
        self.assertEqual(strip_performance_cues("你（轻笑）今日怎么了？"), "你今日怎么了？")
        self.assertEqual(strip_performance_cues("Hello (smile) world."), "Hello world.")
        self.assertEqual(strip_performance_cues("普通回复。"), "普通回复。")
        self.assertEqual(strip_performance_cues("未闭合（提示"), "未闭合（提示")
        self.assertEqual(_find_sentence_end("（轻笑！）你今日怎么了？"), 12)

    def test_tts_policy_removes_all_free_form_cues(self):
        cues = (
            "（轻笑一声）", "(叹了口气)", "（轻轻哼了一声）", "（声音放轻）",
            "（带着一点哭腔）", "（低下头笑了一下）", "（红着脸移开目光）",
            "（把碗筷放到桌上）", "（走到窗边）", "（伸手替你整理衣襟）",
        )
        for cue in cues:
            self.assertEqual(strip_unapproved_tts_cues(f"你{cue}还好吗？"), "你还好吗？")
        self.assertEqual(strip_unapproved_tts_cues("你（未知提示）还好吗？"), "你还好吗？")
        self.assertEqual(strip_unapproved_tts_cues("你(smile)还好吗？"), "你还好吗？")
        self.assertEqual(strip_unapproved_tts_cues("未闭合（提示"), "未闭合（提示")
        self.assertEqual(strip_unapproved_tts_cues("普通回复。"), "普通回复。")

    async def test_filtered_only_reply_completes_without_audio(self):
        self.session.llm_factory = lambda cb: DummyLLMAdapter(
            reply="（轻笑一声）", on_token=cb
        )
        accepted, reason = await self.session.submit_text("filtered-only", "测试")
        self.assertEqual((accepted, reason), (True, None))
        for _ in range(40):
            if self.session._storage.get_completed_turns():
                break
            await asyncio.sleep(0.05)
        self.assertEqual(self.session.state, "paused")
        self.assertEqual(self.sent_texts, [])
        self.assertEqual([c["type"] for c in self.livetalking.calls], ["start", "abort"])
        history = self.session._storage.get_completed_turns()
        self.assertEqual(history[0]["assistant"], "")

    async def test_session_filters_visual_and_unknown_cues_for_tts_only(self):
        self.session.llm_factory = lambda cb: DummyLLMAdapter(
            reply="（走到窗边）你今日怎么了？你好 (smile)呀。", on_token=cb
        )
        accepted, reason = await self.session.submit_text("integration", "测试")
        self.assertEqual((accepted, reason), (True, None))
        await self._wait_state("paused")
        self.assertEqual(self.sent_texts, ["你今日怎么了？", "你好呀。"])
        ai_captions = [c["text"] for c in self.captions if c["kind"] == "ai"]
        self.assertEqual(ai_captions[-1], "你今日怎么了？你好呀。")
        history = self.session._storage.get_completed_turns()
        self.assertEqual(history[-1]["assistant"], "你今日怎么了？你好呀。")


if __name__ == "__main__":
    unittest.main()
