# AI-Wanyun

AI-Wanyun is a Chinese-first, developer-oriented Public Preview for a local
real-time AI companion: FastAPI conversation orchestration, voice Providers,
persistent conversation and memory, and a real-person-style Avatar delivered
through WebRTC with real-time lip-sync.

**Official character: Su Wanyun.** The repository includes her approved Public
Persona and one AI-generated, speaking-safe Avatar asset. Bring your own API
keys (BYOK), or replace the Persona, Avatar, and Providers with your own.

> Developer-oriented Public Preview: this is real implementation, not an API
> stub, but it is not yet a one-command distribution for an unfamiliar GPU
> machine.

> **Security boundary:** Core has no remote-access authentication and supports
> localhost use only. Do not expose it to a LAN, the Internet, or a public reverse proxy.

[中文说明](README.zh-CN.md)

## See the public runtime

![Su Wanyun in the AI-Wanyun public runtime](docs/assets/readme/hero.png)

AI-Wanyun is a local, real-time companion: type or speak, let the conversation
run through your BYOK Providers, and receive a captioned, lip-synced Su Wanyun
character over WebRTC. The screenshot above is the real Public runtime page;
the central Spirit Orb, captions, and character view are part of the product.

- **What it is:** a developer-oriented Public Preview for a local conversation
  loop with text, microphone, memory, and real-time Avatar playback.
- **What it includes:** the FastAPI Core, native browser UI, Official Public Su
  Wanyun Character Pack, Persona, Avatar asset, storage, and integration
  adapters.
- **What is external:** LiveTalking/Wav2Lip, GPU/model files, and Provider
  accounts remain separate runtime and licensing boundaries.

[Watch the 12-second silent live demo](docs/assets/readme/demo.mp4) ·
[Open the current architecture](docs/assets/readme/architecture.svg)

The demo is a real browser capture showing text input → thinking →
speaking/lip-sync → paused. It contains no audio stream, music, or effects.

The official Su Wanyun character contains mature relationship themes between fictional adult spouses, but the repository does not include pornographic or graphically explicit sexual content.

## What is included

- `backend/`: FastAPI endpoints, conversation state machine, SQLite storage,
  deterministic long-term memory of explicit user facts and shared experiences,
  and real ASR/LLM/TTS/LiveTalking adapters.
- `frontend/`: native HTML/CSS/JavaScript, microphone capture, AudioWorklet,
  WebSocket control, WebRTC Avatar playback, captions, and text input.
- `characters/su-wanyun/`: the official Public Persona, AI-generated Avatar
  asset, provenance, and the separate Character Pack license.
- `data/persona.example.md`: an optional generic synthetic example; it is not
  the default character.
- `tests/`: offline contract, state-machine, storage, memory, security, and
  frontend tests.

## External runtime boundary

This repository does **not** bundle LiveTalking, Wav2Lip, GPU drivers,
checkpoints, face-detector weights, model files, datasets, or container images.
Users install and configure those separately under their own upstream terms.
The included Su Wanyun video is a creative input asset only; it does not make
the external lip-sync stack part of this repository or commercially cleared.

Wav2Lip's upstream project describes non-commercial/research restrictions.
Do not assume that the complete runtime is commercially usable merely because
AI-Wanyun source code is MIT-licensed. See `THIRD_PARTY.md`,
`DEPENDENCY_LICENSES.md`, and `NOTICE`.

## Configuration and BYOK

1. Copy `config.example.yaml` to `config.yaml` and adjust non-secret local
   values. The default Persona is the official Su Wanyun Persona.
2. Copy `secrets.example.json` to `secrets.json`, or set
   `DASHSCOPE_API_KEY`, `DEEPSEEK_API_KEY`, `HUOSHAN_APPID`, and
   `HUOSHAN_ACCESS_TOKEN`. Example values are intentionally empty or invalid;
   never commit real credentials.
3. Install LiveTalking separately and register the Avatar with its upstream
   preprocessing step. The reproducible Wav2Lip path is documented in
   [`characters/su-wanyun/README.md`](characters/su-wanyun/README.md): copy the
   distributed MP4 into the external installation, generate
   `data/avatars/suwanyun_mvp_v3/`, then start LiveTalking with
   `--avatar_id suwanyun_mvp_v3`. Do not copy LiveTalking, its models, or its
   generated runtime into this repository.
4. Review the terms for every Provider account and model you choose. BYOK is
   an access pattern, not a license or commercial-use grant.

## Local development

Prerequisites: Windows PowerShell, CPython 3.12, and network access to the
package index. Create a new environment; do not reuse another AI-Wanyun or
LiveTalking environment.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
Copy-Item config.example.yaml config.yaml
Copy-Item secrets.example.json secrets.json
# Edit only local endpoints/settings in config.yaml and add your own Provider
# credentials to the untracked secrets.json (or set the documented env vars).
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 7870
```

In another PowerShell window, verify Core liveness and complete local readiness:

```powershell
Invoke-RestMethod http://127.0.0.1:7870/health
Invoke-RestMethod http://127.0.0.1:7870/ready
```

`/health` verifies the Core process. `/ready` additionally requires all BYOK
credentials/config fields and a reachable, separately installed LiveTalking
service. Open `http://127.0.0.1:7870` only after both pass. Without Provider
credentials or LiveTalking, offline tests and `/health` can still run, but the
complete audio/video loop will not.

## Replace the character or Providers

- Set `persona_path` to a Persona file you are allowed to use.
- Register your own Avatar with your external runtime and set
  `livetalking.avatar_id` accordingly.
- Change Provider endpoints/models and supply your own credentials.

User-supplied Personas, likenesses, media, and Provider outputs remain the
user's responsibility and are not covered by this repository's licenses.

## Data and privacy

Conversation data is stored locally in the configured SQLite database. Logs,
databases, `secrets.json`, and local runtime files are ignored by the candidate
configuration and must not be committed. Review Provider data-use and retention
terms before sending personal or confidential information.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover tests -v
```

Tests are designed to use synthetic fixtures and temporary runtime data.

## Licenses

- AI-Wanyun original content, except where separately stated below: root MIT
  License, `Copyright (c) 2026 AI-Wanyun contributors`.
- Official Su Wanyun Persona and distributed Avatar asset: CC BY 4.0, to the
  extent of rights held by project contributors; required attribution:
  `AI-Wanyun / Su Wanyun Character Pack`.
- Third-party software, models, external runtimes, Provider services, and
  user-supplied content: their own licenses and terms.

The root MIT License does not cover Wav2Lip, LiveTalking, models, checkpoints,
the Character Pack, Avatar generation platforms, or Provider services, and it
does not assert commercial rights in the complete runtime stack.
