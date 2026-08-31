# Dependency and publication boundary matrix

> Audit status: evidence-backed dependency and publication boundary for the
> public candidate. The root `LICENSE` is MIT for AI-Wanyun original content
> except where separately stated; third-party package licenses and
> external runtime/content terms remain separate.

## Evidence method and conclusions

- Direct pins were read from `requirements.txt`; lock pins were read from
  `requirements.lock.txt`.
- The candidate lock was installed in a new Windows CPython 3.12 virtual
  environment during the installability Gate; `pip check` and imports passed.
  For those exact versions, package `dist-info/METADATA` and, where present,
  referenced license files were reviewed. `License-Expression` takes precedence;
  otherwise the metadata `License` field or an authoritative upstream license is
  reported. None of those installed package files, package license files, or
  third-party package sources is copied into this candidate.
- A version shown as `clean-install verified` was installed only in that isolated
  audit environment; it is not bundled in the public candidate.
- `UNKNOWN` means that the evidence gathered here is insufficient; it is not a
  permissive-license assumption. Source-control authorship is weak provenance
  evidence and is not ownership proof.

## AI-Wanyun candidate ownership boundary

| component/package | current version/source | included vs runtime external | upstream | license | license evidence | redistribution status | attribution/NOTICE requirement | commercial concern | action |
|---|---|---|---|---|---|---|---|---|---|
| `backend/` | Candidate source; version is candidate revision | Included original source | N/A; no upstream URL in files | MIT | Project licensing decision; root `LICENSE` | Covered by root MIT as AI-Wanyun original content | Preserve root MIT notice; retain third-party attributions if later identified | MIT applies to this source only; external runtime and service rights remain separate | Keep within root MIT scope; review any future copied code |
| `frontend/` | Candidate HTML/CSS/JS/AudioWorklet; candidate revision | Included original source | N/A; no upstream URL in files | MIT | Project licensing decision; root `LICENSE` | Covered by root MIT as AI-Wanyun original content | Preserve root MIT notice; document any future vendored assets | MIT applies to this source only; media and runtime rights remain separate | Keep within root MIT scope; scan future assets |
| `tests/` | Candidate regression tests; candidate revision | Included original source | N/A; references protocols but no copied upstream file identified | MIT | Project licensing decision; root `LICENSE` | Covered by root MIT as AI-Wanyun original content | Preserve root MIT notice; protocol/provider names are not license grants | MIT applies to these tests only; provider terms remain separate | Keep within root MIT scope; review future copied fixtures |
| `README.md`, `ARCHITECTURE.md` | Candidate documentation | Included original documentation | External names/URLs are references only | Root MIT for AI-Wanyun original content; referenced external rights remain separate | Boundary record; external references remain separate | Keep attribution links and non-endorsement language | Do not imply provider or runtime commercial rights | Keep boundary wording and third-party references |
| `config.example.yaml`, `secrets.example.json`, `requirements*.txt` | Examples and dependency declarations | Included original metadata/examples; packages runtime external | Package upstreams listed below | Root MIT for original portions; package licenses are separate rows | Boundary record; package evidence below | Packages are not vendored; package obligations remain separate | Keep upstream links and dependency notices in release materials as applicable | Provider terms and package obligations remain separate | Maintain dependency matrix and lock provenance |
| `data/persona.example.md` | Synthetic example; candidate revision | Included original example content | N/A; no person/media identified | Root MIT for this original example; user-supplied Personas remain independent | Boundary record; synthetic content inventory | Preserve boundary notice; do not imply rights to user-supplied personas | Avoid identity, likeness, publicity, or commercial claims | Keep user-supplied assets separate |
| `.gitignore` and repository metadata | Candidate housekeeping | Included original metadata | N/A | Root MIT for original housekeeping content; external content remains separate | Boundary record | No special notice identified | No independent clearance for external content | Keep external content outside the candidate |
| `.trae`, private runtime, private/historical Avatar media, checkpoints, Docker images | Confirmed absent from candidate inventory | Not included; external/private boundary | N/A | UNKNOWN / not reviewed | Absence is an inventory fact, not a license conclusion | Not redistributed by candidate; the separately reviewed official Character Pack is a distinct included boundary | If later bundled, obtain per-asset notices and licenses first | High risk if bundled without provenance | Keep excluded; reopen review before bundling |

## Direct dependencies (`requirements.txt`)

The following are exact direct pins. The local metadata check confirmed the exact
direct versions below. Evidence links are official PyPI project records and/or
upstream repositories.

| component/package | current version/source | included vs runtime external | upstream | license | license evidence | redistribution status | attribution/NOTICE requirement | commercial concern | action |
|---|---|---|---|---|---|---|---|---|---|
| FastAPI | 0.139.0; exact metadata observed | Runtime external; not vendored | [GitHub](https://github.com/fastapi/fastapi) | MIT | Metadata `License-Expression: MIT`; [PyPI 0.139.0](https://pypi.org/project/fastapi/0.139.0/) | Not redistributed in source candidate; install separately | If a distribution bundles it, preserve MIT notice/license | Permissive, subject to notice | Keep exact pin; include notice in any bundled distribution |
| Uvicorn | 0.50.0; exact metadata observed | Runtime external; not vendored | [GitHub](https://github.com/Kludex/uvicorn) | BSD-3-Clause | Metadata `License-Expression: BSD-3-Clause`; [PyPI](https://pypi.org/project/uvicorn/0.50.0/) | Not bundled | BSD copyright/conditions notice if bundled | Permissive, notice required | Keep exact pin; record notice if bundling |
| HTTPX | 0.28.1; exact metadata observed | Runtime external; not vendored | [GitHub](https://github.com/encode/httpx) | BSD-3-Clause | Metadata `License: BSD-3-Clause`; [PyPI](https://pypi.org/project/httpx/0.28.1/) | Not bundled | BSD notice/conditions if bundled | Permissive, notice required | Keep exact pin; record notice if bundling |
| websockets | 15.0.1; exact metadata observed | Runtime external; not vendored | [GitHub](https://github.com/python-websockets/websockets) | BSD-3-Clause | Metadata `License: BSD-3-Clause`; [PyPI](https://pypi.org/project/websockets/15.0.1/) | Not bundled | BSD notice/conditions if bundled | Permissive, notice required | Keep exact pin; record notice if bundling |
| PyYAML | 6.0.3; exact metadata observed | Runtime external; not vendored | [GitHub](https://github.com/yaml/pyyaml) | MIT | Metadata `License: MIT`; [PyPI](https://pypi.org/project/PyYAML/6.0.3/) | Not bundled | MIT notice/license if bundled | Permissive, notice required | Keep exact pin; record notice if bundling |
| python-multipart | 0.0.32; exact metadata observed | Runtime external; not vendored | [GitHub](https://github.com/Kludex/python-multipart) | Apache-2.0 | Metadata `License-Expression: Apache-2.0`; [PyPI](https://pypi.org/project/python-multipart/0.0.32/) | Not bundled | Apache license, notices, and attribution if bundled | Commercial use generally allowed under Apache terms; patent/trademark terms still apply | Keep exact pin; do not treat as candidate license |
| pytest | 8.4.0; exact metadata observed | Test-time external; not shipped by runtime candidate | [GitHub](https://github.com/pytest-dev/pytest) | MIT | Metadata `License: MIT`; [PyPI](https://pypi.org/project/pytest/8.4.0/) | Not bundled in product runtime | MIT notice/license if a test distribution bundles it | Permissive, notice required | Keep exact pin; classify test-only |

## Lock dependencies and metadata reconciliation

`requirements.lock.txt` keeps its direct/transitive grouping. The selected pins
were installed together in a new Windows CPython 3.12 virtual environment, then
checked with `pip check` and direct imports. The audit retains only required
base/test dependency edges represented by the selected install.

| component/package | current version/source | included vs runtime external | upstream | license | license evidence | redistribution status | attribution/NOTICE requirement | commercial concern | action |
|---|---|---|---|---|---|---|---|---|---|
| Starlette | 0.52.1; exact clean-install verified | Runtime external; not vendored | [GitHub](https://github.com/Kludex/starlette) | BSD-3-Clause | Installed metadata `License-Expression: BSD-3-Clause`; [PyPI](https://pypi.org/project/starlette/0.52.1/) | Not bundled | BSD notice/conditions if bundled | Permissive, notice required | Keep exact pin |
| Pydantic | 2.12.3; exact clean-install verified | Runtime external; not vendored | [GitHub](https://github.com/pydantic/pydantic) | MIT | Installed metadata `License-Expression: MIT`; [PyPI](https://pypi.org/project/pydantic/2.12.3/) | Not bundled | MIT notice/license if bundled | Permissive, notice required | Keep exact pin |
| pydantic-core | 2.41.4; exact clean-install verified | Runtime external; not vendored | [GitHub](https://github.com/pydantic/pydantic-core) | MIT | Installed metadata `License-Expression: MIT`; [PyPI](https://pypi.org/project/pydantic-core/2.41.4/) | Not bundled | MIT notice/license if bundled | Permissive, notice required | Keep exact pin with Pydantic 2.12.3 |
| annotated-types | 0.7.0; exact clean-install verified | Runtime external; not vendored | [GitHub](https://github.com/annotated-types/annotated-types) | MIT | Installed metadata `License-Expression: MIT`; [PyPI](https://pypi.org/project/annotated-types/0.7.0/) | Not bundled | MIT notice/license if bundled | Permissive, notice required | Keep exact pin |
| typing-extensions | 4.16.0; exact lock and observed match | Runtime external; not vendored | [GitHub](https://github.com/python/typing_extensions) | PSF-2.0 | Observed metadata `License-Expression: PSF-2.0`; [PyPI](https://pypi.org/project/typing-extensions/4.16.0/) | Not bundled | Preserve PSF notice/license if bundled | Permissive with PSF notice obligations | Keep exact pin |
| AnyIO | 4.14.1; exact clean-install verified | Runtime external; not vendored | [GitHub](https://github.com/agronholm/anyio) | MIT | Installed metadata `License-Expression: MIT`; [PyPI](https://pypi.org/project/anyio/4.14.1/) | Not bundled | MIT notice/license if bundled | Permissive, notice required | Keep exact pin |
| h11 | 0.16.0; exact lock and observed match | Runtime external; not vendored | [GitHub](https://github.com/python-hyper/h11) | MIT | Observed metadata `License: MIT`; [PyPI](https://pypi.org/project/h11/0.16.0/) | Not bundled | MIT notice/license if bundled | Permissive, notice required | Keep exact pin |
| httpcore | 1.0.9; exact lock and observed match | Runtime external; not vendored | [GitHub](https://github.com/encode/httpcore) | BSD-3-Clause | Observed metadata `License-Expression: BSD-3-Clause`; [PyPI](https://pypi.org/project/httpcore/1.0.9/) | Not bundled | BSD notice/conditions if bundled | Permissive, notice required | Keep exact pin |
| certifi | 2026.6.17; exact clean-install verified | Runtime external; not vendored | [GitHub](https://github.com/certifi/python-certifi) | MPL-2.0 | Installed metadata `License: MPL-2.0`; [PyPI](https://pypi.org/project/certifi/2026.6.17/) | Not bundled; no CA bundle copied | MPL notice/license; modifications to covered files trigger MPL obligations | Commercial use is possible under MPL, but file-level copyleft and notices matter | Keep exact pin; never describe as MIT/Apache |
| idna | 3.18; exact lock and observed match | Runtime external; not vendored | [GitHub](https://github.com/kjd/idna) | BSD-3-Clause | Observed metadata `License-Expression: BSD-3-Clause`; [PyPI](https://pypi.org/project/idna/3.18/) | Not bundled | BSD notice/conditions if bundled | Permissive, notice required | Keep exact pin |
| Click | 8.4.2; exact lock and observed match | Runtime external; not vendored | [GitHub](https://github.com/pallets/click) | BSD-3-Clause | Observed metadata `License-Expression: BSD-3-Clause`; [PyPI](https://pypi.org/project/click/8.4.2/) | Not bundled | BSD notice/conditions if bundled | Permissive, notice required | Keep exact pin; `colorama` edge is Windows-conditional |
| colorama | 0.4.6; exact observed version; Windows marker in lock | Runtime external; Windows-conditional | [GitHub](https://github.com/tartley/colorama) | BSD-3-Clause | Installed metadata fields are blank; authoritative [upstream LICENSE](https://raw.githubusercontent.com/tartley/colorama/master/LICENSE.txt) is BSD-3-Clause; [PyPI](https://pypi.org/project/colorama/0.4.6/) | Not bundled | Preserve BSD copyright/conditions if bundled | License evidence is closed; platform-specific lock scope still matters | Keep `sys_platform == "win32"` marker and record BSD notice if bundling |
| annotated-doc | 0.0.5; added from exact observed metadata | Runtime external; FastAPI base dependency | [GitHub](https://github.com/fastapi/annotated-doc) | MIT | Metadata `License-Expression: MIT`; [PyPI](https://pypi.org/project/annotated-doc/0.0.5/) | Not bundled | MIT notice/license if bundled | Permissive, notice required | Keep added pin; FastAPI metadata requires `>=0.0.2` |
| typing-inspection | 0.4.4; added from exact observed metadata | Runtime external; FastAPI/Pydantic base dependency | [GitHub](https://github.com/pydantic/typing-inspection) | MIT | Metadata `License-Expression: MIT`; [PyPI](https://pypi.org/project/typing-inspection/0.4.4/) | Not bundled | MIT notice/license if bundled | Permissive, notice required | Keep added pin; metadata requires `typing-extensions>=4.15.0` |
| iniconfig | 2.3.0; added from exact observed metadata | Test-time external; pytest dependency | [GitHub](https://github.com/pytest-dev/iniconfig) | MIT | Metadata `License-Expression: MIT`; [PyPI](https://pypi.org/project/iniconfig/2.3.0/) | Not bundled | MIT notice/license if bundled | Permissive, notice required | Keep added pin; pytest metadata requires `>=1` |
| packaging | 26.3; added from exact observed metadata | Test-time external; pytest dependency | [GitHub](https://github.com/pypa/packaging) | Apache-2.0 OR BSD-2-Clause | Metadata `License-Expression: Apache-2.0 OR BSD-2-Clause`; [PyPI](https://pypi.org/project/packaging/26.3/) | Not bundled | Preserve selected-license notice and conditions if bundled | Permissive choice, notice required | Keep added pin; pytest metadata requires `>=20` |
| pluggy | 1.6.0; added from exact observed metadata | Test-time external; pytest dependency | [GitHub](https://github.com/pytest-dev/pluggy) | MIT | Metadata `License: MIT`; [PyPI](https://pypi.org/project/pluggy/1.6.0/) | Not bundled | MIT notice/license if bundled | Permissive, notice required | Keep added pin; pytest metadata requires `>=1.5,<2` |
| Pygments | 2.20.0; added from exact observed metadata | Test-time external; pytest dependency | [GitHub](https://github.com/pygments/pygments) | BSD-2-Clause | Metadata `License-Expression: BSD-2-Clause`; [PyPI](https://pypi.org/project/Pygments/2.20.0/) | Not bundled | BSD notice/conditions if bundled | Permissive, notice required | Keep added pin; pytest metadata requires `>=2.7.2` |

### Dependency-edge findings

- FastAPI 0.139.0 metadata directly requires `starlette>=0.46.0`,
  `pydantic>=2.9.0`, `typing-extensions>=4.8.0`, `typing-inspection>=0.4.2`,
  and `annotated-doc>=0.0.2`. The lock previously omitted the last two; they
  are now pinned from exact observed metadata.
- Pydantic metadata requires `annotated-types`, `pydantic-core`,
  `typing-extensions`, and `typing-inspection`; the first three were already
  represented, while `typing-inspection` is now represented.
- Pytest 8.4.0 metadata requires `colorama` on Windows, `iniconfig`,
  `packaging`, `pluggy`, and `pygments`. The lock previously omitted the last
  four; exact observed versions are now pinned, and `colorama` now carries an
  explicit Windows marker. `exceptiongroup` and `tomli` are Python-version
  conditional and were not required by the audited CPython 3.12 runtime. The
  lock is explicitly scoped to that interpreter generation; it must be
  regenerated and re-audited for Python below 3.11 or another target platform.
- HTTPX requires `anyio`, `certifi`, `httpcore==1.*`, and `idna`; HTTPcore
  requires `certifi` and `h11`. These edges are represented.
- Uvicorn's `standard` extra is not selected by the candidate. Its optional
  `websockets`, PyYAML, and Windows `colorama` edges must not be mistaken for
  a vendored runtime bundle.

## External runtime and content boundary

| component/package | current version/source | included vs runtime external | upstream | license | license evidence | redistribution status | attribution/NOTICE requirement | commercial concern | action |
|---|---|---|---|---|---|---|---|---|---|
| LiveTalking | Version not pinned by candidate; configured endpoint only | Runtime external; no LiveTalking-owned code, image, model, or media copied; the project-owned Character Pack input asset is separate | [LiveTalking repository](https://github.com/lipku/LiveTalking) | Apache-2.0 for the upstream repository | [Upstream LICENSE](https://raw.githubusercontent.com/lipku/LiveTalking/main/LICENSE) | LiveTalking is not redistributed; user installs it separately | If bundled, include Apache license, retain notices, mark modified files, and review NOTICE; current candidate only references it | Apache may permit commercial redistribution subject to terms; base image, models, avatars, and dependencies are separate reviews | Keep `USER_INSTALLS_SEPARATELY`; do not extend Apache conclusion to Wav2Lip/models/runtime or the Character Pack |
| Wav2Lip | Version not pinned by candidate; referenced runtime | Runtime external; no code/checkpoint copied | [Official repository](https://github.com/Rudrabha/Wav2Lip) | Custom/noncommercial boundary; not treated as a permissive SPDX grant | [Official README license/disclaimer](https://raw.githubusercontent.com/Rudrabha/Wav2Lip/master/README.md) | Not redistributed | Preserve upstream attribution/citation if ever used; obtain separate permission for commercial use | Official README limits open-source code/results/models to personal/research/non-commercial use tied to LRS2; commercial gate remains open | Do not bundle or call commercially cleared without written permission/alternative |
| Checkpoints/models/datasets | No version/artifact included | Runtime external; user-provided/downloaded | Wav2Lip and each model/dataset provider | UNKNOWN per artifact; model/output/dataset terms are separate | Wav2Lip README identifies separate checkpoints, face model, and LRS2 dataset links; each artifact's terms must be read | Not redistributed | Record provenance, exact URL/version, license, attribution, and dataset/model restrictions | High risk: model, output, and dataset rights do not inherit code license | Require per-artifact review before use or bundling |
| Face detector | No detector artifact included | Runtime external; user installs separately | Wav2Lip README's face-detection model link | UNKNOWN | [Official README prerequisite/model link](https://raw.githubusercontent.com/Rudrabha/Wav2Lip/master/README.md) | Not redistributed | Obtain detector license and model notice separately | May have distinct model/data restrictions | Keep outside candidate and require provenance |
| Provider SDK/API | No proprietary SDK vendored; generic HTTPX/websockets only | Service runtime external; BYOK | Provider documentation/terms selected by user (DashScope, DeepSeek, Volcengine endpoints in examples) | N/A for API service license; terms UNKNOWN until provider/account review | Endpoint references in `config.example.yaml`; provider official terms must be checked for selected account/product | No provider SDK or service content redistributed | Follow provider attribution, branding, data-use and output terms where applicable | High: API commercial use, quotas, data retention, generated output and trademarks are contract/service issues, not repo-code license issues | Keep BYOK and add provider-specific compliance review before commercial deployment |
| Official Su Wanyun Character Pack | Controller-approved Public Persona plus one AI-generated 25 fps Avatar derivative | Included creative content; external lip-sync runtime not included | OpenAI Image2 reference image -> China Jimeng AI / Seedance 2.0; record `vid:v02870g10004da8ltpa7dld8u6ca5c6g` | CC BY 4.0, only to the extent of rights held by project contributors | `characters/su-wanyun/LICENSE.md`, `PROVENANCE.md`, locked Persona and asset SHA-256 records | Included and redistributable within that narrow grant; original v3 export is hash-recorded but not distributed | Attribute `AI-Wanyun / Su Wanyun Character Pack`; retain license, provenance, AI-generated disclosure, and modification notice | Does not license generation platforms/models or clear the external runtime; does not assert full-stack commercial rights | Keep as separate Character Pack boundary; do not apply root MIT or external runtime licenses to it |
| Other Avatar/media/Persona | No other user or private media included; optional synthetic Persona example remains | User content/runtime external except the generic example | User/provider source unknown | UNKNOWN for future/user assets | Candidate inventory and `data/persona.example.md` | User assets remain external and are not covered by the official Character Pack grant | Obtain copyright, likeness/publicity, license, provenance, and attribution records | High identity, publicity, copyright, and commercial-use risk | Keep user-supplied assets separate; do not treat separation as clearance |
| GPU drivers, container/runtime, private services | No artifacts included | Runtime external; user installs separately | User-selected distribution/provider | UNKNOWN / out of scope | Candidate inventory only; no image or private runtime copied | Not redistributed | Per-component notices required if later bundled | High; proprietary drivers, image layers, model terms and service contracts may apply | Keep excluded; reopen full SBOM/license review before bundling |
| `USER_INSTALLS_SEPARATELY` boundary | Status: external-only boundary, not resolved | Not included; user installation required | Each upstream/provider separately | UNKNOWN until each item is reviewed | This matrix and `THIRD_PARTY.md` document separation only | No candidate redistribution of external artifacts | Separate installation does not eliminate attribution, license, model, data, or terms obligations | Commercial release remains blocked for unresolved external rights | Treat as an explicit pending gate, never as `resolved` |

## Root MIT scope (not a third-party authorization)

The root MIT License covers AI-Wanyun original content except the separately licensed
Character Pack. It does not extend to
LiveTalking's whole runtime, Wav2Lip, checkpoints, face detectors, datasets,
provider APIs, Avatar/media, or user content. Each external component retains
its own license, terms, attribution, and usage restrictions; the Apache-2.0
entry for LiveTalking records only its upstream repository license.

## Gate result

Current result: **ROOT ORIGINAL CONTENT LICENSED UNDER MIT; EXTERNAL RIGHTS NOT CLEARED**.
The Windows CPython 3.12 lock has passed a clean installation and `pip check`.
Regenerate and re-audit it for another platform or interpreter generation.
Separate rights review remains required for every external runtime, model,
dataset, detector, non-project Avatar/media asset, and provider agreement.
