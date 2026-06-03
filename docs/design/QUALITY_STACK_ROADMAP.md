# Mintarr Quality Stack Roadmap

> **Type:** Quality strategy and roadmap.
> **Status:** 0.3 — 2026-05-24.
> **Scope:** Explains why the current V2 quality model is the right practical solution for this stack, and how future source-specific checks should be added.
> **Related:** [Pipeline architecture](../architecture/PIPELINE.md), [Sidecar format v2](../specs/SIDECAR_FORMAT_v2.md), [F3 source adapters](F3_SOURCE_ADAPTERS_DESIGN.md), [Connector/plugin architecture](CONNECTOR_PLUGIN_ARCHITECTURE.md).

---

## 1. Engineering Verdict

The correct model for this stack is **not** "FLAC Detective alone". The correct model is a layered **pre-import quality gate**:

```text
Lidarr / Prowlarr
  -> Mintarr source adapter
  -> download workspace
  -> hard technical gates
       1. ffprobe codec/container/stream gate
       2. flac -t decode/integrity gate
  -> spectral sensor
       3. FLAC Detective fake-lossless / fake-hi-res analysis
  -> source-specific optional evidence
       4. CUETools / AccurateRip / CTDB for complete CD-rip lane
       5. Picard / beets / AcoustID for identity metadata
  -> verification_score + policy matrix
  -> ACCEPT / ACCEPT_PROVISIONAL / REVIEW_REQUIRED / BLOCK
  -> Lidarr ManualImport only when policy allows it
```

This is the best fit for the current repo because:

- It keeps QC **before** Lidarr imports files into the library.
- It separates hard technical failures from probabilistic audio-quality signals.
- It does not treat metadata or source labels as audio proof.
- It keeps Lidarr as the library/import target, not the quality decision engine.
- It gives uncertain cases a controlled `REVIEW_REQUIRED` state instead of blind import or blind delete.
- It is already implemented as V2 Option A, with lower operational risk than a full staging orchestrator.

The key rule: **no single tool is a truth machine.** Each sensor has a role, a confidence level, and a safe failure mode.

## 2. Sensor Classes

| Class | Examples | What it can prove | What it cannot prove | Policy role |
|---|---|---|---|---|
| Hard technical gate | `ffprobe`, `flac -t` | File is parsable, expected codec/container exists, FLAC decodes consistently | Original source was authentic lossless | Fail closed / BLOCK on failure |
| Spectral heuristic | FLAC Detective | Lossy-transcode/fake-hi-res indicators | Absolute mastering truth | Score + REVIEW/BLOCK policy |
| Strong source-specific proof | CUETools, AccurateRip, CTDB | Complete CD rip matches known CD checksum evidence | WEB/TIDAL hi-res authenticity | Positive evidence for CD-rip lane |
| Metadata/identity signal | Picard, beets, AcoustID, MBID | Recording/release identity hints | Audio quality or lossless authenticity | Low-weight identity aid |
| Library/import state | Lidarr quality labels | What Lidarr classified/imported | Source authenticity | Context only |
| Provenance | TIDAL, Qobuz, Norbits, Soulseek, YouTube | Expected trust level of source | That this specific file is clean | Prior/context, never override hard gates |

## 2.1 Sensor Result Contract

Future sidecars and dashboard drawer payloads should represent every quality signal as a sensor result. This keeps evidence queryable without turning the browser into a policy engine.

```json
{
  "name": "flac_detective",
  "sensor_version": "flac-detective-api wrapper 2026-05-24 / flac-detective 0.9.0",
  "binary_version": null,
  "policy_version": "pipeline-v2-spec-0.4.4",
  "class": "spectral_heuristic",
  "status": "pass",
  "severity": "none",
  "confidence": 0.8,
  "duration_ms": 88000,
  "summary": "Overall verdict AUTHENTIC.",
  "evidence": {
    "overall_verdict": "AUTHENTIC",
    "overrides": []
  }
}
```

Required fields:

| Field | Meaning |
|---|---|
| `name` | Stable sensor id, e.g. `ffprobe`, `flac_t`, `flac_detective`, `ctdb`, `acoustid` |
| `sensor_version` | Tool/wrapper version used for this decision |
| `binary_version` | External binary version when available, e.g. ffprobe/flac/CUETools |
| `policy_version` | Policy/spec version that interpreted the sensor result |
| `class` | `hard_gate`, `spectral_heuristic`, `source_specific_proof`, `metadata_identity`, `provenance`, `library_state` |
| `status` | `pass`, `warn`, `fail`, `skipped`, `unavailable` |
| `severity` | `none`, `info`, `warning`, `blocker` |
| `confidence` | Confidence within this sensor's domain, not universal truth |
| `duration_ms` | Wall-clock runtime for capacity planning |
| `summary` | One-line operator explanation |
| `evidence` | Sensor-specific structured details |

Versioning is audit-critical. If thresholds, wrappers or binaries change later, historical decisions must remain interpretable without guessing which implementation produced them.

## 2.2 Confidence Semantics

`confidence` is **not** a universal probability that a file is "real lossless". It is scoped to the sensor class:

| Sensor class | Confidence means | It does not mean |
|---|---|---|
| Hard gate | confidence that the technical parse/decode result is reliable | authentic original source |
| Spectral heuristic | confidence in spectral/fake-lossless interpretation | absolute mastering truth |
| Source-specific proof | confidence that this source-lane evidence matches, e.g. CD checksum evidence | applies to unrelated WEB/TIDAL material |
| Metadata identity | confidence in identity/release/recording match | audio quality |
| Provenance | confidence in source trust/context | this specific file bypasses QC |

Use confidence to explain and prioritize review. Do not use it to override hard gates.

## 2.3 Performance Budgets

These are operational budgets for planning and alerting, not correctness rules.

| Sensor | Expected cost | Alert condition | Notes |
|---|---|---|---|
| `ffprobe` | usually sub-second per album, low CPU | repeated failures or >10s per album | hard gate; should stay cheap |
| `flac_t` | seconds to tens of seconds depending on album size | p95 grows above 60s for normal albums | IO/decode bound |
| `flac_detective` | heaviest current sensor; p95 should be watched | p95 grows above 2 min for 10-12 track albums or RSS grows unexpectedly | worker recycle mitigates native RSS retention |
| `ctdb` / AccurateRip | CD-rip lane only; can be network-bound | queue buildup or repeated dependency failures | never in hot path for TIDAL lane |
| Picard/beets/AcoustID | network/cache dependent | any uncached call in hot path without timeout | read-only prepass first; no tag-writing until designed |

Dashboard telemetry should show timing percentiles by sensor once sensor result objects are persisted.

## 2.4 Sensor Registry And Plugin Model

Sensors must be pluggable. The quality stack should not grow by hardcoding every
new tool directly into the import function.

Target architecture:

```text
SensorRegistry
  -> enabled SensorRunner instances, ordered by stage
  -> each runner emits SensorResult
  -> policy engine interprets SensorResult objects
  -> sidecar/audit/dashboard store and display the same evidence
```

Each sensor definition should include:

| Field | Meaning |
|---|---|
| `name` | stable id, e.g. `ffprobe`, `flac_t`, `flac_detective`, `ctdb`, `acoustid` |
| `class` | hard gate, spectral heuristic, metadata identity, provenance, source-specific proof |
| `enabled` | operator/config switch |
| `required` | if true, unavailable/error can block the pipeline |
| `stage` | ordering group: `technical_gate`, `spectral`, `source_specific`, `metadata`, `provenance` |
| `timeout_sec` | hard timeout for the runner |
| `fail_policy` | `block`, `review`, `skip`, or `warn` |
| `applies_to` | source lanes/file types where this sensor is meaningful |
| `version_command` | optional command/API used for binary version capture |
| `evidence_schema_version` | version of the structured evidence emitted by the runner |

Default fail policy:

| Sensor class | Default unavailable/error policy |
|---|---|
| Hard technical gate | `block` |
| Primary spectral sensor | `block` for current TIDAL/WEB lane |
| Source-specific proof | `skip` or `review`, never `block` on no-match alone |
| Metadata identity | `warn` or `skip` |
| Provenance | `warn` or `skip` |

Operator-facing sensor configuration should eventually support:

- enable/disable optional sensors
- per-sensor timeout and concurrency cap
- per-source-lane applicability
- health and last-error in dashboard
- dry-run mode where a sensor records evidence but does not affect policy

Design invariant: disabling optional sensors may reduce confidence, but disabling
required hard gates must not be allowed while the connector is in import mode.

## 3. Coverage Boundary

Current V2 pre-import QC covers **Mintarr/TIDAL downloads only**. It does not yet sit in front of every Lidarr import path.

| Import path | Current pre-import QC coverage | Current fallback | Target |
|---|---|---|---|
| Mintarr / TIDAL | Full V2 gate before Lidarr ManualImport | Post-import safety net still useful | Keep as reference lane |
| Plain SABnzbd / Usenet | Not covered by V2 unless routed through Mintarr | Lidarr import + any post-import custom script | Route through shared QC ingest before Lidarr import |
| qBittorrent / torrents | Not covered by V2 | Lidarr import + any post-import custom script | Torrent source lane with same hard gates and policy |
| Soulseek / slskd | Not covered by V2 | Lidarr import + any post-import custom script | Soulseek source lane with same hard gates and provenance-aware policy |
| Future Qobuz / WEB | Not implemented | n/a | WEB source lane, same as TIDAL with source-specific provenance |
| YouTube / Tubifarry | Not implemented as lossless lane | n/a | Never treated as lossless upgrade; metadata/provisional lane only |

The long-term target is a **shared QC control filter in front of the whole Lidarr music import surface**, not a TIDAL-only checker. Mintarr V2 is the first implemented lane because it already owns download, validation and Lidarr ManualImport. Future lanes should reuse the same sensor contract, sidecar/audit model, dashboard evidence model and policy invariants.

Until those lanes exist, non-TIDAL sources should be considered lower-assurance imports. A post-import FLAC Detective custom script is a safety net, not equivalent to V2 pre-import gating.

### 3.1 Future Connector Model

Long term, the app should let the operator choose which music download clients/source
lanes are connected to the shared QC filter. This should be explicit configuration,
not hardcoded assumptions.

Target connector types:

| Connector | Role | Initial mode | Notes |
|---|---|---|---|
| TIDAL/Mintarr | source adapter + downloader | implemented reference lane | owns download + QC + Lidarr ManualImport today |
| SABnzbd / Usenet | completed-download ingest lane | future, disabled by default | must avoid breaking existing Arr completed-download handling |
| qBittorrent / torrents | completed-category/path watcher | future, disabled by default | must respect tracker rules and qbit-manage cleanup policy |
| Soulseek / slskd | completed-download ingest lane | F3.5 completed-folder ingest and F3.5B slskd trigger, disabled by default | weak provenance; always use full hard gates + spectral policy |
| Qobuz / official WEB | source adapter + downloader | future | similar policy to TIDAL, but separate provenance |
| Norbits / CD-rip lane | torrent/CD evidence lane | future | enable CUE/log/CTDB evidence when present |

Operator-facing configuration should eventually include:

- enabled/disabled per connector
- connection settings and health status per connector
- watched category/path per completed-download connector
- source-lane label stored in sidecars and dashboard records
- dry-run mode before a connector is allowed to import into Lidarr
- per-connector concurrency limits so QC does not saturate CPU/IO

Design invariant: every connector must feed the same verification pipeline before
Lidarr import. A connector may add provenance or source-specific evidence, but it
must never bypass `ffprobe`, `flac -t`, FLAC Detective policy, sidecar/audit, or
the `REVIEW_REQUIRED` flow.

Connector/plugin architecture is tracked in
[CONNECTOR_PLUGIN_ARCHITECTURE.md](CONNECTOR_PLUGIN_ARCHITECTURE.md).
That document owns the operator-facing registry, enable/disable model, health,
install/update boundaries and dashboard Integrations view. This roadmap owns the
meaning and limits of the evidence produced by those connectors.

## 4. Current V2 Stack

Current V2 uses the right core sequence for the Mintarr/TIDAL lane:

1. Download to a controlled work/output path.
2. Verify stream/container with `ffprobe`.
3. Verify FLAC decode/integrity with `flac -t`.
4. Analyze with FLAC Detective.
5. Compute `verification_score`.
6. Apply hard overrides and policy matrix.
7. Create `verification.json` sidecar and append `decisions.jsonl`.
8. Import into Lidarr only for accepted decisions.
9. Hold ambiguous cases as `REVIEW_REQUIRED`.

Current V2 intentionally does **not** include MBID/beets/CTDB in the core score, because those components are not consistently available for the TIDAL lane today. Adding score components that are usually absent would make the scale misleading.

## 5. Tool Roles And Limits

### 5.1 ffprobe

`ffprobe` is the right first hard gate. FFmpeg documents it as a tool that gathers multimedia stream information and can report container and stream format/type in machine-readable forms.

Use it for:

- codec/container detection
- sample rate
- bit depth where available
- channels
- duration
- presence/absence of an audio stream

Do not use it as proof of authentic lossless audio.

Example:

```text
codec_name=flac
sample_rate=96000
bits_per_sample=24
```

This proves the file is currently represented as 24/96 FLAC. It does **not** prove the source was originally lossless or genuinely hi-res. MP3/AAC can be transcoded to FLAC and still pass this layer.

Policy:

| Condition | Result |
|---|---|
| missing audio stream | BLOCK |
| codec mismatch | BLOCK |
| unreadable stream metadata | BLOCK or REVIEW depending on failure mode |
| ffprobe OK | technical pass only, no authenticity claim |

Source: FFmpeg ffprobe documentation.

### 5.2 flac -t

`flac -t` is the right second hard gate. The FLAC command-line documentation says test mode behaves like decode mode without writing a decoded output file, and performs additional checks such as metadata parsing and MD5 mismatch detection. The FLAC format overview also documents that the STREAMINFO block can include the MD5 signature of the unencoded audio data, useful for checking transmission errors.

Use it for:

- decode validity
- FLAC stream integrity
- metadata parse errors
- MD5 mismatch where present

Do not use it as proof that the source was never lossy.

Example:

```text
MP3 320 -> FLAC
flac -t -> PASS
```

That is expected. The FLAC file can be perfectly valid while containing audio that came from a lossy source.

Policy:

| Condition | Result |
|---|---|
| `flac -t` non-zero | BLOCK |
| MD5/decode/metadata error | BLOCK |
| `flac -t` pass | technical pass only, not authenticity |

No auto-repair. The FLAC documentation warns that decoding through errors may conceal corruption and may not sound subjectively better. Keep the bad evidence/audit instead of silently repairing.

Sources: Xiph FLAC command-line documentation and FLAC format overview.

### 5.3 FLAC Detective

FLAC Detective remains the primary spectral fake-lossless sensor for this stack. It is the right tool for the current TIDAL/WEB lane, because the problem is usually not "is this a valid FLAC container?" but "does the spectrum look like lossy audio or fake hi-res wrapped in FLAC?"

Use it for:

- spectral cutoff analysis
- fake-lossless indicators
- fake-hi-res indicators
- estimated lossy bitrate evidence
- per-file verdicts and overrides

Treat it as heuristic/probabilistic. It is not absolute truth.

Important limits:

| Limit | Consequence |
|---|---|
| Real masters can have little energy above 18-20 kHz | possible false positive |
| Vinyl/tape/old masters can have unusual spectrum | possible false positive |
| High-quality MP3/AAC can be plausible | possible false negative |
| Short sample windows can miss variation | prefer longer samples for critical cases |
| Low-pass does not always mean fake | needs policy/context |
| Primarily FLAC-focused in this stack | ALAC/WAV/APE need explicit handling |

Policy:

| Verdict/signal | Result |
|---|---|
| `AUTHENTIC` | normally ACCEPT |
| `WARNING` | normally ACCEPT, log warning |
| `SUSPICIOUS` | policy-based, never replace existing complete FLAC |
| `FAKE_CERTAIN` | never auto-import; REVIEW_REQUIRED or BLOCK |
| `fake_hi_res` override | REVIEW_REQUIRED |
| validator unavailable | BLOCK / fail closed |

### 5.4 CUETools / AccurateRip / CTDB

CUETools/AccurateRip/CTDB are stronger than spectral heuristics **when the input is a complete CD-rip lane**. CTDB documents that it stores CD TOC, offset-finding checksums, whole-disc CRC32 evidence and recovery records, and that verification needs the whole CD rip.

This is valuable for:

- complete CD rips
- 16/44.1 CD material
- rips with `.cue` and ideally EAC/XLD/CUERipper logs
- private tracker/Norbits-style CD-rip lanes

It is not a general proof mechanism for:

- TIDAL/Qobuz/WEB FLAC
- 24/96 or 24/192 hi-res
- per-track FLAC without enough disc context

Policy:

| Condition | Result |
|---|---|
| AR/CTDB verified complete CD rip | strong positive evidence |
| AR/CTDB no match | no bonus; not BLOCK |
| AR/CTDB mismatch with complete CUE/log | REVIEW_REQUIRED |
| no CUE/log | skip CTDB lane or treat as low confidence |

Do not block simply because CTDB has no match. No match can mean different pressing, missing pregap, trimmed silence, different mastering, missing database entry, or incomplete rip context.

Source: CUETools Database documentation.

### 5.5 Picard / beets / AcoustID / MBID

Picard, beets, AcoustID and MBID are useful for identity and metadata. They are not proof of audio quality.

Picard documentation describes AcoustID as an identification system: Picard generates a fingerprint, looks it up, and matches it to MusicBrainz recordings when possible. That is useful for deciding what recording/release a file likely represents, but it does not prove the file is authentic lossless.

Use these for:

- artist/album/release identity
- MusicBrainz Release ID / Recording IDs
- track order/count hints
- release-family matching
- reducing Lidarr release mismatch cases

Risks:

| Risk | Consequence |
|---|---|
| wrong MBID/release match | wrong Lidarr release context |
| Picard/beets writes tags | can conflict with Lidarr tag ownership |
| AcoustID matches recording, not necessarily exact release/master | false confidence |
| deluxe/Japan/bonus editions | edition mismatch |

Policy:

- Metadata match can add context.
- Metadata match should not override hard gates.
- Metadata match should not classify audio as lossless.
- MBID should not be a core V2 score component until a tested prepass exists.

Sources: MusicBrainz Picard AcoustID documentation and MusicBrainz AcoustID documentation.

### 5.6 Lidarr Quality Labels

Lidarr labels such as `FLAC`, `FLAC 24bit` and `MP3-320` are import/library classification, not authenticity proof.

Example:

```text
Lidarr: FLAC 24bit
ffprobe: 24/96 FLAC
FLAC Detective: fake_hi_res / SUSPICIOUS
```

The quality gate should trust the QC policy over the label. Lidarr is excellent for desired state, wanted/missing, cutoff-unmet strategy and library import, but it is not a source-authenticity verifier.

Policy:

- Use Lidarr quality as existing-library context.
- Use Lidarr album/release matching as import context.
- Do not use Lidarr quality label as audio proof.

## 6. Provenance Model

Source type is useful context, not a bypass.

| Source lane | Typical trust | Required gates | Optional evidence | Notes |
|---|---|---|---|---|
| TIDAL/Qobuz/official WEB | medium/high | ffprobe, flac -t, FLAC Detective | source metadata | can still be bad master or fake hi-res |
| CD-rip/private tracker/Norbits | medium/high | ffprobe, flac -t, FLAC Detective | CUETools/AR/CTDB, CUE/log | strongest lane when complete |
| Soulseek/slskd | variable | all hard gates + FLAC Detective | AcoustID/Picard context | provenance weak |
| YouTube/Tubifarry | low for lossless | codec gate, metadata | none as lossless proof | should never be treated as lossless upgrade |

Source type can influence score/recommendation, but it must never override hard failures.

## 7. Locked Invariants

These should be treated as repo-level invariants:

1. No import before hard gates are complete.
2. `ffprobe` fail or missing audio stream = BLOCK.
3. `flac -t` fail = BLOCK.
4. FLAC Detective unavailable = BLOCK / fail closed.
5. `SUSPICIOUS` can never replace an existing complete FLAC.
6. `SUSPICIOUS` can only become `ACCEPT_PROVISIONAL` when there is no copy, a clear upgrade from low-bitrate MP3, or a tested completeness-gain policy applies.
7. `FAKE_CERTAIN` is never automatically imported.
8. `fake_hi_res` = REVIEW_REQUIRED.
9. CUETools/CTDB no-match is not BLOCK.
10. Picard/beets/AcoustID/MBID are metadata identity signals, not audio-quality proof.
11. Lidarr quality labels are library/import state, not proof.
12. Do not auto-repair corrupt audio.
13. Do not auto-delete without sidecar/audit.
14. Source/provenance never bypasses hard gates.

### 7.1 Invariant Test Anchors

Every invariant should either have a concrete pytest anchor or be marked as a future-source-lane test. This table is intentionally pragmatic: it points future agents to the closest current enforcement point.

| # | Invariant | Current / planned test anchor |
|---:|---|---|
| 1 | No import before hard gates are complete | Covered indirectly by `_compute_verification` and V2 import-flow tests; add explicit orchestration test when sensor objects land |
| 2 | `ffprobe` fail or missing audio stream = BLOCK | `test_compute_verification_no_audio_files_is_not_validator_error`, `test_compute_verification_blocks_partial_codec_gate_download` |
| 3 | `flac -t` fail = BLOCK | `test_apply_overrides_hard_overrides_force_zero`, `test_decide_hard_overrides_block` |
| 4 | FLAC Detective unavailable = BLOCK / fail closed | `test_compute_verification_validator_error_blocks` |
| 5 | `SUSPICIOUS` never replaces existing complete FLAC | `test_decide_suspicious_existing_flac_blocks`, completeness-rule regression tests |
| 6 | `SUSPICIOUS` only provisional for no copy / low-MP3 upgrade / tested completeness gain | `test_decide_suspicious_without_existing_accepts_provisional`, `test_decide_suspicious_upgrade_accepts_provisional`, completeness-rule tests |
| 7 | `FAKE_CERTAIN` is never automatically imported | `test_decide_fake_with_existing_blocks`, `test_decide_fake_without_existing_goes_review_required`, fake import-flow tests |
| 8 | `fake_hi_res` = REVIEW_REQUIRED | `test_decide_fake_hi_res_review_required_regardless_of_score`, `test_compute_verification_fake_hi_res_requires_review` |
| 9 | CUETools/CTDB no-match is not BLOCK | Planned with CD-rip lane implementation |
| 10 | Picard/beets/AcoustID/MBID are metadata identity signals, not audio-quality proof | Planned with metadata lane implementation |
| 11 | Lidarr quality labels are library/import state, not proof | Covered by existing-kbps policy tests; add explicit dashboard/context test when sensor objects land |
| 12 | Do not auto-repair corrupt audio | Planned guard test if any repair/decode-through-errors code is introduced |
| 13 | Do not auto-delete without sidecar/audit | `test_discard_deletes_output_and_archives_sidecar`, expiry/blocked-sidecar tests |
| 14 | Source/provenance never bypasses hard gates | Planned with source-adapter lane implementation |

## 8. Dashboard Requirements

The dashboard should display the evidence from this quality stack. It should not compute new decisions client-side.

Every drawer should eventually show:

- hard gate status: `ffprobe`, `flac -t`
- spectral verdict and overrides
- per-file evidence where available
- source lane/provenance
- existing Lidarr quality/track state
- metadata identity signals if present
- action availability derived from backend policy
- concise explanation: "why this is review/block/import"

For `REVIEW_REQUIRED`, the UI must answer:

1. What sensor caused review?
2. Is this technically valid audio?
3. Is the uncertainty spectral, metadata, provenance, or import-related?
4. What exists in the library already?
5. What does Promote/Discard/Retry actually do?

## 9. Roadmap

### Current / V2

- ffprobe hard gate
- flac -t hard gate
- FLAC Detective spectral sensor
- completeness rule
- score + policy matrix
- sidecar + audit
- dashboard basic evidence

### Next Quality Improvements

1. Add explicit sensor result objects to sidecar:

```json
{
  "sensor": "ffprobe",
  "class": "hard_gate",
  "status": "pass",
  "severity": "none",
  "confidence": 1.0,
  "duration_ms": 123,
  "evidence": {}
}
```

2. Persist richer FLAC Detective evidence:
   - per-file verdict
   - sample rate / bit depth
   - cutoff Hz
   - Nyquist Hz
   - fake-hi-res reason
   - overrides and confidence
   - sensor/wrapper version

   This is the highest-value analysis improvement because it unlocks dashboard
   cutoff markers and makes manual review explainable without adding another tool.

3. Add timing per sensor:
   - download
   - ffprobe
   - flac -t
   - FLAC Detective
   - Lidarr ManualImport
   - queue cleanup

4. Make the shared ingest target explicit:
   - define `SourceAdapter` contract for all sources
   - keep TIDAL as first adapter
   - add SAB/qBittorrent/Soulseek lanes only after state storage and worker queue are ready
   - expose connector enable/disable + health in the dashboard once more than one lane exists
   - ensure every lane produces the same sensor result objects and sidecars before Lidarr import

5. Decide interception strategy per source:
   - SAB/Usenet: route through shared QC ingest before Lidarr import, or build a tested replacement for the relevant completed-download handling
   - qBittorrent: monitor completed category/path, run QC, then trigger Lidarr ManualImport
   - Soulseek/slskd: F3.5 completed-folder ingest and F3.5B slskd-backed search/download are implemented lanes that must continue to dogfood through the shared QC workflow
   - CD-rip/torrent lane: add CUE/log/CTDB evidence when present

6. Add CD-rip lane design:
   - detect `.cue` / `.log`
   - run CUETools only when the rip is complete enough
   - store CTDB/AR result as positive evidence or review trigger

7. Add metadata lane later:
   - Picard/beets/AcoustID read-only prepass first
   - no tag-writing until ownership with Lidarr is resolved
   - use as identity evidence, not core audio proof

8. Add dashboard evidence drawer:
   - sensor timeline
   - file table
   - source lane
   - "why this decision" explanation

### Not Now

- Full staging orchestrator.
- Replacing FLAC Detective.
- Trusting MBID as quality proof.
- Automatic repair.
- Automatic CTDB blocking on no-match.
- Treating YouTube/Tubifarry as lossless source.
- Disabling Lidarr Completed Download Handling globally without a tested replacement path.

## 10. Source Notes

These sources support the role/limit split above:

- FFmpeg ffprobe docs: https://ffmpeg.org/ffprobe.html
- Xiph FLAC command-line docs: https://xiph.org/flac/documentation_tools_flac.html
- Xiph FLAC format overview: https://xiph.org/flac/documentation_format_overview.html
- CUETools Database docs: https://cue.tools/wiki/CUETools_Database
- MusicBrainz Picard AcoustID docs: https://picard-docs.musicbrainz.org/en/latest/tutorials/acoustid.html
- MusicBrainz AcoustID docs: https://musicbrainz.org/doc/AcoustID
