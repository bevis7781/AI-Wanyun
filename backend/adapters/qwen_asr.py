from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, AsyncIterator, Callable

import websockets

from backend.config import get_config
from backend.logger import get_logger, mask_id

logger = get_logger()
TAG = "qwen_asr"
PCM_CHUNK_BYTES = 3200  # 100 ms at 16 kHz mono PCM16


class QwenASRAdapter:
    def __init__(
        self,
        on_final: Callable[[str], None] | None = None,
        on_close: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_sentence: Callable[[dict[str, Any]], None] | None = None,
        on_activity: Callable[[], None] | None = None,
    ) -> None:
        cfg = get_config()
        self.model = cfg.asr.get("model", "qwen-audio-3.0-asr-flash-streaming")
        self.ws_url = cfg.asr.get(
            "ws_url",
            "wss://ws-ypa7uyj66zsg1d6y.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference",
        )
        self.format = cfg.asr.get("format", "pcm")
        self.sample_rate = int(cfg.asr.get("sample_rate", 16000))
        self.max_sentence_silence = int(cfg.asr.get("max_sentence_silence", 1600))
        self.semantic_punctuation_enabled = bool(cfg.asr.get("semantic_punctuation_enabled", False))
        self.max_retries = int(cfg.asr.get("max_retries", 2))
        self.connect_timeout = float(cfg.asr.get("connect_timeout", 12.0))
        self.retry_cooldown = float(cfg.asr.get("retry_cooldown", 10.0))
        # 主动结束（finish-task）后等待 result-generated / task-finished 的超时
        self.finish_timeout = float(cfg.asr.get("finish_timeout", 5.0))
        self.api_key = cfg.secret("DASHSCOPE_API_KEY")
        self.on_final = on_final
        self.on_close = on_close
        self.on_error = on_error
        self.on_sentence = on_sentence
        # 主动结束完成回调（ok=True 收到 task-finished；False 超时/异常）
        self.on_finish_done: Callable[[bool], None] | None = None
        # 非空中间结果的活动信号（不含正文）：通知 Session 用户仍在说话，
        # 用于阻止上一分句在本地 RMS 未命中（低于阈值的轻声）时被提前提交
        self.on_activity = on_activity

        self.ws: websockets.WebSocketClientProtocol | None = None
        self.task_id: str | None = None
        self.forward_task: asyncio.Task | None = None
        self.started_event = asyncio.Event()
        self.server_ready = False
        self.submitted = False
        self.stopping = False
        self.pcm_buffer = bytearray()
        self.final_text: str | None = None
        self._closed = False
        self._retry_not_before = 0.0
        # 主动结束状态：finish-task 只发一次；waiter 保证 on_finish_done 只回调一次
        self._finish_requested = False
        self._finish_waiter: asyncio.Task | None = None
        self._finish_done_event = asyncio.Event()
        self._finish_callback_fired = False
        # forward 循环收到 task-finished 的标志：被外部取消时据此判定回调 ok 值
        self._task_finished_cleanly = False
        # 多分句聚合状态：sentence_end=true 只是分句结束，不是任务结束
        self._sentence_keys: set[str] = set()
        self.final_sentence_count = 0
        self.last_sentence_end_time: int | None = None

    def _build_run_task_message(self) -> dict[str, Any]:
        return {
            "header": {
                "action": "run-task",
                "task_id": self.task_id,
                "streaming": "duplex",
            },
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": self.model,
                "parameters": {
                    "format": self.format,
                    "sample_rate": self.sample_rate,
                    "max_sentence_silence": self.max_sentence_silence,
                    "semantic_punctuation_enabled": self.semantic_punctuation_enabled,
                },
                "input": {},
            },
        }

    async def start(self) -> bool:
        if self._retry_not_before and time.monotonic() < self._retry_not_before:
            logger.warning(f"[{TAG}] retry cooldown active until {self._retry_not_before}")
            if self.on_error:
                try:
                    self.on_error("ASR 重连冷却中，请稍后再试")
                except Exception:
                    pass
            return False
        for attempt in range(1, self.max_retries + 1):
            if self._closed:
                return False
            self.task_id = uuid.uuid4().hex[:32]
            self.started_event.clear()
            self.server_ready = False
            self.submitted = False
            self.stopping = False
            self._task_finished_cleanly = False
            self._finish_done_event.clear()
            self._finish_callback_fired = False
            self.pcm_buffer.clear()
            self.final_text = None
            self._sentence_keys.clear()
            self.final_sentence_count = 0
            self.last_sentence_end_time = None
            try:
                attempt_started = time.monotonic()
                headers = {"Authorization": f"Bearer {self.api_key}"}
                self.ws = await websockets.connect(
                    self.ws_url,
                    additional_headers=headers,
                    max_size=100_000_000,
                    open_timeout=self.connect_timeout,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                )
                self.forward_task = asyncio.create_task(self._forward())
                await self.ws.send(json.dumps(self._build_run_task_message()))
                remaining = max(1.0, self.connect_timeout - (time.monotonic() - attempt_started))
                await asyncio.wait_for(self.started_event.wait(), timeout=remaining)
                logger.info(f"[{TAG}] session started task={mask_id(self.task_id)}")
                return True
            except Exception as exc:
                logger.warning(f"[{TAG}] start failed attempt={attempt}/{self.max_retries} error={exc}")
                await self._cleanup(send_finish=False)
                if attempt < self.max_retries:
                    await asyncio.sleep(0.5 * attempt)
        self._retry_not_before = time.monotonic() + self.retry_cooldown
        if self.on_error:
            try:
                self.on_error("ASR 连接失败，已达最大重试次数")
            except Exception:
                pass
        return False

    async def send_pcm(self, pcm: bytes) -> None:
        if not self.ws or not self.server_ready or self.stopping:
            return
        self.pcm_buffer.extend(pcm)
        while len(self.pcm_buffer) >= PCM_CHUNK_BYTES:
            chunk = bytes(self.pcm_buffer[:PCM_CHUNK_BYTES])
            del self.pcm_buffer[:PCM_CHUNK_BYTES]
            try:
                await self.ws.send(chunk)
            except Exception as exc:
                logger.warning(f"[{TAG}] send_pcm error={exc}")
                raise

    async def begin_finish(self) -> None:
        """本地确认回合结束后主动结束：发送官方 finish-task 指令。

        发送后不关闭连接：后台 waiter 继续接收剩余 result-generated（最终分句）
        与 task-finished（实测约 +50ms），完成后回调 on_finish_done(ok) 恰好一次。
        幂等：二次调用直接返回。
        """
        if self._finish_requested or not self.ws or self._closed:
            self._finish_requested = True
            return
        self._finish_requested = True
        # 停止接收新音频（send_pcm 门控），flush 缓冲后发送 finish-task
        self.stopping = True
        ws = self.ws
        if self.pcm_buffer:
            try:
                await ws.send(bytes(self.pcm_buffer))
            except Exception as exc:
                logger.warning(f"[{TAG}] finish flush pcm error={exc}")
            self.pcm_buffer.clear()
        try:
            finish_msg = {
                "header": {
                    "action": "finish-task",
                    "task_id": self.task_id,
                    "streaming": "duplex",
                },
                "payload": {"input": {}},
            }
            await ws.send(json.dumps(finish_msg))
            logger.info(f"[{TAG}] finish-task sent task={mask_id(self.task_id)}")
        except Exception as exc:
            logger.warning(f"[{TAG}] send finish-task error={exc}")
        self._finish_waiter = asyncio.create_task(self._wait_finish_done())

    async def _wait_finish_done(self) -> None:
        """等待协议完成事件，不等待 WebSocket 关闭握手。"""
        ok = False
        cancelled = False
        try:
            await asyncio.wait_for(self._finish_done_event.wait(), timeout=self.finish_timeout)
            ok = self._task_finished_cleanly
        except asyncio.TimeoutError:
            logger.warning(
                f"[{TAG}] finish wait timeout={self.finish_timeout}s task={mask_id(self.task_id)}"
            )
        except asyncio.CancelledError:
            cancelled = True
            ok = self._task_finished_cleanly
        except Exception as exc:
            logger.warning(f"[{TAG}] finish wait error={exc}")
        self._fire_finish_done(ok)
        if cancelled:
            raise

    def _fire_finish_done(self, ok: bool) -> None:
        if self._finish_callback_fired:
            return
        self._finish_callback_fired = True
        if self.on_finish_done:
            try:
                self.on_finish_done(ok)
            except Exception as exc:
                logger.error(f"[{TAG}] on_finish_done error={exc}")

    async def finish(self) -> None:
        """优雅收尾：确保 finish-task 已发送，有界等待最终结果后清理连接。

        正常流程下提交发生在 on_finish_done 之后（waiter 已完成），此处瞬时返回；
        兜底路径（max_turn/暂停）最多等待 1 秒，避免 close 握手阻塞主流程
        （旧实现 close 超时 3s 曾串行拖慢 LLM/TTS 启动）。
        """
        if not self._finish_requested:
            await self.begin_finish()
        waiter = self._finish_waiter
        if waiter is not None and not waiter.done():
            try:
                await asyncio.wait_for(asyncio.shield(waiter), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass
        await self._cleanup(send_finish=False)

    async def stop(self) -> None:
        self.stopping = True
        await self._cleanup(send_finish=False)

    async def _forward(self) -> None:
        current_task = asyncio.current_task()
        try:
            while self.ws and not self._closed:
                try:
                    msg = await asyncio.wait_for(self.ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if isinstance(msg, bytes):
                    continue
                result = json.loads(msg)
                header = result.get("header") or {}
                payload = result.get("payload") or {}
                event = header.get("event", "")

                if event == "task-started":
                    self.server_ready = True
                    self.started_event.set()
                    continue

                if event == "result-generated":
                    output = payload.get("output") or {}
                    sentence = output.get("sentence") or {}
                    text = str(sentence.get("text") or "").strip()
                    sentence_end = bool(sentence.get("sentence_end", False))
                    begin_time = sentence.get("begin_time")
                    end_time = sentence.get("end_time")
                    if not text:
                        continue
                    if not sentence_end:
                        # 非空中间结果：仅作活动信号（不含正文），不提交字幕/LLM/TTS。
                        # 轻声低于本地 RMS 阈值时，这是阻止提前提交的唯一信号。
                        if self.on_activity:
                            try:
                                self.on_activity()
                            except Exception as exc:
                                logger.error(f"[{TAG}] on_activity error={exc}")
                        continue
                    # 按 sentence_id 去重（服务端无该字段时退化为 begin_time）
                    raw_key = sentence.get("sentence_id")
                    key = str(raw_key) if raw_key is not None else f"b{begin_time}"
                    if key in self._sentence_keys:
                        continue
                    self._sentence_keys.add(key)
                    self.final_sentence_count += 1
                    self.last_sentence_end_time = end_time
                    logger.info(
                        f"[{TAG}] sentence#{self.final_sentence_count} key={key[:8]}"
                        f" begin_time={begin_time} end_time={end_time} text_length={len(text)}"
                    )
                    if self.on_sentence:
                        try:
                            self.on_sentence(
                                {
                                    "sentence_key": key,
                                    "begin_time": begin_time,
                                    "end_time": end_time,
                                    "text": text,
                                }
                            )
                        except Exception as exc:
                            logger.error(f"[{TAG}] on_sentence error={exc}")
                    # 不 break：分句结束不等于任务结束，继续接收后续分句
                    continue

                if event == "task-failed":
                    logger.error(
                        f"[{TAG}] task-failed code={header.get('error_code')} msg={header.get('error_message')}"
                    )
                    self._finish_done_event.set()
                    break

                if event == "task-finished":
                    self._task_finished_cleanly = True
                    self._finish_done_event.set()
                    break
        except websockets.ConnectionClosed as exc:
            logger.warning(f"[{TAG}] connection closed code={exc.code}")
        except Exception as exc:
            logger.error(f"[{TAG}] forward error={exc}")
        finally:
            # 断线和协议异常也必须立即唤醒 finish waiter；ok 由 clean 标志决定。
            self._finish_done_event.set()
            if not self.submitted and not self.stopping and not self._closed:
                if self.on_close:
                    try:
                        self.on_close()
                    except Exception:
                        pass
            await self._cleanup(send_finish=False, current_task=current_task)

    async def _cleanup(self, send_finish: bool, current_task: asyncio.Task | None = None) -> None:
        ws = self.ws
        self.ws = None
        self.server_ready = False
        self.stopping = True
        # 不取消 _finish_waiter：waiter 有自身超时且保证回调恰一次；
        # 此处取消会在 task-finished 到达与回调之间制造竞态，导致提交信号丢失
        if ws:
            try:
                if send_finish and self.task_id and not self._finish_requested:
                    finish_msg = {
                        "header": {
                            "action": "finish-task",
                            "task_id": self.task_id,
                            "streaming": "duplex",
                        },
                        "payload": {"input": {}},
                    }
                    await ws.send(json.dumps(finish_msg))
                # close 超时从 3s 收紧到 1.5s：task-finished 后服务端主动断开，
                # close 通常瞬时完成；过长超时会串行阻塞主流程（历史 3s 卡顿根因）
                await asyncio.wait_for(ws.close(), timeout=1.5)
            except Exception:
                pass
        task = self.forward_task
        self.forward_task = None
        if task and task is not current_task and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass


class DummyASRAdapter(QwenASRAdapter):
    """Test adapter that yields a fixed final text after receiving a few seconds of audio."""

    def __init__(self, final_text: str = "你好，这是一个测试。", **kwargs: Any) -> None:
        self.on_final = kwargs.get("on_final")
        self.on_close = kwargs.get("on_close")
        self.on_error = kwargs.get("on_error")
        self.model = "dummy"
        self.ws_url = ""
        self.format = "pcm"
        self.sample_rate = 16000
        self.max_sentence_silence = 1600
        self.semantic_punctuation_enabled = False
        self.max_retries = 1
        self.connect_timeout = 5.0
        self.retry_cooldown = 1.0
        self.api_key = "dummy"
        self.ws = None
        self.task_id = None
        self.forward_task = None
        self.started_event = asyncio.Event()
        self.server_ready = False
        self.submitted = False
        self.stopping = False
        self.pcm_buffer = bytearray()
        self.final_text = None
        self._closed = False
        self._retry_not_before = 0.0
        self._dummy_text = final_text
        self._bytes_received = 0
        self._dummy_task: asyncio.Task | None = None

    async def start(self) -> bool:
        if self._retry_not_before and time.monotonic() < self._retry_not_before:
            return False
        self.server_ready = True
        self.started_event.set()
        self.submitted = False
        self.final_text = None
        self._bytes_received = 0
        return True

    async def send_pcm(self, pcm: bytes) -> None:
        self._bytes_received += len(pcm)
        if self._dummy_task is None and self._bytes_received > 6400:
            self._dummy_task = asyncio.create_task(self._emit_after_delay())

    async def _emit_after_delay(self) -> None:
        await asyncio.sleep(1.0)
        if not self.submitted and not self.stopping:
            self.final_text = self._dummy_text
            self.submitted = True
            if self.on_final:
                self.on_final(self._dummy_text)

    async def finish(self) -> None:
        if not self.submitted and not self.stopping:
            self.final_text = self._dummy_text
            self.submitted = True
            if self.on_final:
                self.on_final(self._dummy_text)
        await self.stop()

    async def begin_finish(self) -> None:
        """主动结束（Dummy）：立即产出最终文本并回调 on_finish_done。"""
        if not self.submitted and not self.stopping:
            self.final_text = self._dummy_text
            self.submitted = True
            if self.on_final:
                self.on_final(self._dummy_text)
        if getattr(self, "on_finish_done", None):
            try:
                self.on_finish_done(True)
            except Exception:
                pass

    async def stop(self) -> None:
        self.stopping = True
        if self._dummy_task and not self._dummy_task.done():
            self._dummy_task.cancel()
            try:
                await self._dummy_task
            except BaseException:
                pass
