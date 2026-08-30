from __future__ import annotations

import asyncio
import math
import re
import time
from collections import deque
from typing import Any, Callable

from backend.adapters.deepseek_llm import DeepSeekLLMAdapter
from backend.adapters.huoshan_tts import HuoshanTTSAdapter
from backend.adapters.livetalking import LiveTalkingAdapter
from backend.adapters.qwen_asr import QwenASRAdapter
from backend.config import get_config
from backend.logger import get_logger, mask_id
from backend.memory import build_memory_prompt, extract_memories
from backend.storage import Storage

logger = get_logger()
TAG = "session"

STATES = {"paused", "connecting", "listening", "thinking", "speaking", "error"}
SENTENCE_END_RE = re.compile(r"[。！？；.!?;]")
PERFORMANCE_CUE_RE = re.compile(r"\(([^()]*)\)|（([^（）]*)）")
PAREN_CLOSE = {"(": ")", "（": "）"}
# 客户端 correlation id 安全约束：仅允许字母数字/下划线/连字符，1–32 字符
RESUME_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# 回合安全上限的最小值：低于此值一律校正（禁止隐性缩短长句保护）
MIN_MAX_TURN_MS = 120_000
TEXT_REQUEST_ID_CACHE_SIZE = 256


def _find_sentence_end(text: str) -> int | None:
    """Return the first sentence boundary outside paired stage-cue brackets.

    LLM tokens can split both the cue and its closing bracket.  Looking for
    punctuation with a bare regex would therefore send partial cues to TTS.
    An unmatched opener intentionally keeps the text buffered until the
    stream's final safe boundary.
    """
    expected_closes: list[str] = []
    for index, char in enumerate(text):
        close = PAREN_CLOSE.get(char)
        if close is not None:
            expected_closes.append(close)
            continue
        if expected_closes and char == expected_closes[-1]:
            expected_closes.pop()
            continue
        if not expected_closes and SENTENCE_END_RE.match(char):
            return index + 1
    return None


def _strip_complete_performance_cues(text: str) -> str:
    """Remove every complete free-form Chinese/English parenthesized cue."""
    if not text:
        return text
    cleaned = text
    removed = False
    while True:
        changed = False

        def replace(match: re.Match[str]) -> str:
            nonlocal changed
            changed = True
            return ""

        next_cleaned = PERFORMANCE_CUE_RE.sub(replace, cleaned)
        if not changed:
            break
        removed = True
        cleaned = next_cleaned
    if not removed:
        return text
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s+([，。！？；：、,.!?;:])", r"\1", cleaned)
    cleaned = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", cleaned)
    return cleaned.strip()


def strip_performance_cues(text: str) -> str:
    """Remove complete Chinese/English parenthesized performance cues.

    The caller invokes this only for a completed sentence or final stream
    boundary.  Unmatched brackets remain untouched.  Whitespace and
    punctuation normalization is limited to text from which a cue was
    actually removed, so ordinary replies retain their original bytes.
    """
    return _strip_complete_performance_cues(text)


def strip_unapproved_tts_cues(text: str) -> str:
    """Remove every complete free-form cue before text reaches TTS.

    Unmatched brackets remain for final safe-boundary handling; no cue content
    is allowlisted or inferred as speech.
    """
    return _strip_complete_performance_cues(text)


class TurnGuard:
    def __init__(self, turn_id: int) -> None:
        self.turn_id = turn_id
        self.storage_id: int | None = None
        self.created_mono: float = time.monotonic()
        # 延迟分段计时（单调时钟）：打点见 ConversationSession._mark
        self.metrics: dict[str, float] = {}
        self.generation_id: int | None = None
        self.asr_final: str | None = None
        self.llm_text = ""
        # Keep the raw LLM response for turn identity/debugging; TTS receives a
        # policy-filtered derivation, while captions/history use display_text.
        self.display_text = ""
        self._display_source = ""
        self.tts_started = False
        self.tts_text_queued = False
        self.tts_finished = False
        self.tts: Any = None
        self.audio_started = False
        self.audio_ended = False
        self.interrupted = False
        self.error: str | None = None
        self._tasks: list[asyncio.Task] = []
        # 长句多分句聚合状态
        self.pending_sentences: list[tuple[Any, Any, str]] = []  # (begin_time, end_time, text)
        self.seen_sentence_keys: set[str] = set()
        self.asr_submit_reason: str | None = None
        self.final_call_count = 0
        # 延迟专项阶段2：静音达标后已发送 finish-task（等待 task-finished 回调提交）
        self.finish_started = False
        # TTS 会话预热任务（人声开始即与 ASR/LLM 全程并行握手，首句到达即免握手）
        self.tts_prewarm_task: asyncio.Task | None = None
        self.tts_attempt_id = 0
        self.tts_first_pcm_event = asyncio.Event()
        self.tts_first_pcm_received = False
        # 延迟专项阶段3：LiveTalking 流并行准备任务与已准备标记（无音频时回收）
        self.lt_prep_task: asyncio.Task | None = None
        self.audio_prepared = False
        # 安全上限已告警标记（无最终分句时禁止每帧重复告警）
        self.max_turn_warned = False
        # 文字输入轮：回复结束后的回落状态（paused 一次性文字轮 / listening 连续语音轮）
        self.return_state: str | None = None

    def track(self, task: asyncio.Task) -> None:
        self._tasks.append(task)

    async def cancel_all(self) -> None:
        self.interrupted = True
        for task in self._tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass


class ConversationSession:
    def __init__(
        self,
        asr_factory: Callable[[Callable[[str], None]], QwenASRAdapter] | None = None,
        llm_factory: Callable[[Callable[[str], None]], DeepSeekLLMAdapter] | None = None,
        tts_factory: Callable[[Callable[[bytes], None]], HuoshanTTSAdapter] | None = None,
        livetalking_factory: Callable[[], LiveTalkingAdapter] | None = None,
    ) -> None:
        cfg = get_config()
        self.audio_cfg = cfg.audio
        self.sample_rate = int(self.audio_cfg.get("sample_rate", 16000))
        self.channels = int(self.audio_cfg.get("channels", 1))
        self.frame_duration_ms = int(self.audio_cfg.get("frame_duration_ms", 20))
        self.frame_bytes = int(self.sample_rate * self.frame_duration_ms / 1000) * self.channels * 2
        self.rms_threshold = float(self.audio_cfg.get("rms_threshold", 0.015))
        self.rms_consecutive_frames = int(self.audio_cfg.get("rms_consecutive_frames", 3))
        self.prebuffer_ms = int(self.audio_cfg.get("prebuffer_ms", 500))
        self.max_prebuffer_frames = self.prebuffer_ms // self.frame_duration_ms
        # 应用层回合结束判定：静音达到阈值 + 已有最终分句 -> 提交聚合文本
        self.turn_end_silence_ms = int(self.audio_cfg.get("turn_end_silence_ms", 1800))
        self.turn_end_silence_frames = max(1, self.turn_end_silence_ms // self.frame_duration_ms)
        # 回合安全上限：强制不低于 120s（低于最小值时校正并告警，不得静默缩短）
        raw_max_turn_ms = int(self.audio_cfg.get("max_turn_ms", MIN_MAX_TURN_MS))
        if raw_max_turn_ms < MIN_MAX_TURN_MS:
            logger.warning(
                f"[{TAG}] max_turn_ms={raw_max_turn_ms} below minimum {MIN_MAX_TURN_MS}, "
                f"corrected to {MIN_MAX_TURN_MS}"
            )
            raw_max_turn_ms = MIN_MAX_TURN_MS
        self.max_turn_ms = raw_max_turn_ms
        self.max_turn_frames = max(1, self.max_turn_ms // self.frame_duration_ms)

        self._turn_id = 0
        self._generation_id = 0
        self._state = "paused"
        self._lock = asyncio.Lock()
        self._current_turn: TurnGuard | None = None

        self._prebuffer: deque[bytes] = deque(maxlen=self.max_prebuffer_frames)
        self._rms_hits = 0
        self._asr_connected = False
        self._asr_started_count = 0
        # 回合内帧计数：静音帧计数（回合结束判定）+ 回合总帧数（安全上限）
        self._frames_since_voice = 0
        self._turn_frames = 0

        queue_limit = int(cfg.tts.get("pcm_queue_limit", 200))
        self.tts_first_pcm_timeout = max(0.5, float(cfg.tts.get("first_pcm_timeout", 3.0)))
        self._tts_queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue(maxsize=queue_limit)
        self._tts_consumer_task: asyncio.Task | None = None

        self._raw_pcm_queue: asyncio.Queue[tuple[int, bytes | None]] = asyncio.Queue(maxsize=queue_limit)
        self._pcm_buffer = bytearray()
        self._pcm_lock = asyncio.Lock()
        self._pcm_sender_task: asyncio.Task | None = None
        self._current_audio_turn: TurnGuard | None = None
        self._current_generation: int | None = None

        self._storage = Storage()
        self._persona = self._load_persona()

        self.on_state_change: Callable[[str], None] | None = None
        self.on_caption: Callable[[dict[str, Any]], None] | None = None
        self.on_error: Callable[[str], None] | None = None

        self.asr_factory = asr_factory or self._default_asr_factory
        self.llm_factory = llm_factory or (lambda cb: DeepSeekLLMAdapter(on_token=cb))
        self.tts_factory = tts_factory or (lambda cb: HuoshanTTSAdapter(on_pcm=cb))
        self.livetalking_factory = livetalking_factory or LiveTalkingAdapter

        self.asr: QwenASRAdapter | None = None
        self.llm: DeepSeekLLMAdapter | None = None
        self.tts: HuoshanTTSAdapter | None = None
        self.livetalking: LiveTalkingAdapter | None = None
        self._bound_livetalking_session_id: str | None = None

        # G8-A：已接受文字 request_id 去重缓存（只记录完成原子准入的 id，
        # 最近 TEXT_REQUEST_ID_CACHE_SIZE 个；拒绝路径不得污染）
        self._accepted_request_ids: deque[str] = deque()
        self._accepted_request_id_set: set[str] = set()

        self._shutdown = False

        # Voice Resume 链路 instrumentation（纯 metadata 计数，不参与任何控制逻辑）
        self._resume_seq = 0
        self._resume_id: str | None = None
        self._resume_start_mono: float | None = None
        self._resume_frames = 0
        self._resume_first_pcm_seen = False
        self._resume_first_pcm_mono: float | None = None
        self._resume_last_rms = 0.0
        self._resume_vad_hits = 0
        self._resume_asr_attempts = 0
        self._resume_asr_started = 0
        self._resume_asr_failed = 0

        # Long-term memory is deterministic and best-effort; it never blocks or
        # invalidates an already completed conversation turn.
        self._memory_available = True
        self._memory_write_count = 0
        self._memory_reject_count = 0
        self._memory_last_status = "none"

    def _default_asr_factory(self, on_final: Callable[[str], None]) -> QwenASRAdapter:
        # Keep the adapter identity in the callback.  Qwen invokes on_error
        # synchronously at the end of startup (after retries/cooldown), while
        # the session is still inside handle_pcm's non-reentrant lock.  The
        # callback therefore must not blindly enqueue a second session-wide
        # error after a failed adapter has already been replaced.
        adapter_ref: list[QwenASRAdapter] = []

        def _on_error(message: str) -> None:
            adapter = adapter_ref[0] if adapter_ref else None
            asyncio.create_task(self._enter_error_state_for_asr(adapter, message))

        adapter = QwenASRAdapter(
            on_final=on_final,
            on_close=lambda: asyncio.create_task(self._on_asr_close()),
            on_error=_on_error,
        )
        adapter_ref.append(adapter)
        return adapter

    def _load_persona(self) -> str:
        path = get_config()._raw.get("persona_path", "data/persona.example.md")
        from pathlib import Path
        p = Path(path)
        if not p.is_absolute():
            p = get_config().config_path.parent / p
        try:
            return p.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning(f"[{TAG}] failed to load persona: {exc}")
            return "You are a helpful assistant. Answer in Chinese, briefly."

    @property
    def state(self) -> str:
        return self._state

    def _set_state(self, state: str) -> None:
        if state not in STATES:
            raise ValueError(f"invalid state {state}")
        if self._state == state:
            return
        self._state = state
        logger.info(f"[{TAG}] state={state}")
        if state == "speaking":
            self._mark(self._current_turn, "speaking_started")
        if self.on_state_change:
            try:
                self.on_state_change(state)
            except Exception as exc:
                logger.error(f"[{TAG}] state callback error={exc}")

    def _mark(
        self, turn: TurnGuard | None, key: str, overwrite: bool = False, log: bool = True
    ) -> None:
        """延迟分段打点（单调时钟）：只记录时间戳与脱敏 turn id，不含任何内容。"""
        if turn is None:
            return
        m = turn.metrics
        if key in m and not overwrite:
            return
        m[key] = time.monotonic()
        if log:
            logger.info(
                f"[metrics] turn={turn.turn_id} key={key}"
                f" at_ms={int((m[key] - turn.created_mono) * 1000)}"
            )

    def _log_metrics_summary(self, turn: TurnGuard) -> None:
        """回合收尾时输出分段耗时汇总（仅耗时与 turn id，无正文）。"""
        m = turn.metrics
        # 阶段3 后 TTS 预热早于提交发生，改以回合起点（人声触发 ASR）计量握手全程
        m.setdefault("turn_created", turn.created_mono)

        def d(a: str, b: str) -> str | None:
            if a in m and b in m:
                return str(int((m[b] - m[a]) * 1000))
            return None

        pairs = [
            ("voice_to_asr_final", "last_local_voice", "asr_task_finished_received"),
            ("asr_final_to_submit", "asr_task_finished_received", "turn_submitted"),
            ("submit_to_llm_first_token", "turn_submitted", "llm_first_token"),
            ("llm_first_token_to_first_sentence", "llm_first_token", "llm_first_sentence"),
            ("voice_start_to_tts_ready", "turn_created", "tts_session_ready"),
            ("first_sentence_to_tts_first_pcm", "llm_first_sentence", "tts_first_pcm"),
            ("tts_first_pcm_to_speaking", "tts_first_pcm", "speaking_started"),
            ("submit_to_lt_stream_ready", "turn_submitted", "livetalking_stream_ready"),
            ("TOTAL_voice_to_speaking", "last_local_voice", "speaking_started"),
        ]
        parts = [f"{name}={d(a, b)}" for name, a, b in pairs if d(a, b) is not None]
        missing = [name for name, a, b in pairs if d(a, b) is None and name == "TOTAL_voice_to_speaking"]
        if missing:
            parts.append(f"missing={','.join(missing)}")
        logger.info(f"[metrics-summary] turn={turn.turn_id} " + " ".join(parts))

    def _next_turn_id(self) -> int:
        self._turn_id += 1
        return self._turn_id

    def _next_generation_id(self) -> int:
        self._generation_id += 1
        return self._generation_id

    @staticmethod
    def _sanitize_resume_id(resume_id: Any) -> str | None:
        """把客户端提供的 correlation 值约束为服务端安全内部表示。

        仅接受 1–32 字符的字母数字/下划线/连字符；其余（非字符串、空白、
        超长、控制字符、注入型内容）一律返回 None，拒绝作为 correlation。
        用户可控原文绝不直接进入日志 / diagnostics。
        """
        if not isinstance(resume_id, str):
            return None
        value = resume_id.strip()
        if not RESUME_ID_RE.fullmatch(value):
            return None
        return value

    def _begin_resume(self, resume_id: str | None) -> None:
        """开启一次 voice resume 追踪：关联 id 采用安全内部表示。

        仅重置 metadata 计数，不改任何状态机/门控/行为；instrumentation 非控制逻辑。
        """
        rid = self._sanitize_resume_id(resume_id) if resume_id is not None else None
        if rid is None:
            self._resume_seq += 1
            rid = f"R{self._resume_seq}_{int(time.monotonic() * 1000)}"
        self._resume_id = rid
        self._resume_start_mono = time.monotonic()
        self._resume_frames = 0
        self._resume_first_pcm_seen = False
        self._resume_first_pcm_mono = None
        self._resume_last_rms = 0.0
        self._resume_vad_hits = 0
        self._resume_asr_attempts = 0
        self._resume_asr_started = 0
        self._resume_asr_failed = 0
        self._trace("voice/start", {})

    def _end_resume(self, reason: str) -> None:
        """结束当前 resume 追踪并输出帧计数汇总（纯观测）。"""
        if self._resume_id is None:
            return
        self._trace(
            "resume/end",
            {
                "reason": reason,
                "frames": self._resume_frames,
                "vad_hits": self._resume_vad_hits,
                "asr_started": self._resume_asr_started,
            },
        )
        self._resume_id = None
        self._resume_start_mono = None

    def _trace(self, event: str, fields: dict[str, Any] | None = None) -> None:
        """Voice Resume 链路 metadata 打点（纯观测，无任何控制副作用）。

        只允许白名单字段（帧序号 / RMS 能量标量 / 计数 / 原因），
        禁止 PCM 内容、语音文本、session id、secret 等任何敏感数据。
        """
        if self._resume_id is None or self._resume_start_mono is None:
            return
        t_ms = int((time.monotonic() - self._resume_start_mono) * 1000)
        allowed = {
            "frame_idx", "rms", "threshold", "above", "consecutive",
            "required", "counter_reset", "previous_consecutive", "vad_hits",
            "asr_attempts", "asr_started", "asr_failed", "frames", "reason",
        }
        safe = {k: v for k, v in (fields or {}).items() if k in allowed}
        line = f"[voice-trace] resume={self._resume_id} event={event} t_ms={t_ms}"
        if safe:
            line += " " + " ".join(f"{k}={v}" for k, v in sorted(safe.items()))
        logger.info(line)

    async def set_session_id(self, session_id: str) -> None:
        async with self._lock:
            if self.livetalking is None:
                self.livetalking = self.livetalking_factory()
            old_session_id = self._bound_livetalking_session_id
        if old_session_id and old_session_id != session_id:
            logger.info(
                f"[{TAG}] replacing livetalking session old={mask_id(old_session_id)}"
                f" new={mask_id(session_id)}"
            )
            await self.stop_current_streams()
        async with self._lock:
            if self.livetalking is None:
                self.livetalking = self.livetalking_factory()
            self.livetalking.session_id = session_id
            self._bound_livetalking_session_id = session_id
            logger.info(f"[{TAG}] livetalking session_id={mask_id(session_id)}")

    async def start(self, resume_id: str | None = None) -> None:
        async with self._lock:
            if self._state in ("listening", "thinking", "speaking"):
                return
            if self._state == "error":
                await self._reset_error_locked()
            if self.livetalking is None:
                self.livetalking = self.livetalking_factory()
            self._set_state("listening")
            self._asr_connected = False
            self._rms_hits = 0
            self._prebuffer.clear()
        self._begin_resume(resume_id)
        self._emit_caption("status", "正在倾听…")

    def _reset_gating(self) -> None:
        """复位 RMS 门控与回合判定计数（须持 self._lock 调用）。"""
        self._asr_connected = False
        self._rms_hits = 0
        self._prebuffer.clear()
        self._frames_since_voice = 0
        self._turn_frames = 0

    async def _cancel_current_turn_locked(self, status: str, error: str) -> TurnGuard | None:
        """统一取消当前 turn（须持 self._lock 调用）。

        pause / 浏览器断开 / 会话停止共用：标记 interrupted、清空 _current_turn、
        落库终结状态（不留 active 孤儿）、复位门控计数、清空 TTS/PCM 队列。
        音频流已收尾（audio_ended）的 turn 已有终态（completed/failed），不得回写覆盖。
        """
        turn = self._current_turn
        if turn is not None:
            turn.interrupted = True
            self._current_turn = None
            if not turn.audio_ended:
                self._storage.update_turn_status(turn.storage_id, status, error)
        self._reset_gating()
        self._clear_queue(self._tts_queue)
        self._clear_queue(self._raw_pcm_queue)
        return turn

    async def _abort_live_audio(self) -> None:
        """中止当前数字人音频流（不关闭远端 WebRTC 会话）。"""
        audio_turn = self._current_audio_turn
        if (
            audio_turn is not None
            and self.livetalking
            and audio_turn.generation_id is not None
            and not audio_turn.audio_ended
        ):
            try:
                await self.livetalking.abort_audio_stream(audio_turn.generation_id)
            except Exception:
                pass
            audio_turn.audio_ended = True
            self._current_audio_turn = None
            self._current_generation = None

    async def pause(self) -> None:
        """暂停：完整取消当前 turn（含待聚合分句与 TTS/PCM 队列）。

        不关闭远端 LiveTalking WebRTC 会话（页面视频仍在使用）；
        浏览器断开与应用关闭才执行完整远端会话关闭（stop_current_streams）。
        """
        turn: TurnGuard | None = None
        async with self._lock:
            if self._state == "paused":
                return
            turn = await self._cancel_current_turn_locked("interrupted", "用户暂停")
            self._set_state("paused")
        if turn is not None:
            await turn.cancel_all()
        await self._stop_asr()
        if self.tts:
            await self.tts.cancel()
        await self._abort_live_audio()
        if turn is not None:
            await self._recycle_prepared_lt(turn)
        async with self._pcm_lock:
            self._pcm_buffer.clear()
        self._emit_caption("status", "已暂停")
        self._end_resume("pause")
        logger.info(f"[{TAG}] paused, turn cancelled={turn.turn_id if turn else 'none'}")

    async def submit_text(self, request_id: str, text: str) -> tuple[bool, str | None]:
        """G8 文字提交：原子准入 + ACK 语义。

        返回 (accepted, reason)：
        - (True, None)：服务端已正式接管该 request_id 的文本，保证不会因文字
          准入阶段的状态竞态而静默消失（不代表 LLM/TTS/LiveTalking 已完成）；
        - (False, reason)：拒绝，reason 为 invalid_request / empty_text /
          invalid_state / livetalking_not_ready / duplicate / internal_error。

        准入（状态检查 / LiveTalking 绑定 / request_id 去重 / 取得本轮处理权）
        全部发生在 self._lock 保护范围内，listening→thinking 竞态由锁裁决；
        不能只相信前端发送瞬间看到的 state。

        - paused / listening / error 允许；connecting / thinking / speaking 拒绝；
        - LiveTalking 未绑定：reject + 前端保留草稿（不建 turn / 不改状态 / 不落库）；
        - 同一已 accepted 的 request_id 再次到达：拒绝 duplicate，不产生第二个 turn。
        """
        text = (text or "").strip()
        if not text:
            return False, "empty_text"
        payload: tuple[TurnGuard, str, str] | None = None
        old_turn: TurnGuard | None = None
        async with self._lock:
            if not self._bound_livetalking_session_id:
                return False, "livetalking_not_ready"
            if request_id in self._accepted_request_id_set:
                return False, "duplicate"
            if self._state in ("connecting", "thinking", "speaking"):
                return False, "invalid_state"
            if self._state == "error":
                await self._reset_error_locked()
            if self._state == "listening":
                # 取代本轮 ASR 输入：取消当前 turn 与待聚合分句（复用 pause 的取消语义）
                old_turn = await self._cancel_current_turn_locked(
                    "interrupted", "文字输入取代语音"
                )
                return_state = "listening"
            else:
                return_state = "paused"
            self._next_turn_id()
            turn = TurnGuard(self._turn_id)
            turn.return_state = return_state
            self._current_turn = turn
            self._asr_started_count += 1
            turn.generation_id = self._next_generation_id()
            turn.storage_id = self._storage.save_turn(turn.turn_id, status="active")
            payload = self._prepare_submit_locked(turn, "typed", direct_text=text)
            if payload is None:
                # 防御路径（本轮非空文本不应到达）：终结已建 turn，避免 active 孤儿
                self._storage.update_turn_status(
                    turn.storage_id, "interrupted", "文字提交内部异常"
                )
                self._current_turn = None
                self._reset_gating()
                return False, "internal_error"
            # 只有真正完成原子准入的 request_id 才进入 accepted 缓存
            self._mark_request_accepted(request_id)
        if old_turn is not None:
            await old_turn.cancel_all()
        await self._stop_asr()
        if payload is not None:
            await self._finalize_turn(*payload)
        return True, None

    def _mark_request_accepted(self, request_id: str) -> None:
        """记录 accepted request_id（bounded：最近 TEXT_REQUEST_ID_CACHE_SIZE 个）。

        只在 submit_text 原子准入成功路径调用（须持 self._lock）；
        拒绝路径不得污染 accepted-ID 缓存。
        """
        if request_id in self._accepted_request_id_set:
            return
        if len(self._accepted_request_id_set) >= TEXT_REQUEST_ID_CACHE_SIZE:
            old = self._accepted_request_ids.popleft()
            self._accepted_request_id_set.discard(old)
        self._accepted_request_ids.append(request_id)
        self._accepted_request_id_set.add(request_id)

    async def interrupt(self) -> None:
        async with self._lock:
            turn = self._current_turn
            if turn is None:
                return
            self._next_turn_id()
            self._current_turn = None
            turn.interrupted = True
            self._reset_gating()
            self._clear_queue(self._tts_queue)
            self._clear_queue(self._raw_pcm_queue)
            # 清理完成后再切回 listening，避免麦克风在清理期间上传
            keep_state = self._state

        if turn:
            await turn.cancel_all()
        await self._stop_asr()
        tts_to_cancel = turn.tts if turn is not None and turn.tts is not None else self.tts
        if tts_to_cancel:
            await tts_to_cancel.cancel()
        if self.livetalking and turn.generation_id is not None and not turn.audio_ended:
            if self._current_audio_turn is turn:
                await self.livetalking.abort_audio_stream(turn.generation_id)
                turn.audio_ended = True
            else:
                await self._recycle_prepared_lt(turn)
        async with self._pcm_lock:
            self._pcm_buffer.clear()
        self._storage.update_turn_status(turn.storage_id, "interrupted", "用户打断")
        async with self._lock:
            if self._state == keep_state:
                self._set_state("listening")
        self._emit_caption("status", "已打断，继续倾听…")
        logger.info(f"[{TAG}] interrupted turn={turn.turn_id} generation={turn.generation_id}")

    async def reset_error(self) -> None:
        async with self._lock:
            await self._reset_error_locked()

    async def _reset_error_locked(self) -> None:
        self._set_state("paused")
        self._current_turn = None
        self._reset_gating()

    async def clear_history(self) -> None:
        count = self._storage.clear_history()
        logger.info(f"[{TAG}] cleared {count} history rows")
        self._emit_caption("status", "对话已清空")

    def _persist_memories_for_turn(self, turn: TurnGuard) -> None:
        """Persist only explicit facts from a successfully completed turn."""
        if turn.storage_id is None or not turn.asr_final:
            return
        try:
            candidates = extract_memories(turn.asr_final)
            self._memory_last_status = "none" if not candidates else "processed"
            for candidate in candidates:
                if candidate.operation == "delete":
                    result = self._storage.delete_memory(
                        candidate.scope, candidate.memory_key, turn.storage_id
                    )
                else:
                    result = self._storage.upsert_memory(
                        candidate.scope,
                        candidate.memory_key,
                        candidate.subject,
                        candidate.value,
                        turn.storage_id,
                    )
                if result.get("action") != "noop":
                    self._memory_write_count += 1
        except Exception as exc:
            # Memory is optional: a schema/parser failure must not change a
            # completed turn into failed or expose user text in diagnostics.
            self._memory_available = False
            self._memory_reject_count += 1
            self._memory_last_status = "error"
            logger.warning(f"[{TAG}] memory write skipped error={type(exc).__name__}")

    async def handle_pcm(self, pcm: bytes) -> None:
        if self._shutdown:
            return
        # Voice Resume instrumentation：真实首包 receive 观测。
        # 必须在任何 listening/state 门控、RMS 计算、VAD、ASR 逻辑之前；
        # 即使该帧随后因状态门控被丢弃，也必须能看到 receive 已发生。
        if self._resume_id is not None:
            self._resume_frames += 1
            if not self._resume_first_pcm_seen:
                self._resume_first_pcm_seen = True
                self._resume_first_pcm_mono = time.monotonic()
                self._trace("pcm/first", {"frame_idx": self._resume_frames})
        submit_payload: tuple[TurnGuard, str, str] | None = None
        async with self._lock:
            if self._state != "listening":
                return

            rms = self._compute_rms(pcm)
            if self._resume_id is not None:
                self._resume_last_rms = rms
            previous_consecutive = self._rms_hits
            if rms >= self.rms_threshold:
                self._rms_hits += 1
                self._frames_since_voice = 0
                self._mark(self._current_turn, "last_local_voice", overwrite=True, log=False)
            else:
                self._rms_hits = 0
                self._frames_since_voice += 1
            if self._resume_id is not None:
                self._trace(
                    "vad/frame",
                    {
                        "frame_idx": self._resume_frames,
                        "rms": rms,
                        "threshold": self.rms_threshold,
                        "above": rms >= self.rms_threshold,
                        "consecutive": self._rms_hits,
                        "required": self.rms_consecutive_frames,
                        "counter_reset": rms < self.rms_threshold and previous_consecutive > 0,
                        "previous_consecutive": previous_consecutive,
                    },
                )

            if not self._asr_connected:
                self._prebuffer.append(pcm)
                if self._rms_hits >= self.rms_consecutive_frames:
                    self._asr_connected = True
                    self._resume_vad_hits += 1
                    self._trace(
                        "vad/hit",
                        {
                            "frame_idx": self._resume_frames,
                            "rms": round(rms, 4),
                            "vad_hits": self._resume_vad_hits,
                        },
                    )
                    self._frames_since_voice = 0
                    self._turn_frames = 0
                    await self._start_asr_locked()
                    for cached in self._prebuffer:
                        if self.asr:
                            await self.asr.send_pcm(cached)
                    self._prebuffer.clear()
                return

            # 回合内帧计数（安全上限）
            self._turn_frames += 1
            turn = self._current_turn
            if turn and turn.asr_final is None:
                if turn.pending_sentences:
                    if not turn.finish_started:
                        reason = None
                        if self._frames_since_voice >= self.turn_end_silence_frames:
                            reason = "silence"
                        elif self._turn_frames >= self.max_turn_frames:
                            reason = "max_turn"
                        if reason == "silence" and self.asr is not None and hasattr(self.asr, "begin_finish"):
                            # 延迟专项阶段2：静音达标即发送 finish-task 主动结束，
                            # 等待全部最终分句（task-finished 实测约 +50ms）后
                            # 由 _on_asr_finish_done 统一提交，不依赖服务端 VAD 慢速断句
                            turn.finish_started = True
                            self._mark(turn, "asr_finish_sent")
                            self._ensure_tts_prewarm(turn)
                            try:
                                await self.asr.begin_finish()
                            except Exception as exc:
                                logger.warning(
                                    f"[{TAG}] begin_finish error={exc}, fallback direct submit"
                                )
                                submit_payload = self._prepare_submit_locked(turn, reason)
                        elif reason:
                            submit_payload = self._prepare_submit_locked(turn, reason)
                    elif self._turn_frames >= self.max_turn_frames:
                        # finish 等待期间达到安全上限：直接提交已有分句（禁止静默截断）
                        submit_payload = self._prepare_submit_locked(turn, "max_turn")
                elif self._turn_frames >= self.max_turn_frames and not turn.max_turn_warned:
                    # 达到上限但尚无最终分句：记录原因并继续收音（禁止静默截断/丢弃）
                    turn.max_turn_warned = True
                    logger.warning(
                        f"[{TAG}] max_turn reached without final sentences turn={turn.turn_id}"
                        f" frames={self._turn_frames}, keep listening"
                    )

        if submit_payload is not None:
            await self._finalize_turn(*submit_payload)
            return

        try:
            if self.asr:
                await self.asr.send_pcm(pcm)
        except Exception as exc:
            logger.error(f"[{TAG}] ASR send failed: {exc}")
            await self._enter_error_state("ASR 发送失败")

    def _compute_rms(self, pcm: bytes) -> float:
        import struct
        if len(pcm) < 2:
            return 0.0
        count = len(pcm) // 2
        if count == 0:
            return 0.0
        squares = 0.0
        for i in range(count):
            val = struct.unpack_from("<h", pcm, i * 2)[0]
            squares += val * val
        return math.sqrt(squares / count) / 32768.0

    async def _start_asr_locked(self) -> None:
        self._next_turn_id()
        turn = TurnGuard(self._turn_id)
        self._current_turn = turn
        self._asr_started_count += 1
        turn.storage_id = self._storage.save_turn(turn.turn_id, status="active")
        self._emit_caption("status", "正在识别…")

        def _final_cb(text: str, t: TurnGuard = turn) -> None:
            asyncio.create_task(self._on_asr_final(t, text))

        self.asr = self.asr_factory(_final_cb)
        # 分句/活动回调绑定本轮 turn：迟到事件（turn 已换）会被丢弃
        self.asr.on_sentence = lambda meta, t=turn: self._on_asr_sentence(t, meta)
        if hasattr(self.asr, "on_activity"):
            self.asr.on_activity = lambda t=turn: self._on_asr_activity(t)
        # finish-task 完成回调（延迟专项阶段2）：等待全部最终分句后统一提交一次
        self.asr.on_finish_done = lambda ok, t=turn: asyncio.create_task(
            self._on_asr_finish_done(t, ok)
        )
        self._resume_asr_attempts += 1
        self._trace(
            "asr/start_attempt",
            {"frame_idx": self._resume_frames, "asr_attempts": self._resume_asr_attempts},
        )
        started = await self.asr.start()
        if not started:
            self._resume_asr_failed += 1
            self._trace(
                "asr/failed",
                {"frame_idx": self._resume_frames, "asr_failed": self._resume_asr_failed},
            )
            # handle_pcm already owns self._lock.  Do not call the public
            # acquiring wrapper here: asyncio.Lock is deliberately
            # non-reentrant and that would leave this task (and all future
            # PCM) waiting forever.  Drop the failed adapter before its
            # queued Qwen on_error task can run; that task is identity-gated
            # and cannot mutate the next attempt.
            self.asr = None
            self._enter_error_state_locked("ASR 连接失败")
            self._emit_error_state("ASR 连接失败")
            return
        self._resume_asr_started += 1
        self._trace("asr/started", {"frame_idx": self._resume_frames})
        # generation_id 提前分配，供临近断句时的 TTS 预热与音频流使用。
        turn.generation_id = self._next_generation_id()

    def _on_asr_sentence(self, turn: TurnGuard, meta: dict[str, Any]) -> None:
        """ASR 最终分句回调（同步）：按 key 去重并聚合。

        注意：不在此处重置 _frames_since_voice —— 最终分句的 end_time 在过去，
        静音时长必须从真实最后人声位置累计，不能从分句到达时间重新计算。
        防提前提交由 _on_asr_activity（非空中间结果）负责。
        """
        if self._current_turn is not turn or turn.interrupted or turn.asr_final is not None:
            logger.info(
                f"[{TAG}] late sentence dropped turn={turn.turn_id} "
                f"current={'none' if self._current_turn is None else self._current_turn.turn_id}"
            )
            return
        key = str(meta.get("sentence_key", ""))
        if not key or key in turn.seen_sentence_keys:
            return
        turn.seen_sentence_keys.add(key)
        turn.pending_sentences.append(
            (meta.get("begin_time"), meta.get("end_time"), str(meta.get("text", "")))
        )
        self._mark(turn, "asr_sentence_first_received")
        self._mark(turn, "asr_sentence_last_received", overwrite=True)
        logger.info(
            f"[{TAG}] sentence aggregated turn={turn.turn_id} key={key[:8]}"
            f" begin_time={meta.get('begin_time')} end_time={meta.get('end_time')}"
            f" text_length={len(str(meta.get('text', '')))} count={len(turn.pending_sentences)}"
        )

    def _on_asr_activity(self, turn: TurnGuard) -> None:
        """ASR 非空中间结果的活动信号（无正文）：用户仍在说话。

        轻声低于本地 RMS 阈值时，这是阻止上一分句被提前提交的关键信号；
        仅复位静音计数，不产生字幕/LLM/TTS。绑定所属 turn，迟到信号不污染下一轮。
        """
        if self._current_turn is not turn or turn.interrupted or turn.asr_final is not None:
            return
        if self._frames_since_voice:
            self._frames_since_voice = 0

    def _prepare_submit_locked(
        self, turn: TurnGuard, reason: str, direct_text: str | None = None
    ) -> tuple[TurnGuard, str, str] | None:
        """锁内组装最终文本并进入 thinking；返回提交负载或 None（不可提交）。"""
        if turn.asr_final is not None or turn.interrupted:
            return None
        if direct_text is not None:
            final_text = direct_text.strip()
        else:
            sentences = sorted(turn.pending_sentences, key=lambda s: (s[0] is None, s[0], s[1] or 0))
            final_text = "".join(s[2] for s in sentences).strip()
        if not final_text:
            return None
        turn.asr_final = final_text
        turn.asr_submit_reason = reason
        turn.final_call_count += 1
        self._set_state("thinking")
        return (turn, final_text, reason)

    async def _finalize_turn(self, turn: TurnGuard, text: str, reason: str) -> None:
        """锁外提交收尾：字幕、后台停 ASR、并行启动 LLM 与 LiveTalking 流准备。

        整个回合只执行一次。延迟专项：ASR 收尾与 LiveTalking 准备全部后台化，
        提交主链路零串行等待（历史 await _stop_asr() 曾串行阻塞 ~3s 拖慢 LLM/TTS）。
        """
        self._mark(turn, "turn_submitted")
        silence_ms = self._frames_since_voice * self.frame_duration_ms
        logger.info(
            f"[{TAG}] turn submit turn={turn.turn_id} reason={reason}"
            f" sentences={len(turn.pending_sentences)} text_length={len(text)}"
            f" silence_before_submit_ms={silence_ms} final_calls={turn.final_call_count}"
        )
        self._emit_caption("user", text)
        self._emit_caption("status", "正在思考…")
        # ASR 收尾后台化：finish-task 已随静音判定发送（begin_finish），此处仅清理
        stop_task = asyncio.create_task(self._stop_asr())
        turn.track(stop_task)
        # LiveTalking 流准备与 LLM 并行（延迟专项阶段3）；TTS 会话已随人声预热
        prep_task = asyncio.create_task(self._prepare_lt_stream(turn))
        turn.lt_prep_task = prep_task
        turn.track(prep_task)
        llm_task = asyncio.create_task(self._run_llm(turn, text))
        turn.track(llm_task)

    async def _on_asr_final(self, turn: TurnGuard, text: str) -> None:
        """适配器直接给出最终文本（Dummy/兼容路径）：走统一提交。

        已有聚合分句时以聚合结果为准（begin_finish 触发的 Dummy on_final
        不得覆盖已聚合的真实分句）。
        """
        payload: tuple[TurnGuard, str, str] | None = None
        async with self._lock:
            if self._current_turn is not turn:
                logger.info(f"[{TAG}] dropping late ASR result turn={turn.turn_id}")
                return
            self._mark(turn, "asr_task_finished_received")
            payload = self._prepare_submit_locked(
                turn, "adapter_final", direct_text=None if turn.pending_sentences else text
            )
        if payload is not None:
            await self._finalize_turn(*payload)

    async def _on_asr_finish_done(self, turn: TurnGuard, ok: bool) -> None:
        """finish-task 收尾完成（或超时）回调（延迟专项阶段2）。

        此刻服务端最终分句已全部聚合（task-finished 之前逐条送达），
        统一提交一次；与 _on_asr_final 互斥（asr_final 状态位防重复提交）。
        """
        payload: tuple[TurnGuard, str, str] | None = None
        empty_reset = False
        async with self._lock:
            if self._current_turn is not turn or turn.interrupted or turn.asr_final is not None:
                return
            self._mark(turn, "asr_task_finished_received")
            payload = self._prepare_submit_locked(turn, "silence" if ok else "finish_timeout")
            if payload is None:
                # 无可提交内容（防御路径：begin_finish 仅在已有分句时触发）：
                # 复位门控回倾听，避免 _asr_connected 残留阻塞下一轮
                empty_reset = True
                turn.interrupted = True
                self._current_turn = None
                self._reset_gating()
                self._set_state("listening")
        if empty_reset:
            self._storage.update_turn_status(turn.storage_id, "interrupted", "ASR 无有效分句")
            self._emit_caption("status", "未识别到内容，请重新说话")
            logger.warning(f"[{TAG}] finish done without submittable text turn={turn.turn_id}")
            return
        if payload is not None:
            await self._finalize_turn(*payload)

    async def _on_asr_close(self) -> None:
        async with self._lock:
            self._asr_connected = False
            turn = self._current_turn
            if turn and turn.asr_final is None and not turn.interrupted:
                # 断线：取消待提交任务（丢弃 pending 分句），恢复 listening
                turn.interrupted = True
                self._current_turn = None
                self._reset_gating()
                self._storage.update_turn_status(turn.storage_id, "interrupted", "ASR 连接断开")
                self._set_state("listening")
                self._emit_caption("status", "识别连接断开，请重新说话")

    async def _run_llm(self, turn: TurnGuard, user_text: str) -> None:
        if self._shutdown:
            return
        try:
            # generation_id 已在回合开始时分配（_start_asr_locked，人声即预热 TTS 依赖）
            self.llm = self.llm_factory(lambda token: self._on_llm_token(turn, token))
            history = self._storage.get_completed_turns()
            messages = []
            for h in history:
                messages.append({"role": "user", "content": h["user"]})
                messages.append({"role": "assistant", "content": h["assistant"]})
            messages = messages[-(get_config().llm.get("max_context_turns", 20) * 2):]

            system_prompt = self._persona
            try:
                memory_block = build_memory_prompt(
                    user_text, self._storage.get_active_memories()
                )
                if memory_block:
                    system_prompt += "\n\n" + memory_block
            except Exception as exc:
                self._memory_available = False
                self._memory_reject_count += 1
                self._memory_last_status = "error"
                logger.warning(f"[{TAG}] memory recall skipped error={type(exc).__name__}")

            sentence_buffer = ""
            async for token in self.llm.chat(system_prompt, messages, user_text):
                if turn.interrupted or self._shutdown:
                    break
                self._mark(turn, "llm_first_token")
                sentence_buffer += token
                turn.llm_text += token
                while True:
                    idx = _find_sentence_end(sentence_buffer)
                    if idx is None:
                        break
                    raw_sentence = sentence_buffer[:idx]
                    sentence = raw_sentence.strip()
                    sentence_buffer = sentence_buffer[idx:]
                    if sentence:
                        # Display/TTS cue cleanup happens only once this
                        # sentence is complete; every complete free-form cue
                        # is removed from each derived text channel.
                        turn._display_source += raw_sentence
                        turn.display_text = strip_performance_cues(turn._display_source)
                        if turn.display_text:
                            self._emit_caption("ai", turn.display_text)
                        if not turn.tts_started:
                            self._mark(turn, "llm_first_sentence")
                        turn.tts_started = True
                        tts_sentence = strip_unapproved_tts_cues(sentence)
                        if tts_sentence:
                            turn.tts_text_queued = True
                            await self._tts_queue.put((turn.turn_id, tts_sentence))
            if sentence_buffer.strip() and not turn.interrupted and not self._shutdown:
                # Stream completion is the safe boundary for a final sentence
                # without terminal punctuation (and for a completed cue).
                turn._display_source += sentence_buffer
                turn.display_text = strip_performance_cues(turn._display_source)
                if turn.display_text:
                    self._emit_caption("ai", turn.display_text)
                if not turn.tts_started:
                    self._mark(turn, "llm_first_sentence")
                turn.tts_started = True
                tts_sentence = strip_unapproved_tts_cues(sentence_buffer.strip())
                if tts_sentence:
                    turn.tts_text_queued = True
                    await self._tts_queue.put((turn.turn_id, tts_sentence))
            elif not turn.interrupted and not self._shutdown:
                # Ensure persistence reflects the complete raw response even
                # when the final token ended exactly at a sentence boundary.
                turn.display_text = strip_performance_cues(turn.llm_text)
            if not turn.interrupted and not self._shutdown:
                turn.tts_finished = True
                await self._tts_queue.put((turn.turn_id, ""))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"[{TAG}] LLM error={exc}")
            await self._abort_turn(turn, "思考失败")
        finally:
            if self.llm:
                await self.llm.close()

    def _on_llm_token(self, turn: TurnGuard, token: str) -> None:
        pass

    async def _enqueue_tts(self, turn_id: int, sentence: str) -> None:
        await self._tts_queue.put((turn_id, sentence))

    async def _tts_consumer(self) -> None:
        while not self._shutdown:
            turn_id, sentence = await self._tts_queue.get()
            try:
                if self._shutdown:
                    break
                turn = self._current_turn
                if turn is None or turn.turn_id != turn_id or turn.interrupted:
                    continue
                if sentence == "":
                    if not turn.tts_text_queued:
                        await self._complete_no_audio_turn(turn)
                        continue
                    # 轮次结束：先收尾 TTS 会话（等待尾部音频），再结束音频流
                    if turn.tts is not None:
                        try:
                            await turn.tts.finish()
                        except Exception as exc:
                            raise RuntimeError(f"TTS 合成失败: {exc}") from exc
                    if not turn.audio_started:
                        # 全轮无音频输出：回收已并行准备的 LiveTalking 流，避免残留会话
                        await self._recycle_prepared_lt(turn)
                    await self._raw_pcm_queue.put((turn.generation_id, None))
                    continue
                if not turn.audio_started:
                    ok = False
                    prep = turn.lt_prep_task
                    if prep is not None:
                        # 等待提交时并行启动的流准备完成（通常早已就绪）
                        turn.lt_prep_task = None
                        try:
                            ok = await prep
                        except Exception:
                            ok = False
                    if not ok:
                        ok = await self._start_audio_stream_for_turn(turn)
                    if not ok:
                        await self._abort_turn(turn, "真人音频流启动失败")
                        continue
                    turn.audio_started = True
                    self._current_audio_turn = turn
                    self._current_generation = turn.generation_id
                await self._speak_sentence(turn, sentence)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[{TAG}] TTS consumer error={exc}")
                if self._current_turn and not self._current_turn.interrupted:
                    await self._abort_turn(self._current_turn, f"语音合成失败: {exc}")
            finally:
                self._tts_queue.task_done()

    async def _start_audio_stream_for_turn(self, turn: TurnGuard) -> bool:
        if self.livetalking is None or not self.livetalking.session_id:
            logger.error(f"[{TAG}] cannot start audio stream: no livetalking session_id")
            return False
        try:
            ok = await self.livetalking.start_audio_stream(turn.generation_id)
            if ok:
                self._mark(turn, "livetalking_stream_ready")
            return ok
        except Exception as exc:
            logger.error(f"[{TAG}] start_audio_stream error={exc}")
            return False

    async def _prepare_lt_stream(self, turn: TurnGuard) -> bool:
        """提交时并行准备 LiveTalking 流（延迟专项阶段3，与 LLM/TTS 并行）。

        每轮最多一次；失败由 _tts_consumer 回落到按需启动；
        无音频输出/中断路径经 _recycle_prepared_lt 回收。
        """
        if turn.interrupted or self._current_turn is not turn:
            return False
        # 先占位再等待：取消路径（cancel_all）也能按 gid 回收半开的流
        turn.audio_prepared = True
        try:
            ok = await self._start_audio_stream_for_turn(turn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[{TAG}] lt stream prepare error={exc}")
            ok = False
        if not ok:
            turn.audio_prepared = False
        return ok

    async def _recycle_prepared_lt(self, turn: TurnGuard) -> None:
        """回收已准备但未真正送音频的 LiveTalking 流（无音频/中断路径）。"""
        if (
            turn.audio_prepared
            and not turn.audio_started
            and not turn.audio_ended
            and turn.generation_id is not None
            and self.livetalking
        ):
            try:
                await self.livetalking.abort_audio_stream(turn.generation_id)
            except Exception:
                pass
            turn.audio_prepared = False

    def _ensure_tts_prewarm(self, turn: TurnGuard) -> None:
        """临近断句时启动预热，避免长语音让远端会话空闲过期。"""
        if turn.tts is not None or turn.tts_prewarm_task is not None or turn.interrupted:
            return
        turn.tts_prewarm_task = asyncio.create_task(self._prewarm_tts(turn))
        turn.track(turn.tts_prewarm_task)

    def _new_tts_adapter(self, turn: TurnGuard) -> Any:
        turn.tts_attempt_id += 1
        attempt_id = turn.tts_attempt_id
        return self.tts_factory(
            lambda pcm, tid=turn.turn_id, gid=turn.generation_id, aid=attempt_id: self._on_tts_pcm(
                tid, gid, aid, pcm
            )
        )

    async def _prewarm_tts(self, turn: TurnGuard) -> None:
        """在本地判定回合将结束时预热 TTS，会话只需短暂空闲。"""
        tts = None
        try:
            tts = self._new_tts_adapter(turn)
            started = await tts.start()
            if not started:
                await tts.cancel()
                return
            if self._current_turn is turn and not turn.interrupted and turn.tts is None:
                turn.tts = tts
                self.tts = tts
                self._mark(turn, "tts_session_ready")
                logger.info(f"[{TAG}] tts prewarmed turn={turn.turn_id}")
            else:
                await tts.cancel()
        except asyncio.CancelledError:
            if tts is not None:
                try:
                    await tts.cancel()
                except Exception:
                    pass
            raise
        except Exception as exc:
            logger.warning(f"[{TAG}] tts prewarm failed={exc}")
            if tts is not None:
                try:
                    await tts.cancel()
                except Exception:
                    pass

    @staticmethod
    def _tts_is_active(tts: Any) -> bool:
        """TTS 会话是否仍可用；无 is_active 能力的测试适配器视为可用。

        兼容 property（取值为 bool）与 callable（绑定方法）两种实现。
        """
        value = getattr(tts, "is_active", None)
        if value is None:
            return True
        if callable(value):
            try:
                value = value()
            except Exception:
                return False
        try:
            return bool(value)
        except Exception:
            return False

    async def _speak_sentence(self, turn: TurnGuard, sentence: str) -> None:
        if self._shutdown:
            return
        try:
            # 等待在途预热完成（成功则直接复用；失败则按需启动）
            prewarm = turn.tts_prewarm_task
            if prewarm is not None:
                turn.tts_prewarm_task = None
                if not prewarm.cancelled():
                    try:
                        await prewarm
                    except Exception:
                        pass
            if turn.tts is not None and not self._tts_is_active(turn.tts):
                # 预热会话已失效（长语音期间被服务端关闭）：回收并回落按需启动
                logger.info(f"[{TAG}] prewarmed tts stale, restart on demand turn={turn.turn_id}")
                try:
                    await turn.tts.cancel()
                except Exception:
                    pass
                turn.tts = None
                self.tts = None
            if turn.tts is None:
                self.tts = self._new_tts_adapter(turn)
                turn.tts = self.tts
                started = await turn.tts.start()
                if not started:
                    turn.tts = None
                    self.tts = None
                    raise RuntimeError("TTS 启动失败")
                self._mark(turn, "tts_session_ready")
            if turn.interrupted:
                return
            self._mark(turn, "tts_text_sent")
            await turn.tts.send_text(sentence)
            if (
                getattr(turn.tts, "supports_first_pcm_watchdog", False)
                and not turn.tts_first_pcm_received
            ):
                try:
                    await asyncio.wait_for(
                        turn.tts_first_pcm_event.wait(), timeout=self.tts_first_pcm_timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[{TAG}] first PCM timeout, restart TTS once turn={turn.turn_id}"
                    )
                    stale_tts = turn.tts
                    turn.tts = None
                    if self.tts is stale_tts:
                        self.tts = None
                    try:
                        await stale_tts.cancel()
                    except Exception:
                        pass
                    if turn.interrupted or self._current_turn is not turn:
                        return
                    turn.tts_first_pcm_event.clear()
                    retry_tts = self._new_tts_adapter(turn)
                    turn.tts = retry_tts
                    self.tts = retry_tts
                    if not await retry_tts.start():
                        raise RuntimeError("TTS 重建失败")
                    self._mark(turn, "tts_session_restarted")
                    await retry_tts.send_text(sentence)
                    try:
                        await asyncio.wait_for(
                            turn.tts_first_pcm_event.wait(), timeout=self.tts_first_pcm_timeout
                        )
                    except asyncio.TimeoutError as exc:
                        raise RuntimeError(
                            f"TTS 首音频超时，重试后仍未收到 PCM"
                            f"（{self.tts_first_pcm_timeout:.1f}s）"
                        ) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeError(f"TTS 合成失败: {exc}") from exc

    async def _on_tts_pcm(
        self, turn_id: int, generation_id: int, attempt_id: int, pcm: bytes
    ) -> None:
        if self._shutdown:
            return
        turn = self._current_turn
        if (
            turn is None
            or turn.turn_id != turn_id
            or turn.generation_id != generation_id
            or turn.tts_attempt_id != attempt_id
            or turn.interrupted
        ):
            return
        if not turn.tts_text_queued:
            # 预热会话（Dummy 适配器 start 即发 PCM）在首句入队前的音频一律丢弃
            return
        turn.tts_first_pcm_received = True
        turn.tts_first_pcm_event.set()
        self._mark(turn, "tts_first_pcm")
        await self._raw_pcm_queue.put((generation_id, pcm))

    async def _pcm_sender(self) -> None:
        while not self._shutdown:
            generation_id, pcm = await self._raw_pcm_queue.get()
            try:
                if self._shutdown:
                    break
                if pcm is None:
                    await self._end_audio_stream_for_generation(generation_id)
                    continue
                async with self._pcm_lock:
                    self._pcm_buffer.extend(pcm)
                await self._drain_pcm_buffer(generation_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[{TAG}] PCM sender error={exc}")
            finally:
                self._raw_pcm_queue.task_done()

    async def _drain_pcm_buffer(self, generation_id: int) -> None:
        while True:
            turn = self._current_audio_turn
            if turn is None or turn.generation_id != generation_id or turn.interrupted:
                break
            async with self._pcm_lock:
                if len(self._pcm_buffer) < self.frame_bytes:
                    break
                frame = bytes(self._pcm_buffer[:self.frame_bytes])
                del self._pcm_buffer[:self.frame_bytes]
            await self._send_pcm_frame(generation_id, frame)

    async def _send_pcm_frame(self, generation_id: int, frame: bytes) -> None:
        if self._state == "thinking":
            self._set_state("speaking")
        try:
            await self.livetalking.send_pcm(frame)
        except Exception as exc:
            logger.warning(f"[{TAG}] send PCM failed: {exc}")
            turn = self._current_audio_turn
            if turn and turn.generation_id == generation_id and not turn.interrupted:
                await self._recover_livetalking_failure(turn, f"发送音频失败: {exc}")

    async def _recover_livetalking_failure(self, turn: TurnGuard, error: str) -> None:
        """Fail only the current turn when LiveTalking transport is broken.

        ``_abort_turn`` owns the established cancellation, queue draining,
        stale-stream cleanup, and failed-row semantics.  A downstream audio
        transport failure is the one abort class that must not leave the
        conversation admission gate in ``error``: after cleanup, return to
        this turn's resting state so the next text/voice turn can proceed.
        """
        await self._abort_turn(turn, error)
        if self._shutdown:
            return
        async with self._lock:
            if self._current_audio_turn is turn:
                self._current_audio_turn = None
                self._current_generation = None
            if self._state == "error":
                self._set_state(turn.return_state or "listening")
        if not turn.return_state:
            self._emit_caption("status", "继续倾听…")

    async def _complete_no_audio_turn(self, turn: TurnGuard) -> None:
        """Complete a successful turn whose TTS policy produced no text.

        This path deliberately skips TTS ``finish`` and LiveTalking audio
        start. It still preserves the normal completed-row, memory, metrics,
        and return-state lifecycle used by an audio-bearing turn.
        """
        if turn.interrupted or self._current_turn is not turn:
            return
        prewarm = turn.tts_prewarm_task
        if prewarm is not None and not prewarm.done():
            prewarm.cancel()
            try:
                await prewarm
            except BaseException:
                pass
        turn.tts_prewarm_task = None
        if turn.tts is not None:
            try:
                await turn.tts.cancel()
            except Exception:
                pass
            if self.tts is turn.tts:
                self.tts = None
            turn.tts = None

        prep = turn.lt_prep_task
        if prep is not None and not prep.done():
            try:
                await prep
            except BaseException:
                pass
        await self._recycle_prepared_lt(turn)
        try:
            display_text = turn.display_text or strip_performance_cues(turn.llm_text)
            self._storage.complete_turn(
                turn.storage_id, turn.asr_final or "", display_text
            )
            self._persist_memories_for_turn(turn)
        except Exception as exc:
            logger.error(f"[{TAG}] no-audio turn completion error={exc}")
            self._storage.update_turn_status(turn.storage_id, "failed", str(exc))
        finally:
            self._log_metrics_summary(turn)
            turn.audio_ended = True
            self._current_audio_turn = None
            self._current_generation = None
            if self._state in {"thinking", "speaking"} and not self._shutdown:
                self._set_state(turn.return_state or "listening")
                if not turn.return_state:
                    self._emit_caption("status", "继续倾听…")

    async def _end_audio_stream_for_generation(self, generation_id: int) -> None:
        turn = self._current_audio_turn
        if turn is None or turn.generation_id != generation_id or turn.audio_ended:
            return
        async with self._pcm_lock:
            remainder = self._pcm_buffer
            frames: list[bytes] = []
            if 0 < len(remainder) < self.frame_bytes:
                frames.append(bytes(remainder) + bytes(self.frame_bytes - len(remainder)))
                self._pcm_buffer.clear()
            elif len(remainder) >= self.frame_bytes:
                # Should have been drained; drain synchronously to avoid losing data.
                while len(self._pcm_buffer) >= self.frame_bytes:
                    frames.append(bytes(self._pcm_buffer[:self.frame_bytes]))
                    del self._pcm_buffer[:self.frame_bytes]
        try:
            if turn.interrupted or turn.error:
                await self.livetalking.abort_audio_stream(generation_id)
            else:
                for frame in frames:
                    await self._send_pcm_frame(generation_id, frame)
                ok = await self.livetalking.end_audio_stream(generation_id)
                if ok:
                    # Conversation history stores the display variant; TTS
                    # received its separately policy-filtered text derivation.
                    display_text = turn.display_text or strip_performance_cues(turn.llm_text)
                    self._storage.complete_turn(
                        turn.storage_id, turn.asr_final or "", display_text
                    )
                    self._persist_memories_for_turn(turn)
                else:
                    self._storage.update_turn_status(turn.storage_id, "failed", "音频流结束失败")
        except Exception as exc:
            logger.error(f"[{TAG}] end audio stream error={exc}")
            self._storage.update_turn_status(turn.storage_id, "failed", str(exc))
        finally:
            self._log_metrics_summary(turn)
            turn.audio_ended = True
            self._current_audio_turn = None
            self._current_generation = None
            if self._state == "speaking" and not self._shutdown:
                # 文字输入轮回落 return_state（paused 一次性轮 / listening 连续语音轮），
                # 语音轮 return_state 为 None，保持原 listening 行为
                self._set_state(turn.return_state or "listening")
                if not turn.return_state:
                    self._emit_caption("status", "继续倾听…")

    async def _abort_turn(self, turn: TurnGuard, error: str) -> None:
        async with self._lock:
            if turn.error or turn.interrupted:
                return
            turn.error = error
            turn.interrupted = True
            if self._current_turn is turn:
                self._current_turn = None
            self._asr_connected = False
            self._rms_hits = 0
            self._prebuffer.clear()
            self._clear_queue(self._tts_queue)
            self._clear_queue(self._raw_pcm_queue)
            self._set_state("error")
        await turn.cancel_all()
        await self._stop_asr()
        if self.tts:
            await self.tts.cancel()
        if self.livetalking and turn.generation_id is not None and not turn.audio_ended:
            if self._current_audio_turn is turn:
                try:
                    await self.livetalking.abort_audio_stream(turn.generation_id)
                except Exception:
                    pass
                turn.audio_ended = True
            else:
                # 已并行准备但尚未送音频的流：回收，避免残留会话
                await self._recycle_prepared_lt(turn)
        async with self._pcm_lock:
            self._pcm_buffer.clear()
        self._storage.update_turn_status(turn.storage_id, "failed", error)
        self._emit_caption("error", error)

    def _clear_queue(self, q: asyncio.Queue[Any]) -> None:
        while not q.empty():
            try:
                q.get_nowait()
                q.task_done()
            except asyncio.QueueEmpty:
                break

    def _enter_error_state_locked(self, message: str) -> None:
        """Apply error cleanup while the caller already owns ``self._lock``.

        This helper intentionally contains only synchronous, lock-protected
        state/storage mutations.  Async callers use ``_enter_error_state``;
        startup failure in ``_start_asr_locked`` uses this helper directly.
        """
        if self._current_turn:
            self._current_turn.error = message
            self._current_turn.interrupted = True
            self._storage.update_turn_status(self._current_turn.storage_id, "failed", message)
            self._current_turn = None
        self._reset_gating()
        self._clear_queue(self._tts_queue)
        self._clear_queue(self._raw_pcm_queue)
        self._set_state("error")

    def _emit_error_state(self, message: str) -> None:
        """Emit an already-applied error without touching the session lock."""
        self._emit_caption("error", message)
        if self.on_error:
            try:
                self.on_error(message)
            except Exception:
                pass

    async def _enter_error_state(self, message: str) -> None:
        async with self._lock:
            self._enter_error_state_locked(message)
        self._emit_error_state(message)

    async def _enter_error_state_for_asr(
        self, adapter: QwenASRAdapter | None, message: str
    ) -> None:
        """Handle an adapter callback only if it still owns the active ASR turn.

        Startup failure clears ``self.asr`` before returning from the locked
        path, so Qwen's queued on_error callback becomes a harmless no-op.
        This also prevents a late callback from an old adapter from changing a
        successfully recovered session to ``error``.
        """
        async with self._lock:
            if adapter is None or self.asr is not adapter:
                return
            self._enter_error_state_locked(message)
        self._emit_error_state(message)

    async def _stop_asr(self) -> None:
        if self.asr:
            try:
                await self.asr.finish()
            except Exception:
                pass
            self.asr = None
        # 重置 RMS 门控状态：否则 _asr_connected 残留 True 且 self.asr=None，
        # 下一轮 PCM 会被静默丢弃（第二轮无法识别的根因）
        self._asr_connected = False
        self._rms_hits = 0
        self._prebuffer.clear()
        # 重置回合判定计数（取消待提交状态）
        self._frames_since_voice = 0
        self._turn_frames = 0

    async def _stop_asr_locked(self) -> None:
        await self._stop_asr()

    def _emit_caption(self, kind: str, text: str) -> None:
        if self.on_caption:
            try:
                self.on_caption({"kind": kind, "text": text})
            except Exception as exc:
                logger.error(f"[{TAG}] caption callback error={exc}")

    async def start_tts_consumer(self) -> None:
        if self._tts_consumer_task is None or self._tts_consumer_task.done():
            self._tts_consumer_task = asyncio.create_task(self._tts_consumer())

    async def start_pcm_sender(self) -> None:
        if self._pcm_sender_task is None or self._pcm_sender_task.done():
            self._pcm_sender_task = asyncio.create_task(self._pcm_sender())

    async def stop_current_streams(self) -> None:
        """停止当前对话流（浏览器最后连接断开 / 手动关闭 / 应用收尾）。

        复用 _cancel_current_turn_locked：取消 turn 与待聚合分句、落库终结状态、
        复位门控、清空 TTS/PCM 队列；随后停 ASR/TTS、中止音频流并关闭远端会话。
        """
        async with self._lock:
            turn = await self._cancel_current_turn_locked("interrupted", "浏览器断开")
            self._set_state("paused")
        if turn:
            await turn.cancel_all()
        await self._stop_asr()
        if self.tts:
            await self.tts.cancel()
        await self._abort_live_audio()
        if turn:
            await self._recycle_prepared_lt(turn)
        async with self._pcm_lock:
            self._pcm_buffer.clear()
        # 兜底关闭远端 LiveTalking 会话（pause 不走此处，页面视频不受影响）
        if self.livetalking and self.livetalking.session_id:
            try:
                await self.livetalking.close_session()
            except Exception:
                pass
        self._bound_livetalking_session_id = None
        self._end_resume("streams_stopped")

    async def shutdown(self) -> None:
        self._shutdown = True
        if self._tts_consumer_task:
            self._tts_consumer_task.cancel()
            try:
                await self._tts_consumer_task
            except BaseException:
                pass
        if self._pcm_sender_task:
            self._pcm_sender_task.cancel()
            try:
                await self._pcm_sender_task
            except BaseException:
                pass
        if self.asr:
            await self.asr.stop()
        if self.tts:
            await self.tts.cancel()
        if self.livetalking:
            await self.livetalking.close()
        if self.llm:
            await self.llm.close()
        self._storage.close()

    def diagnostics(self) -> dict[str, Any]:
        turn = self._current_turn
        try:
            memory_active_count = self._storage.count_active_memories()
        except Exception as exc:
            self._memory_available = False
            self._memory_reject_count += 1
            self._memory_last_status = "error"
            logger.warning(f"[{TAG}] memory diagnostics skipped error={type(exc).__name__}")
            memory_active_count = 0
        return {
            "state": self._state,
            "turn_id": self._turn_id,
            "generation_id": self._generation_id,
            "asr_started_count": self._asr_started_count,
            "pending_sentences": len(turn.pending_sentences) if turn else 0,
            "frames_since_voice": self._frames_since_voice,
            "turn_end_silence_ms": self.turn_end_silence_ms,
            "max_turn_ms": self.max_turn_ms,
            "rms_threshold": self.rms_threshold,
            "rms_consecutive_frames": self.rms_consecutive_frames,
            "prebuffer_ms": self.prebuffer_ms,
            "tts_queue_size": self._tts_queue.qsize(),
            "raw_pcm_queue_size": self._raw_pcm_queue.qsize(),
            "pcm_buffer_bytes": len(self._pcm_buffer),
            "history_turns": len(self._storage.get_completed_turns()),
            "session_id": self.livetalking.session_id if self.livetalking else None,
            "resume_id": self._resume_id,
            "resume_frames": self._resume_frames,
            "resume_last_rms": round(self._resume_last_rms, 4) if self._resume_id else None,
            "resume_vad_hits": self._resume_vad_hits,
            "resume_asr_attempts": self._resume_asr_attempts,
            "resume_asr_started": self._resume_asr_started,
            "resume_asr_failed": self._resume_asr_failed,
            "memory_available": self._memory_available,
            "memory_active_count": memory_active_count,
            "memory_write_count": self._memory_write_count,
            "memory_reject_count": self._memory_reject_count,
            "memory_last_status": self._memory_last_status,
        }
