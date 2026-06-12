# Edition Preference Policy

> **Type:** Design document
> **Version:** 0.1 - 2026-06-12
> **Status:** Proposed
> **Related:** [ADR-0013 release-family identity policy](../architecture/adr/0013-release-family-identity-policy.md), [Edition / track-count guard](EDITION_TRACK_COUNT_GUARD.md), [F5.4 library evidence index](F5.4_LIBRARY_EVIDENCE_INDEX.md), [ADR-0008 strategic positioning](../architecture/adr/0008-strategic-positioning.md)

## 1. Problem

Mintarr now detects several cases where Lidarr's release matching is technically
"same family" but operationally risky:

- a deluxe / anniversary / box-set edition can be a real audio upgrade but not
  the edition the operator wants in the library
- a remaster may be preferred over an original edition for one operator, while
  another operator prefers original album editions
- a candidate can have fewer tracks than the tracked Lidarr release, which
  predicts a Lidarr ManualImport failure
- a larger edition can be acceptable when the operator explicitly wants expanded
  editions, but should be review-only when the operator wants strict album
  editions

The shipped [edition / track-count guard](EDITION_TRACK_COUNT_GUARD.md) is a
conservative safety net: large or short track-count mismatches route to review.
It intentionally does **not** express the operator's long-term preference. The
next layer should let Mintarr answer:

> "When audio quality is acceptable and this is the same release family, which
> edition should win?"

## 2. Decision

Add an **edition preference policy** as a separate policy layer after release
identity and audio quality, but before any automatic import or release switch.

Mintarr keeps the ADR-0013 two-axis model:

```text
audio_quality:
  codec / integrity / authenticity / score / candidate-vs-existing quality

release_identity:
  SAME_RELEASE | SAME_FAMILY | AMBIGUOUS_EDITION | INSUFFICIENT_EVIDENCE | WRONG_ALBUM
```

Edition preference is not a peer axis that can win against those gates. It is a
policy layer that runs only after audio and identity have already allowed an
import path inside the same release family:

```text
edition_preference:
  operator preference over valid editions inside the same release family

edition_policy_mode:
  manual vs conservative vs automatic preference application
```

The hard invariant remains:

> Edition preference can never rescue wrong identity or hard audio failure.

The first implementation should not mutate Lidarr's release model. It should
only produce an import decision:

```text
ACCEPT | ACCEPT_PROVISIONAL | REVIEW_REQUIRED | BLOCK
```

Release switching stays the separate, default-off path from ADR-0013.

Important invariant:

> An edition change that requires a different Lidarr release must not auto-import
> unless the audited release-switch path is also enabled and succeeds.

Without that invariant, a "prefer expanded" profile could repeat the a-ha
dogfood failure: a 60-track deluxe import into a 10-track standard release slot.
Therefore v1 edition preference can automatically keep same-release / near-equal
same-family decisions, but a true edition change is at most
`REVIEW_REQUIRED` until release-switch integration is explicitly enabled.

## 3. Policy profiles

The product should expose a small number of understandable profiles before it
exposes a fully custom rule engine.

### Manual

All edition changes route to `REVIEW_REQUIRED`.

Use when:

- dogfooding new release-family logic
- operator wants explicit control over every deluxe/remaster/anniversary swap
- library curation values exact edition more than automation

This should be the safest first dogfood mode.

### Conservative

Prefer a candidate only when it is clearly better quality and does not represent
a large edition change.

This profile should be equivalent to today's shipped guard behavior unless the
classifier proves a near-equal same-release / same-family case. It is the
compatibility baseline: enabling `Conservative` must not create a second
parallel track-count policy that conflicts with `apply_track_count_guard()`.

Default behavior:

- same release + better audio may import
- near-equal same-family release may import
- remaster may import only when track count is near-equal
- deluxe / anniversary / expanded / box-set routes to review
- fewer-track candidate routes to review when existing copy is already complete

This is the likely default if edition policy is ever enabled broadly.

### Prefer remaster / hi-res remaster

Prefer remastered or hi-res remastered editions over original editions when
identity is same-family and track counts are near-compatible.

Still routes to review for:

- box sets
- very large expanded editions
- live editions
- compilations
- weak edition classification

### Prefer expanded editions

Allow deluxe / expanded / anniversary editions to replace standard editions
when:

- identity is `SAME_FAMILY`
- audio decision is accepted or provisionally accepted
- the edition classifier identifies the candidate as an allowed expanded type
- the configured maximum expansion boundary is not exceeded, unless explicitly
  allowed

In v1 this profile can only **recommend** the preference and route to review
unless release-switch integration is enabled. Box sets should remain review-only
in the first version even under this profile.

### Custom

Allow the operator to rank edition categories. A future UI can expose this as a
drag/drop preference list.

Example:

```text
1. hi_res_remaster
2. remaster
3. original_album
4. deluxe
5. anniversary
6. expanded
7. box_set
8. live
9. compilation
```

The custom mode should still use guardrails: a higher-ranked category does not
override hard audio failure, `WRONG_ALBUM`, or confidence below threshold.

## 4. Edition categories

The classifier should map release metadata into coarse categories. The initial
set should be intentionally small and explainable:

| Category | Examples | Default treatment |
|---|---|---|
| `original_album` | original album / standard edition | neutral baseline |
| `remaster` | remastered, remaster, remastered 2003 | often preferred if near-equal |
| `hi_res_remaster` | hi-res remaster, 24-bit remaster | quality-preferred if near-equal |
| `deluxe` | deluxe edition, expanded deluxe | review unless profile allows |
| `anniversary` | 30th anniversary, 40th anniversary | review unless profile allows |
| `expanded` | expanded edition, bonus tracks | review unless profile allows |
| `box_set` | complete sessions, super deluxe, multi-disc box | review in v1 |
| `live` | live, concert, radio broadcast | review or block depending identity evidence |
| `compilation` | greatest hits, anthology, best of | usually not a replacement for an album |
| `unknown` | weak/missing metadata | review when edition matters |

Classification evidence sources:

- Lidarr release title and release track count
- observed album title tags
- filenames and folder names
- MusicBrainz release/release-group metadata when available
- track-count delta and title similarity from release-family scoring

The classifier must carry confidence:

```text
edition_category
edition_confidence
edition_reasons[]
```

Weak evidence must produce `unknown` or review, not an aggressive automatic
edition decision.

## 5. Preference comparison

Add a pure domain module:

```text
app/edition_policy.py
```

Suggested inputs:

```text
EditionPolicyConfig
ExpectedEdition
ExistingEdition
CandidateEdition
AudioDecision
IdentityDecision
TrackCountEvidence
```

Suggested output:

```text
EditionPreferenceDecision:
  result: KEEP_DECISION | REVIEW_REQUIRED | NO_OP
  category: original_album | remaster | deluxe | ...
  confidence: 0.0..1.0
  reason: human-readable explanation
  markers: []
```

Edition preference is a review/import choice, not a hard quality failure. Hard
block remains the job of audio and identity policy.

Comparison rules:

1. If audio decision is `BLOCK`, return `NO_OP` and let audio block win.
2. If identity decision is `WRONG_ALBUM`, return `NO_OP` and let identity block
   win.
3. If identity decision is `AMBIGUOUS_EDITION` or `INSUFFICIENT_EVIDENCE`, route
   to review unless policy mode is `Manual` (also review).
4. If mode is `Manual`, route any edition change to review.
5. If candidate and existing/current edition are same category and track count is
   near-compatible, do not change the audio decision.
6. If candidate category ranks higher than current category under the active
   profile, keep the import decision only when confidence, track-count
   boundaries, and release-switch requirements pass. If a release switch would
   be required but is disabled, route to review.
7. If candidate category ranks lower than current category, route accepted
   decisions to review; do not silently downgrade edition.
8. If category is `unknown`, route to review when edition evidence would affect
   the import decision.

## 6. Track-count boundaries

Edition preference should subsume the shipped track-count guard instead of
creating a second hidden threshold system. The implementation should have one
decision path: `edition_policy.py` should eventually own the checks currently
performed by `apply_track_count_guard()`, with the existing helper either
delegating to the new module or being retired after migration.

Baseline:

```text
near_equal:
  abs(delta) < 4 and ratio < 1.5

expanded:
  candidate_tracks >= expected_tracks + 4
  or candidate_tracks >= expected_tracks * 1.5

undercount:
  candidate_tracks < expected_tracks
  and existing_tracks >= expected_tracks
```

Profiles can decide what to do with `expanded`, but they cannot ignore
`undercount` silently. A candidate with fewer tracks than an already complete
library copy should route to review even when it is otherwise a quality upgrade.

## 7. Configuration

Initial environment-level configuration:

```text
MINTARR_EDITION_POLICY=manual|conservative|prefer_remaster|prefer_expanded|custom
MINTARR_EDITION_POLICY_ENABLED=false
```

Future custom config can live in state DB / dashboard settings:

```json
{
  "mode": "custom",
  "rank": [
    "hi_res_remaster",
    "remaster",
    "original_album",
    "deluxe",
    "anniversary",
    "expanded",
    "box_set",
    "live",
    "compilation"
  ],
  "allow_box_set_auto_replace_after_release_switch": false,
  "allow_compilation_auto_replace_after_release_switch": false,
  "max_auto_track_ratio": 1.5,
  "max_auto_extra_tracks": 4
}
```

The dashboard should eventually expose this as:

- profile selector
- ranked edition-category list for custom mode
- per-category "auto / review / never" controls
- preview examples showing how common releases would be classified

## 8. Dashboard behavior

The dashboard should explain edition decisions like other evidence:

```text
Audio: accepted
Identity: same family
Edition: deluxe vs original album
Policy: Manual -> review required
Reason: candidate is a larger deluxe edition; confirm before replacing the tracked standard edition.
```

For custom mode, show:

```text
Candidate category: remaster
Current category: original album
Policy rank: remaster is preferred over original album
Track count: near-equal
Decision: accept
```

Operator actions:

- accept this edition once
- discard/block this release
- optionally remember preference for this album / artist / globally (future)
- apply release switch only through the existing ADR-0013 audited release-switch
  workflow

Do not hide the raw release identity evidence. Edition preference is policy; it
must remain inspectable.

## 9. Safety and audit

Every edition-policy intervention should record:

- active policy mode
- resolved preference scope (`global`, future `artist`, future `album`)
- candidate category and confidence
- expected/current category and confidence when known
- track-count evidence
- final edition-policy result
- reason text shown to the operator

Audit is required because edition preference is subjective. Two operators may
reasonably prefer opposite outcomes.

No automatic tag writing, file renaming, file deletion, MusicBrainz editing, or
Lidarr release mutation is included in this design. Those remain outside
Mintarr's read-only quality-gate scope unless separately approved.

## 10. Rollout plan

1. **Design only** - this document.
2. **Classifier slice** - pure edition-category classifier + tests; no policy
   effect.
3. **Policy slice** - `Manual` and `Conservative` modes only, default-off,
   decision output + dashboard reasons.
4. **Dogfood slice** - run against real review records and tune category
   detection.
5. **Classifier trust gate** - do not enable richer profiles until dry-run /
   preview output shows stable category confidence on real Lidarr records.
6. **Preference profiles** - add `prefer_remaster` and `prefer_expanded`.
   Expanded/edition-changing profiles remain review-only until release-switch
   integration is enabled.
7. **Custom UI** - ranked list / drag-drop controls after Phase 2 UI patterns are
   mature.
8. **Release-switch integration** - optional, default-off, only after operator
   visibility is strong and ADR-0013 audit/restore paths remain intact.

## 11. Non-goals

- No attempt to decide the universally "best" edition.
- No MusicBrainz cleanup or Lidarr metadata ownership.
- No auto box-set replacement in v1.
- No automatic replacement of album with compilation/greatest-hits release.
- No bypass of audio `BLOCK` or identity `WRONG_ALBUM`.

## 12. Open questions

- Should custom preferences be global only, or also per artist / per album?
- Should the first implementation treat `remaster` as preferred over
  `original_album`, or merely review?
- How should regional editions be classified when tracklists differ only by
  ordering or bonus tracks?
- Should "prefer expanded editions" allow replacing a complete standard album,
  or only upgrading lossy/incomplete existing copies?
- Should manual override offer "remember this preference" during review, or keep
  learning out of v1?
