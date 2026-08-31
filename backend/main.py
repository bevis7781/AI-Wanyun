from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.config import PROJECT_ROOT, get_config
from backend.logger import get_logger, redact_exc, setup_logging
from backend.session import ConversationSession

SERVICE_NAME = "ai-wanyun"
LIVETALKING_HEALTH_PATH = "/api/admin/sessions"

logger = get_logger()
setup_logging()

app = FastAPI(title="Voice Client")
FRONTEND_DIR = PROJECT_ROOT / "frontend"

# Single global session for this single-user MVP
_session: ConversationSession | None = None
_session_lock = asyncio.Lock()
_startup_task: asyncio.Task | None = None


async def get_session() -> ConversationSession:
    global _session
    if _session is None:
        async with _session_lock:
            if _session is None:
                _session = ConversationSession()
                await _session.start_tts_consumer()
                await _session.start_pcm_sender()
    return _session


def _broadcast(state: dict[str, Any]) -> None:
    for ws in list(_active_websockets):
        asyncio.create_task(_safe_send(ws, state))


_active_websockets: set[WebSocket] = set()


async def _safe_send(ws: WebSocket, message: dict[str, Any]) -> None:
    try:
        await ws.send_json(message)
    except Exception:
        _active_websockets.discard(ws)


@app.on_event("startup")
async def on_startup() -> None:
    global _startup_task
    logger.info("Voice client starting up")
    cfg = get_config()
    logger.info(f"host={cfg.app.get('host')} port={cfg.app.get('port')}")
    # 在终端展示非敏感的就绪状态（仅布尔项/字段名，不输出密钥值）
    readiness = cfg.readiness()
    for name, present in readiness["secrets_present"].items():
        if not present:
            logger.warning(f"readiness: secret missing: {name}")
    for name, present in readiness["config_present"].items():
        if not present:
            logger.warning(f"readiness: config missing: {name}")
    _startup_task = asyncio.create_task(_log_livetalking_readiness())


async def _log_livetalking_readiness() -> None:
    """启动后探测本地 LiveTalking，把结果打印到终端（非敏感）。"""
    await asyncio.sleep(1.5)
    try:
        cfg = get_config()
        url = str(cfg.livetalking.get("http_url") or "http://127.0.0.1:8010")
        if await probe_livetalking(url):
            logger.info("readiness: livetalking_reachable=true")
        else:
            logger.warning("readiness: livetalking_reachable=false")
    except Exception as exc:
        logger.warning(f"readiness: livetalking probe error: {redact_exc(exc)}")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    global _session, _startup_task
    if _startup_task is not None and not _startup_task.done():
        _startup_task.cancel()
    _startup_task = None
    if _session:
        await _session.shutdown()
        _session = None


async def probe_livetalking(base_url: str, timeout: float = 3.0) -> bool:
    """直接探测 LiveTalking 本地健康接口。

    - 不依赖任何 ConversationSession / 适配器实例
    - trust_env=False：禁止继承系统/环境代理，避免 localhost 探测被代理干扰
    - HTTP 200 且 JSON code == 0 视为可达
    """
    url = f"{base_url.rstrip('/')}{LIVETALKING_HEALTH_PATH}"
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return False
            data = resp.json()
            return isinstance(data, dict) and data.get("code") == 0
    except Exception as exc:
        logger.warning(f"livetalking probe failed: {redact_exc(exc)}")
        return False


@app.get("/health")
async def health() -> JSONResponse:
    """进程存活检查：只表明本项目进程在运行。

    - 不创建 ConversationSession，不访问任何外部 API
    """
    return JSONResponse({"ok": True, "service": SERVICE_NAME})


@app.get("/ready")
async def readiness() -> JSONResponse:
    """本地运行条件检查：密钥/配置存在 + LiveTalking 可达。

    - 只返回布尔诊断，不输出任何密钥值或异常细节
    - 不创建人物会话，不调用 ASR/LLM/TTS 收费接口
    - LiveTalking 探测独立于全局 session 中的适配器实例
    """
    try:
        cfg = get_config()
        readiness = cfg.readiness()
        base_url = str(cfg.livetalking.get("http_url") or "http://127.0.0.1:8010")
        readiness["livetalking_reachable"] = await probe_livetalking(base_url)
        ok = (
            readiness["all_secrets_present"]
            and readiness["all_config_present"]
            and readiness["livetalking_reachable"]
        )
        readiness["ok"] = ok
        return JSONResponse(readiness, status_code=200 if ok else 503)
    except Exception as exc:
        logger.warning(f"readiness check error: {redact_exc(exc)}")
        return JSONResponse({"ok": False}, status_code=503)


@app.post("/api/session/close")
async def close_session() -> JSONResponse:
    try:
        sess = await get_session()
        # stop_current_streams 末尾已关闭远端 LiveTalking 会话，无需重复调用
        await sess.stop_current_streams()
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.error(f"session close error={redact_exc(exc)}")
        return JSONResponse({"ok": False, "error": "internal_error"}, status_code=500)


@app.get("/diagnostics")
async def diagnostics_page() -> HTMLResponse:
    path = FRONTEND_DIR / "diagnostics.html"
    if path.exists():
        return HTMLResponse(path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Diagnostics page not found</h1>", status_code=404)


@app.get("/api/diagnostics")
async def diagnostics_data() -> JSONResponse:
    sess = await get_session()
    return JSONResponse(sess.diagnostics())


def _valid_lt_url(value: str) -> bool:
    """G8-B：LiveTalking URL 校验。

    必须 http/https、存在 host、不含 username/password、不含 fragment；
    其余视为明显无效，拒绝暴露给前端。
    """
    if not value or not isinstance(value, str):
        return False
    try:
        parsed = urllib.parse.urlparse(value)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.hostname:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.fragment:
        return False
    return True


@app.get("/api/runtime-config")
async def runtime_config() -> JSONResponse:
    """G8-B：前端 LiveTalking 运行时配置单一事实源。

    数据唯一来源 get_config().livetalking，只返回 livetalking_url / avatar_id
    两个业务字段；绝不返回 API Key / Secret / Token / ASR / LLM / TTS 凭据
    或其他 secrets。无效配置返回 503，只暴露 runtime_config_invalid。
    """
    try:
        cfg = get_config()
        lt = cfg.livetalking
        url = str(lt.get("http_url") or "")
        avatar_id = str(lt.get("avatar_id") or "").strip()
        if not _valid_lt_url(url) or not avatar_id:
            return JSONResponse({"ok": False, "error": "runtime_config_invalid"}, status_code=503)
        return JSONResponse({"livetalking_url": url, "avatar_id": avatar_id})
    except Exception as exc:
        logger.warning(f"runtime-config error={redact_exc(exc)}")
        return JSONResponse({"ok": False, "error": "runtime_config_invalid"}, status_code=503)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    _active_websockets.add(ws)
    sess = await get_session()

    def on_state(state: str) -> None:
        _broadcast({"type": "state", "state": state})

    def on_caption(caption: dict[str, Any]) -> None:
        _broadcast({"type": "caption", **caption})

    sess.on_state_change = on_state
    sess.on_caption = on_caption

    # Send current state immediately
    await _safe_send(ws, {"type": "state", "state": sess.state})

    try:
        while True:
            message = await ws.receive()
            msg_type = message.get("type", "")
            # 新版 Starlette: 断开时 receive 返回 {"type": "websocket.disconnect"} dict
            # 而不是抛 WebSocketDisconnect；不检查会二次 receive 抛 RuntimeError
            if msg_type == "websocket.disconnect":
                logger.info("WebSocket disconnected")
                break
            if "bytes" in message:
                await sess.handle_pcm(message["bytes"])
            elif "text" in message:
                try:
                    data = json.loads(message["text"])
                except json.JSONDecodeError:
                    continue
                await _handle_command(ws, sess, data)
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception as exc:
        logger.error(f"WebSocket error={exc}")
    finally:
        _active_websockets.discard(ws)
        if not _active_websockets:
            # 最后一个浏览器连接断开：停止当前对话流（取消 turn 与待聚合分句、
            # 停 ASR/TTS/PCM 队列、关闭远端 LiveTalking 会话），状态回 paused。
            # 应用继续运行；重新打开页面可重建会话并正常对话。
            # 仍有其他浏览器连接时不清理，避免误杀在用连接。
            logger.info("last browser websocket closed, stopping current streams")
            try:
                await sess.stop_current_streams()
            except Exception as exc:
                logger.error(f"stream cleanup after last disconnect failed={exc}")


async def _handle_command(ws: WebSocket, sess: ConversationSession, data: dict[str, Any]) -> None:
    cmd = data.get("type")
    if cmd == "sessionid":
        session_id = str(data.get("sessionId", ""))
        if session_id:
            await sess.set_session_id(session_id)
            await _safe_send(ws, {"type": "status", "text": "真人角色已绑定"})
    elif cmd == "start":
        # resume_id 仅用于 voice-resume 链路 metadata 关联（instrumentation），
        # 不参与任何控制逻辑；前端未携带时后端自生成。
        await sess.start(resume_id=data.get("resume_id"))
    elif cmd == "pause":
        await sess.pause()
    elif cmd == "text":
        # G8-A：文字消息 ACK 协议。请求必须携带非空 request_id 与字符串 text；
        # 缺失/非法请求结构 → invalid_request；内部异常 → internal_error。
        # ACK 在服务端原子准入完成后立即返回，不等待 LLM/TTS/LiveTalking。
        request_id = data.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            await _safe_send(ws, {
                "type": "text_ack",
                "request_id": str(request_id) if isinstance(request_id, str) else "",
                "accepted": False,
                "reason": "invalid_request",
            })
            return
        text = data.get("text")
        if not isinstance(text, str):
            await _safe_send(ws, {
                "type": "text_ack",
                "request_id": request_id,
                "accepted": False,
                "reason": "invalid_request",
            })
            return
        try:
            accepted, reason = await sess.submit_text(request_id, text)
        except Exception as exc:
            # 不把内部 traceback / 密钥 / 配置细节发给前端
            logger.error(f"text submit error={redact_exc(exc)}")
            await _safe_send(ws, {
                "type": "text_ack",
                "request_id": request_id,
                "accepted": False,
                "reason": "internal_error",
            })
            return
        ack: dict[str, Any] = {"type": "text_ack", "request_id": request_id, "accepted": accepted}
        if not accepted:
            ack["reason"] = reason or "invalid_request"
        await _safe_send(ws, ack)
    elif cmd == "interrupt":
        await sess.interrupt()
    elif cmd == "reset":
        await sess.reset_error()
    elif cmd == "clear":
        await sess.clear_history()
    elif cmd == "ping":
        await _safe_send(ws, {"type": "pong"})


# Static files (index.html served explicitly to avoid caching issues)
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    cfg = get_config()
    host = cfg.app.get("host", "127.0.0.1")
    port = int(cfg.app.get("port", 7870))
    uvicorn.run("backend.main:app", host=host, port=port, reload=False)
