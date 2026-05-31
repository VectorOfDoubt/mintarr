# Pipeline

> **Type:** Architecture / subsystem
> **Version:** 1.0 — 2026-05-26
> **Status:** Living document. Phase boundaries are stable; per-phase implementation evolves.
> **Audience:** Anyone touching `app/pipeline.py` or any of the four pipeline phases. Contributors writing source adapters need to understand the phase boundaries to know what their adapter does and does not own.
> **Related:** [OVERVIEW.md](OVERVIEW.md), [ADAPTER_PROTOCOL_v1.md](../specs/ADAPTER_PROTOCOL_v1.md)

---

## 1. The four phases

Mintarr's pipeline runs every grab through four named phases:

```
┌──────────────┐    ┌────────────────┐    ┌──────────┐    ┌──────────────────┐
│ download_raw │───>│ normalize_audio│───>│  verify  │───>│ import_to_lidarr │
│   (adapter)  │    │    (common)    │    │ (common) │    │    (common)      │
└──────────────┘    └────────────────┘    └──────────┘    └──────────────────┘
       │                    │                  │                    │
       │                    │                  │                    │
       ▼                    ▼                  ▼                    ▼
   ctx.raw_dir       ctx.raw_dir       sensor evidence       ctx.output_dir
   filled with       contains          + V2 decision         + Lidarr import
   adapter output    normalized FLAC                         attempt
```

The phases are explicit functions in `app/pipeline.py`. Adding a source adapter means writing `download_raw()`. The other three are common code that applies to every source.

This document specifies what each phase does, what invariants it maintains, and where its boundaries are.

## 2. Phase 1 — `download_raw`

**Owner:** the source adapter. The pipeline calls `adapter.download_raw(candidate_id, ctx)`.

**Input:** `candidate_id` (the adapter's source_id) and a `PipelineContext`.

**Output:** a `RawDownload` dataclass.

**Side effect:** files placed under `ctx.raw_dir`.

### 2.1 What download_raw does

For each source type, this varies:

- **TIDAL:** invokes `tidal-dl-ng dl <url>` as a subprocess. Output is `.m4a` files (and sometimes pre-converted `.flac`) under `ctx.raw_dir`.
- **LocalFolder:** copies files from `LOCAL_INGEST_PATH/<rel-path>/` to `ctx.raw_dir`. Source files are not modified.
- **Soulseek (planned):** copies files from the slskd completed-download root to `ctx.raw_dir`, with completed-folder checks first.
- **Future sources:** follow the same pattern — produce files in `ctx.raw_dir`.

### 2.2 What download_raw does not do

- Codec verification. That's normalize_audio's job.
- File integrity checking. That's normalize_audio's job.
- Quality scoring. That's verify's job.
- Lidarr import. That's import_to_lidarr's job.
- Source file modification. Adapters copy, never move or delete.

### 2.3 Phase 1 invariants

1. **Files go into `ctx.raw_dir` only.** Writing elsewhere is a spec violation.
2. **The adapter calls `ctx.check_cancelled()` at natural checkpoints.** Long-running adapters that don't check cancel are user-hostile.
3. **The adapter calls `ctx.set_progress(...)` at meaningful state transitions.** The dashboard polls progress; silent adapters look stuck.
4. **The adapter uses `ctx.run_subprocess(...)` for child processes.** Direct `subprocess.run` bypasses cancel handling and timeout management.
5. **The adapter raises on permanent failures.** Returning a `RawDownload` with `file_count=0` is forbidden; raise instead.

### 2.4 download_raw cancellation

When the operator cancels:

1. `cancel_requested` is set in the `jobs` table
2. The adapter's next `ctx.check_cancelled()` raises `worker.JobCancelled`
3. The adapter does NOT catch `JobCancelled` — it propagates up
4. The pipeline catches it in `execute_source_grab` and calls `adapter.cleanup(jid, ctx)`
5. The worker marks the job cancelled

If the adapter ignores cancel for >30 seconds, the dashboard surfaces a warning. The worker may force-kill subprocesses launched via `ctx.run_subprocess` after a longer timeout.

## 3. Phase 2 — `normalize_audio`

**Owner:** the common pipeline (`pipeline.normalize_audio(raw_dir, ctx)`).

**Input:** files under `raw_dir` and a `PipelineContext`.

**Output:** a `NormalizeStats` dataclass with counters (`codec_gate_skipped`, `conversion_failed`, `integrity_failed`).

**Side effect:** files in `raw_dir` are converted, validated, and trimmed.

### 3.1 What normalize_audio does

For each `.m4a` file in `raw_dir`:

1. **Codec gate:** ffprobe checks that the audio stream codec is `flac` or `alac`. If it's `aac` or another lossy codec, the file is rejected (`m.unlink()` and `codec_gate_skipped += 1`).
2. **Conversion:** ffmpeg copy (bit-perfect FLAC stream extraction from MP4 container). Fallback to ffmpeg re-encode (still lossless since the codec is FLAC/ALAC).
3. **Integrity check:** `flac -t` on the resulting `.flac` file. If it fails, the file is deleted (`integrity_failed += 1`).

For each `.flac` file directly under `raw_dir` (e.g., adapter delivered FLAC, not M4A):

1. **Integrity check:** `flac -t`. Failed files deleted.

The result: `raw_dir` contains only files that passed the codec gate and integrity check. Files that failed are gone.

### 3.2 What normalize_audio does not do

- Spectral analysis. That's verify's job (FLAC Detective).
- V2 scoring. That's verify's job.
- Moving files. The files stay in `raw_dir`; the next phase moves them.
- Tag writing or metadata fixing. Out of scope (ADR-0008).

### 3.3 Phase 2 invariants

1. **Files outside `raw_dir` are not touched.** Source files (adapter's input) are not modified.
2. **Non-audio files (cover art, log files, etc.) are preserved.** Only `.m4a` and `.flac` are processed.
3. **Failed files are deleted, not quarantined.** A file that fails codec gate or integrity check is gone after this phase.
4. **`ctx.check_cancelled()` is called between files.** Cancel during normalize_audio works.
5. **Phase 2 emits progress.** `ctx.set_progress(stage="postprocess", ...)` so the dashboard shows progress.

### 3.4 Why codec gate and integrity check live in normalize_audio

A common question: why not run codec gate as part of verify (Phase 3)?

The answer: codec gate operates on the raw `.m4a` container before ffmpeg conversion. Running it later, after the file has been converted to `.flac`, would lose information — ffmpeg's `copy` codec copies the inner stream verbatim, so an AAC-in-MP4 ends up as AAC-in-FLAC-container which ffprobe would still identify as FLAC. The codec gate must run on the original container.

flac integrity check (`flac -t`) similarly tests the bitstream that exists right after conversion. Moving it to verify would mean re-reading the same files; the cost is unchanged but the boundary becomes muddier.

## 4. Phase 3 — `verify`

**Owner:** the common pipeline.

**Input:** files under `raw_dir` after normalize_audio.

**Output:** a `VerificationResult` (in `app/verification.py`).

**Side effect:** sensor evidence collected; V2 policy decision made.

### 4.1 What verify does

1. **flac-detective HTTP call.** Sends `{"path": <output_dir>}` to the FLAC Detective service. Receives per-file spectral analysis + overall verdict.
2. **V2 component scoring.** Computes scores for codec_integrity, flac_integrity, spectral, completeness. Each gets a weight; aggregate is `v2_score`.
3. **Override application.** Hard-fail conditions (codec mismatch, flac -t failed, validator unavailable) force overrides regardless of score.
4. **Policy decision.** Maps score and overrides to one of `ACCEPT` / `ACCEPT_PROVISIONAL` / `REVIEW_REQUIRED` / `BLOCK`.

### 4.2 V2 decision logic

The decision is determined by score thresholds, with overrides taking precedence:

| Condition | Decision |
|---|---|
| Override `codec_mismatch` present | `BLOCK` |
| Override `flac_t_failed` present | `BLOCK` |
| Override `validator_unavailable` present | `BLOCK` (fail-closed) |
| Verdict `FAKE_CERTAIN` with existing high-quality library entry | `BLOCK` |
| Verdict `SUSPICIOUS` with existing high-quality library entry | `BLOCK` |
| Verdict `SUSPICIOUS` without existing or ≥20% kbps upgrade | `ACCEPT_PROVISIONAL` |
| Verdict `FAKE_CERTAIN` without existing | `REVIEW_REQUIRED` |
| Score 70-100 | `ACCEPT` |
| Score 50-69 | `ACCEPT_PROVISIONAL` |
| Score 20-49 | `REVIEW_REQUIRED` |
| Score 0-19 | `BLOCK` |

The full decision matrix is in `app/verification.py`. Changes require a regression test demonstrating the old behaviour and a new test demonstrating the new behaviour.

### 4.3 What verify does not do

- File modification. Verify reads files; never modifies them.
- Lidarr communication. That's import_to_lidarr.
- Triggering operator notifications. The decision is recorded in the sidecar; notifications are Phase 3 (observability) work.

### 4.4 Phase 3 invariants

1. **Verify is deterministic for a given input.** Same files + same verdict → same decision. Tested explicitly.
2. **Hard-gate failures cannot be overridden by score.** Codec mismatch always BLOCKs regardless of how good other components look.
3. **Validator unavailable is fail-closed.** If FLAC Detective times out or returns an error, the decision is BLOCK. Mintarr does not "trust" sources that the verifier could not check.
4. **Verify does not write the sidecar yet.** That happens in import_to_lidarr alongside the actual import attempt, so the sidecar contains both the verification decision and the import outcome.

## 5. Phase 4 — `import_to_lidarr`

**Owner:** the common pipeline (delegates to `server._trigger_lidarr_import`).

**Input:** files under `raw_dir`, the V2 VerificationResult, and `PipelineContext`.

**Output:** an updated `VerificationResult` with `import_outcome` set.

**Side effect:** files moved to `output_dir`; sidecar written; Lidarr ManualImport (or rescue) attempted.

### 5.1 What import_to_lidarr does

1. **Prepare output:** moves files from `raw_dir` to `output_dir` (typically `OUTPUT_BASE/<jid>/`). Computes final stats.
2. **Sidecar write:** writes `verification.json` with the V2 decision.
3. **Decision branching:**
   - **BLOCK or SKIPPED:** sidecar moved to `/config/blocked_decisions/<jid>.json`. Files deleted. Blocklist trigger sent to Lidarr. No import attempt.
   - **REVIEW_REQUIRED:** sidecar kept in `output_dir`. Files preserved. Lidarr queue cleaned up so the grab doesn't loiter. Dashboard surfaces the record.
   - **ACCEPT or ACCEPT_PROVISIONAL:** Lidarr ManualImport API called. On failure, rescue path (place-files-and-rescan) attempted. Import outcome recorded in sidecar.

### 5.2 What import_to_lidarr does not do

- Verification. That's verify (Phase 3). By the time we reach this phase, the decision is made.
- Modify file contents. Files are moved verbatim.
- Operator notification. Notifications are observability work.

### 5.3 Phase 4 invariants

1. **BLOCK decisions never reach Lidarr.** The blockade is enforced; sidecar records the prevention.
2. **REVIEW_REQUIRED never auto-imports.** Operator action required.
3. **Sidecar is the source of truth.** Mintarr writes the sidecar before attempting Lidarr ManualImport; if Lidarr is unreachable, the sidecar remains and the operator can retry.
4. **File preservation through rescue path.** Files are moved (not copied) to output_dir; rescue path copies from output_dir to library; original output_dir copy is preserved until rescue succeeds.

### 5.4 The rescue / place-files-and-rescan path

When `POST /api/v1/command` with `ManualImport` fails (Lidarr matched but rejected, typically due to release-family mismatch), Mintarr falls back to a rescue path:

1. Read Lidarr's album and artist data for the matched album
2. Sanitise artist + album names for path safety
3. Copy files from `output_dir` to `/music/<artist>/<album>/`
4. Trigger Lidarr `RescanFolder` command on the library root
5. Lidarr finds the files in the next scan and imports them as library files (not via download client), bypassing the 80%-match-bug

The rescue path is gated by `MINTARR_RESCUE_RESCAN_ENABLED=true`. Default false because broad library rescans can be disruptive in some operator setups.

## 6. PipelineContext

`PipelineContext` is the runtime handle the adapter sees. Its full Protocol is in [`ADAPTER_PROTOCOL_v1.md` §6](../specs/ADAPTER_PROTOCOL_v1.md#6-pipelinecontext-the-runtime-handle).

Summary of what's exposed:

- `jid` — the job ID for correlation
- `worker_job_id` — the state_db.jobs row ID
- `raw_dir` — adapter writes here in Phase 1
- `output_dir` — pipeline moves files here in Phase 4
- `check_cancelled()` — raises if cancel requested
- `run_subprocess(...)` — cancellable + timeout-managed subprocess execution
- `set_progress(...)` — dashboard + worker queue progress updates
- `log(...)` — structured logging with adapter name prefix

## 7. Coupling rules

These hold for the pipeline subsystem and are tested.

| Rule | Why |
|---|---|
| Adapters do not import from `pipeline.py` | Decouples adapter contract from pipeline internals |
| Pipeline does not know which adapter ran | The pipeline operates on `RawDownload` + `ctx`; adapter identity is opaque |
| Each phase is independently testable | Phase boundaries are explicit function calls in `execute_source_grab` |
| Phase 3 (verify) and Phase 4 (import) are factored apart | Even though they share code today (V2 verification runs inside `_trigger_lidarr_import`), the design treats them as separate phases |
| No phase writes outside `ctx.raw_dir` / `ctx.output_dir` | File-system containment for safety |

Violating these is a structural regression and is caught by both tests and PR review.

## 8. What happens on failure

Failure handling per phase:

| Phase | Failure mode | Worker outcome | Operator visibility |
|---|---|---|---|
| download_raw | `RuntimeError` raised by adapter | job state=failed, result_state=failed | dashboard shows "Failed: <adapter message>" |
| download_raw | `JobCancelled` raised | job state=cancelled | dashboard shows "Cancelled" |
| normalize_audio | All files fail codec gate / integrity | pipeline continues with 0 files; verify phase sees no audio | dashboard shows the V2 BLOCK reason |
| verify | FLAC Detective unreachable | V2 decision = BLOCK with `validator_unavailable` override | dashboard shows "Blocked: validator unavailable" |
| import_to_lidarr | ManualImport fails AND rescue fails | result_state=failed; sidecar `v2_import_outcome=FAILED` | dashboard shows record with "Import failed" status |
| import_to_lidarr | Lidarr unreachable | RuntimeError raised; worker may retry | dashboard shows job in retry state |

The worker's retry policy (`worker._is_transient_failure`) classifies errors. Allow-list strings trigger retry; deny-list strings do not.

## 9. Future direction

- **Hoist verification fully out of import_to_lidarr.** Today the V2 verification logic lives inside `_trigger_lidarr_import` for legacy reasons. Phase 4 cross-cutting work will refactor this so Phase 3 (verify) is a standalone function returning a VerificationResult that Phase 4 consumes.
- **Streaming download_raw for very large transfers.** Today `download_raw` is synchronous; future v2 of the adapter protocol may add a generator-based variant.
- **Per-source pipeline customisation.** F5.2 introduces source-aware verification thresholds; the pipeline plumbing exists, the policy doesn't yet.

These are tracked in [ROADMAP.md](../strategy/ROADMAP.md).

---

> Last updated: 2026-05-26
