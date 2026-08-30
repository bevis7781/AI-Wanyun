const WS_URL = `ws://${location.host}/ws`;

/* ---------------- Voice Resume 链路 instrumentation（纯 metadata，无控制逻辑） ----------------
 * 只记录 resume 各阶段的 epoch / 状态 / 帧序号 / 字节长度，
 * 绝不记录 PCM 内容、语音文本、session id 或 secret。
 * 测试可通过 G8-TRACE-PURE 区间提取纯函数。 */
/* G8-TRACE-PURE-BEGIN（node 单测提取区间：不引用 DOM/外部变量） */
function formatVoiceTraceEvent(evt) {
  const head = `[voice-trace] resume=${evt.resumeId} event=${evt.name} t_ms=${evt.tMs}`;
  if (!evt.fields) return head;
  const safeKeys = ['frameIdx', 'bytes', 'trackCount', 'trackStates', 'ctxState', 'processor', 'reason', 'seq'];
  const fields = Object.keys(evt.fields)
    .filter((k) => safeKeys.indexOf(k) !== -1)
    .sort()
    .map((k) => `${k}=${evt.fields[k]}`)
    .join(' ');
  return fields ? `${head} ${fields}` : head;
}
/* G8-TRACE-PURE-END */

let resumeSeq = 0;
let resumeId = null;
let resumeStartT = 0;
let resumeWorkletFrames = 0;
let resumePcmSent = false;

function nowMonoMs() {
  return (typeof performance !== 'undefined' && typeof performance.now === 'function')
    ? performance.now() : Date.now();
}

function emitVoiceTrace(name, fields) {
  if (!resumeId) return;
  const line = formatVoiceTraceEvent({ resumeId, name, tMs: Math.round(nowMonoMs() - resumeStartT), fields });
  console.info(line);
  try {
    if (typeof CustomEvent !== 'undefined' && typeof document !== 'undefined') {
      document.dispatchEvent(new CustomEvent('wanyun:trace', { detail: { line } }));
    }
  } catch (_) {}
}

function nextResumeId() {
  resumeSeq += 1;
  return `r${resumeSeq}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

/* ---------------- G8-B 运行时配置（单一事实源） ----------------
 * LT URL / Avatar ID 不再前端硬编码，统一由后端 GET /api/runtime-config 提供。
 * 失败不永久缓存失败 Promise：用户之后再次操作时自动重试读取。 */
let runtimeConfig = null;
let runtimeConfigPromise = null;

function loadRuntimeConfig() {
  if (!runtimeConfigPromise) {
    runtimeConfigPromise = fetch('/api/runtime-config', { cache: 'no-store' })
      .then((resp) => {
        if (!resp.ok) throw new Error(`runtime-config ${resp.status}`);
        return resp.json();
      })
      .then((cfg) => {
        if (!cfg || !cfg.livetalking_url || !cfg.avatar_id) throw new Error('runtime-config 字段缺失');
        runtimeConfig = cfg;
        runtimeConfigPromise = null;
        return cfg;
      })
      .catch((err) => {
        runtimeConfigPromise = null; // 失败不缓存，后续操作可重试
        throw err;
      });
  }
  return runtimeConfigPromise;
}

async function ensureRuntimeConfig() {
  if (runtimeConfig) return runtimeConfig;
  return loadRuntimeConfig();
}

const elStatus = document.getElementById('status');
const elStack = document.getElementById('caption-stack');
const elVideo = document.getElementById('avatar-video');
const elBtn = document.getElementById('talk-btn');

let ws = null;
let pc = null;
let remoteStream = null;
let audioCtx = null;
let workletNode = null;
let mediaSourceNode = null;
let mediaStream = null;
let state = 'paused';
let sessionId = null;
let audioStarted = false;
let avatarPlaybackUnlockRequested = false;
let textPending = null; // G8-A：单 pending 草稿 { requestId, text, timer }；不实现消息队列

const elInput = document.getElementById('text-input');

/* ---------------- 字幕轮次系统 ----------------
 * 一轮 = 用户一句 + 助手一段回复；最多保留 4 轮（当前 + 3 历史）。
 * 历史轮逐级缩小、变暗、上移，最终融入背景后移除。 */
const MAX_TURNS = 4;
const EASE_SOFT = 'cubic-bezier(0.22, 0.61, 0.36, 1)';
let turns = []; // { el, aiEl }，末位为当前轮

function makeTurnEl(userText) {
  const el = document.createElement('div');
  el.className = 'turn' + (userText ? '' : ' no-user');
  el.dataset.depth = '0';
  const inner = document.createElement('div');
  inner.className = 'turn-inner';
  const spUser = document.createElement('div');
  spUser.className = 'speaker';
  spUser.textContent = '你';
  const tUser = document.createElement('p');
  tUser.className = 't-user';
  tUser.textContent = userText || '';
  const spAi = document.createElement('div');
  spAi.className = 'speaker speaker-ai';
  spAi.textContent = '助手';
  const tAi = document.createElement('p');
  tAi.className = 't-ai';
  inner.append(spUser, tUser, spAi, tAi);
  el.appendChild(inner);
  return el;
}

function addTurn(userText) {
  // FLIP 快照：记录历史轮当前位置，换轮后平滑上移，避免列表重排感
  const snapshot = turns.map((t) => ({
    inner: t.el.firstChild,
    top: t.el.firstChild.getBoundingClientRect().top,
  }));
  const el = makeTurnEl(userText);
  el.style.opacity = '0';
  el.style.transform = 'translateY(14px)';
  el.style.transition = `opacity 640ms ${EASE_SOFT}, transform 640ms ${EASE_SOFT}`;
  elStack.appendChild(el);
  const rec = { el, aiEl: el.querySelector('.t-ai') };
  turns.push(rec);

  // 4 轮上限：最旧一轮淡出后移除
  while (turns.length > MAX_TURNS) {
    const old = turns.shift();
    old.el.classList.add('leave');
    setTimeout(() => old.el.remove(), 760);
  }
  turns.forEach((t, i) => { t.el.dataset.depth = String(turns.length - 1 - i); });

  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      el.style.opacity = '';
      el.style.transform = '';
      setTimeout(() => { el.style.transition = ''; }, 700);
      snapshot.forEach((s) => {
        if (!s.inner.isConnected) return;
        const dy = s.top - s.inner.getBoundingClientRect().top;
        if (Math.abs(dy) < 0.5) return;
        s.inner.style.transition = 'none';
        s.inner.style.transform = `translateY(${dy}px)`;
        void s.inner.offsetHeight;
        s.inner.style.transition = `transform 760ms ${EASE_SOFT}`;
        s.inner.style.transform = '';
        setTimeout(() => { s.inner.style.transition = ''; }, 820);
      });
    });
  });
  return rec;
}

/* 状态文字非常驻（G7）：正常状态提示显示约 3s 后淡出；
 * 异常（error 或含失败/断开/未就绪等）保持常驻，直至下一条消息。 */
const STATUS_FADE_MS = 3000;
const STATUS_ABNORMAL_RE = /未就绪|失败|断开|超时|不可用|错误|未识别/;
let statusFadeTimer = null;

function showStatus(text, isError) {
  elStatus.textContent = text;
  elStatus.classList.toggle('error', !!isError);
  elStatus.style.opacity = '1';
  if (statusFadeTimer) { clearTimeout(statusFadeTimer); statusFadeTimer = null; }
  if (!isError && !STATUS_ABNORMAL_RE.test(text)) {
    statusFadeTimer = setTimeout(() => { elStatus.style.opacity = '0'; }, STATUS_FADE_MS);
  }
}

function setCaption(kind, text) {
  if (kind === 'status') {
    showStatus(text, false);
  } else if (kind === 'error') {
    showStatus(text, true);
  } else if (kind === 'user') {
    if (text) addTurn(text);
  } else if (kind === 'ai') {
    if (!text) return;
    let cur = turns[turns.length - 1];
    if (!cur) cur = addTurn('');
    if (!cur.aiEl.textContent) cur.el.classList.add('has-ai');
    cur.aiEl.textContent = text;
  }
}

// 集成/测试钩子：document.dispatchEvent(new CustomEvent('wanyun:caption', { detail: { kind, text } }))
document.addEventListener('wanyun:caption', (e) => setCaption(e.detail.kind, e.detail.text));

/* 玉片开关：只表达"连续对话关闭 / 开启"两态。
 * on = 会话进行中（connecting/listening/thinking/speaking，灯芯较亮 + 呼吸）；
 * off = paused/error（待机微光约 30%，灯芯永不完全熄灭）。
 * 亮度与环境光全部由 body[data-state] 驱动 CSS，JS 只负责 aria 同步。 */
const CONVERSATION_ON = new Set(['connecting', 'listening', 'thinking', 'speaking']);

function updateToggle() {
  const on = CONVERSATION_ON.has(state);
  elBtn.setAttribute('aria-pressed', String(on));
  const label = on ? '停止连续对话' : '开始连续对话';
  elBtn.setAttribute('aria-label', label);
  elBtn.title = label;
}

function setState(newState) {
  state = newState;
  // G8-A：状态变化（thinking/speaking/paused/listening）不是文字 ACK，
  // 不得清 pending —— 只有对应 request_id 的 text_ack 才是确认。
  document.body.dataset.state = newState;
  updateToggle();
}

async function ensureWs() {
  if (ws && ws.readyState === WebSocket.OPEN) return;
  return new Promise((resolve, reject) => {
    ws = new WebSocket(WS_URL);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => {
      if (sessionId) sendSessionId();
      resolve();
    };
    ws.onclose = () => {
      setCaption('status', '后端连接已断开');
      restoreTextDraft(); // G8-A：断开时未确认的 pending 草稿必须恢复
      setState('error');
      stopMicrophone();
    };
    ws.onerror = (e) => reject(e);
    ws.onmessage = (ev) => {
      const data = JSON.parse(ev.data);
      if (data.type === 'state') setState(data.state);
      if (data.type === 'text_ack') handleTextAck(data); // G8-A：只有对应 request_id 的 ACK 才算确认
      if (data.type === 'caption') {
        if (data.kind === 'ai' && data.text) setCaption('ai', data.text);
        else if (data.kind === 'user') setCaption('user', data.text);
        else if (data.kind === 'status') setCaption('status', data.text);
        else if (data.kind === 'error') setCaption('error', data.text);
      }
      if (data.type === 'status') setCaption('status', data.text);
    };
  });
}

function sendSessionId() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: 'sessionid', sessionId }));
}

async function connectLiveTalking() {
  // G8-B：任何入口调用本函数都必须先确保运行时配置已加载（失败可重试）
  const cfg = await ensureRuntimeConfig();
  await closePeer();
  pc = new RTCPeerConnection({
    sdpSemantics: 'unified-plan',
    iceServers: [],
  });
  remoteStream = new MediaStream();
  elVideo.srcObject = remoteStream;
  pc.addTransceiver('audio', { direction: 'recvonly' });
  pc.addTransceiver('video', { direction: 'recvonly' });
  pc.ontrack = (event) => {
    if (remoteStream.getTracks().some(t => t.id === event.track.id)) return;
    remoteStream.addTrack(event.track);
    if (avatarPlaybackUnlockRequested) ensureAvatarMediaPlayback('ontrack');
    else tryPlayAvatarMedia('ontrack');
  };
  pc.onconnectionstatechange = () => {
    if (pc.connectionState === 'connected') setCaption('status', '真人角色已连接');
    if (pc.connectionState === 'failed') setCaption('status', '真人角色连接失败');
  };

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  await waitIce(pc);

  const resp = await fetch(`${cfg.livetalking_url}/offer`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      sdp: pc.localDescription.sdp,
      type: pc.localDescription.type,
      avatar: cfg.avatar_id,
    }),
  });
  if (!resp.ok) throw new Error(`LiveTalking /offer ${resp.status}`);
  const answer = await resp.json();
  if (!answer.sessionid || !answer.sdp) throw new Error('LiveTalking 响应缺少 sessionid');
  sessionId = String(answer.sessionid);
  await pc.setRemoteDescription({ type: answer.type, sdp: answer.sdp });
  await waitConnected(pc, 10000);
  sendSessionId();
}

function waitIce(pc) {
  return new Promise((resolve) => {
    if (pc.iceGatheringState === 'complete') return resolve();
    const onChange = () => {
      if (pc.iceGatheringState === 'complete') {
        pc.removeEventListener('icegatheringstatechange', onChange);
        resolve();
      }
    };
    pc.addEventListener('icegatheringstatechange', onChange);
    setTimeout(resolve, 3000);
  });
}

function waitConnected(pc, timeoutMs) {
  return new Promise((resolve, reject) => {
    if (pc.connectionState === 'connected') return resolve();
    const timer = setTimeout(() => done(new Error('WebRTC 连接超时')), timeoutMs);
    const onState = () => {
      if (pc.connectionState === 'connected') done();
      else if (pc.connectionState === 'failed' || pc.connectionState === 'closed') {
        done(new Error(`WebRTC ${pc.connectionState}`));
      }
    };
    const done = (err) => {
      clearTimeout(timer);
      pc.removeEventListener('connectionstatechange', onState);
      err ? reject(err) : resolve();
    };
    pc.addEventListener('connectionstatechange', onState);
    onState();
  });
}

async function closePeer() {
  if (pc) {
    try { await pc.close(); } catch (_) {}
    pc = null;
  }
  remoteStream = null;
  elVideo.srcObject = null;
}

function reportAvatarPlaybackFailure(trigger, error) {
  const name = error && typeof error.name === 'string' ? error.name : 'Error';
  const message = error && typeof error.message === 'string' ? error.message : String(error);
  console.warn(`[avatar-media] play failed trigger=${trigger}: ${name}: ${message}`);
}

function tryPlayAvatarMedia(trigger) {
  let playResult;
  try {
    playResult = elVideo.play();
  } catch (err) {
    reportAvatarPlaybackFailure(trigger, err);
    return Promise.resolve(false);
  }
  if (!playResult || typeof playResult.then !== 'function') return Promise.resolve(true);
  return Promise.resolve(playResult).then(
    () => true,
    (err) => {
      reportAvatarPlaybackFailure(trigger, err);
      return false;
    },
  );
}

function ensureAvatarMediaPlayback(trigger) {
  // 记录用户解锁意图；remote track 迟到时由 ontrack 自动重试。
  avatarPlaybackUnlockRequested = true;
  try {
    elVideo.muted = false;
  } catch (err) {
    reportAvatarPlaybackFailure(trigger, err);
    return Promise.resolve(false);
  }
  return tryPlayAvatarMedia(trigger).then((success) => {
    if (success) audioStarted = true;
    return success;
  });
}

async function startMicrophone() {
  if (!audioCtx) {
    audioCtx = new AudioContext({ sampleRate: 48000 });
    emitVoiceTrace('ctx/state', { ctxState: audioCtx.state });
    await audioCtx.audioWorklet.addModule('/static/audio-worklet.js');
  }
  if (mediaStream) return;
  emitVoiceTrace('gum/requested', {});
  mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
  emitVoiceTrace('gum/resolved', {
    trackCount: mediaStream.getTracks().length,
    trackStates: mediaStream.getTracks().map((t) => t.readyState).join(','),
  });
  const src = audioCtx.createMediaStreamSource(mediaStream);
  const node = new AudioWorkletNode(audioCtx, 'pcm16-resampler', {
    numberOfInputs: 1,
    numberOfOutputs: 0,
    channelCount: 1,
  });
  mediaSourceNode = src;
  workletNode = node;
  emitVoiceTrace('worklet/created', { processor: 'pcm16-resampler' });
  node.port.onmessage = (ev) => {
    // pause/resume 之间可能仍有旧消息排队；只有当前 source/worklet 才能发送。
    if (workletNode !== node || mediaSourceNode !== src) return;
    resumeWorkletFrames += 1;
    if (resumeWorkletFrames === 1) emitVoiceTrace('worklet/first_frame', { frameIdx: 1 });
    if (state !== 'listening' || !ws || ws.readyState !== WebSocket.OPEN) return;
    if (!resumePcmSent) {
      resumePcmSent = true;
      emitVoiceTrace('pcm/first_send', { bytes: ev.data.byteLength });
    }
    ws.send(ev.data);
  };
  src.connect(node);
  emitVoiceTrace('worklet/connected', {});
  if (audioCtx.state === 'suspended') await audioCtx.resume();
  emitVoiceTrace('ctx/state', { ctxState: audioCtx.state });
}

function stopMicrophone() {
  const src = mediaSourceNode;
  const node = workletNode;
  mediaSourceNode = null;
  workletNode = null;
  if (src) {
    try { src.disconnect(node); } catch (_) {
      try { src.disconnect(); } catch (_) {}
    }
  }
  if (node) {
    if (node.port) node.port.onmessage = null;
    try { node.disconnect(); } catch (_) {}
  }
  if (mediaStream) {
    mediaStream.getTracks().forEach(t => t.stop());
    mediaStream = null;
  }
}

async function handleButton() {
  try {
    if (!audioStarted) {
      // 播放解锁是 best-effort；失败不能阻断玉片原有的麦克风流程。
      ensureAvatarMediaPlayback('orb');
    }
    await ensureWs();
    if (!sessionId) await connectLiveTalking();

    if (state === 'paused' || state === 'error') {
      resumeId = nextResumeId();
      resumeStartT = nowMonoMs();
      resumeWorkletFrames = 0;
      resumePcmSent = false;
      emitVoiceTrace('resume/epoch', { seq: resumeSeq });
      await startMicrophone();
      ws.send(JSON.stringify({ type: 'start', resume_id: resumeId }));
    } else {
      // 连续对话已开启（connecting/listening/thinking/speaking）：
      // 点击玉片一律结束连续会话（pause 会取消当前轮并回到 paused）
      ws.send(JSON.stringify({ type: 'pause' }));
      emitVoiceTrace('pause/cleanup', {
        trackCount: mediaStream ? mediaStream.getTracks().length : 0,
      });
      stopMicrophone();
    }
  } catch (err) {
    setCaption('error', String(err.message || err));
    setState('error');
  }
}

elBtn.addEventListener('click', handleButton);

/* ---------------- 文字输入通道（G3-v2） ----------------
 * 只绕过 ASR，与语音共用同一提交/回复/字幕链路（后端 session.submit_text）。
 * 规则：
 *  - paused：一次性文字轮，回复结束回 paused，绝不触碰麦克风；
 *  - listening：文字取代本轮语音输入，回复结束回 listening；
 *  - thinking/speaking：暂不提交，草稿保留，仅边缘轻反馈；
 *  - IME composition 期间的 Enter（选词/确认候选）不发送。 */
const TEXT_SENDABLE = new Set(['paused', 'listening']);
let imeComposing = false;

/* ---------------- G8-A 文字 ACK / pending 草稿 ----------------
 * 发送后 textarea 视觉清空，原文保留在 pending（单 pending，不排队）；
 * 只有 request_id 对应的 text_ack 才是确认，状态变化不是确认。
 * rejected / timeout / disconnect / ws.send 异常 → 恢复草稿（A 或 A+B）。 */
const TEXT_ACK_TIMEOUT_MS = 9000; // 8–10 秒窗口内
let textAckSeq = 0;

function genRequestId() {
  textAckSeq += 1;
  return `t${textAckSeq}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
}

/* G8-TEXT-PURE-BEGIN（node 单测提取区间：区间内不得引用 DOM/外部变量） */
function mergeTextDraft(currentValue, pendingText) {
  return pendingText + currentValue;
}

function isStaleTextAck(requestId, pending) {
  return !pending || requestId !== pending.requestId;
}
/* G8-TEXT-PURE-END */

function restoreTextDraft() {
  const pending = textPending;
  if (!pending) return;
  clearTimeout(pending.timer);
  textPending = null;
  elInput.value = mergeTextDraft(elInput.value, pending.text);
  autoGrowInput();
  nudgeInput();
}

function handleTextAck(data) {
  const pending = textPending;
  if (isStaleTextAck(data.request_id, pending)) return; // stale ACK：完全忽略
  clearTimeout(pending.timer);
  textPending = null;
  if (data.accepted === false) {
    elInput.value = mergeTextDraft(elInput.value, pending.text);
    autoGrowInput();
    nudgeInput();
    setCaption('status', '文字未发送，草稿已恢复');
  }
  // accepted:true：只清 pending/timer，绝不触碰 textarea（用户可能已输入新草稿 B）
}

function autoGrowInput() {
  if (!elInput) return;
  elInput.style.height = 'auto';
  const max = elInput.scrollHeight;
  const cap = 3 * Math.round(1.65 * 14) + 26; // 与 CSS 三行上限一致
  elInput.style.height = Math.min(max, cap) + 'px';
  elInput.style.overflowY = elInput.scrollHeight > cap ? 'auto' : 'hidden';
}

function nudgeInput() {
  if (!elInput) return;
  elInput.classList.remove('nudge');
  void elInput.offsetWidth; // 重启动画
  elInput.classList.add('nudge');
}

async function trySendText() {
  const text = elInput.value.replace(/\s+$/, '').trim();
  if (!text) return;                       // 空/全空格/全换行：不生成空轮
  if (!TEXT_SENDABLE.has(state) || textPending) {
    nudgeInput();                          // thinking/speaking/已有 pending：暂不提交，草稿保留
    return;
  }
  // 必须在本次真实文字手势中的第一次 await 之前发起播放解锁；helper 自行吸收失败。
  ensureAvatarMediaPlayback('text');
  try {
    await ensureWs();
  } catch (_) {
    nudgeInput();
    return;
  }
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    nudgeInput();
    return;
  }
  const requestId = genRequestId();
  const pending = { requestId, text, timer: null };
  pending.timer = setTimeout(() => {
    if (textPending === pending) restoreTextDraft(); // ACK timeout：恢复草稿
  }, TEXT_ACK_TIMEOUT_MS);
  textPending = pending;
  // 先视觉清空再 send：若 ws.send() 同步抛错，恢复逻辑拿到的是空输入框，
  // mergeTextDraft('', A) === A —— 原始草稿只保留一次，不会变成 AA。
  elInput.value = '';
  autoGrowInput();
  try {
    ws.send(JSON.stringify({ type: 'text', request_id: requestId, text }));
  } catch (err) {
    restoreTextDraft(); // ws.send 抛异常：恢复原始草稿（textarea 已清空 → 结果恰为 A）
    return;
  }
}

if (elInput) {
  elInput.addEventListener('compositionstart', () => { imeComposing = true; });
  elInput.addEventListener('compositionend', () => { imeComposing = false; });
  elInput.addEventListener('input', autoGrowInput);
  elInput.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    if (e.shiftKey) return;                                  // Shift+Enter：仅换行
    if (imeComposing || e.isComposing || e.keyCode === 229) return; // IME 选词 Enter：不发送
    e.preventDefault();
    trySendText();
  });
  autoGrowInput();
}

async function closeRemoteSession() {
  try {
    await fetch('/api/session/close', { method: 'POST', keepalive: true });
  } catch (_) {}
}

window.addEventListener('pagehide', closeRemoteSession);
window.addEventListener('beforeunload', closeRemoteSession);

// Initial connection（G8-B：loadRuntimeConfig → ensureWs → connectLiveTalking）
(async () => {
  try {
    await ensureRuntimeConfig();
  } catch (_) {
    setCaption('error', '运行配置加载失败，请点击按钮重试');
    return;
  }
  try {
    await ensureWs();
    await connectLiveTalking();
    setCaption('status', '点击中央按钮开始对话');
  } catch (err) {
    setCaption('status', '真人角色未就绪，点击按钮重试');
  }
})();
