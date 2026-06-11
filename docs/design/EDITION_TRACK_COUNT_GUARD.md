# Edition / Track-count Guard

> **Type:** Design document
> **Version:** 1.0 - 2026-06-10
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

## 2. Decision

Add a conservative **edition/tracklist guard** to the verification decision.

When the combined decision is `ACCEPT`, the identity is `SAME_FAMILY` or
`AMBIGUOUS_EDITION`, and the candidate's tracklist is *much* larger than the
expected Lidarr release, route the record to `REVIEW_REQUIRED` with the reason
**"edition/tracklist mismatch"** so an operator confirms the edition swap.

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

`expected_tracks` is the max `trackCount` across matched Lidarr albums (the
currently-tracked release); `candidate_tracks` is the count of audio files in the
grab.

## 4. Placement

`edition_track_count_mismatch()` is a pure predicate in `app/verification.py`,
applied in `_compute_verification` immediately after
`combine_audio_identity_decision`. On a trip it sets `REVIEW_REQUIRED` and appends
the `edition_tracklist_mismatch` override marker, which surfaces the reason in
`VerificationResult._legacy_reason` (decisions log) and
`dashboard._review_reason` (operator UI).

## 5. Non-goals

- It does not change `BLOCK`/`WRONG_ALBUM` behaviour.
- It does not perform a release switch; it only defers to the operator.
- It does not infer the "correct" edition — that is the operator's call.

## 6. Follow-ups

- Tune thresholds if real dogfood shows over- or under-triggering, especially on
  very large box sets where `+4` is proportionally tiny.
- Consider linking the review action to the existing opt-in release-switch flow so
  an operator can accept the edition swap with one click.
