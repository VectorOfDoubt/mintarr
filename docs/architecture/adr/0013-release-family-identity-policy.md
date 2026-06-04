# ADR-0013: Release-family identity policy for F5.1

**Status:** Accepted — locked 2026-06-04
**Deciders:** Eivind Sjursen, Claude, Codex
**Related:** [ADR-0007 No Lidarr fork](0007-no-lidarr-fork.md), [ADR-0008 Strategic positioning](0008-strategic-positioning.md), [ADR-0012 QC import-gate scope](0012-qc-import-gate-scope.md), [ROADMAP Phase 5](../../strategy/ROADMAP.md), [F4.5 Optional verifier connectors](../../design/F4.5_OPTIONAL_VERIFIER_CONNECTORS.md)

---

## Context

F5.1 exists to mitigate Lidarr's release / edition matching weakness, most
visibly the ManualImport rejection:

```text
Album match is not close enough: 70.1 % vs 80 %
```

[ADR-0007](0007-no-lidarr-fork.md) rejected a Lidarr fork and explicitly
states that this class of bug is not solved by inheriting Lidarr's internals:
the root causes are MusicBrainz data quality, deluxe/remaster/anniversary
variants, physical-vs-digital tracklist mismatch, and interaction with the
existing library state. Mintarr can mitigate this in the QC/import orchestration
layer, but it must not become a library manager or MusicBrainz owner
([ADR-0008](0008-strategic-positioning.md)).

The current codebase already contains an informal first pass at F5.1:

- `_normalize_track_title_for_match()`
- `_score_release_match()`
- `_is_release_family_rejection()`
- an inline auto-release-switch block in `server.py` before ManualImport
- force-import of Lidarr release-family rejections after V2 audio QC passes

Those helpers prove the mechanism is useful, but they are not yet a proper
architecture. The logic is embedded in the import path, hard to audit as a
policy decision, and the auto-release-switch mutates Lidarr's active
`albumReleaseId` as a hidden side effect. F5.1 therefore is not "start from
zero"; it is **formalise the hidden release-family logic into a testable,
auditable identity-policy layer**.

## Decision

Mintarr will model release-family matching as a separate **release identity
policy axis**, parallel to but distinct from audio-quality verification.

The two axes are:

```text
audio_decision:
  ACCEPT | ACCEPT_PROVISIONAL | REVIEW_REQUIRED | BLOCK

identity_decision:
  SAME_RELEASE | SAME_FAMILY | AMBIGUOUS_EDITION | INSUFFICIENT_EVIDENCE | WRONG_ALBUM
```

The final import policy combines them with this invariant:

> Good audio can never compensate for wrong identity.

The F5.1 implementation direction is:

1. **Lidarr-first expected metadata.** Lidarr remains the source of truth for
   the target album/release/track model because Lidarr is the output system that
   must import the files. Mintarr scores against Lidarr's album, releases, and
   tracks first.
2. **MusicBrainz as advisory evidence, not the import truth.** MusicBrainz IDs,
   AcoustID, Picard, and beets evidence may strengthen or weaken confidence,
   especially for artist/release-group identity, but direct MusicBrainz lookup
   does not replace Lidarr as the expected metadata source.
3. **Identity is evidence -> policy.** Release-family scoring emits evidence
   into the existing sidecar/sensor model; policy consumes that evidence to
   produce `identity_decision`. This follows the verifier architecture instead
   of creating a parallel heuristics silo.
4. **Confidence and abstain are first-class.** Identity scoring must carry a
   confidence value and enough explanatory evidence to justify the decision.
   Weak metadata produces `INSUFFICIENT_EVIDENCE`, not `WRONG_ALBUM`.
5. **Lidarr mutation is default-off and explicit.** Writing back to Lidarr
   (`PUT /album/{id}` to switch active release) is a separate, opt-in release
   switch strategy. It is never a hidden default side effect.

## Policy model

The combined policy is intentionally conservative:

| Audio decision | Identity decision | Default result |
|---|---|---|
| `BLOCK` | any | `BLOCK` |
| any | `WRONG_ALBUM` | `BLOCK` |
| `REVIEW_REQUIRED` | any non-wrong identity | `REVIEW_REQUIRED` |
| audio accepted | `SAME_RELEASE` | import |
| audio accepted | `SAME_FAMILY` | import or controlled force-import of release-family rejection |
| audio accepted | `AMBIGUOUS_EDITION` | `REVIEW_REQUIRED` |
| audio accepted | `INSUFFICIENT_EVIDENCE` | `REVIEW_REQUIRED` |

Rows are evaluated top-to-bottom and the first match wins, so an audio `BLOCK`
or an `identity == WRONG_ALBUM` always takes precedence over any import path —
neither axis can be overridden by the other being clean.

`WRONG_ALBUM` requires strong evidence. Examples:

- target artist MusicBrainz ID and observed artist MusicBrainz ID are both
  known and differ
- target release-group ID and observed release-group ID are both known and
  differ
- Lidarr target album ID is known and ManualImport resolves to a different
  album ID
- high-confidence tracklist/title evidence points outside the target release
  family

Weak or missing tags, noisy filenames, or missing optional verifier output must
not produce `WRONG_ALBUM`; they produce `INSUFFICIENT_EVIDENCE` or
`AMBIGUOUS_EDITION`.

## Metadata sources

### Expected metadata

F5.1 uses Lidarr APIs first:

- target album ID from Mintarr job payload, Lidarr queue, or Lidarr history
- `GET /api/v1/album/{id}` for album, current release, releases, statistics
- `GET /api/v1/track?albumReleaseId=<id>` for release-specific track titles
- `GET /api/v1/manualimport?folder=<path>` for Lidarr's own candidate and
  rejection state

Direct MusicBrainz lookup is not required for the first implementation. If
added later, it is advisory evidence and must not create a second import truth.

### Observed metadata

Observed metadata comes from read-only sources:

- existing source metadata when the adapter has it
- file tags read with a read-only tag reader such as `mutagen`
- normalized filenames as fallback
- optional `picard_beets_acoustid` evidence when that verifier gains a runtime
  runner

F5.1 must not write tags to source or output files. Tag writing remains out of
scope until a separate ADR establishes tag-ownership boundaries.

## Release scoring

The scoring module should be pure and independently testable. It should not
import Flask, call Lidarr, touch the filesystem, mutate global state, or log
operator decisions.

The first implementation should extract the existing helper behavior from
`server.py` into a domain module, tentatively:

```text
app/release_family.py
```

Suggested domain objects:

```text
ObservedRelease
ExpectedRelease
ReleaseFamilyEvidence
ReleaseIdentityDecision
```

Scoring inputs:

- target album ID / release ID when known
- release IDs and track counts from Lidarr
- normalized expected track titles
- normalized observed track titles
- observed file count
- observed artist / album / release-group MBIDs when available
- Lidarr ManualImport rejection reasons

Scoring outputs:

- identity decision
- confidence
- best matched release ID
- current release ID
- track-count delta
- title similarity ratio
- MBID agreement/disagreement
- relevant Lidarr rejections
- human-readable explanation strings for dashboard/audit

Thresholds are design parameters, not hard-coded magic in the import function.
The first cut should prefer review over risky automation.

## Lidarr release switching

Switching `albumReleaseId` in Lidarr is the highest-risk part of F5.1 because it
mutates Lidarr's own model. It is permitted only under an explicit strategy.

Default strategy:

```text
release_switch_strategy = disabled
```

Allowed strategies:

- `disabled` - never write back to Lidarr; use force-import/review/rescue only
- `review` - surface the proposed switch to the operator for confirmation
- `auto_high_confidence` - automatically switch only when all high-confidence
  conditions pass

`auto_high_confidence` requires all of:

- same Lidarr album ID
- best release confidence above the locked threshold
- no strong MusicBrainz artist/release-group disagreement
- clear track-count or track-title advantage over the current release
- old and new release IDs recorded before mutation

Every switch attempt must audit:

- jid
- album ID
- old release ID
- new release ID
- strategy
- confidence
- score components
- reason / explanation
- result of the Lidarr API write

If a switch is performed and the import fails, Mintarr should attempt to restore
the previous release where safe. If restore cannot be guaranteed, the sidecar
and audit log must say so.

## Dashboard and operator workflow

F5.1 identity evidence should be surfaced like other verifier evidence:

- identity decision and confidence
- best matched release vs Lidarr current release
- track-count comparison
- title mismatch summary
- MBID agreement/disagreement when known
- Lidarr ManualImport rejection reasons
- whether a release switch was proposed, skipped, performed, restored, or failed

Operator actions:

- approve a proposed release switch
- retry import after identity policy is unchanged
- manually override an identity review after audio QC has passed
- discard/block as today

Manual override is allowed only for identity ambiguity after audio QC has
accepted or provisionally accepted the files. It must not bypass hard audio
blocks such as codec mismatch, FLAC integrity failure, or validator error.

## Failure behavior

Identity evaluation is proactive, but mutation is reactive:

- Mintarr should collect identity evidence during the pipeline for every
  candidate when metadata is available.
- Mintarr should act on that evidence when Lidarr rejects, when confidence is
  low, or when the identity decision affects import safety.
- If Lidarr metadata APIs are unavailable, F5.1 must fail to
  `INSUFFICIENT_EVIDENCE` / `REVIEW_REQUIRED`, not `BLOCK`.
- If optional MusicBrainz/AcoustID/beets/Picard evidence is unavailable, policy
  must continue from Lidarr + tag/filename evidence.

## Rationale

### Separate axes prevent false confidence

Combining identity into the audio score would let excellent FLAC evidence hide a
wrong album. That is the failure Mintarr must avoid. Audio authenticity and
release identity are different questions and must remain separately visible.

### Lidarr-first avoids two truths

Mintarr imports into Lidarr. If Mintarr scores against MusicBrainz directly
while Lidarr uses its own release model, Mintarr can be "right" in abstract and
still fail import in practice. Using Lidarr first aligns the scoring target with
the system that must accept the files.

### MusicBrainz IDs still matter

Names are unreliable: `10cc` vs `10-cc` is exactly the kind of case where string
comparison is the wrong identity primitive. When available, MusicBrainz artist,
release-group, and release IDs should be used as high-value identity evidence.
But Mintarr must not automatically merge, delete, rename, or relink Lidarr
artists/albums. It clarifies the decision basis; it does not repair the
library.

### Release switching needs stricter control than force-import

Force-import after Mintarr QC is still Mintarr owning its import decision.
Changing Lidarr's active release changes Lidarr's model. That write path is
close to ADR-0008's boundary and therefore requires opt-in strategy, audit, and
restore handling.

### Abstain beats false block

Completed-folder lanes can have weak provenance: missing tags, poor filenames,
or partial metadata. Treating weak metadata as proof of wrong identity would
create false blocks. `INSUFFICIENT_EVIDENCE` preserves safety by routing to
review.

## Consequences

### Positive

- F5.1 becomes testable as pure domain logic instead of embedded import-flow
  heuristics.
- Operators get clear explanations for Lidarr match failures.
- Multi-release / deluxe / remaster / anniversary failures can be mitigated
  without forking Lidarr.
- MusicBrainz evidence improves identity confidence without pulling Mintarr into
  library management.
- Dangerous writes back to Lidarr are explicit, configurable, and auditable.

### Negative / accepted trade-offs

- Some cases go to review instead of being auto-fixed, especially when metadata
  confidence is low.
- First implementation does not promise automatic MusicBrainz cleanup of Lidarr
  artists/albums.
- Release switching becomes more verbose and policy-heavy than the current
  inline heuristic.
- Additional sidecar evidence and dashboard UI are required for the feature to
  be understandable.

## Alternatives considered

### Alternative 1: Keep the current inline heuristics

Rejected. The current code proves the mitigation can work, but the hidden
release switch and loosely explained force-import behavior are not robust enough
for a Phase 5 policy feature.

### Alternative 2: MusicBrainz-first scoring

Rejected for the primary path. It creates two truths: MusicBrainz may identify a
release correctly while Lidarr's local model still imports differently. Direct
MusicBrainz remains useful as advisory evidence.

### Alternative 3: Always auto-switch Lidarr to the best release

Rejected. This mutates Lidarr library metadata by default and crosses too close
to the library-management boundary. Auto-switching is allowed only as an
opt-in, high-confidence strategy.

### Alternative 4: Treat metadata mismatch as an audio-verifier failure

Rejected. Identity mismatch is not audio authenticity. It needs its own policy
axis and explanation surface.

## Implementation sequence

1. Extract a pure `release_family` scoring module from the existing helpers.
2. Add unit tests for track normalization, track-count similarity, title
   similarity, MBID agreement/disagreement, confidence, and abstain behavior.
3. Add read-only observed metadata extraction (`mutagen` tags + filename
   fallback), persisted as sidecar evidence.
4. Integrate identity evidence into V2 policy as a separate axis.
5. Replace the inline auto-release-switch block with an explicit release switch
   strategy.
6. Add dashboard explanations and audited operator actions.

## Out of scope

- Forking Lidarr or changing Lidarr internals.
- Auto-fixing Lidarr artists, albums, metadata profiles, or MusicBrainz links.
- Writing tags to files.
- Making Picard/beets/AcoustID a required verifier.
- Universal-gate coverage for Lidarr-owned grabs beyond ADR-0012's scoped gate.

---

> Locked: 2026-06-04
