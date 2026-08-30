"""G8 前端 contract 测试（源码级静态契约 + node 真实行为测试）。

项目没有 JS 测试运行时，遵循 G8 任务书 §29“不要引入新的大型前端测试框架”：
- 静态契约：直接读取 frontend/app.js 源码，断言 G8 行为要求的结构真实存在、
  生产硬编码已消失、IME / Shift+Enter 等既有体验保持。
- node 真实行为：以最小 DOM/网络 mock 在 node 中执行 app.js 的真实函数
  （trySendText / handleTextAck / restoreTextDraft / setState），验证：
  ws.send() 同步抛错 → 草稿只恢复一次（A，不是 AA）；
  accepted / rejected / stale ACK / setState 等路径的真实语义。
  node 或子进程不可用时自动跳过，静态契约仍完整运行。
"""

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "frontend" / "app.js"
APP_CSS = ROOT / "frontend" / "app.css"

NODE_CMD = shutil.which("node")

# 最小浏览器环境 mock：足以让 app.js 顶层求值（init 因 runtime-config fetch
# 拒绝而提前返回）并驱动生产 trySendText / handleTextAck / restoreTextDraft /
# setState / handleButton / connectLiveTalking / ontrack handler。
_NODE_STUBS = """
const __els = {};
function __makeEl() {
  const listeners = {};
  return {
    value: '', textContent: '', title: '', srcObject: null, muted: true,
    __listeners: listeners, __playCalls: 0, __playImpl: null,
    offsetWidth: 0, scrollHeight: 0, style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) {
      listeners[type] = (listeners[type] || []).filter((item) => item !== fn);
    },
    dispatchEvent(event) {
      for (const fn of (listeners[event.type] || [])) fn(event);
    },
    setAttribute() {},
    appendChild() {}, querySelector() { return __makeEl(); },
    play() {
      this.__playCalls += 1;
      return this.__playImpl ? this.__playImpl() : Promise.resolve();
    },
    getBoundingClientRect() { return { top: 0 }; },
    firstChild: null, isConnected: false,
  };
}
const document = {
  body: { dataset: {} },
  getElementById(id) { if (!__els[id]) __els[id] = __makeEl(); return __els[id]; },
  addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
  createElement() { return __makeEl(); },
};
const window = { addEventListener() {}, removeEventListener() {} };
const location = { host: '127.0.0.1:7870' };
let fetch = () => Promise.reject(new Error('runtime-config server absent (test)'));
const WebSocket = { OPEN: 1 };
let __getUserMediaCalls = 0;
const __sources = [];
const __worklets = [];
const navigator = {
  mediaDevices: {
    getUserMedia() {
      __getUserMediaCalls += 1;
      return Promise.resolve({ getTracks() { return []; } });
    },
  },
};
class AudioContext {
  constructor() {
    this.state = 'running';
    this.audioWorklet = { addModule() { return Promise.resolve(); } };
  }
  createMediaStreamSource() {
    const source = {
      connectedTo: null, connectCalls: 0, disconnectCalls: 0,
      disconnectedTarget: null,
      connect(target) { this.connectedTo = target; this.connectCalls += 1; },
      disconnect(target) {
        this.disconnectedTarget = target;
        this.connectedTo = null;
        this.disconnectCalls += 1;
      },
    };
    __sources.push(source);
    return source;
  }
  resume() { return Promise.resolve(); }
}
class AudioWorkletNode {
  constructor() {
    this.port = { onmessage: null };
    this.disconnectCalls = 0;
    __worklets.push(this);
  }
  disconnect() { this.disconnectCalls += 1; }
}
class MediaStream {
  constructor() { this.tracks = []; }
  addTrack(track) { this.tracks.push(track); }
  getTracks() { return this.tracks; }
}
class RTCPeerConnection {
  constructor() {
    this.iceGatheringState = 'complete';
    this.connectionState = 'connected';
    this.localDescription = { type: 'offer', sdp: 'offer' };
  }
  addTransceiver() {}
  createOffer() { return Promise.resolve({ type: 'offer', sdp: 'offer' }); }
  setLocalDescription(offer) { this.localDescription = offer; return Promise.resolve(); }
  setRemoteDescription() { return Promise.resolve(); }
  close() { return Promise.resolve(); }
  addEventListener() {}
  removeEventListener() {}
}
"""


def _src() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _between(src: str, begin_marker: str, end_marker: str) -> str:
    # 定位整条 marker 注释（含注释起始），取 BEGIN 注释之后、END 注释之前的内容
    begin = src.index(f"/* {begin_marker}")
    begin_end = src.index("*/", begin) + 2  # 完整吃掉 BEGIN 注释
    end = src.index(f"/* {end_marker}")
    return src[begin_end:end]


class TestG8FrontendStaticContract(unittest.TestCase):
    """G8-A / G8-B 前端源码级契约。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _src()

    def test_no_production_lt_url_hardcode(self):
        self.assertNotIn("LT_URL", self.src)
        self.assertNotIn("'http://127.0.0.1:8010'", self.src)
        self.assertNotIn('"http://127.0.0.1:8010"', self.src)

    def test_no_production_avatar_id_hardcode(self):
        self.assertNotIn("AVATAR_ID", self.src)
        self.assertNotIn("'private_avatar_legacy_v1'", self.src)
        self.assertNotIn('"private_avatar_legacy_v1"', self.src)

    def test_runtime_config_loaded_with_no_store(self):
        self.assertIn("loadRuntimeConfig", self.src)
        self.assertIn("ensureRuntimeConfig", self.src)
        self.assertIn("'/api/runtime-config'", self.src)
        self.assertIn("cache: 'no-store'", self.src)

    def test_init_order_runtime_config_then_ws_then_livetalking(self):
        self.assertLess(self.src.index("await ensureRuntimeConfig();"),
                        self.src.index("await ensureWs();"))
        self.assertLess(self.src.index("await ensureWs();"),
                        self.src.index("await connectLiveTalking();"))

    def test_connect_livetalking_ensures_runtime_config(self):
        start = self.src.index("async function connectLiveTalking()")
        end = self.src.index("function waitIce", start)
        body = self.src[start:end]
        self.assertIn("await ensureRuntimeConfig()", body)
        self.assertIn("cfg.livetalking_url", body)
        self.assertIn("cfg.avatar_id", body)

    def test_runtime_config_failure_not_cached_for_retry(self):
        # 失败路径必须复位 runtimeConfigPromise，允许后续重试
        start = self.src.index("function loadRuntimeConfig()")
        end = self.src.index("async function ensureRuntimeConfig()", start)
        body = self.src[start:end]
        self.assertIn("runtimeConfigPromise = null", body)
        self.assertIn("throw err", body)

    def test_pending_draft_single_and_request_id(self):
        self.assertIn("textPending", self.src)
        self.assertIn("genRequestId", self.src)
        self.assertIn("request_id", self.src)

    def test_text_unlock_starts_before_first_await(self):
        start = self.src.index("async function trySendText()")
        end = self.src.index("if (elInput) {", start)
        body = self.src[start:end]
        unlock_idx = body.index("ensureAvatarMediaPlayback('text')")
        await_idx = body.index("await ensureWs()")
        self.assertLess(unlock_idx, await_idx)
        self.assertNotIn("getUserMedia", body)

    def test_avatar_playback_failures_are_observable_and_retryable(self):
        self.assertIn("avatarPlaybackUnlockRequested", self.src)
        self.assertIn("reportAvatarPlaybackFailure", self.src)
        self.assertIn("console.warn", self.src)
        self.assertIn("trigger=${trigger}", self.src)
        self.assertNotIn("elVideo.play().catch(() => {})", self.src)

    def test_ontrack_retries_when_text_unlock_intent_exists(self):
        start = self.src.index("pc.ontrack =")
        end = self.src.index("pc.onconnectionstatechange", start)
        body = self.src[start:end]
        self.assertIn("avatarPlaybackUnlockRequested", body)
        self.assertIn("ensureAvatarMediaPlayback('ontrack')", body)

    def test_ack_timeout_within_8_to_10_seconds(self):
        m = re.search(r"TEXT_ACK_TIMEOUT_MS\s*=\s*(\d+)", self.src)
        self.assertIsNotNone(m, "TEXT_ACK_TIMEOUT_MS constant missing")
        timeout = int(m.group(1))
        self.assertTrue(8000 <= timeout <= 10000, f"timeout={timeout}")

    def test_send_payload_contains_request_id(self):
        start = self.src.index("async function trySendText()")
        end = self.src.index("if (elInput) {", start)
        body = self.src[start:end]
        self.assertIn("request_id: requestId", body)

    def test_pending_blocks_second_send(self):
        start = self.src.index("async function trySendText()")
        end = self.src.index("if (elInput) {", start)
        body = self.src[start:end]
        self.assertIn("textPending", body)
        # 已有 pending 时不得发送第二条
        self.assertRegex(body, r"if\s*\(!TEXT_SENDABLE\.has\(state\)\s*\|\|\s*textPending\)")

    def test_textarea_cleared_before_send(self):
        # Codex Major 修复：视觉清空必须先于 ws.send()，send 同步抛错时
        # 恢复逻辑（merge('', A) === A）才不会把 A 拼成 AA。
        start = self.src.index("async function trySendText()")
        end = self.src.index("if (elInput) {", start)
        body = self.src[start:end]
        clear_idx = body.index("elInput.value = '';")
        send_idx = body.index("ws.send(JSON.stringify")
        self.assertLess(clear_idx, send_idx, "elInput.value 清空必须发生在 ws.send 之前")

    def test_set_state_does_not_clear_pending(self):
        start = self.src.index("function setState(")
        end = self.src.index("async function ensureWs()", start)
        body = self.src[start:end]
        self.assertNotIn("textPending", body)
        self.assertNotIn("restoreTextDraft", body)

    def test_accepted_ack_does_not_touch_textarea(self):
        start = self.src.index("function handleTextAck(")
        end = self.src.index("function autoGrowInput()", start)
        body = self.src[start:end]
        rejected_idx = body.index("if (data.accepted === false)")
        accepted_part = body[:rejected_idx]
        # accepted 分支只清 pending/timer，绝不写 textarea（用户可能已输入 B）
        self.assertNotIn("elInput.value", accepted_part)

    def test_rejected_ack_merges_draft_a_plus_b(self):
        start = self.src.index("function handleTextAck(")
        end = self.src.index("function autoGrowInput()", start)
        body = self.src[start:end]
        self.assertIn("mergeTextDraft(elInput.value, pending.text)", body)

    def test_stale_ack_ignored(self):
        start = self.src.index("function handleTextAck(")
        end = self.src.index("function autoGrowInput()", start)
        body = self.src[start:end]
        self.assertIn("isStaleTextAck(data.request_id, pending)", body)

    def test_timeout_restores_draft(self):
        start = self.src.index("async function trySendText()")
        end = self.src.index("if (elInput) {", start)
        body = self.src[start:end]
        self.assertIn("TEXT_ACK_TIMEOUT_MS", body)
        self.assertIn("restoreTextDraft()", body)

    def test_disconnect_restores_draft(self):
        start = self.src.index("ws.onclose = ")
        end = self.src.index("ws.onerror", start)
        body = self.src[start:end]
        self.assertIn("restoreTextDraft()", body)

    def test_send_throw_restores_draft(self):
        start = self.src.index("async function trySendText()")
        end = self.src.index("if (elInput) {", start)
        body = self.src[start:end]
        self.assertIn("try {", body)
        self.assertIn("ws.send(JSON.stringify", body)
        self.assertIn("catch (err)", body)
        self.assertIn("restoreTextDraft()", body)

    def test_ime_guards_preserved(self):
        for marker in ("compositionstart", "compositionend", "isComposing", "keyCode === 229"):
            self.assertIn(marker, self.src, f"IME guard {marker!r} missing")

    def test_shift_enter_preserved(self):
        self.assertIn("e.shiftKey", self.src)

    def test_pending_keeps_textarea_editable(self):
        # pending 期间不得锁定/禁用输入框
        self.assertNotIn("elInput.readonly", self.src)
        self.assertNotIn("elInput.disabled", self.src)
        self.assertIn("elInput.addEventListener('input'", self.src)

    def test_microphone_cleanup_invalidates_old_audio_graph(self):
        self.assertIn("let mediaSourceNode = null", self.src)
        self.assertIn("src.disconnect(node)", self.src)
        self.assertIn("node.port.onmessage = null", self.src)
        self.assertIn("if (workletNode !== node || mediaSourceNode !== src) return", self.src)


@unittest.skipUnless(NODE_CMD, "node not available on PATH")
class TestG8TextPureLogicNode(unittest.TestCase):
    """提取 app.js 内 G8-TEXT-PURE 纯函数区间，在 node 中验证行为。"""

    @classmethod
    def setUpClass(cls):
        src = _src()
        cls.pure = _between(src, "G8-TEXT-PURE-BEGIN", "G8-TEXT-PURE-END")

    def _run_node(self, checks):
        lines = [self.pure]
        for i, check in enumerate(checks):
            lines.append(f"console.log('check{i}|true|' + ({check}));")
        script = "\n".join(lines)
        try:
            proc = subprocess.run(
                [NODE_CMD, "-e", script], capture_output=True, text=True, timeout=30
            )
        except Exception as exc:  # 沙箱/环境限制：跳过，静态契约已覆盖
            self.skipTest(f"node subprocess unavailable: {exc}")
        self.assertEqual(proc.returncode, 0, f"node exited {proc.returncode}: {proc.stderr}")
        for line in proc.stdout.strip().splitlines():
            name, expected, actual = line.split("|")
            self.assertEqual(actual, expected, name)

    def test_merge_text_draft(self):
        self._run_node([
            "mergeTextDraft('', 'A') === 'A'",
            "mergeTextDraft('B', 'A') === 'AB'",
            "mergeTextDraft('', '') === ''",
            "mergeTextDraft('B2', 'A1') === 'A1B2'",
        ])

    def test_stale_ack(self):
        self._run_node([
            "isStaleTextAck('x', {requestId: 'y'}) === true",
            "isStaleTextAck('x', null) === true",
            "isStaleTextAck('x', undefined) === true",
            "isStaleTextAck('x', {requestId: 'x'}) === false",
        ])


@unittest.skipUnless(NODE_CMD, "node not available on PATH")
class TestG8TextSendBehaviorNode(unittest.TestCase):
    """真实行为测试：以最小 DOM mock 执行 app.js 真实发送/ACK 逻辑。

    覆盖 Codex 确认的 Major（ws.send 同步抛错 → 草稿只恢复一次，A 而非 AA）
    以及与其共享恢复逻辑的 rejected / timeout / disconnect / accepted / stale /
    setState 路径。
    """

    @classmethod
    def _script(cls, snippet: str) -> str:
        # 不统一追加 process.exit(0)：异步片段须在自身 async 链内退出，
        # 否则会在 await 恢复前被提前退出、RESULT 行来不及打印。
        return _NODE_STUBS + "\n" + _src() + "\n" + snippet + "\n"

    def _run(self, snippet: str) -> dict[str, str]:
        try:
            proc = subprocess.run(
                [NODE_CMD, "-e", self._script(snippet)],
                capture_output=True, text=True, timeout=30,
            )
        except Exception as exc:  # 沙箱/环境限制：跳过
            self.skipTest(f"node subprocess unavailable: {exc}")
        self.assertEqual(
            proc.returncode, 0,
            f"node exited {proc.returncode}:\nstderr={proc.stderr}\nstdout={proc.stdout}",
        )
        results: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("RESULT|"):
                _, name, value = line.split("|", 2)
                results[name] = value
        return results

    def test_send_throw_restores_original_draft_once(self):
        """Codex Major：textarea=A → ws.send() 同步 throw → 最终必须为 A（不是 AA）。"""
        results = self._run('''
const input = document.getElementById('text-input');
input.value = 'A';
state = 'listening';
ws = { readyState: 1, send() { throw new Error('ws send boom (test)'); } };
(async () => {
  await trySendText();
  console.log('RESULT|final|' + input.value);
  console.log('RESULT|pending|' + String(textPending));
  process.exit(0);
})();
''')
        self.assertEqual(results.get("final"), "A", "send throw 后草稿必须只保留一次 A")
        self.assertEqual(results.get("pending"), "null", "send throw 后 pending 必须被清理")

    def test_send_success_clears_textarea_keeps_pending(self):
        results = self._run('''
const input = document.getElementById('text-input');
input.value = 'A';
state = 'listening';
ws = { readyState: 1, send() {} };
(async () => {
  await trySendText();
  console.log('RESULT|final|' + input.value);
  console.log('RESULT|pending_text|' + (textPending ? textPending.text : 'null'));
  console.log('RESULT|pending_rid|' + (textPending ? textPending.requestId : 'null'));
  process.exit(0);
})();
''')
        self.assertEqual(results.get("final"), "", "send 成功后 textarea 视觉清空")
        self.assertEqual(results.get("pending_text"), "A", "send 成功后 pending 内存保留原文 A")
        self.assertTrue(results.get("pending_rid"), "send 成功后 pending 携带 request_id")

    def test_ack_accepted_keeps_user_typed_b(self):
        results = self._run('''
const input = document.getElementById('text-input');
textPending = { requestId: 'r1', text: 'A', timer: setTimeout(() => {}, 9000) };
input.value = 'B';
handleTextAck({ type: 'text_ack', request_id: 'r1', accepted: true });
console.log('RESULT|final|' + input.value);
console.log('RESULT|pending|' + String(textPending));
process.exit(0);
''')
        self.assertEqual(results.get("final"), "B", "accepted 时用户后输入的 B 不能被清掉")
        self.assertEqual(results.get("pending"), "null", "accepted 后 pending 被清理")

    def test_ack_rejected_restores_a(self):
        results = self._run('''
const input = document.getElementById('text-input');
textPending = { requestId: 'r1', text: 'A', timer: setTimeout(() => {}, 9000) };
input.value = '';
handleTextAck({ type: 'text_ack', request_id: 'r1', accepted: false, reason: 'invalid_state' });
console.log('RESULT|final|' + input.value);
process.exit(0);
''')
        self.assertEqual(results.get("final"), "A", "rejected 且无新输入 → 恢复 A")

    def test_ack_rejected_merges_a_plus_b(self):
        results = self._run('''
const input = document.getElementById('text-input');
textPending = { requestId: 'r1', text: 'A', timer: setTimeout(() => {}, 9000) };
input.value = 'B';
handleTextAck({ type: 'text_ack', request_id: 'r1', accepted: false, reason: 'invalid_state' });
console.log('RESULT|final|' + input.value);
process.exit(0);
''')
        self.assertEqual(results.get("final"), "AB", "rejected 且已输入 B → 恢复为 A+B")

    def test_stale_ack_does_not_touch_current_pending(self):
        results = self._run('''
const input = document.getElementById('text-input');
textPending = { requestId: 'r1', text: 'A', timer: setTimeout(() => {}, 9000) };
input.value = 'B';
handleTextAck({ type: 'text_ack', request_id: 'r2', accepted: false, reason: 'invalid_state' });
console.log('RESULT|pending_rid|' + (textPending ? textPending.requestId : 'null'));
console.log('RESULT|final|' + input.value);
process.exit(0);
''')
        self.assertEqual(results.get("pending_rid"), "r1", "stale ACK 不得清当前 pending")
        self.assertEqual(results.get("final"), "B", "stale ACK 不得改当前用户输入")

    def test_set_state_does_not_clear_pending(self):
        results = self._run('''
textPending = { requestId: 'r1', text: 'A', timer: setTimeout(() => {}, 9000) };
setState('thinking');
console.log('RESULT|pending_rid|' + (textPending ? textPending.requestId : 'null'));
process.exit(0);
''')
        self.assertEqual(results.get("pending_rid"), "r1", "setState(thinking) 不是 ACK，不得清 pending")

    def test_restore_text_draft_from_empty_textarea(self):
        # timeout / disconnect 共享的恢复路径：textarea 为空时恢复为 A（只一次）
        results = self._run('''
const input = document.getElementById('text-input');
textPending = { requestId: 'r1', text: 'A', timer: setTimeout(() => {}, 9000) };
input.value = '';
restoreTextDraft();
console.log('RESULT|final|' + input.value);
console.log('RESULT|pending|' + String(textPending));
process.exit(0);
''')
        self.assertEqual(results.get("final"), "A")
        self.assertEqual(results.get("pending"), "null")

    def test_fresh_text_submit_unlocks_remote_video_without_microphone(self):
        results = self._run('''
const input = document.getElementById('text-input');
const video = document.getElementById('avatar-video');
const events = [];
video.srcObject = { getTracks() { return [{ id: 'ready-remote' }]; } };
video.__playImpl = () => { events.push('play'); return Promise.resolve(); };
const sent = [];
input.value = 'A';
state = 'paused';
ws = { readyState: 1, send(payload) { events.push('send'); sent.push(JSON.parse(payload)); } };
(async () => {
  await trySendText();
  await new Promise((resolve) => setImmediate(resolve));
  console.log('RESULT|muted|' + String(video.muted));
  console.log('RESULT|plays|' + video.__playCalls);
  console.log('RESULT|events|' + events.join(','));
  console.log('RESULT|sent|' + sent.length);
  console.log('RESULT|pending|' + (textPending ? textPending.text : 'null'));
  console.log('RESULT|gum|' + __getUserMediaCalls);
  process.exit(0);
})();
''')
        self.assertEqual(results.get("muted"), "false")
        self.assertEqual(results.get("plays"), "1")
        self.assertEqual(results.get("events"), "play,send")
        self.assertEqual(results.get("sent"), "1")
        self.assertEqual(results.get("pending"), "A")
        self.assertEqual(results.get("gum"), "0", "文字路径不得调用 getUserMedia")

    def test_play_rejection_keeps_text_flow_and_retries_next_gesture(self):
        results = self._run('''
const input = document.getElementById('text-input');
const video = document.getElementById('avatar-video');
const warnings = [];
const unhandled = [];
console.warn = (...args) => warnings.push(args.join(' '));
process.on('unhandledRejection', (reason) => unhandled.push(String(reason)));
video.__playImpl = () => Promise.reject(Object.assign(new Error('autoplay blocked'), { name: 'NotAllowedError' }));
input.value = 'A';
state = 'paused';
ws = { readyState: 1, send() {} };
(async () => {
  await trySendText();
  await new Promise((resolve) => setImmediate(resolve));
  const firstPending = textPending ? textPending.text : 'null';
  handleTextAck({ request_id: textPending.requestId, accepted: true });
  input.value = 'B';
  await trySendText();
  await new Promise((resolve) => setImmediate(resolve));
  console.log('RESULT|first_pending|' + firstPending);
  console.log('RESULT|final|' + input.value);
  console.log('RESULT|pending|' + (textPending ? textPending.text : 'null'));
  console.log('RESULT|plays|' + video.__playCalls);
  console.log('RESULT|warnings|' + warnings.length);
  console.log('RESULT|unhandled|' + unhandled.length);
  process.exit(0);
})();
''')
        self.assertEqual(results.get("first_pending"), "A")
        self.assertEqual(results.get("final"), "")
        self.assertEqual(results.get("pending"), "B")
        self.assertEqual(results.get("plays"), "2", "下一次文字手势必须允许重试 play")
        self.assertEqual(results.get("warnings"), "2")
        self.assertEqual(results.get("unhandled"), "0")

    def test_play_sync_throw_keeps_pending_and_reports_error(self):
        results = self._run('''
const input = document.getElementById('text-input');
const video = document.getElementById('avatar-video');
const warnings = [];
const unhandled = [];
console.warn = (...args) => warnings.push(args.join(' '));
process.on('unhandledRejection', (reason) => unhandled.push(String(reason)));
video.__playImpl = () => { throw Object.assign(new Error('sync play failure'), { name: 'PlayError' }); };
input.value = 'A';
state = 'listening';
ws = { readyState: 1, send() {} };
(async () => {
  await trySendText();
  await new Promise((resolve) => setImmediate(resolve));
  console.log('RESULT|final|' + input.value);
  console.log('RESULT|pending|' + (textPending ? textPending.text : 'null'));
  console.log('RESULT|warnings|' + warnings.length);
  console.log('RESULT|unhandled|' + unhandled.length);
  process.exit(0);
})();
''')
        self.assertEqual(results.get("final"), "")
        self.assertEqual(results.get("pending"), "A")
        self.assertEqual(results.get("warnings"), "1")
        self.assertEqual(results.get("unhandled"), "0")

    def test_delayed_track_retries_after_text_unlock_intent(self):
        results = self._run('''
const input = document.getElementById('text-input');
const video = document.getElementById('avatar-video');
const warnings = [];
console.warn = (...args) => warnings.push(args.join(' '));
video.__playImpl = () => video.__playCalls === 1
  ? Promise.reject(new Error('no track yet'))
  : Promise.resolve();
input.value = 'A';
state = 'paused';
ws = { readyState: 1, send() {} };
(async () => {
  await trySendText();
  await new Promise((resolve) => setImmediate(resolve));
  const beforeTrack = video.__playCalls;
  runtimeConfig = { livetalking_url: 'http://test.invalid', avatar_id: 'test-avatar' };
  fetch = () => Promise.resolve({
    ok: true,
    json: async () => ({ sessionid: 'sid', sdp: 'answer', type: 'answer' }),
  });
  await connectLiveTalking();
  const afterConnect = video.__playCalls;
  pc.ontrack({ track: { id: 'delayed-remote-track' } });
  await new Promise((resolve) => setImmediate(resolve));
  console.log('RESULT|before_track|' + beforeTrack);
  console.log('RESULT|after_connect|' + afterConnect);
  console.log('RESULT|after_track|' + video.__playCalls);
  console.log('RESULT|intent|' + String(avatarPlaybackUnlockRequested));
  console.log('RESULT|muted|' + String(video.muted));
  console.log('RESULT|gum|' + __getUserMediaCalls);
  console.log('RESULT|warnings|' + warnings.length);
  process.exit(0);
})();
''')
        self.assertEqual(results.get("before_track"), "1")
        self.assertEqual(results.get("after_connect"), "1")
        self.assertEqual(results.get("after_track"), "2")
        self.assertEqual(results.get("intent"), "true")
        self.assertEqual(results.get("muted"), "false")
        self.assertEqual(results.get("gum"), "0")
        self.assertEqual(results.get("warnings"), "1")

    def test_orb_unlock_does_not_remove_microphone_flow(self):
        results = self._run('''
const video = document.getElementById('avatar-video');
const sent = [];
video.__playImpl = () => Promise.resolve();
state = 'paused';
sessionId = 'sid';
ws = { readyState: 1, send(payload) { sent.push(JSON.parse(payload)); } };
(async () => {
  await handleButton();
  await new Promise((resolve) => setImmediate(resolve));
  console.log('RESULT|muted|' + String(video.muted));
  console.log('RESULT|plays|' + video.__playCalls);
  console.log('RESULT|intent|' + String(avatarPlaybackUnlockRequested));
  console.log('RESULT|gum|' + __getUserMediaCalls);
  console.log('RESULT|start|' + sent.filter((item) => item.type === 'start').length);
  process.exit(0);
})();
''')
        self.assertEqual(results.get("muted"), "false")
        self.assertEqual(results.get("plays"), "1")
        self.assertEqual(results.get("intent"), "true")
        self.assertEqual(results.get("gum"), "1")
        self.assertEqual(results.get("start"), "1")

    def test_send_throw_restores_once_for_media_success_reject_and_throw(self):
        results = self._run('''
const input = document.getElementById('text-input');
const video = document.getElementById('avatar-video');
const modes = ['success', 'reject', 'throw'];
console.warn = () => {};
(async () => {
  for (const mode of modes) {
    input.value = 'A';
    textPending = null;
    audioStarted = false;
    avatarPlaybackUnlockRequested = false;
    state = 'listening';
    video.__playImpl = () => {
      if (mode === 'reject') return Promise.reject(new Error('media reject'));
      if (mode === 'throw') throw new Error('media throw');
      return Promise.resolve();
    };
    ws = { readyState: 1, send() { throw new Error('send throw'); } };
    await trySendText();
    await new Promise((resolve) => setImmediate(resolve));
    console.log('RESULT|' + mode + '|value=' + input.value + ',pending=' + String(textPending));
  }
  process.exit(0);
})();
''')
        for mode in ("success", "reject", "throw"):
            self.assertEqual(
                results.get(mode), "value=A,pending=null",
                f"media {mode} + ws.send throw 不得将草稿恢复成 AA",
            )

    def test_ime_and_shift_enter_event_guards_still_hold(self):
        results = self._run('''
const input = document.getElementById('text-input');
const keydown = input.__listeners.keydown[0];
const sent = [];
let prevented = 0;
state = 'paused';
ws = { readyState: 1, send(payload) { sent.push(JSON.parse(payload)); } };
(async () => {
  input.value = 'IME';
  input.dispatchEvent({ type: 'compositionstart' });
  keydown({ key: 'Enter', shiftKey: false, isComposing: false, keyCode: 13,
    preventDefault() { prevented += 1; } });
  input.dispatchEvent({ type: 'compositionend' });
  const imeValue = input.value;
  input.value = 'Shift';
  keydown({ key: 'Enter', shiftKey: true, isComposing: false, keyCode: 13,
    preventDefault() { prevented += 1; } });
  const shiftValue = input.value;
  input.value = 'Normal';
  keydown({ key: 'Enter', shiftKey: false, isComposing: false, keyCode: 13,
    preventDefault() { prevented += 1; } });
  await new Promise((resolve) => setImmediate(resolve));
  console.log('RESULT|ime|' + imeValue);
  console.log('RESULT|shift|' + shiftValue);
  console.log('RESULT|sent|' + sent.length);
  console.log('RESULT|sent_text|' + (sent[0] ? sent[0].text : 'null'));
  console.log('RESULT|prevented|' + prevented);
  process.exit(0);
})();
''')
        self.assertEqual(results.get("ime"), "IME")
        self.assertEqual(results.get("shift"), "Shift")
        self.assertEqual(results.get("sent"), "1")
        self.assertEqual(results.get("sent_text"), "Normal")
        self.assertEqual(results.get("prevented"), "1")


class TestVoiceResumeTraceContract(unittest.TestCase):
    """R1-INSTRUMENT-VOICE-RESUME 前端静态契约：trace 事件/消息字段必须存在，
    且纯函数区间不得引用任何内容数据（PCM/文本/secret）。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _src()

    def test_trace_pure_markers_present(self):
        self.assertIn("G8-TRACE-PURE-BEGIN", self.src)
        self.assertIn("G8-TRACE-PURE-END", self.src)

    def test_required_frontend_events_present(self):
        for ev in (
            "resume/epoch", "gum/requested", "gum/resolved", "worklet/created",
            "worklet/connected", "worklet/first_frame", "pcm/first_send",
            "pause/cleanup", "ctx/state",
        ):
            self.assertIn(ev, self.src)

    def test_start_message_carries_resume_id(self):
        self.assertIn("resume_id: resumeId", self.src)

    def test_trace_region_logs_no_content(self):
        pure = _between(self.src, "G8-TRACE-PURE-BEGIN", "G8-TRACE-PURE-END")
        for banned in ("textContent", "caption", "transcript", "ev.data",
                       "api_key", "access_token", "secret"):
            self.assertNotIn(banned, pure)


@unittest.skipUnless(NODE_CMD, "node not available on PATH")
class TestVoiceResumeTracePureNode(unittest.TestCase):
    """提取 app.js 内 G8-TRACE-PURE 纯函数区间，在 node 中验证格式与字段白名单。"""

    @classmethod
    def setUpClass(cls):
        cls.pure = _between(_src(), "G8-TRACE-PURE-BEGIN", "G8-TRACE-PURE-END")

    def _run_node(self, checks):
        lines = [self.pure]
        for i, check in enumerate(checks):
            lines.append(f"console.log('check{i}|true|' + ({check}));")
        script = "\n".join(lines)
        try:
            proc = subprocess.run(
                [NODE_CMD, "-e", script], capture_output=True, text=True, timeout=30
            )
        except Exception as exc:
            self.skipTest(f"node subprocess unavailable: {exc}")
        self.assertEqual(proc.returncode, 0, f"node exited {proc.returncode}: {proc.stderr}")
        for line in proc.stdout.strip().splitlines():
            name, expected, actual = line.split("|")
            self.assertEqual(actual, expected, name)

    def test_format_voice_trace_event(self):
        self._run_node([
            "formatVoiceTraceEvent({resumeId:'r1', name:'resume/epoch', tMs:0}) === '[voice-trace] resume=r1 event=resume/epoch t_ms=0'",
            "formatVoiceTraceEvent({resumeId:'r1', name:'pcm/first_send', tMs:5, fields:{bytes:1280, frameIdx:1}}) === '[voice-trace] resume=r1 event=pcm/first_send t_ms=5 bytes=1280 frameIdx=1'",
            "formatVoiceTraceEvent({resumeId:'r1', name:'gum/resolved', tMs:3, fields:{trackCount:1, trackStates:'live'}}) === '[voice-trace] resume=r1 event=gum/resolved t_ms=3 trackCount=1 trackStates=live'",
            "formatVoiceTraceEvent({resumeId:'r1', name:'x', tMs:1, fields:{apiKey:'S', token:'T', text:'hello'}}) === '[voice-trace] resume=r1 event=x t_ms=1'",
            "formatVoiceTraceEvent({resumeId:'r1', name:'x', tMs:1, fields:{}}) === '[voice-trace] resume=r1 event=x t_ms=1'",
        ])


@unittest.skipUnless(NODE_CMD, "node not available on PATH")
class TestVoiceResumeTraceNodeBehavior(unittest.TestCase):
    """真实行为：一次 handleButton resume 必须产生 epoch/gum/worklet trace，
    且 start 消息携带 resume_id（协议向后兼容：原 start 计数不变）。"""

    @classmethod
    def _script(cls, snippet: str) -> str:
        return _NODE_STUBS + "\n" + _src() + "\n" + snippet + "\n"

    def _run(self, snippet: str) -> dict[str, str]:
        try:
            proc = subprocess.run(
                [NODE_CMD, "-e", self._script(snippet)],
                capture_output=True, text=True, timeout=30,
            )
        except Exception as exc:
            self.skipTest(f"node subprocess unavailable: {exc}")
        self.assertEqual(
            proc.returncode, 0,
            f"node exited {proc.returncode}:\nstderr={proc.stderr}\nstdout={proc.stdout}",
        )
        results: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("RESULT|"):
                _, name, value = line.split("|", 2)
                results[name] = value
        return results

    def test_resume_trace_emits_epoch_gum_worklet_and_start_resume_id(self):
        results = self._run('''
const sent = [];
const traces = [];
console.info = (line) => traces.push(line);
state = 'paused';
sessionId = 'sid';
ws = { readyState: 1, send(payload) { sent.push(JSON.parse(payload)); } };
(async () => {
  await handleButton();
  await new Promise((resolve) => setImmediate(resolve));
  const startMsg = sent.find((item) => item.type === 'start');
  console.log('RESULT|epoch|' + traces.filter((l) => l.includes('event=resume/epoch')).length);
  console.log('RESULT|gum_req|' + traces.filter((l) => l.includes('event=gum/requested')).length);
  console.log('RESULT|gum_ok|' + traces.filter((l) => l.includes('event=gum/resolved')).length);
  console.log('RESULT|worklet|' + traces.filter((l) => l.includes('event=worklet/created') || l.includes('event=worklet/connected')).length);
  console.log('RESULT|ctx|' + traces.filter((l) => l.includes('event=ctx/state')).length);
  console.log('RESULT|start_count|' + sent.filter((item) => item.type === 'start').length);
  console.log('RESULT|start_rid|' + String(Boolean(startMsg && startMsg.resume_id)));
  console.log('RESULT|no_pcm|' + traces.every((l) => !l.includes('frameBytes') && !l.includes('pcmData')));
  process.exit(0);
})();
''')
        self.assertEqual(results.get("epoch"), "1")
        self.assertEqual(results.get("gum_req"), "1")
        self.assertEqual(results.get("gum_ok"), "1")
        self.assertEqual(results.get("worklet"), "2")
        self.assertGreaterEqual(int(results.get("ctx") or "0"), 1)
        self.assertEqual(results.get("start_count"), "1")
        self.assertEqual(results.get("start_rid"), "true")
        self.assertEqual(results.get("no_pcm"), "true")

    def test_worklet_first_frame_and_first_pcm_send(self):
        """真实覆盖：首个 worklet frame 事件与首个 PCM WebSocket send 事件
        各恰好触发一次，且 send 已发生（后续帧不再重复打点）。"""
        results = self._run('''
const sent = [];
const traces = [];
console.info = (line) => traces.push(line);
state = 'paused';
sessionId = 'sid';
ws = { readyState: 1, send(payload) { sent.push(payload); } };
(async () => {
  await handleButton();
  await new Promise((resolve) => setImmediate(resolve));
  state = 'listening';
  workletNode.port.onmessage({ data: new ArrayBuffer(640) });
  workletNode.port.onmessage({ data: new ArrayBuffer(640) });
  console.log('RESULT|first_frame|' + traces.filter((l) => l.includes('event=worklet/first_frame')).length);
  console.log('RESULT|first_send|' + traces.filter((l) => l.includes('event=pcm/first_send')).length);
  console.log('RESULT|bytes|' + traces.filter((l) => l.includes('event=pcm/first_send') && l.includes('bytes=640')).length);
  console.log('RESULT|sent|' + sent.length);
  process.exit(0);
})();
''')
        self.assertEqual(results.get("first_frame"), "1")
        self.assertEqual(results.get("first_send"), "1")
        self.assertEqual(results.get("bytes"), "1")
        # 总 send = 1 次 start 消息 + 2 帧 PCM（instrumentation 不改变实际发送行为）
        self.assertEqual(results.get("sent"), "3")

    def test_pause_resume_rebuilds_single_graph_and_rejects_stale_handler(self):
        """真实覆盖 pause cleanup、resume 重建及旧 handler 的排队消息隔离。"""
        results = self._run('''
const sent = [];
state = 'paused';
sessionId = 'sid';
ws = { readyState: 1, send(payload) { sent.push(payload); } };
(async () => {
  await handleButton();
  const oldSource = __sources[0];
  const oldNode = __worklets[0];
  const oldHandler = oldNode.port.onmessage;
  console.log('RESULT|first_graph|' + String(__sources.length === 1 && __worklets.length === 1 && oldSource.connectedTo === oldNode));

  state = 'listening';
  await handleButton();
  console.log('RESULT|pause_msg|' + String(sent.filter((item) => typeof item === 'string' && JSON.parse(item).type === 'pause').length));
  console.log('RESULT|old_source_disconnected|' + String(oldSource.disconnectCalls === 1 && oldSource.disconnectedTarget === oldNode && oldSource.connectedTo === null));
  console.log('RESULT|old_worklet_cleaned|' + String(oldNode.disconnectCalls === 1 && oldNode.port.onmessage === null && workletNode === null && mediaSourceNode === null));

  state = 'paused';
  await handleButton();
  const newSource = __sources[1];
  const newNode = __worklets[1];
  const newHandler = newNode.port.onmessage;
  state = 'listening';
  oldHandler({ data: new ArrayBuffer(640) });
  newHandler({ data: new ArrayBuffer(640) });
  newHandler({ data: new ArrayBuffer(640) });
  console.log('RESULT|resume_graph|' + String(__sources.length === 2 && __worklets.length === 2 && newSource.connectedTo === newNode));
  console.log('RESULT|stale_ignored|' + String(sent.filter((item) => item instanceof ArrayBuffer).length === 2));
  console.log('RESULT|one_frame_one_send|' + String(sent.filter((item) => item instanceof ArrayBuffer).length === 2));
  console.log('RESULT|start_count|' + sent.filter((item) => typeof item === 'string' && JSON.parse(item).type === 'start').length);
  console.log('RESULT|pause_count|' + sent.filter((item) => typeof item === 'string' && JSON.parse(item).type === 'pause').length);
  process.exit(0);
})();
''')
        self.assertEqual(results.get("first_graph"), "true")
        self.assertEqual(results.get("pause_msg"), "1")
        self.assertEqual(results.get("old_source_disconnected"), "true")
        self.assertEqual(results.get("old_worklet_cleaned"), "true")
        self.assertEqual(results.get("resume_graph"), "true")
        self.assertEqual(results.get("stale_ignored"), "true")
        self.assertEqual(results.get("one_frame_one_send"), "true")
        self.assertEqual(results.get("start_count"), "2")
        self.assertEqual(results.get("pause_count"), "1")

    def test_legitimate_correlation_id_flows_through_frontend(self):
        """同一个合法 correlation id 贯穿前端 trace 与 start 消息，
        且格式满足后端白名单（^[A-Za-z0-9_-]{1,32}$），保证后端接受。"""
        results = self._run('''
const sent = [];
const traces = [];
console.info = (line) => traces.push(line);
state = 'paused';
sessionId = 'sid';
ws = { readyState: 1, send(payload) { sent.push(JSON.parse(payload)); } };
(async () => {
  await handleButton();
  await new Promise((resolve) => setImmediate(resolve));
  const startMsg = sent.find((item) => item.type === 'start');
  const rid = startMsg.resume_id;
  const traceRid = traces[0].match(/resume=([^ ]+)/)[1];
  console.log('RESULT|same|' + String(rid === traceRid));
  console.log('RESULT|safe_format|' + String(/^[A-Za-z0-9_-]{1,32}$/.test(rid)));
  console.log('RESULT|epoch|' + String(traces[0].includes('event=resume/epoch')));
  process.exit(0);
})();
''')
        self.assertEqual(results.get("same"), "true")
        self.assertEqual(results.get("safe_format"), "true")
        self.assertEqual(results.get("epoch"), "true")


class TestFrozenAvatarVideoLayout(unittest.TestCase):
    """MVP 1.0 的完整 16:9 场景是页面主体，不得退回右栏视频块。"""

    @classmethod
    def setUpClass(cls):
        cls.css = APP_CSS.read_text(encoding="utf-8")

    def test_avatar_video_is_full_page_scene_without_crop_or_mask(self):
        block_match = re.search(r"#avatar-video\s*\{(?P<body>[^}]*)\}", self.css)
        self.assertIsNotNone(block_match, "#avatar-video CSS block missing")
        block = block_match.group("body")
        self.assertRegex(block, r"object-fit\s*:\s*contain\s*;")
        self.assertNotRegex(block, r"object-fit\s*:\s*cover\s*;")
        self.assertRegex(block, r"-webkit-mask-image\s*:\s*none\s*;")
        self.assertRegex(block, r"(?<!-)mask-image\s*:\s*none\s*;")

        column_match = re.search(r"\.video-column\s*\{(?P<body>[^}]*)\}", self.css)
        self.assertIsNotNone(column_match, ".video-column CSS block missing")
        column = column_match.group("body")
        self.assertRegex(column, r"position\s*:\s*absolute\s*;")
        self.assertRegex(column, r"inset\s*:\s*0\s*;")
        self.assertRegex(column, r"width\s*:\s*100%\s*;")
        self.assertRegex(column, r"height\s*:\s*100%\s*;")

    def test_left_readability_layer_is_soft_transparent_gradient(self):
        overlay_match = re.search(r"\.app::before\s*\{(?P<body>[^}]*)\}", self.css)
        self.assertIsNotNone(overlay_match, ".app::before overlay missing")
        overlay = overlay_match.group("body")
        self.assertIn("linear-gradient", overlay)
        self.assertRegex(overlay, r"to\s+right")
        self.assertIn("transparent", overlay)
        self.assertRegex(overlay, r"pointer-events\s*:\s*none\s*;")
        self.assertNotRegex(overlay, r"#000(?:000)?\b|\bblack\b")


if __name__ == "__main__":
    unittest.main()
