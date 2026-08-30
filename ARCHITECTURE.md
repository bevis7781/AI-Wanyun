# 架构与协议说明

## 整体链路

```
麦克风 -> AudioWorklet(16k/mono/PCM16/20ms) -> /ws binary
                                                  |
                                                  v
 listening -> ASR(Qwen Audio 3.0) -> LLM(DeepSeek) -> TTS(火山双流) -> PCM
                                                  |
                                                  v
                                  ws://127.0.0.1:8110/audio-stream
                                                  |
                                                  v
                                  外部 LiveTalking/Wav2Lip -> WebRTC A/V
```

## 后端模块

- `main.py`：FastAPI、WebSocket、静态文件、健康检查、诊断。
- `session.py`：状态机、turnId、RMS 门控、前置音频、LLM 分句、TTS 队列、打断。
- `storage.py`：SQLite，只保存 `completed` 轮次。
- `adapters/qwen_asr.py`：Qwen Audio 3.0 流式 ASR，仅 `sentence_end=true` 提交。
- `adapters/deepseek_llm.py`：OpenAI 兼容流式 Chat Completions。
- `adapters/huoshan_tts.py`：火山双流 TTS 二进制协议，直接取 PCM。
- `adapters/livetalking.py`：外部 LiveTalking 的 `/offer`、`/interrupt_talk`、`/audio-stream` start/end/abort。

## 前端模块

- `index.html` / `app.css`：三栏布局与无媒体依赖的安全占位。
- `app.js`：WebSocket、WebRTC、按钮状态、字幕。
- `audio-worklet.js`：AudioWorklet 重采样到 16 kHz，输出 640-byte PCM16 帧。

## 状态机

```
paused -> listening
listening -> paused | thinking (ASR 最终文本到达) -> speaking -> listening
speaking -> listening (interrupt)
error -> paused (reset)
```

注意：在 ASR 流式识别过程中，状态保持 `listening`，麦克风持续向 Qwen 发送音频；只有收到 `sentence_end=true` 的最终文本后，才进入 `thinking` 并触发 LLM。

## 打断语义

1. `turnId` 单调递增。
2. 取消旧 ASR、LLM、TTS 任务。
3. 清空 TTS 队列。
4. 向 LiveTalking 发送旧 generation 的 `abort`。
5. 调用 `/interrupt_talk`。
6. 丢弃迟到 token 和 PCM。
7. 不重建 WebRTC。

## LiveTalking PCM 流协议

```json
{"type":"start","sessionId":"...","generationId":1,"sampleRate":16000,"channels":1,"format":"pcm_s16le"}
```

随后每条二进制消息严格 640 bytes，最后：

```json
{"type":"end","sessionId":"...","generationId":1}
```

打断时：

```json
{"type":"abort","sessionId":"...","generationId":1}
```

## 安全与隐私

- API Key 只从 `secrets.json` 或环境变量读取。
- 日志中密钥脱敏，不记录完整对话、persona、请求体或 PCM。
- 运行时生成的 `secrets.json`、SQLite、日志被 `.gitignore` 排除。
- LiveTalking、Wav2Lip、GPU、模型/检查点与 face detector 属于 USER_INSTALLS_SEPARATELY 边界，不由本仓库提供或替代其许可审查。仓库仅提供独立许可的苏挽云 Character Pack 输入素材；该素材仍须注册到外部 LiveTalking runtime，且不改变外部运行栈的许可证边界。
