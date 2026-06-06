# Sidecar Format — v2 (`verification.json`)

> **Type:** Spec / contract
> **Version:** 2.1.0 — runtime-validated 2026-06-04
> **Status:** Locked. Editorial fixes allowed; semantic changes require `SIDECAR_FORMAT_v3.md` per [ADR-0004](../architecture/adr/0004-api-versioning-semver.md).
> **Audience:** Anyone reading or writing `verification.json` files. Includes Mintarr code, external archival tools, dashboards built on top of Mintarr data, and audit consumers.

---

## 1. Why this spec exists

`verification.json` sidecars are **the source of truth** for Mintarr record state. The state_db (SQLite) is a query index rebuilt from sidecars; if the index is corrupted, it can be regenerated. The sidecar files themselves are the persistent record.

External tools (archival systems, backup scripts, custom dashboards) depend on the sidecar schema. Locking the schema means those tools can be written without coordinating with Mintarr maintainers.

This spec is named `v2` because the current shape is the second generation of Mintarr's verification format. v1 was an earlier ad-hoc shape; it is not used by current Mintarr code and is not documented. New deployments will only see v2 sidecars.

## 2. File location

Sidecars live in one of four locations during a record's lifecycle:

| State | Location |
|---|---|
| Active (Lidarr import in flight, REVIEW_REQUIRED waiting) | `<OUTPUT_BASE>/<jid>/verification.json` |
| BLOCK terminal | `/config/blocked_decisions/<jid>.json` |
| Discarded (operator action) | `/config/discarded/<jid>.json` |
| Expired REVIEW_REQUIRED | `/config/expired_review/<jid>.json` |

`<OUTPUT_BASE>` is the operator-configured output directory (typically `/output` inside the container). `<jid>` is the 12-character hex job ID.

Mintarr atomically writes sidecars via temp-file + rename. Readers should tolerate a momentary absence (the temp file briefly exists before rename) but the final file is always either fully written or absent.

## 3. Top-level schema

```json
{
  "jid": "0cd9dbf08198",
  "title": "Artist - Album (Year) [Source] [Quality]",
  "source_type": "tidal",
  "album_ids": [12345, 67890],
  "ts": 1779789600.0,
  "ts_iso": "2026-05-26T18:30:00",
  "verdict": "AUTHENTIC",
  "v2_verification_decision": "ACCEPT",
  "v2_import_outcome": "MANUAL_IMPORTED",
  "v2_score": 87,
  "v2_components": { ... },
  "v2_overrides": [],
  "release_identity_decision": "SAME_RELEASE",
  "release_identity_confidence": 98.0,
  "release_identity_reasons": ["matches current Lidarr release"],
  "release_identity_best_release_id": 456,
  "release_identity_current_release_id": 456,
  "reason": "passed validation",
  "sensors": [ ... ],
  "files": [ ... ],
  "lifecycle": { ... },
  "timings": { ... }
}
```

### 3.1 Top-level field reference

| Field | Type | Required | Description |
|---|---|---|---|
| `jid` | string | yes | 12-character hex job ID. Globally unique. |
| `title` | string | yes | Release title at grab time. May differ from current Lidarr title if metadata changed. |
| `source_type` | string | yes (v2.0.0+) | Source identifier (`tidal`, `local`, `soulseek`, future source ids, ...). v1 sidecars absent this default to `tidal` at read time. |
| `album_ids` | array of int | no | Lidarr album IDs matched at grab time. Empty array if no match. |
| `ts` | float | yes | Unix timestamp of sidecar write (most recent revision). |
| `ts_iso` | string | yes | ISO-8601 timestamp matching `ts`. For human readers. |
| `verdict` | string | yes | FLAC Detective verdict at verify time. One of: `AUTHENTIC`, `WARNING`, `SUSPICIOUS`, `FAKE_CERTAIN`, `UNKNOWN`. |
| `v2_verification_decision` | string | yes | V2 policy decision. See §4. |
| `v2_import_outcome` | string \| null | yes | Result of import after verification allowed it. See §5. |
| `v2_score` | int | yes | V2 score 0-100. See §6. |
| `v2_components` | object | yes | Per-component score breakdown. See §6.1. |
| `v2_overrides` | array of string | yes | Override tags applied. Examples: `codec_mismatch`, `flac_t_failed`, `validator_unavailable`. |
| `release_identity_decision` | string | yes (v2.1.0+) | F5.1 release-family identity axis decision. See §4.1. Older v2 readers should default missing values to `INSUFFICIENT_EVIDENCE`. |
| `release_identity_confidence` | number | yes (v2.1.0+) | Identity-axis confidence from 0-100. |
| `release_identity_reasons` | array[string] | yes (v2.1.0+) | Short audit reasons for the identity decision. |
| `release_identity_best_release_id` | int \| string \| null | yes (v2.1.0+) | Lidarr release id with the best identity match, when known. |
| `release_identity_current_release_id` | int \| string \| null | yes (v2.1.0+) | Lidarr release id active at verification time, when known. |
| `reason` | string | yes | Human-readable short reason for the decision. Legacy field name retained for deployed compatibility. |
| `decision` | string | no | Legacy decision label for older dashboards/log readers. New readers should prefer `v2_verification_decision` + `v2_import_outcome`. |
| `new_kbps` | int | no | Legacy approximate bitrate/quality signal for the new candidate. |
| `existing_quality` | string | no | Legacy description of the library item being compared against. |
| `existing_kbps` | int | no | Legacy approximate bitrate/quality signal for the existing library item. |
| `sensors` | array | yes for current writes | Per-verifier sensor results. See §7. Older pre-sensor-audit v2 sidecars may omit; readers should default to `[]`. |
| `files` | array | yes for current writes | Per-file evidence. See §8. Older pre-sensor-audit v2 sidecars may omit; readers should default to `[]`. |
| `lifecycle` | object | yes | Lifecycle state. See §9. |
| `timings` | object | no | Phase-by-phase timing data. See §10. |

## 4. `v2_verification_decision` values

One of:

| Value | Meaning |
|---|---|
| `ACCEPT` | All evidence supports authenticity; import proceeds normally. |
| `ACCEPT_PROVISIONAL` | Evidence is mixed; import proceeds with a warning recorded. |
| `REVIEW_REQUIRED` | Evidence is conflicting; import held for operator decision. |
| `BLOCK` | Evidence indicates fake / mislabelled audio; import rejected and files removed. |

The decision is determined by the V2 policy from sensor evidence at verify time.
From F5.1 onward, `v2_score` remains the audio-QC score, then the
release-family identity axis is combined with the audio decision using ADR-0013
precedence. A sidecar's `v2_verification_decision` is immutable for the life of
the record — operator promotion of a REVIEW_REQUIRED record adds a `promoted_at`
timestamp under `lifecycle` but does not change the decision.

### 4.1 Release-family identity decisions

| Value | Effect |
|---|---|
| `SAME_RELEASE` | Preserve the audio-axis decision. |
| `SAME_FAMILY` | Preserve the audio-axis decision; the best match may be another Lidarr release in the same album family. |
| `AMBIGUOUS_EDITION` | Route audio ACCEPT / ACCEPT_PROVISIONAL to `REVIEW_REQUIRED`. Never hard-blocks on its own. |
| `INSUFFICIENT_EVIDENCE` | Route audio ACCEPT / ACCEPT_PROVISIONAL to `REVIEW_REQUIRED`. Used when Lidarr or tag evidence is too thin. |
| `WRONG_ALBUM` | Force `BLOCK` unless the audio axis already blocked first. Strong MBID mismatch only. |

Precedence is top-to-bottom: audio hard-blocks win first, then `WRONG_ALBUM`
blocks, then audio review wins, then ambiguous/insufficient identity routes to
review. A clean audio result cannot rescue `WRONG_ALBUM`, and weak identity
evidence cannot create a hard block.

## 5. `v2_import_outcome` values

One of:

| Value | Meaning |
|---|---|
| `null` | No import attempt yet (record is in REVIEW_REQUIRED state, or BLOCK skipped import). |
| `MANUAL_IMPORTED` | Lidarr's ManualImport API accepted the files. Success. |
| `RESCUED` | ManualImport failed; place-files-and-rescan recovered. Success with caveat. |
| `FAILED` | Both ManualImport and rescue failed. Operator action may help. |
| `PENDING` | Lidarr accepted the import as a long-running command and Mintarr stopped waiting. May resolve on its own. |
| `SKIPPED` | Verification BLOCK prevented import from being attempted. |

`v2_import_outcome` may be updated by operator actions (`promote_import`, `retry_import`). Each update bumps `ts` and writes a new sidecar.

## 6. `v2_score` and `v2_components`

`v2_score` is an integer 0-100 produced by V2 verification logic. It is a weighted sum of components; the deployed runtime surfaces the integer component contributions in `v2_components` for debuggability and auditability.

Score thresholds drive decisions:

| Score | Default decision (subject to overrides) |
|---|---|
| 70-100 | `ACCEPT` |
| 50-69 | `ACCEPT_PROVISIONAL` |
| 20-49 | `REVIEW_REQUIRED` |
| 0-19 | `BLOCK` |

Overrides (in `v2_overrides`) can downgrade decisions regardless of score (e.g., `codec_mismatch` forces BLOCK regardless of score).

### 6.1 `v2_components` shape

```json
{
  "v2_components": {
    "ffprobe": 25,
    "flac_t": 25,
    "detective": 35,
    "complete": 15
  }
}
```

Each component is the number of score points contributed by that stage. The aggregate `v2_score` is the sum of these values after hard overrides have been applied.

Current component names are:

| Component | Meaning |
|---|---|
| `ffprobe` | Codec/container hard-gate contribution |
| `flac_t` | FLAC integrity hard-gate contribution |
| `detective` | FLAC Detective spectral-analysis contribution |
| `complete` | Album completeness / track-count contribution |

Readers should tolerate unknown future component names.

## 7. `sensors` array

Each entry is one verifier's run on this record.

```json
{
  "sensors": [
    {
      "name": "ffprobe",
      "class": "hard_gate",
      "status": "pass",
      "severity": "none",
      "confidence": 1.0,
      "duration_ms": 132,
      "evidence": {
        "codec_skipped": 0,
        "codec_accepted": 11
      }
    },
    {
      "name": "flac_t",
      "class": "hard_gate",
      "status": "pass",
      "severity": "none",
      "confidence": 1.0,
      "duration_ms": 8542,
      "evidence": {
        "files_tested": 11,
        "files_failed": 0
      }
    },
    {
      "name": "flac_detective",
      "class": "spectral_heuristic",
      "status": "pass",
      "severity": "none",
      "confidence": 0.92,
      "duration_ms": 4123,
      "evidence": {
        "verdict": "AUTHENTIC",
        "file_count": 11,
        "suspicious_files": 0
      }
    },
    {
      "name": "release_identity",
      "class": "metadata_identity",
      "status": "warn",
      "severity": "info",
      "confidence": 0.3,
      "summary": "Observed release identity evidence uses filename fallback only.",
      "evidence": {
        "file_count": 11,
        "track_titles": ["track one", "track two"],
        "artist_mbids": [],
        "release_group_mbids": [],
        "release_mbids": [],
        "mutagen_available": true,
        "tag_read_errors": 0
      }
    }
  ]
}
```

### 7.1 Sensor entry fields

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Sensor identifier; matches the connector ID for verifier connectors. |
| `sensor_version` | string | no | Wrapper/policy implementation version that produced this sensor entry. |
| `binary_version` | string \| null | no | External binary version when known. |
| `policy_version` | string | no | Policy/spec version used when interpreting the sensor output. |
| `evidence_schema_version` | string | no | Sensor-specific evidence payload version. |
| `class` | string | yes | Sensor category: `hard_gate`, `spectral_heuristic`, `source_specific_proof`, `metadata_identity`, `provenance`, `library_state`. (`source_specific_proof` covers the CD-rip evidence lane.) |
| `status` | string | yes | One of: `pass`, `fail`, `warn`, `error`, `skipped`. |
| `severity` | string | yes | One of: `none`, `info`, `warning`, `error`, `blocker`. |
| `confidence` | float | yes | 0.0-1.0 — how confident the sensor is in its verdict. |
| `duration_ms` | int \| null | yes | Sensor runtime in milliseconds when measured. |
| `summary` | string | no | Human-readable sensor summary for dashboard display. |
| `evidence` | object | yes | Sensor-specific evidence payload. Sensor-defined schema; not normalized. |

### 7.2 `release_identity` evidence

F5.1 adds a read-only `release_identity` metadata sensor. It collects observed
release metadata from audio file tags with filename fallback and persists the
identity result used by the release-family policy axis. The sensor never changes
`v2_score` or `v2_overrides`; `v2_verification_decision` is changed only by the
explicit policy combiner described in §4.1.

Current evidence keys:

| Field | Type | Description |
|---|---|---|
| `file_count` | int | Number of supported audio files inspected. |
| `track_titles` | array[string] | Normalized observed track titles, from tags when possible, otherwise filenames. |
| `artist_names` | array[string] | Artist names observed in file tags. Empty when unavailable. |
| `album_titles` | array[string] | Album titles observed in file tags. Empty when unavailable. |
| `artist_mbids` | array[string] | MusicBrainz artist IDs observed in tags. |
| `release_group_mbids` | array[string] | MusicBrainz release-group IDs observed in tags. |
| `release_mbids` | array[string] | MusicBrainz release IDs observed in tags. |
| `mutagen_available` | bool | Whether the Mutagen reader was importable at runtime. |
| `tag_read_errors` | int | Number of files where tag reading failed before filename fallback. |
| `files` | array[object] | Per-file metadata evidence: path, raw title, normalized title, optional artist/album/MBIDs, tag source, and tag-read error. |
| `identity_decision` | string | Same value as top-level `release_identity_decision`. |
| `identity_confidence` | number | Same value as top-level `release_identity_confidence`. |
| `identity_reasons` | array[string] | Same value as top-level `release_identity_reasons`. |
| `best_release_id` | int \| string \| null | Best matching Lidarr release id, when known. |
| `current_release_id` | int \| string \| null | Lidarr current release id, when known. |
| `score` | number | Raw identity match score from 0-100. |
| `track_count_delta` | int \| null | Absolute difference between observed file count and expected track count. |
| `title_similarity` | number \| null | Fraction of observed normalized titles that matched expected titles, when both sides had titles. |

## 8. `files` array

Per-file evidence from FLAC Detective. Surfaces in the dashboard drawer for inspection.

```json
{
  "files": [
    {
      "filename": "01 Track One.flac",
      "sample_rate": 96000,
      "bit_depth": 24,
      "cutoff_hz": 47500,
      "nyquist_hz": 48000,
      "detective_verdict": "AUTHENTIC",
      "is_fake_high_res": false,
      "estimated_mp3_bitrate": null,
      "evidence": { ... }
    }
  ]
}
```

### 8.1 File entry fields

| Field | Type | Required | Description |
|---|---|---|---|
| `filename` | string | yes | File basename. |
| `size_bytes` | int \| null | no | File size when available. |
| `sample_rate` | int \| null | yes | Sample rate in Hz. Null for non-audio files. |
| `bit_depth` | int \| null | yes | Bit depth (16, 24). Null for non-audio. |
| `duration_sec` | float \| null | no | Duration when available. |
| `estimated_kbps` | int \| null | no | Approximate bitrate when available. |
| `cutoff_hz` | float \| null | yes | Spectral cutoff frequency. Null if not measured. |
| `nyquist_hz` | float \| null | yes | Theoretical Nyquist (sample_rate / 2). Null for non-audio. |
| `detective_verdict` | string \| null | yes | Per-file verdict from FLAC Detective. |
| `is_fake_high_res` | bool | yes | True if spectral cutoff suggests upsampling. |
| `estimated_mp3_bitrate` | int \| null | yes | If suspicious, FLAC Detective's estimate of equivalent MP3 bitrate. |
| `wrapper_overrides` | array | no | Per-file overrides applied by Mintarr's FLAC Detective wrapper. |
| `error` | string \| null | no | Per-file analysis error if the wrapper recorded one. |
| `evidence` | object | no | Sensor-specific extra fields. |

## 9. `lifecycle` object

Tracks the record's state machine over time.

```json
{
  "lifecycle": {
    "state": "promoted",
    "created_at": 1779789600.0,
    "promoted_at": 1779790200.0,
    "promoted_by": "operator-via-dashboard",
    "discarded_at": null,
    "expired_at": null,
    "blocklist_policy": "blocklist_now",
    "blocklist_status": "done"
  }
}
```

### 9.1 Lifecycle states

| Value | Meaning |
|---|---|
| `created` | Initial state after sidecar is first written. |
| `pending_review` | V2 returned REVIEW_REQUIRED; waiting for operator. |
| `promoted` | Operator promoted a REVIEW_REQUIRED record; import was attempted. |
| `discarded` | Operator discarded the record; files removed. |
| `expired` | REVIEW_REQUIRED record was auto-expired after retention period. |

### 9.2 Lifecycle fields

| Field | Type | Required | Description |
|---|---|---|---|
| `state` | string | yes | Current lifecycle state. |
| `created_at` | float | yes | Unix timestamp of first sidecar write. |
| `promoted_at` | float \| null | yes | When operator promoted; null if never promoted. |
| `promoted_by` | string \| null | no | Identifier for the promoter (`operator-via-dashboard`, `api-key:xxx`, `auto-policy`). |
| `discarded_at` | float \| null | yes | When operator discarded; null if not discarded. |
| `expired_at` | float \| null | yes | When auto-expired; null if not expired. |
| `blocklist_policy` | string | no | Mintarr's blocklisting policy for this record. One of `no_blocklist`, `blocklist_now`, `defer_to_operator`. |
| `blocklist_status` | string | no | One of `pending`, `done`, `failed`, `skipped`. |

## 10. `timings` object

Phase-by-phase timing in seconds.

```json
{
  "timings": {
    "tidal_download_sec": 36.4,
    "postprocess_sec": 0.85,
    "pre_import_total_sec": 38.2,
    "lidarr_precheck_sec": 2.43,
    "flac_detective_sec": 4.12,
    "lidarr_manualimport_sec": 1.84,
    "queue_cleanup_sec": 0.06
  }
}
```

Field names are conventional and may evolve. Readers should tolerate unknown keys. All values are non-negative floats representing seconds.

## 11. Reading older sidecars

For sidecars written by Mintarr versions that predate the source-type field, readers SHOULD default `source_type` to `"tidal"`. Reason: pre-v2 sidecars existed only on TIDAL grabs.

```python
source_type = sidecar.get("source_type") or "tidal"
```

Early v2 sidecars written before the sensor-audit expansion may also omit `sensors` and `files`. Readers SHOULD treat missing values as empty arrays:

```python
sensors = sidecar.get("sensors") or []
files = sidecar.get("files") or []
```

Current runtime writes both fields for new sidecars. Missing fields should be displayed as "no detailed evidence recorded" rather than as a corrupt record.

## 12. Atomic write protocol

Mintarr writes sidecars via:

1. Write content to `<target>.tmp` with `fsync()` before close
2. `os.rename("<target>.tmp", "<target>")` (POSIX-atomic on same filesystem)

Readers may encounter `<target>.tmp` briefly between steps 1 and 2 but should ignore it. The `<target>` file is always either fully written or absent.

This guarantee depends on `<target>` and `<target>.tmp` being on the same filesystem. Mintarr ensures this by using the same directory.

## 13. Invariants

These hold for all sidecars Mintarr writes:

1. `jid` matches the filename: `<jid>.json` or `<jid>/verification.json`
2. `ts` is non-decreasing across sidecar revisions for the same `jid`
3. `lifecycle.state` transitions are monotonic in this DAG: `created → pending_review → promoted | discarded | expired`. (Other transitions are spec violations.)
4. `v2_verification_decision` does not change after first write
5. `v2_import_outcome` may change on subsequent operator actions (promote, retry_import); each change bumps `ts`
6. `sensors[].class` values match the sensor's connector kind classification
7. Aggregate `v2_score` equals the sum of integer values in `v2_components` after hard overrides are applied

Mintarr's tests check most of these. External readers should rely on them but tolerate the rare violation; loud failure is worse than degraded display.

## 14. Backward compatibility for readers

When `SIDECAR_FORMAT_v3.md` ships, v2 sidecars will continue to exist on disk indefinitely. External tools targeting v2 should:

- Pin against `SIDECAR_FORMAT_v2.md`
- Detect format version by presence of a field that exists in one and not the other
- Default to v2 semantics on ambiguity (v2 sidecars will outnumber v3 for years)

Mintarr will publish a `convert_v2_to_v3.py` script in the same release that ships v3, allowing operators to upgrade existing sidecars in place. The script's behaviour is documented in that release's `UPGRADE_GUIDE.md`.

## 15. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0.0 | 2026-05-26 | Initial locked spec. Documents the current format which has been deployed since F1 (state index + sensor evidence). |
| 2.0.1 | 2026-05-31 | Corrected field names and sensor enums to match deployed sidecars (`v2_components`, `reason`, `pass`/`none`, `warn`/`warning`). Marked draft until public cutover validation. |
| 2.0.2 | 2026-05-31 | Locked after fixture validation against three live sidecars (imported, blocked, review). Added deployed optional legacy fields, sensor metadata fields, file metadata fields and `severity=info`. |

---

> Last updated: 2026-05-31
