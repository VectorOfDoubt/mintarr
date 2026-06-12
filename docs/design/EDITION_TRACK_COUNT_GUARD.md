# Edition / Track-count Guard

> **Type:** Design document
> **Version:** 1.1 - 2026-06-11
> **Status:** Implemented
> **Related:** [ADR-0013 release-family identity policy](../architecture/adr/0013-release-family-identity-policy.md), [Lidarr dogfood validation](../development/LIDARR_DOGFOOD_VALIDATION.md)

## 1. Problem

The 2026-06-10 measured-existing dogfood surfaced a real gap. An a-ha
*Hunting High and Low* **deluxe** edition (60 tracks) matched the standard album
(10 tracked tracks) as `SAME_FAMILY` with high confidence — because it matched a
real 60-track release inside the same release family — and, because the tracked
copy was lossy MP3, measured-existing scored it a clear lossy→FLAC upgrade. The
two-axis policy therefore returned `ACCEPT` and the deluxe **auto-imported,
silently replacing the standard edition**.

This is not a `WRONG_ALBUM` (already blocked) and not `AMBIGUOUS_EDITION`
(already routed to review). It rides in on the **audio-upgrade axis** while the
identity axis is confident, so neither existing guard catches it. For RSS/auto
mode an edition change that also happens to be an audio upgrade is silent.

The 2026-06-11 continuation found the opposite failure mode. A Weezer *Green
Album* candidate had clean audio and was a real lossy→FLAC upgrade, but it had
10 tracks while the existing/tracked Lidarr release had 11. Mintarr accepted it,
then Lidarr rejected every `ManualImport` item with "Has fewer tracks than
existing release". That is safe, but the failure is predictable before
`ManualImport` and should be an operator review rather than a failed import.

## 2. Decision

Add a conservative **track-count guard** to the verification decision.

When the combined decision is an auto-import decision (`ACCEPT` **or**
`ACCEPT_PROVISIONAL` — both proceed to Lidarr ManualImport), the identity is
`SAME_FAMILY` or `AMBIGUOUS_EDITION`, and the candidate's tracklist is *much*
larger than the expected Lidarr release, route the record to `REVIEW_REQUIRED`
with the reason **"edition/tracklist mismatch"** so an operator confirms the
edition swap. Covering `ACCEPT_PROVISIONAL` matters because a large edition
mismatch can otherwise still auto-import via a provisional accept
(suspicious-but-upgrade or measured-existing rescue).

Also route an otherwise-accepted candidate to `REVIEW_REQUIRED` when it has fewer
tracks than the expected Lidarr release **and** the existing library copy already
has at least the expected track count. This catches Lidarr's predictable
"fewer tracks than existing release" `ManualImport` rejection before import while
preserving incomplete-existing rescue.

- **Never escalates to `BLOCK`** — it may genuinely be the right edition.
- **Never auto-switches** the Lidarr release.
- **Does not touch `WRONG_ALBUM`** (already blocked) or missing track counts.
- A near-equal count (a complete candidate filling an incomplete existing
  release) does **not** trip it.

## 3. Threshold

```text
trip if  candidate_tracks >= expected_tracks + 4
     or  candidate_tracks >= expected_tracks * 1.5
```

Constants `EDITION_TRACK_COUNT_ABS_MARGIN = 4` and
`EDITION_TRACK_COUNT_RATIO = 1.5` (`app/verification.py`). Conservative by design
— it errs toward review on a likely edition swap. Examples:

| expected | candidate | trips? | why |
|---|---|---|---|
| 10 | 12 | no | +2 / 1.2x — a couple of bonus tracks |
| 10 | 14 | yes | absolute margin (+4) |
| 10 | 60 | yes | obvious deluxe |
| 20 | 30 | yes | ratio (1.5x) |
| 10 | 10 | no | complete candidate for an incomplete existing release |

Under-count guard:

```text
trip if candidate_tracks < expected_tracks
    and existing_tracks >= expected_tracks
```

Examples:

| expected | existing | candidate | trips? | why |
|---|---|---|---|---|
| 11 | 11 | 10 | yes | would be rejected by Lidarr as fewer tracks than existing release |
| 10 | 5 | 7 | no | incomplete-existing rescue remains possible |
| 10 | 10 | 10 | no | same count |
| 0 | 0 | 9 | no | missing expected count; cannot judge |

`expected_tracks` is the max `trackCount` across matched Lidarr albums (the
currently-tracked release); `candidate_tracks` is the count of audio files in the
grab; `existing_tracks` is the current count of trackfiles in the target album.

## 4. Placement

Two pure helpers in `app/verification.py`: `edition_track_count_mismatch()` (the
oversized-edition predicate) and `track_count_undercount_mismatch()` (the
under-count predicate). `apply_track_count_guard(decision, identity, candidate,
expected, existing) -> (decision, marker)` gates on `ACCEPT`/`ACCEPT_PROVISIONAL`.
`_compute_verification` calls `apply_track_count_guard` immediately after
`combine_audio_identity_decision`; on a trip it appends either the
`edition_tracklist_mismatch` or `track_count_undercount` override marker, which
surfaces the reason in `VerificationResult._legacy_reason` (decisions log) and
`dashboard._review_reason` (operator UI).

`apply_edition_guard()` remains as a backward-compatible wrapper for tests and
older call sites, but new code should use `apply_track_count_guard()`.

## 5. Non-goals

- It does not change `BLOCK`/`WRONG_ALBUM` behaviour.
- It does not perform a release switch; it only defers to the operator.
- It does not infer the "correct" edition — that is the operator's call.

## 6. Follow-ups

- Tune thresholds if real dogfood shows over- or under-triggering, especially on
  very large box sets where `+4` is proportionally tiny.
- Consider linking the review action to the existing opt-in release-switch flow so
  an operator can accept the edition swap with one click.
