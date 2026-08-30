"""G3-v2 文字输入通道 + G8-A 文字 ACK 准入测试。

覆盖（G8 任务书 §28）：
- paused + LT bound → accepted，成功后恢复 paused
- listening + LT bound → accepted，成功后恢复 listening
- thinking / speaking / connecting → invalid_state（不排队不打断）
- LiveTalking 未绑定 → livetalking_not_ready（不建 turn / 不改状态 / 不落库）
- empty_text / missing request_id / invalid request / internal_error
- duplicate accepted request_id → 拒绝且不产生第二个 turn
- 拒绝路径不得污染 accepted-ID 缓存
- listening→thinking 竞态由 self._lock 裁决
- _handle_command 成功返回 ACK，request_id 原样对应
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.adapters.deepseek_llm import DummyLLMAdapter
from backend.adapters.huoshan_tts import DummyTTSAdapter
from backend.adapters.livetalking import DummyLiveTalkingAdapter
from backend.adapters.qwen_asr import DummyASRAdapter
from backend import main as main_module
from backend.session import ConversationSession

from test_core import _make_config


async def _build_session(tmp: Path, bind: bool = True) -> ConversationSession:
    """构造隔离测试会话；bind=True 时绑定 LiveTalking（设置 _bound_livetalking_session_id）。"""
    _make_config(tmp)
    session = ConversationSession(
        asr_factory=lambda cb: DummyASRAdapter(final_text="语音输入", on_final=cb),
        llm_factory=lambda cb: DummyLLMAdapter(reply="好的，我听到了。", on_token=cb),
        tts_factory=lambda cb: DummyTTSAdapter(duration_seconds=0.1, on_pcm=cb),
        livetalking_factory=DummyLiveTalkingAdapter,
    )
    if bind:
        await session.set_session_id("dummy-session")
    await session.start_tts_consumer()
    await session.start_pcm_sender()
    return session


class FakeWS:
    """最小 WS 替身：记录 send_json 发送的消息（用于 _handle_command 测试）。"""

    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)


class TestTextInput(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        self.captions = []
        self.session = await _build_session(self.tmp)
        self.session.on_caption = lambda c: self.captions.append(c)

    async def asyncTearDown(self):
        await self.session.shutdown()
        self.tmp_dir.cleanup()

    async def _wait_state(self, target, timeout=8.0):
        for _ in range(int(timeout / 0.05)):
            await asyncio.sleep(0.05)
            if self.session.state == target:
                return True
        return False

    async def test_text_round_from_paused_returns_to_paused(self):
        self.assertEqual(self.session.state, "paused")
        accepted, reason = await self.session.submit_text("rid-p1", "今晚不方便讲话。")
        self.assertEqual((accepted, reason), (True, None))
        self.assertEqual(self.session.state, "thinking")
        ok = await self._wait_state("speaking", timeout=10.0)
        self.assertTrue(ok, f"never reached speaking, state={self.session.state}")
        ok = await self._wait_state("paused", timeout=10.0)
        self.assertTrue(ok, f"never returned to paused, state={self.session.state}")
        # 用户文本进入同一 caption 流
        user_caps = [c for c in self.captions if c["kind"] == "user"]
        self.assertTrue(any(c["text"] == "今晚不方便讲话。" for c in user_caps))
        # 落库：与语音轮同一 storage
        completed = self.session._storage.get_completed_turns()
        self.assertEqual(completed[-1]["user"], "今晚不方便讲话。")
        self.assertEqual(completed[-1]["assistant"], "好的，我听到了。")

    async def test_text_round_from_listening_returns_to_listening(self):
        await self.session.start()
        self.assertEqual(self.session.state, "listening")
        accepted, reason = await self.session.submit_text("rid-l1", "打字代替说话")
        self.assertEqual((accepted, reason), (True, None))
        self.assertEqual(self.session.state, "thinking")
        ok = await self._wait_state("paused", timeout=10.0)
        # 应回 listening 而不是 paused
        ok = await self._wait_state("listening", timeout=10.0)
        self.assertTrue(ok, f"never returned to listening, state={self.session.state}")
        completed = self.session._storage.get_completed_turns()
        self.assertEqual(completed[-1]["user"], "打字代替说话")

    async def test_text_rejected_during_thinking_and_speaking(self):
        accepted, reason = await self.session.submit_text("rid-t1", "第一句")
        self.assertEqual((accepted, reason), (True, None))
        self.assertEqual(self.session.state, "thinking")
        accepted2, reason2 = await self.session.submit_text("rid-t2", "第二句不该被接受")
        self.assertEqual((accepted2, reason2), (False, "invalid_state"))
        # 仍处于同一轮（无第二 turn）
        self.assertEqual(self.session._current_turn.asr_final, "第一句")
        await self._wait_state("speaking", timeout=10.0)
        accepted3, reason3 = await self.session.submit_text("rid-t3", "第三句也不该被接受")
        self.assertEqual((accepted3, reason3), (False, "invalid_state"))
        ok = await self._wait_state("paused", timeout=10.0)
        self.assertTrue(ok)
        completed = self.session._storage.get_completed_turns()
        users = [t["user"] for t in completed]
        self.assertNotIn("第二句不该被接受", users)
        self.assertNotIn("第三句也不该被接受", users)

    async def test_blank_text_rejected_empty_text(self):
        accepted, reason = await self.session.submit_text("rid-blank", "   ")
        self.assertEqual((accepted, reason), (False, "empty_text"))
        accepted2, reason2 = await self.session.submit_text("rid-blank2", "\n\t ")
        self.assertEqual((accepted2, reason2), (False, "empty_text"))
        self.assertEqual(self.session.state, "paused")
        self.assertIsNone(self.session._current_turn)
        # empty_text 拒绝不得污染 accepted 缓存
        self.assertEqual(len(self.session._accepted_request_id_set), 0)

    async def test_text_round_from_listening_cancels_pending_asr_turn(self):
        import struct

        await self.session.start()
        samples = [int(0.5 * 0x7fff)] * 320
        voice = b"".join(struct.pack("<h", s) for s in samples)
        for _ in range(20):
            await self.session.handle_pcm(voice)
        await asyncio.sleep(0.3)
        pending = self.session._current_turn
        self.assertIsNotNone(pending, "voice turn should exist before text submit")
        accepted, reason = await self.session.submit_text("rid-l2", "文字取代这轮语音")
        self.assertEqual((accepted, reason), (True, None))
        # 旧语音 turn 已被终结，不再是当前 turn
        self.assertIsNot(self.session._current_turn, pending)
        self.assertTrue(pending.interrupted)
        await self._wait_state("listening", timeout=10.0)
        completed = self.session._storage.get_completed_turns()
        self.assertEqual(completed[-1]["user"], "文字取代这轮语音")

    # ---------------- G8-A ----------------

    async def test_text_rejected_when_livetalking_not_bound(self):
        session = await _build_session(self.tmp, bind=False)
        try:
            accepted, reason = await session.submit_text("rid-nb", "未绑定文字")
            self.assertEqual((accepted, reason), (False, "livetalking_not_ready"))
            # 不得建 turn / 不得改状态 / 不得落库 / 不启 ASR
            self.assertEqual(session.state, "paused")
            self.assertIsNone(session._current_turn)
            self.assertEqual(session._turn_id, 0)
            self.assertEqual(session._asr_started_count, 0)
            self.assertEqual(len(session._storage.get_recent_turns()), 0)
            self.assertEqual(len(session._accepted_request_id_set), 0)
        finally:
            await session.shutdown()

    async def test_text_rejected_in_connecting_state(self):
        async with self.session._lock:
            self.session._state = "connecting"
        accepted, reason = await self.session.submit_text("rid-conn", "连接中文字")
        self.assertEqual((accepted, reason), (False, "invalid_state"))
        self.assertIsNone(self.session._current_turn)
        self.assertEqual(len(self.session._accepted_request_id_set), 0)

    async def test_duplicate_request_id_rejected_no_second_turn(self):
        accepted, reason = await self.session.submit_text("dup-rid", "第一轮")
        self.assertEqual((accepted, reason), (True, None))
        self.assertEqual(self.session._turn_id, 1)
        # 同一已 accepted 的 request_id 再次到达（首轮仍在 thinking/speaking 中）：
        # 拒绝 duplicate，不得产生第二个 turn
        accepted2, reason2 = await self.session.submit_text("dup-rid", "第二轮")
        self.assertEqual((accepted2, reason2), (False, "duplicate"))
        self.assertEqual(self.session._turn_id, 1)
        ok = await self._wait_state("paused", timeout=10.0)
        self.assertTrue(ok)
        completed = self.session._storage.get_completed_turns()
        users = [t["user"] for t in completed]
        self.assertEqual(len(completed), 1)
        self.assertNotIn("第二轮", users)

    async def test_rejected_request_id_not_polluting_cache(self):
        await self.session.submit_text("rid-a", "第一句")
        # 新的 request_id 在 thinking 中被拒绝（invalid_state）→ 不得污染缓存
        accepted, reason = await self.session.submit_text("rid-b", "不应接受")
        self.assertEqual((accepted, reason), (False, "invalid_state"))
        await self._wait_state("paused", timeout=10.0)
        # 同一 rid-b 在可接受状态再次提交：应 accepted（拒绝未污染缓存）
        accepted2, reason2 = await self.session.submit_text("rid-b", "恢复后可接受")
        self.assertEqual((accepted2, reason2), (True, None))

    async def test_listening_to_thinking_race_rejected(self):
        """竞态：前端看到 listening 时发送，但请求到达时服务端已切 thinking。"""
        await self.session.start()
        self.assertEqual(self.session.state, "listening")
        async with self.session._lock:
            self.session._state = "thinking"  # 模拟服务端在请求到达前完成状态切换
        accepted, reason = await self.session.submit_text("rid-race", "竞态文字")
        self.assertEqual((accepted, reason), (False, "invalid_state"))
        self.assertIsNone(self.session._current_turn)
        self.assertEqual(len(self.session._accepted_request_id_set), 0)


class TestTextAckCommand(unittest.IsolatedAsyncioTestCase):
    """G8-A：_handle_command 层 ACK 协议（服务端准入裁决 + request_id 原样对应）。"""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        self.session = await _build_session(self.tmp)
        self.ws = FakeWS()

    async def asyncTearDown(self):
        await self.session.shutdown()
        self.tmp_dir.cleanup()

    async def test_text_command_accepted_ack_echoes_request_id(self):
        await main_module._handle_command(
            self.ws, self.session, {"type": "text", "request_id": "h-1", "text": "你好"}
        )
        self.assertEqual(len(self.ws.sent), 1)
        ack = self.ws.sent[0]
        self.assertEqual(ack["type"], "text_ack")
        self.assertEqual(ack["request_id"], "h-1")
        self.assertTrue(ack["accepted"])
        self.assertNotIn("reason", ack)
        # 服务端已正式接管：进入 thinking（后续 LLM/TTS 是异步后台链路）
        self.assertEqual(self.session.state, "thinking")

    async def test_text_command_missing_request_id_invalid_request(self):
        await main_module._handle_command(self.ws, self.session, {"type": "text", "text": "你好"})
        ack = self.ws.sent[0]
        self.assertEqual(ack["type"], "text_ack")
        self.assertFalse(ack["accepted"])
        self.assertEqual(ack["reason"], "invalid_request")
        self.assertIsNone(self.session._current_turn)

    async def test_text_command_blank_request_id_invalid_request(self):
        await main_module._handle_command(
            self.ws, self.session, {"type": "text", "request_id": "   ", "text": "你好"}
        )
        ack = self.ws.sent[0]
        self.assertFalse(ack["accepted"])
        self.assertEqual(ack["reason"], "invalid_request")

    async def test_text_command_non_string_text_invalid_request(self):
        await main_module._handle_command(
            self.ws, self.session, {"type": "text", "request_id": "h-2", "text": 123}
        )
        ack = self.ws.sent[0]
        self.assertFalse(ack["accepted"])
        self.assertEqual(ack["reason"], "invalid_request")

    async def test_text_command_empty_text_rejected(self):
        await main_module._handle_command(
            self.ws, self.session, {"type": "text", "request_id": "h-3", "text": "  "}
        )
        ack = self.ws.sent[0]
        self.assertFalse(ack["accepted"])
        self.assertEqual(ack["reason"], "empty_text")

    async def test_text_command_internal_error(self):
        with mock.patch.object(
            self.session, "submit_text", new=mock.AsyncMock(side_effect=RuntimeError("boom"))
        ):
            await main_module._handle_command(
                self.ws, self.session, {"type": "text", "request_id": "h-4", "text": "触发异常"}
            )
        ack = self.ws.sent[0]
        self.assertFalse(ack["accepted"])
        self.assertEqual(ack["reason"], "internal_error")
        # 不把内部 traceback / 异常细节发给前端
        self.assertNotIn("boom", str(ack))

    async def test_text_command_invalid_state_ack(self):
        await main_module._handle_command(
            self.ws, self.session, {"type": "text", "request_id": "h-5", "text": "第一句"}
        )
        self.assertEqual(self.session.state, "thinking")
        self.ws.sent.clear()
        await main_module._handle_command(
            self.ws, self.session, {"type": "text", "request_id": "h-6", "text": "第二句"}
        )
        ack = self.ws.sent[0]
        self.assertFalse(ack["accepted"])
        self.assertEqual(ack["reason"], "invalid_state")
        self.assertEqual(ack["request_id"], "h-6")


if __name__ == "__main__":
    unittest.main()
