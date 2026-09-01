# AI-Wanyun

[English README](README.md)

**一个本地实时 Windows AI 伴侣：真人风格 Avatar、语音、长期记忆和实时唇形同步。**

![AI-Wanyun 公共运行时中的苏挽云](docs/assets/readme/hero.png)

AI-Wanyun 默认附带官方角色 **苏挽云**。你可以通过文字或语音与她对话，在多次对话间保留持久记忆，并通过 WebRTC 运行时获得带字幕和实时唇形同步的 Avatar 回应。

* 🎙️ 文字与语音对话
* 🧠 持久化长期记忆
* 👩 真人风格 Avatar 与实时唇形同步
* 🔑 自带 LLM / ASR / TTS Provider 凭据（BYOK）
* 🏠 本地 Core 与 SQLite 对话存储

> ⭐ 如果你觉得 AI-Wanyun 有点意思或对你有用，欢迎给仓库一个 Star——它能帮助更多人看到这个项目。

## 看它跑起来

[观看 12 秒真实运行录屏（无声）](docs/assets/readme/demo.mp4) · [查看当前架构](docs/assets/readme/architecture.svg)

录屏来自真实浏览器运行时，展示文字输入 → thinking → speaking / 实时唇形同步 → paused 的完整过程。

> **当前状态：** 面向开发者的 Public Preview。完整 Avatar 运行时需要单独配置 LiveTalking/Wav2Lip。Core 仅支持 localhost，本身不提供远程访问鉴权；不要直接暴露到局域网、互联网或公开反向代理之后。


## 仓库包含什么

- `backend/`：FastAPI、对话状态机、SQLite 存储、deterministic 长期记忆（仅记录
  用户明确事实与共同经历），以及真实的 ASR/LLM/TTS/LiveTalking 适配器。
- `frontend/`：原生 HTML/CSS/JavaScript、麦克风采集、AudioWorklet、
  WebSocket 控制、WebRTC Avatar 播放、字幕和文字输入。
- `characters/su-wanyun/`：官方 Public Persona、AI 生成 Avatar、生成来源记录
  与独立 Character Pack 许可证。
- `data/persona.example.md`：可选的中性 synthetic 示例，不再是默认角色。
- `tests/`：离线协议、状态机、存储、记忆、安全和前端契约测试。

## 外部运行时边界

本仓库不捆绑 LiveTalking、Wav2Lip、GPU 驱动、checkpoints、face detector
权重、模型、数据集或容器镜像。使用者必须依据各自上游条款单独安装和配置。
仓库中的苏挽云视频只是创意输入素材，不代表外部唇形同步运行栈被纳入本仓库，
也不代表整个运行栈已获得商业使用授权。

Wav2Lip 上游项目说明了非商业/研究用途限制。AI-Wanyun 原创内容采用 MIT，
不能据此外推整个运行栈可商业使用。详见 `THIRD_PARTY.md`、
`DEPENDENCY_LICENSES.md` 与 `NOTICE`。

## 配置与 BYOK

1. 将 `config.example.yaml` 复制为 `config.yaml`，再调整本机非敏感配置；默认
   Persona 已指向苏挽云官方 Public Persona。
2. 将 `secrets.example.json` 复制为 `secrets.json`，或设置
   `DASHSCOPE_API_KEY`、`DEEPSEEK_API_KEY`、`HUOSHAN_APPID`、
   `HUOSHAN_ACCESS_TOKEN`。示例值均为空或明显无效；不要提交真实凭据。
3. 单独安装 LiveTalking，并使用其上游预处理步骤注册 Avatar。可复现的
   Wav2Lip 流程见 [`characters/su-wanyun/README.md`](characters/su-wanyun/README.md)：
   将随仓 MP4 复制到外部安装目录，生成
   `data/avatars/suwanyun_mvp_v3/`，再以 `--avatar_id suwanyun_mvp_v3`
   启动 LiveTalking。不要把 LiveTalking、模型或生成后的运行时复制进本仓库。
4. 逐项核对所选 Provider 账号和模型条款。BYOK 只是接入方式，不是许可证或
   商业使用授权。

## 本地开发

前置条件：Windows PowerShell、CPython 3.12，以及能够访问 Python 包索引的网络。
请创建全新环境，不要复用其他 AI-Wanyun 或 LiveTalking 环境。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
Copy-Item config.example.yaml config.yaml
Copy-Item secrets.example.json secrets.json
# 只在 config.yaml 中编辑本机 endpoint/设置；把自己的 Provider 凭据写入
# 不受版本控制的 secrets.json，或设置前文列出的环境变量。
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 7870
```

在另一个 PowerShell 窗口检查 Core 存活和完整本机就绪状态：

```powershell
Invoke-RestMethod http://127.0.0.1:7870/health
Invoke-RestMethod http://127.0.0.1:7870/ready
```

`/health` 只验证 Core 进程；`/ready` 还要求 BYOK 凭据、配置项齐全，并且单独
安装的 LiveTalking 可达。两项均通过后再打开 `http://127.0.0.1:7870`。缺少
Provider 凭据或 LiveTalking 时，仍可运行离线测试和 `/health`，但不能完成真实
音视频闭环。

## 替换角色或 Provider

- 将 `persona_path` 改为你有权使用的 Persona 文件；
- 在外部运行时注册自己的 Avatar，并修改 `livetalking.avatar_id`；
- 修改 Provider endpoint/model，并提供自己的凭据。

用户自行提供的 Persona、肖像、媒体和 Provider 输出由用户负责，不受本仓库
许可证自动覆盖。

## 数据与隐私

对话数据保存在配置指定的本地 SQLite 数据库中。候选配置会忽略日志、数据库、
`secrets.json` 与本地运行文件；这些内容不得提交。发送个人或机密信息前，应先
核对 Provider 的数据使用和保留条款。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover tests -v
```

测试使用 synthetic fixtures 和临时运行数据。

## 许可证

- 除下列另行声明外，AI-Wanyun 原创内容适用根目录 MIT License，版权声明为
  `Copyright (c) 2026 AI-Wanyun contributors`。
- 苏挽云官方 Persona 与随仓 Avatar：CC BY 4.0，仅在项目贡献者实际持有的
  权利范围内授权；署名为 `AI-Wanyun / Su Wanyun Character Pack`。
- 第三方软件、模型、外部运行时、Provider 服务与用户内容：分别适用其自身
  许可证和条款。

根 MIT License 不覆盖 Wav2Lip、LiveTalking、模型、checkpoints、Character
Pack、Avatar 生成平台或 Provider 服务，也不声明整个运行栈具有商业使用权。
