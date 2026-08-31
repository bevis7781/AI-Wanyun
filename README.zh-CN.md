# AI-Wanyun

AI-Wanyun 是一个中文优先、面向开发者的 Public Preview：它在本地串联
FastAPI 对话编排、语音 Provider、持久化对话与长期记忆，并通过 WebRTC 与实时
唇形同步呈现真人风格 Avatar。

**官方角色：苏挽云。** 仓库包含经批准的 Public Persona，以及唯一一份由 AI
生成、适合 Idle/Speaking 场景的官方 Avatar 素材。项目采用 BYOK（自带 API
Key）；开发者也可以替换为自己有权使用的 Persona、Avatar 与 Providers。

> Developer-oriented Public Preview：这里保留的是真实实现，不是接口空壳；
> 但它尚不是能在陌生 GPU 机器上一条命令安装完成的发行包。

## 先看真实运行时

![AI-Wanyun 公共运行时中的苏挽云](docs/assets/readme/hero.png)

AI-Wanyun 是一个本地实时陪伴运行时：输入文字或使用麦克风，经由你自备
API Key（BYOK）接入的 Providers 处理对话，再通过 WebRTC 获得带字幕、实时
唇形同步的苏挽云角色。上图来自真实 Public runtime 页面；中央 Spirit Orb、
字幕和角色画面都是产品本身的一部分。

- **它是什么：** 面向开发者的 Public Preview，包含文字、麦克风、记忆和
  实时 Avatar 播放的本地对话闭环。
- **包含什么：** FastAPI Core、原生浏览器界面、Official Public Su Wanyun
  Character Pack、Persona、Avatar 素材、本地存储和集成适配器。
- **哪些在外部：** LiveTalking/Wav2Lip、GPU/模型文件和 Provider 账号仍是
  独立的运行时与许可边界。

[观看 12 秒无声真实录屏](docs/assets/readme/demo.mp4) ·
[查看当前架构](docs/assets/readme/architecture.svg)

录屏是真实浏览器页面捕获，完整展示文字输入 → thinking →
speaking/唇形同步 → paused；不含音频流、音乐或特效。

苏挽云官方角色包含虚构成年夫妻之间的成熟亲密关系主题，但仓库不包含色情或
图像化露骨性内容。

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
