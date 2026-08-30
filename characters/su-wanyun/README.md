# Official Su Wanyun Character Pack

This directory contains the official public Su Wanyun character content for
AI-Wanyun. Su Wanyun is a fictional adult character and the default character
of this Developer-oriented Public Preview.

## Included files

- `persona.md` — the Controller-approved Public Su Wanyun Persona. It is used
  verbatim; downstream maintainers must not silently rewrite it.
- `avatar/suwanyun_idle_speaking_v3_25fps.mp4` — the only distributed official
  Idle/Speaking-safe visual asset. It is an AI-generated 25 fps runtime
  derivative intended to be registered in a separately installed LiveTalking
  runtime.
- `PROVENANCE.md` — generation lineage, hashes, and the human provenance
  attestation accepted by the project Controller.
- `LICENSE.md` — the Character Pack's CC BY 4.0 boundary.

The official Su Wanyun character contains mature relationship themes between
fictional adult spouses, but this repository does not include pornographic or
graphically explicit sexual content.

## Runtime boundary

The Avatar video is included as a creative asset, not as a bundled lip-sync
runtime. LiveTalking, Wav2Lip, checkpoints, face detectors, models, GPU
components, and their dependencies remain external and must be installed and
reviewed separately. The root MIT License does not cover this Character Pack.

Users may replace the official Persona, Avatar, and Providers with content and
services for which they hold the necessary rights. Doing so does not transfer
the Su Wanyun Character Pack license to user-supplied content.

## Verified LiveTalking registration (external)

The upstream LiveTalking source documents Wav2Lip avatar preprocessing and
loads generated assets from `data/avatars/<avatar_id>`. The following commands
use only upstream entry points; run them with the separately installed
LiveTalking environment. Replace both example roots with your own local paths;
neither directory is part of this repository.

```powershell
$wanyunRoot = "C:\path\to\AI-Wanyun"
$liveTalkingRoot = "C:\path\to\LiveTalking"
Copy-Item -LiteralPath (Join-Path $wanyunRoot "characters\su-wanyun\avatar\suwanyun_idle_speaking_v3_25fps.mp4") `
  -Destination (Join-Path $liveTalkingRoot "data\avatars\suwanyun_idle_speaking_v3_25fps.mp4")
Set-Location -LiteralPath $liveTalkingRoot
python avatars\wav2lip\genavatar.py `
  --video_path data/avatars/suwanyun_idle_speaking_v3_25fps.mp4 `
  --avatar_id suwanyun_mvp_v3 `
  --save_path data/avatars `
  --img_size 96
python app.py --transport webrtc --model wav2lip `
  --avatar_id suwanyun_mvp_v3 --listenport 8110
```

The generator creates `data/avatars/suwanyun_mvp_v3/` (including the frames
and `coords.pkl` required by the Wav2Lip loader). The upstream source also
documents `python app.py --transport webrtc --model wav2lip --avatar_id ...`;
the `--listenport 8110` argument aligns that server with this project's
`config.example.yaml`. This is an external runtime setup, not a repository
install step. If your LiveTalking revision does not contain the cited
`avatars/wav2lip/genavatar.py` entry point, mark the procedure
`external/UNKNOWN` and follow that revision's own documentation rather than
guessing a replacement command.
