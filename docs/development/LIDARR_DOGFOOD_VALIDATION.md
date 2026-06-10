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

### 2026-06-10 (cont.) — completed-folder (LocalFolder) + hard-block path (Claude)

Setup unchanged: Mintarr-only Lidarr config, `MINTARR_MEASURED_EXISTING=false`,
`MINTARR_REQUIRE_INTEGRITY` unset.

- **Hard-block path verified (code + live residue):** the V2 `BLOCK` branch
  aborts *before* Lidarr ManualImport, sets outcome `SKIPPED`, blocklists the
  grabbed release via `/history/failed/<id>`, calls `_cleanup_lidarr_queue`, and
  removes the output dir (containment-checked). Live state corroborates: 18
  archived block sidecars / 19 `blocked` decisions, with Lidarr `queue_total=0`
  and `0` active/blocking commands — no stuck queue rows or retry loops. A fresh
  fabricated-bad-candidate block run remains the only stronger proof.
- **Completed-folder (LocalFolder) lane passed end-to-end:** a controlled
  synthetic-but-genuine 16/44 FLAC album (non-monitored artist "Mintarr
  Dogfood") was dropped into `LOCAL_INGEST_PATH` and triggered via
  `POST /local/ingest`. JID `7588c40967a6` ran the **same QC gate** (ffprobe
  pass, `flac -t` pass, FLAC Detective `pass`/authentic on real lossless),
  release identity returned `AMBIGUOUS_EDITION`/warn (correctly unidentifiable),
  and the two-axis policy produced `REVIEW_REQUIRED` — clean audio but weak
  identity held for review, **not auto-imported**. Sidecar written with full
  sensor evidence; `imported` stayed 28; Lidarr untouched (no ManualImport, queue
  0). Confirms the local lane shares the QC/sidecar/decision flow and never
  auto-imports an unidentifiable release.
- **Discard-sync re-validated:** discarding the test record via
  `POST /verification/<jid>/discard` moved `needs_review` 0 and `discarded`
  13→14 with no backfill needed — the `7f6da83` fix holds on a fresh record.

Remaining: fresh fabricated hard-block grab; SAB/qBit completed-folder lanes
(after LocalFolder/Soulseek); then consider `MINTARR_MEASURED_EXISTING=true`
(optionally with `MINTARR_REQUIRE_INTEGRITY=true`).

### 2026-06-10 (cont.) — fresh fake-hi-res via local lane (Claude)

Attempted a fresh hard-block by ingesting fabricated fake-hi-res FLAC through the
local lane (fully contained — a local grab has no Lidarr download to blocklist or
dequeue). Findings:

- **FLAC Detective works on real transcodes.** A genuine fake (real library track
  → MP3 96k/48k → repacked 24/96 FLAC) was flagged `SUSPICIOUS` with reason
  "Looks like upsampled hi-res: useful high-frequency content stops at the file's
  technical cutoff" (detective score 5). A naive synthetic fake (lowpassed pink
  noise repacked 24/96) was *not* flagged (`AUTHENTIC`) — Detective keys on real
  transcode artifacts, not a bare spectral cutoff, so it does not false-positive
  on non-transcode content.
- **`SUSPICIOUS` → `REVIEW_REQUIRED`, never auto-import.** With no existing-library
  downgrade context and no `FAKE_CERTAIN` verdict, a suspicious fake routes to
  operator review (two-axis: suspicious audio + `AMBIGUOUS_EDITION` identity), is
  retained not imported, and never calls Lidarr ManualImport. `imported` stayed
  28, Lidarr queue 0 throughout. This is the key safety property: bad/fake audio
  is never silently imported.
- **A hard `BLOCK` needs `FAKE_CERTAIN` or a downgrade-vs-existing comparison** —
  neither is reproducible on the local lane with synthetic non-monitored content.
  The 19 historical blocks (code path verified above) remain the evidence for the
  hard-block branch; a fresh hard `BLOCK` will fall out naturally once
  `MINTARR_MEASURED_EXISTING=true` gives a real existing-quality comparison, or
  from a real `FAKE_CERTAIN` grab.
- All three test records were discarded; `needs_review=0`, no library mutation.

### 2026-06-10 (cont.) — measured-existing live dogfood (Claude)

Flipped the live container to the conservative metadata-tier config
(`MINTARR_MEASURED_EXISTING=true`, `MINTARR_REQUIRE_INTEGRITY=false`,
`MINTARR_LIBRARY_SPECTRAL=false`) and drove real Lidarr grabs through the chain.

**Decision results**

| Scenario | Outcome | Verdict |
|---|---|---|
| Missing album (Inger Lise Rypdal — *Just for You*) | `ACCEPT` → imported; measured-existing no-op (`existing=nothing`) | ✅ correct |
| Existing lossy compilation (Dire Straits *Private Investigations*, MP3-VBR) | `REVIEW_REQUIRED` via `AMBIGUOUS_EDITION` (identity dominated; `title_similarity` 0.0, 21 files vs 14) — library untouched, no ManualImport | ✅ two-axis correct |
| a-ha *Hunting High and Low* **deluxe** (60 trk FLAC 24bit, grabbed by mistake) | `AUTHENTIC` + `SAME_FAMILY` (confidence 94.8, `title_similarity` 0.91, **`track_count_delta` 0** — matched the real 60-trk deluxe release 258) → imported as a lossy→FLAC upgrade, replacing the original MP3 association | ✅ **F5.1 correct** — recognized the deluxe as a legitimate same-family edition; not a mis-score |
| a-ha *Hunting High and Low* **1985 original** (TIDAL FLAC 24bit) | `FAKE_CERTAIN` → `BLOCK` → `SKIPPED`, not imported | ✅ **fresh hard BLOCK confirmed** — TIDAL's "24bit" of the 1985 master is upsampled hi-res; Detective caught it. Deluxe (genuine remaster) was correctly `AUTHENTIC`. |

**Key findings**
- Fresh hard `BLOCK` reproduced from a real `FAKE_CERTAIN` (upsampled TIDAL hi-res),
  confirming the earlier prediction. `context.existing.label` is Lidarr's label view
  (manual grab ⇒ "nothing"), *not* the measured-existing evidence — measured-existing
  adjusts the audio decision separately and is subordinate to identity in
  `combine_audio_identity_decision`.
- **F5.1 release-family matching (shipped, [ADR-0013](../architecture/adr/0013-release-family-identity-policy.md)) worked correctly.** The deluxe matched a real
  60-track release in the same family (`track_count_delta` 0, confidence 94.8), so
  `SAME_FAMILY` was the right verdict — the deluxe *is* a legitimate edition of the
  album. It imported because existing was lossy MP3 (measured-existing upgrade
  lossy→FLAC) and the operator-review release-switch is opt-in default-off. The
  outcome (keep the deluxe) matched operator preference. The only defect was the
  accidental release pick, not a Mintarr identity/policy bug.
- **Open edition-policy question (not a bug):** auto-importing a substantially larger
  edition (60-trk deluxe over a standard 10-trk edition) when existing is lossy
  happens silently. Whether that should surface an operator notice is a policy
  refinement, not an F5.1 correctness gap.

**Operational gaps found (real, verified — candidate issues)**
1. Lidarr "remove from download client + blocklist" does **not** stop Mintarr's
   download/import — `mode=delete` only `_hide_from_lidarr`; QC→import proceeds.
2. The `cancel_requested` flag is only honored inside the download subprocess, not
   during QC/normalize/import. A cancel set after the download finishes is ignored
   and the job imports. Combined with (1): once a grab's download completes there is
   no operator-facing way to prevent its import.

**Incident note:** the deluxe grab was an accidental release pick (picker matched the
8.9 GB Super Deluxe, not the 1985 original). Per operator decision the deluxe was kept
as album 13 and the original MP3 rip removed; no data loss to other albums. Config
remains live; rollback = `mintarr-prev-measured-existing-20260610134928`.
