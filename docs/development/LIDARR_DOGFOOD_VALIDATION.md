# Lidarr dogfood validation

> **Type:** Development runbook
> **Status:** active
> **Scope:** prove that Mintarr behaves as the quality gate in front of Lidarr

This runbook is for controlled dogfood sessions after changes to import policy,
source adapters, release matching, or library-quality evidence. It is not a
general operator guide.

## Goal

Validate the two hard product promises:

1. **Everything that is meant to be quality-gated reaches Lidarr only through
   Mintarr.**
2. **Automatic, review, and block paths behave exactly as the policy says.**

Do not enable stricter decision flags such as `MINTARR_MEASURED_EXISTING=true`
until these paths have been dogfooded against a real Lidarr instance.

## Preconditions

- Mintarr `/health` is `ok`.
- Lidarr health is `ok` or known unrelated warnings are documented.
- Mintarr is the tested Lidarr indexer/download client for the source under
  test.
- Other Lidarr indexers/download clients that could satisfy the same test grab
  are disabled or excluded from the test.
- `MINTARR_MEASURED_EXISTING=false` unless the test explicitly covers measured
  existing-library decisions.
- Library scans are idle unless the test explicitly covers import-priority
  yielding.
- API keys and private paths are not copied into the dogfood notes.

## Evidence to capture

For every dogfood candidate, record:

- Lidarr action used: search, manual grab, automatic search, or monitored queue.
- Mintarr record/JID.
- Source type: TIDAL, Soulseek, LocalFolder, SAB, qBit, or other.
- Mintarr decision: `ACCEPT`, `ACCEPT_PROVISIONAL`, `REVIEW_REQUIRED`, or `BLOCK`.
- Import outcome: imported, held for review, blocked, failed, rescued, or discarded.
- Lidarr queue state after completion.
- Whether files appeared in the final Lidarr library path.
- Relevant dashboard/sidecar evidence: codec gate, `flac -t`, FLAC Detective,
  release identity, measured existing-library evidence when enabled.

## Invariants

The dogfood run fails if any of these are violated:

- A completed import reaches the Lidarr library without a Mintarr record/JID.
- Mintarr calls Lidarr `ManualImport` for an item whose policy result is `BLOCK`.
- Mintarr calls Lidarr `ManualImport` for an item that is still
  `REVIEW_REQUIRED` and has not been explicitly promoted by an operator.
- A blocked/review-held record remains stuck in Lidarr's queue as an active
  retry loop.
- A successful Mintarr-owned import leaves a stale Mintarr-owned downloadId in
  Lidarr's queue.
- An unrelated Lidarr-native grab is silently treated as quality-gated by
  Mintarr.
- A hard evidence failure (`codec_mismatch`, real decode corruption, wrong album)
  is overridden by a better score on a lower-priority axis.

## Test matrix

### 1. Happy path: automatic import

Purpose: prove that known-good candidates flow through Mintarr and import without
manual intervention.

Expected result:

- Mintarr creates a record/JID.
- The source job completes.
- Hard gates pass.
- Release identity is same album or same release family.
- Mintarr calls Lidarr `ManualImport`.
- Lidarr imports the expected track count.
- Mintarr record lands in imported/rescued state.
- Lidarr queue does not retain a Mintarr-owned active row.

### 2. Review path: ambiguous but not hard-failed

Purpose: prove that uncertain evidence stops before Lidarr import and surfaces a
human decision.

Good candidates:

- ambiguous edition/release-family match;
- weak tag evidence;
- suspicious but not hard-blocking authenticity evidence;
- measured-existing comparison that requires review when the flag is enabled.

Expected result:

- Mintarr creates a record/JID.
- Decision is `REVIEW_REQUIRED`.
- No automatic `ManualImport` happens.
- The dashboard Review/Quality surface explains why it is held.
- Operator promote/discard works and writes an audit action.

### 3. Block path: hard failure

Purpose: prove that bad candidates never reach Lidarr's library.

Good candidates:

- non-FLAC candidate when FLAC/lossless is required;
- real FLAC decode corruption;
- wrong-album MBID/release identity;
- hard validator error.

Expected result:

- Mintarr decision is `BLOCK`.
- No Lidarr `ManualImport` happens.
- Files are not imported into the Lidarr library.
- The record explains the hard evidence that blocked it.
- Lidarr queue does not keep retrying the same Mintarr-owned item indefinitely.

### 4. Completed-folder source path

Purpose: prove that SAB/qBit/LocalFolder/Soulseek completed-folder lanes use the
same quality gate.

Expected result:

- The connector copies into Mintarr output and enqueues a normal source job.
- Adapter code does not call Lidarr `ManualImport` directly.
- Settle-window and partial-marker guards run.
- The same verification sidecar and decision path are used before any import.

### 5. Bypass audit

Purpose: prove the deployment shape matches ADR-0012 scoped-gate assumptions.

Check:

- Lidarr indexers/download clients for the tested source point at Mintarr.
- Any remaining Lidarr-native indexer/download client is intentionally outside
  the test scope and documented.
- Recent Lidarr imported items from the test window all have a corresponding
  Mintarr record/JID, unless explicitly marked out of scope.
- Mintarr-owned queue cleanup only touches Mintarr-owned download IDs.

## Current rollout rule

Keep `MINTARR_MEASURED_EXISTING=false` until at least one dogfood pass covers:

- one automatic happy-path import;
- one review-held candidate promoted or discarded by the operator;
- one blocked candidate confirmed not imported;
- one completed-folder ingest path, if that source remains enabled;
- a bypass audit confirming the tested source cannot reach Lidarr around Mintarr.

Only after that should measured-existing decision use be tested, and then as a
separate opt-in dogfood run with `MINTARR_REQUIRE_INTEGRITY=true` considered for
stricter evidence semantics.

## Dogfood log

### 2026-06-10 — TIDAL via Lidarr, Mintarr-only mode

Setup:

- Lidarr non-Mintarr indexers and native download clients were disabled for the
  test window.
- Lidarr's Mintarr/TidalHires indexer remained enabled for automatic and
  interactive search.
- Lidarr's Mintarr/TidalHires download client remained enabled.
- `MINTARR_MEASURED_EXISTING=false`; this was evidence-only for measured
  library quality.

Results:

- **Happy path passed:** Lidarr grabbed `ERA - ERA VIII (2026) [TIDAL] [FLAC
  24bit]` through Mintarr. Mintarr created JID `3b3b9b09fa98`, downloaded 10
  FLAC files, FLAC Detective returned `AUTHENTIC`, Lidarr ManualImport found 10
  candidates, and Mintarr recorded `ACCEPT` + `MANUAL_IMPORTED`. Lidarr showed
  10 imported trackfiles and no remaining queue row for the item.
- **Review/discard path passed after fix:** A `REVIEW_REQUIRED` Don Williams
  candidate was discarded. Dogfood exposed a stale dashboard-index bug where the
  sidecar lifecycle moved to discarded but the SQLite `records` index still
  listed the item under `needs_review`. Fixed in `7f6da83` by syncing discarded
  sidecars back into `state_db` and invalidating dashboard caches. A backfill
  repaired the live index; summary and records now agree (`needs_review=0`).
- **Bypass audit passed for the test window:** Lidarr queue was empty after the
  happy-path import, and the active Lidarr source/download path for the test was
  Mintarr-only.

Remaining before enabling measured-existing decisions:

- Dogfood one completed-folder source path if SAB/qBit/LocalFolder/Soulseek
  remains in scope for the first measured-existing rollout.
- Confirm a hard-block candidate still never reaches Lidarr ManualImport in the
  current runtime. Existing blocked records demonstrate the path historically,
  but a fresh post-cutover block run is still the stronger proof.
