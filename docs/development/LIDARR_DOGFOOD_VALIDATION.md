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

Do not enable stricter decision flags until the relevant paths have been
dogfooded against a real Lidarr instance. As of the 2026-06-10/11 dogfood,
`MINTARR_MEASURED_EXISTING=true` is live in the conservative metadata-tier mode;
`MINTARR_REQUIRE_INTEGRITY=false` and `MINTARR_LIBRARY_SPECTRAL=false` remain off.

## Preconditions

- Mintarr `/health` is `ok`.
- Lidarr health is `ok` or known unrelated warnings are documented.
- Mintarr is the tested Lidarr indexer/download client for the source under
  test.
- Other Lidarr indexers/download clients that could satisfy the same test grab
  are disabled or excluded from the test.
- `MINTARR_MEASURED_EXISTING` state is recorded for the run. It is currently live
  in conservative metadata-tier mode; do not silently change stricter flags.
- `MINTARR_REQUIRE_INTEGRITY=false` unless an integrity-scan-backed run explicitly
  tests stricter integrity semantics.
- `MINTARR_LIBRARY_SPECTRAL=false` unless the run explicitly tests existing-file
  spectral authenticity.
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

`MINTARR_MEASURED_EXISTING=true` is now live after the 2026-06-10/11 controlled
dogfood. Keep it in conservative metadata-tier mode:

- `MINTARR_REQUIRE_INTEGRITY=false`
- `MINTARR_LIBRARY_SPECTRAL=false`

Do not enable broader RSS/autonomous testing, `MINTARR_REQUIRE_INTEGRITY`, or
`MINTARR_LIBRARY_SPECTRAL` until the next checklist below has been run with real
Lidarr grabs and no new queue/import ownership bugs appear.

## Next measured-existing validation checklist

Run these one at a time, with the operator watching Lidarr and Mintarr. The goal
is to prove that automatic, review, block, discard, cancel, and queue-hold paths
match the policy before enabling broader unattended search.

| Case | Expected Mintarr result | Expected Lidarr result |
|---|---|---|
| Missing album / nothing existing | `ACCEPT` or `ACCEPT_PROVISIONAL` when hard gates pass | Imported; no stale Mintarr queue row |
| Existing lossy release, clean FLAC candidate, same edition | Auto import as an upgrade when identity is confident | Imported; existing lossy replaced |
| Existing good FLAC, worse/fake/down-tier candidate | `REVIEW_REQUIRED` or `BLOCK` depending on evidence | No automatic import |
| Oversized same-family edition (deluxe/anniversary, e.g. `>= +4` tracks or `>= 1.5x`) | `REVIEW_REQUIRED` with `edition/tracklist mismatch` | Held as SAB `Paused`; no re-grab while pending |
| Real hard failure (`FAKE_CERTAIN`, real decode corruption, wrong album) | `BLOCK` | No `ManualImport`; queue cleaned/blocklisted as applicable |
| Review discard | lifecycle `discarded`; exact release blocklisted | Held queue row removed; later re-grab only for different release is acceptable |
| Review promote | lifecycle `promoted`; `manual_promote` override present | Imported, then Mintarr-owned queue row cleaned/settled |
| Operator cancel after download but before import | worker job terminalizes `cancelled` | No `ManualImport`; no library mutation |
| Bypass audit after test window | all in-scope imports have Mintarr JID/sidecar | No Lidarr-native path silently bypassed Mintarr |

Pass criteria:

- no item reaches Lidarr's library without a Mintarr record/JID;
- no `REVIEW_REQUIRED` item imports before explicit operator promote;
- no `BLOCK` item calls Lidarr `ManualImport`;
- review-held items remain visible as `Paused` until promote/discard/expire;
- discard/expire blocklists and cleans the held queue row;
- post-discard re-grabs are treated as new candidates and re-gated by Mintarr;
- the runbook captures the exact JID, source, decision, import outcome, and
  queue state for each case.

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

**Open finding — edition switch can bypass operator review on a lossy→FLAC upgrade.**
The release-switch review ([HTTP_API_v1.md](../specs/HTTP_API_v1.md)) only engages for
`AMBIGUOUS_EDITION`/`INSUFFICIENT_EVIDENCE` identity. The a-ha deluxe matched a real
60-track release in its own family → `SAME_FAMILY` (confident), and existing was lossy
MP3, so measured-existing scored it a pure lossy→FLAC upgrade → auto-ACCEPT. Net effect:
a *different edition* (60 trk deluxe vs the 10 trk standard that was there) was imported
and replaced the original with no edition-switch review, because the swap rode in on the
audio-upgrade axis, not the identity axis. Not a bug (operator wanted the deluxe here),
but for RSS/auto mode an edition change that also happens to be an audio upgrade is
silent. Worth a policy decision: should a large `track_count_delta` vs the
currently-tracked edition surface a notice even on `SAME_FAMILY` + audio-upgrade?

### 2026-06-11 — edition guard + cancel-fix live (Claude)

Redeployed Mintarr with the merged edition-guard (#149) and cancel-fix (#148),
measured-existing flags preserved. Grabbed Depeche Mode *Black Celebration* (album
600, existing MP3-192) **deluxe** via TIDAL.

- **Edition guard — clean PASS.** `existing=14 / expected=14 / new=22`, identity
  `SAME_FAMILY`, `flac-detective AUTHENTIC` (audio would otherwise `ACCEPT` as a
  lossy→FLAC upgrade). Decision: **`REVIEW_REQUIRED`**, reason surfaced in both the
  decisions log and dashboard as *"Edition/tracklist mismatch: the candidate has
  many more tracks than the tracked release — likely a different edition
  (deluxe/anniversary). Confirm before it replaces the current edition."* The deluxe
  was **not** imported; album 600 stayed MP3-192.
- **Cancel-fix (#146) validated live.** After discarding the review, Lidarr
  auto-re-grabbed (see finding below); a Lidarr "remove from download client +
  blocklist" arrived as `mode=history&name=delete&value=<jid>` and was correctly
  routed to `request_job_cancel` (`SAB delete → cancel requested for worker job 58`)
  — the exact no-op gap #146 closed.
- **Bonus QC catch.** The *standard* Black Celebration TIDAL "FLAC" is itself an
  upsampled fake → `FAKE_CERTAIN` → `BLOCK` with existing MP3 present. Correctly
  blocked + blocklisted; not imported.

**Finding — review-held Lidarr-monitored albums get auto-re-grabbed.** A
`REVIEW_REQUIRED` hold cleans the Lidarr queue entry (so Lidarr does not see it as
downloading), but because review does not blocklist, a monitored album with
automatic search enabled is **re-grabbed by Lidarr**. Observed here: after the
deluxe went to review, Lidarr immediately re-grabbed a (different) standard release.
For the edition guard specifically this risks a re-grab loop if Lidarr keeps finding
the same deluxe. Worth a follow-up: an edition-guard review should likely blocklist
the specific oversized release (while keeping it promotable) so Lidarr does not
re-grab it; or the review-hold should retain the Lidarr queue item instead of
clearing it. Prior reviews did not surface this because they were local-lane (no
Lidarr search).

### 2026-06-11 (cont.) — review-hold invariant live (Claude)

Redeployed with the review-hold fix (#150) and re-ran the edition-guard review
(Depeche Mode *Black Celebration* deluxe, album 600). Resolves the open caveat:
does Lidarr leave a SAB Paused-at-100% item without stalling or re-grabbing?

- **Review hold — clean PASS.** Decision `REVIEW_REQUIRED` (edition guard). The
  emulated SAB queue presented the held job as `Paused 100%`; Lidarr kept the item
  in its queue and, after 90 s, still showed **the same single item** as `paused`
  with **no re-grab**. The previous behaviour (immediate auto-re-grab on review) is
  gone. Answer to the caveat: **yes, Lidarr leaves a Paused-at-100% hold alone.**
- **Discard — clean PASS.** Discarding the held review blocklisted the deluxe and
  `Lidarr queue cleaned: 1 entries removed`; Lidarr queue and SAB queue both went to
  0. Album 600 stayed MP3-192 (not imported).
- **Post-discard re-grab is correct, not the bug.** After discard (operator
  rejected), Lidarr grabbed a *different* release (the standard) — expected, since
  the album is still wanted; the deluxe is blocklisted so only a new candidate is
  tried, and it is re-gated by Mintarr.
- **Cancel-fix re-validated, cleanly.** Removing that re-grab via Lidarr
  remove+blocklist arrived as `mode=history&name=delete` → `request_job_cancel`, and
  because it was caught during download the job terminalized as **`cancelled`** (not
  blocked/failed) — confirming both #146 (delete→cancel routing) and #147
  (JobCancelled propagation) live.

### 2026-06-11 (cont.) — measured-existing checklist continuation (Codex)

Continued the controlled measured-existing dogfood with the live configuration:
`MINTARR_MEASURED_EXISTING=true`, `MINTARR_REQUIRE_INTEGRITY=false`,
`MINTARR_LIBRARY_SPECTRAL=false`. Mintarr health was `ok`, `active_jobs=0` before
each grab, and Lidarr queue/SAB-emulated queue were clean after terminal outcomes.

**Checklist results**

| Scenario | JID | Outcome | Verdict |
|---|---|---|---|
| Missing album / nothing existing: Inger Lise Rypdal — *Ansikter* | `6256ab090215` | `ACCEPT` → `MANUAL_IMPORTED`; Lidarr imported 10 FLAC trackfiles; no stale queue row | ✅ pass |
| Existing lossy → clean FLAC, same track count: The Police — *Outlandos d'Amour* | `bd404685590d` | `ACCEPT` → `MANUAL_IMPORTED`; Lidarr replaced 10 MP3-VBR-V0 trackfiles with 10 FLAC trackfiles | ✅ pass |
| Hard evidence failure on existing lossy album: 10cc — *The Original Soundtrack* | `4032baff1c2c` | 9 authentic FLAC tracks + 1 `FAKE_CERTAIN` track → `BLOCK`/`SKIPPED`; no import; album stayed MP3-192 | ✅ pass |
| Re-grab after block, bad source payload: 10cc — *The Original Soundtrack [Bonus Tracks]* | `45c965e75bcb` | codec gate found no usable FLAC/audio files → `BLOCK`/`SKIPPED`; no import; queue cleaned | ✅ pass |
| Existing lossy → candidate with fewer tracks than existing: Weezer — *Weezer (Green Album)* | `c632f057e983` | audio passed (`AUTHENTIC`, `ACCEPT`) but Lidarr `ManualImport` rejected every file with "Has fewer tracks than existing release" → `FAILED`; album stayed MP3-192 | 🟡 product gap |

**Passes confirmed**

- The positive measured-existing upgrade path works end-to-end: a measured lossy
  album can be replaced by a clean FLAC candidate through Mintarr, with the final
  Lidarr library showing FLAC and no stale queue rows.
- The missing-album path still imports automatically when hard gates pass.
- Hard audio failures still win over the measured-existing upgrade axis: one
  `FAKE_CERTAIN` track in an otherwise plausible FLAC release blocked the entire
  candidate before `ManualImport`.
- Post-block re-grabs are treated as new candidates and re-gated by Mintarr. The
  second 10cc candidate was blocked independently by the codec gate; no bypass was
  observed.

**Follow-up findings**

1. **ManualImport track-count rejection should be predicted before import
   ([#151](https://github.com/eivindsjursen-lab/mintarr/issues/151)).** The
   Weezer candidate had 10 new tracks while the existing/tracked Lidarr release had
   11. Mintarr accepted the audio-quality upgrade, then Lidarr rejected all files as
   "Has fewer tracks than existing release". This is safe (no library mutation), but
   the operator experience is poor and the job lands as `FAILED`. A future guard
   should route "candidate materially has fewer tracks than existing/tracked release"
   to `REVIEW_REQUIRED` before `ManualImport`, analogous to the oversized-edition
   guard but in the opposite direction.
2. **Hard-block reason text can hide the real blocker
   ([#152](https://github.com/eivindsjursen-lab/mintarr/issues/152)).** JID
   `4032baff1c2c` blocked
   correctly because one track was `FAKE_CERTAIN`, but the dashboard/status reason
   read "Blocked by policy: upgrade from MP3-192." The decision is correct; the
   human-facing reason should prefer the hard blocker (`FAKE_CERTAIN` / Detective)
   over the lower-priority upgrade context.

### 2026-06-12 — fewer-track guard live validation (Codex)

Redeployed `main` with the fewer-track guard
([#153](https://github.com/eivindsjursen-lab/mintarr/pull/153), closing
[#151](https://github.com/eivindsjursen-lab/mintarr/issues/151)). The same Weezer
*Weezer (Green Album)* candidate that previously failed at Lidarr `ManualImport`
was re-run:

- Existing/tracked album: 11 MP3-192 trackfiles.
- Candidate: 10 TIDAL FLAC files, `AUTHENTIC`.
- JID: `8b9235bbdedf`.
- Result: **`REVIEW_REQUIRED`** with override `track_count_undercount`.
- Dashboard/status reason: *"Track-count mismatch: the candidate has fewer tracks
  than the tracked release. Confirm before it replaces a more complete library
  copy."*
- No `ManualImport` was called; album stayed MP3-192.
- Discarding the review removed the held Lidarr queue row; final Mintarr health
  `ok`, `active_jobs=0`, Lidarr queue `0`.

This closes the product gap from JID `c632f057e983`: predictable Lidarr
"Has fewer tracks than existing release" failures now stay in Mintarr's review lane
instead of becoming failed imports.

### 2026-06-12 — hard-block reason priority (Codex)

Fixed the follow-up from JID `4032baff1c2c`
([#152](https://github.com/eivindsjursen-lab/mintarr/issues/152)): blocked
dashboard/status reasons now prefer hard evidence over measured-existing upgrade
context. For a `BLOCK` record, codec gate, `flac -t`, validator failure,
`WRONG_ALBUM`, and FLAC Detective hard fake verdicts are surfaced before fallback
reasons such as "upgrade from MP3-192".

Expected result for the 10cc case: the decision remains `BLOCK`/`SKIPPED`, but the
operator-facing reason should now explain the hard `FAKE_CERTAIN` Detective verdict
instead of the lower-priority upgrade context.

### 2026-06-12 — controlled happy-path grab after status/branding polish (Codex)

Ran a single interactive Lidarr release grab after the dashboard status-label and
Mintarr-branding fixes were live. Configuration remained conservative:
`MINTARR_MEASURED_EXISTING=true`, `MINTARR_REQUIRE_INTEGRITY=false`,
`MINTARR_LIBRARY_SPECTRAL=false`.

Candidate:

- Lidarr action: interactive release grab through Lidarr `/release`.
- Source/indexer: Mintarr/TidalHires.
- Album: D'Sound — *25* (album id `12132`), missing before the grab.
- JID/downloadId: `a056864653a5`.

Result:

- Mintarr created a `tidal_grab` job and Lidarr queue showed the same JID as the
  download id while the item was downloading.
- Mintarr progressed through download, postprocess, FLAC Detective, then
  `ManualImport`; no import happened before QC completed.
- Decision: `ACCEPT`.
- Import outcome: `MANUAL_IMPORTED`.
- Derived status: `imported`.
- FLAC Detective verdict: `AUTHENTIC`; dashboard file evidence showed 8 FLAC
  files.
- Lidarr imported 8 FLAC trackfiles for album `12132`.
- Final Lidarr queue: `0`; final Mintarr active jobs: `0`.

Verdict: **happy path still passes** with measured-existing enabled in
metadata-tier mode. The current dashboard display also presents the record as
`Imported` / `ACCEPT` / `Imported`, with no available operator actions.

### 2026-06-12 — review discard path and blocklist idempotence (Codex)

Closed the remaining pending review record to re-check the operator discard path
after the status-label changes:

- Record/JID: `759411b624be`.
- Source: TIDAL through Mintarr.
- Album: Roger Whittaker — *Festliche Weihnacht*.
- Review reason: FLAC Detective found one `FAKE_CERTAIN` track in an otherwise
  authentic-looking release.
- Before action: `derived_status=needs_review`, available actions
  `promote, discard`, Lidarr queue `0`.
- Operator action used: dashboard discard API.

Result:

- Discard returned `200`.
- Record moved to `derived_status=discarded`; available actions became empty.
- State index recorded a `discard` audit action from `user_dashboard`.
- Lidarr queue remained `0`.
- No library import happened.

Finding and follow-up:

- The release was already present in Lidarr's blocklist from the original review
  creation, but the later discard overwrote the sidecar's `blocklist_status` from
  `done` to `failed` because there was no new `grabbed` history row to blocklist
  again.
- This was a status/idempotence bug, not a safety failure: Lidarr already had the
  blocklist entry, the queue was empty, and no import happened.
- Follow-up fix: discard now preserves `blocklist_status=done` and does not call
  the blocklist endpoint again when the record is already blocklisted.

### 2026-06-12 — LocalFolder hard-block path (Codex)

Ran a fabricated LocalFolder hard-block test to verify that invalid local source
audio is stopped by Mintarr's shared QC gate and never reaches Lidarr.

Candidate:

- Source: LocalFolder via `POST /local/ingest`.
- Test path: `Mintarr Dogfood HardBlock TextFLAC`.
- Payload: one intentionally invalid `01 - Fake.flac` text file.
- JID: `d88c282d4ba6`.

Result:

- Job type: `local_grab`.
- Worker result: `blocked`.
- Decision: `BLOCK`.
- Import outcome: `SKIPPED`.
- Derived status: `blocked`.
- Sensor evidence: `ffprobe` blocker with `audio_count=0`; `flac_t` blocker with
  `integrity_failed=1`; FLAC Detective skipped because no valid FLAC remained.
- Lidarr queue stayed `0`.
- No `ManualImport` was called and no library mutation happened.

Verdict: **hard-block path passes** for the LocalFolder/completed-folder lane. The
operator-facing message was safe but generic (*"no audio files downloaded"*); a
future UX polish could explain this class as "invalid FLAC wrapper / no decodable
audio" more directly.

### 2026-06-13 — controlled measured-existing upgrade after redeploy (Codex)

Redeployed current `main` and ran one controlled interactive Lidarr grab with the
conservative measured-existing configuration preserved:

```text
MINTARR_MEASURED_EXISTING=true
MINTARR_REQUIRE_INTEGRITY=false
MINTARR_LIBRARY_SPECTRAL=false
```

Candidate:

- Lidarr action: interactive release grab through Lidarr `/release`.
- Source/indexer: Mintarr/TidalHires.
- Album: Jack Johnson — *In Between Dreams* (album id `924`).
- Existing library evidence: 14 measured lossy trackfiles; Lidarr label
  `MP3-192`.
- Candidate title: `Jack Johnson - In Between Dreams (2005) [TIDAL] [FLAC 24bit]`.
- JID/downloadId: `18ff49e52894`.

Result:

- Mintarr created a `tidal_grab` job and completed the full download/postprocess
  path before import.
- Existing library quality was detected as `MP3-192` (`~192 kbps`).
- Track counts: existing `14`, expected `14`, candidate `15`.
- FLAC Detective verdict: `AUTHENTIC` for 15 files.
- Decision: `ACCEPT`.
- Import outcome: `MANUAL_IMPORTED`.
- Derived status: `imported`.
- Lidarr `ManualImport` succeeded for 15/15 files.
- Final Lidarr queue: `0`; final Mintarr active jobs: `0`.

Verdict: **happy path passes** after the redeploy. Measured-existing correctly
treated an existing lossy album as replaceable by a clean FLAC candidate, and
the import happened only after Mintarr QC completed.

Product observation:

- The candidate contained one extra demo/bonus track (`expected=14`,
  `candidate=15`). Today's edition/track-count guard intentionally treats this
  as near-equal and allows the import.
- This is not a failure under the current conservative guard, but it is a useful
  real example for the proposed edition-preference policy: minor bonus tracks
  should be configurable as part of the deluxe/remaster/expanded edition policy,
  not as a separate quality setting.

### 2026-06-13 — review-hold deluxe edition validation (Codex)

Ran one controlled interactive Lidarr grab specifically to validate the
review-hold invariant from the review-hold fix: a `REVIEW_REQUIRED` item should
remain visible to Lidarr as a paused download while the operator decides, so
Lidarr does not immediately re-grab the same wanted album.

Candidate:

- Lidarr action: interactive release grab through Lidarr `/release`.
- Source/indexer: Mintarr/TidalHires.
- Album: 3 Doors Down — *Away From The Sun* (album id `7`).
- Existing library evidence: 12 measured lossy trackfiles.
- Candidate title:
  `3 Doors Down - Away From The Sun (Deluxe) (2002) [TIDAL] [FLAC 24bit]`.
- JID/downloadId: `448c38bea957`.

Result:

- Mintarr completed the download and QC path.
- Candidate contained 22 FLAC files for a tracked/existing 12-track album.
- FLAC Detective completed before policy was applied.
- Decision: `REVIEW_REQUIRED`.
- Reason: edition/tracklist mismatch; candidate has many more tracks than the
  tracked release, likely a different edition.
- Import outcome remained `PENDING`; no `ManualImport` was called.
- Mintarr SAB emulation exposed the held item as:

  ```text
  nzo_id=448c38bea957
  status=Paused
  mbleft=0
  percentage=100
  ```

- Lidarr continued to show exactly one queue row for the same download id. After
  a one-minute hold check it reported the row as `paused`; no re-grab occurred.

Cleanup:

- Operator discard was executed through `POST /verification/448c38bea957/discard`.
- Lifecycle moved to `discarded`, `blocklist_status=done`.
- Mintarr SAB queue became empty.
- Lidarr queue became `0`.

Verdict: **review-hold path passes**. A larger deluxe edition is held for human
review, visible to Lidarr as paused, and cleaned/blocklisted correctly when
discarded.

### 2026-06-13 — Lidarr remove/blocklist cancel propagation (Codex)

Ran one controlled cancel-propagation dogfood to validate that a Lidarr
"remove from download client + blocklist" action stops the active Mintarr worker
before QC/import.

Candidate:

- Lidarr action: interactive release grab through Lidarr `/release`.
- Source/indexer: Mintarr/TidalHires.
- Album: Hank Williams — *The Original Singles Collection . . . Plus* (album id
  `857`).
- Candidate title:
  `Hank Williams - The Original Singles Collection . . . Plus (1992) [TIDAL] [FLAC 24bit]`.
- First JID/downloadId: `8a507b46b060`.

Result:

- Lidarr queued the TIDAL grab and Mintarr started `tidal_grab`.
- Operator cancel was issued via Lidarr queue delete with `removeFromClient=true`
  and `blocklist=true`.
- Mintarr received the SAB delete shapes (`mode=queue&name=delete` and
  `mode=history&name=delete`) and set `cancel_requested`.
- Worker job `71` terminalized as `cancelled` / `cancelled`.
- No verification record was created and no `ManualImport` happened.

Follow-up observation:

- After the first exact release was cancelled/blocklisted, Lidarr immediately
  searched again and grabbed a second candidate for the same wanted album from
  Soulseek.
- Second JID/downloadId: `9e6d2631f136`.
- The second job was also cancelled via the same Lidarr delete path and
  terminalized as `cancelled` / `cancelled`.
- After a short hold check, Lidarr queue was `0` and no third candidate was
  grabbed.

Verdict: **per-release cancel propagation passes**. Mintarr stops active workers
before QC/import when Lidarr removes a queued download.

Product gap:

- The operator intent may be album-level ("stop trying this album now"), but
  Lidarr's remove/blocklist semantics are release-level. After one release is
  cancelled/blocklisted, Lidarr may immediately try the next candidate for the
  same monitored/wanted album.
- This should be handled as a separate album-level cancel/hold design rather
  than by weakening per-release blocklist behavior. Follow-up:
  [#160](https://github.com/eivindsjursen-lab/mintarr/issues/160).
