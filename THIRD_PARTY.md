# Third-party and installation boundary

> This is a boundary record. The root `LICENSE` is MIT for AI-Wanyun original
> content except where separately stated; third-party and user-provided
> components remain independent.

## What this candidate contains

The candidate contains AI-Wanyun backend, frontend, tests, examples, dependency
declarations, and the separately licensed official Su Wanyun Character Pack.
It does not contain third-party package source, LiveTalking or Wav2Lip source,
model/checkpoint files, face-detector weights, `.trae` content, private runtime
files, or container images. Python packages are installed by the user from the
pinned requirement files; they are not vendored here.

AI-Wanyun original content in this candidate is covered by the root MIT License
except where separately stated.
The root MIT License does not cover the official Character Pack. Its approved
Public Persona and distributed Avatar video are licensed under CC BY 4.0 only
to the extent of rights held by project contributors, with attribution
`AI-Wanyun / Su Wanyun Character Pack`; see
`characters/su-wanyun/LICENSE.md` and `PROVENANCE.md`. The generic synthetic
Persona remains an optional example and is not the default character.

## External components requiring separate review

- [LiveTalking](https://github.com/lipku/LiveTalking) is an external runtime.
  Its upstream repository publishes an [Apache-2.0 license](https://raw.githubusercontent.com/lipku/LiveTalking/main/LICENSE),
  but that conclusion applies only to the upstream repository as licensed. It
  does not extend to its base image, models, dependencies, Avatar assets, or
  other runtime contents. If bundled later, preserve Apache notices and review
  every bundled component.
- [Wav2Lip](https://github.com/Rudrabha/Wav2Lip) is an external runtime. Its
  [official README](https://raw.githubusercontent.com/Rudrabha/Wav2Lip/master/README.md)
  states a personal/research/non-commercial boundary for the open-source
  code/results/models and identifies separate checkpoint, face-detection, and
  LRS2 dataset concerns. Do not infer a commercial grant or an Apache/MIT
  license. Commercial use requires separate written permission or an
  alternative with suitable terms.
- Checkpoints, model weights, face detectors, datasets, GPU drivers, container
  layers, and LiveTalking runtime assets are **USER_INSTALLS_SEPARATELY** and
  remain unresolved until each exact artifact's provenance, version, license,
  attribution, and usage restrictions are recorded.
- Provider APIs are accessed through generic HTTPX/websockets code; no
  proprietary provider SDK is vendored. Provider service terms, data-use rules,
  quotas, output rights, branding, and commercial permissions are separate from
  this repository's source-code license. BYOK does not close that review.
- One official AI-generated Avatar video and the approved Su Wanyun Public
  Persona are supplied under the separate Character Pack boundary described
  above. OpenAI Image2 and China Jimeng AI / Seedance 2.0 are provenance
  references, not bundled components and not licensed by this repository.
  User installation or upload of other content does not establish copyright,
  likeness, publicity, consent, or commercial rights.

## Python dependency notices

See `DEPENDENCY_LICENSES.md` for the per-package matrix, exact metadata basis,
upstream evidence, dependency-edge reconciliation, and bundled-distribution
notice conditions. In particular, `certifi` is **MPL-2.0**, not MIT or Apache;
`colorama`'s installed metadata is blank but its authoritative upstream LICENSE
is BSD-3-Clause.

## Release gate

No external component is marked `resolved` merely because it is installed
separately. Before any release, reconcile the lock to a deliberate target
environment and complete the per-artifact and provider-terms reviews. The root
MIT License covers AI-Wanyun original content except the separately licensed
Character Pack; it does not clear the
external runtime, model, dataset, detector, generation platform, other media,
or provider rights. The Character Pack's narrow CC BY 4.0 grant does not widen
that boundary. LiveTalking's Apache-2.0 license remains an upstream fact only
and does not extend to its whole runtime or to other components.
