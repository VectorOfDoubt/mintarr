# Album-Level Cancel Hold

> **Type:** Design document
> **Version:** 0.1 - 2026-06-13
> **Status:** Proposed
> **Related:** [Review hold visible to Lidarr](REVIEW_HOLD_VISIBLE_TO_LIDARR.md), [Lidarr dogfood validation](../development/LIDARR_DOGFOOD_VALIDATION.md), [ADR-0012 QC import-gate scope](../architecture/adr/0012-qc-import-gate-scope.md), [F2 worker queue](F2_WORKER_QUEUE_DESIGN.md)
> **Tracking:** [#160](https://github.com/eivindsjursen-lab/mintarr/issues/160)

## 1. Problem

Mintarr now correctly honors Lidarr's "remove from download client +
blocklist" action for an active grab:

1. Lidarr sends SAB delete (`mode=queue&name=delete` and/or
   `mode=history&name=delete`) for a `downloadId`.
2. Mintarr maps the `downloadId` to an active worker job.
3. Mintarr sets `cancel_requested`.
4. The worker stops before QC/import and terminalizes as `cancelled`.

The 2026-06-13 dogfood proved the per-release behavior is correct. It also
exposed a second-level product gap:

- The operator may mean "stop trying this album now".
- Lidarr's remove/blocklist action is release-level.
- After one release is cancelled/blocklisted, Lidarr may immediately search and
  grab a different candidate for the same monitored/wanted album.

Observed dogfood:

- First JID `8a507b46b060`: TIDAL candidate cancelled successfully.
- Lidarr immediately grabbed a second Soulseek candidate for the same album.
- Second JID `9e6d2631f136`: also cancelled successfully.

Mintarr should keep the correct per-release cancel behavior, but add an
optional album-level hold so operator intent can be represented without turning
off a source globally.

## 2. Decision

Add a short-lived **album-level cancel hold** keyed by Lidarr `albumId`.

When Mintarr observes a user/operator removal of an active grab from Lidarr,
it may create a hold for the target album. While the hold is active, Mintarr's
search/Newznab exposure suppresses or rejects candidates for that album across
Mintarr-controlled sources. The exact cancelled release is still blocklisted as
today.

The hold is:

- **album-scoped**, not source-global;
- **time-boxed** by default;
- **visible** in dashboard/audit;
- **operator-clearable**;
- **read-only** with respect to the music library;
- **separate** from review-hold, which keeps a specific candidate paused while
  awaiting a promote/discard decision.

## 3. Scope

### In scope

- Holds created from active-grab cancel/blocklist events observed through the
  emulated SAB API.
- Suppression of future Mintarr Newznab/search candidates for the held album.
- State persistence, expiry, audit, and dashboard visibility.
- Manual clear endpoint/action.

### Out of scope

- Changing Lidarr monitoring status.
- Mutating Lidarr metadata or album state.
- Global source disable.
- Permanent blocklisting of an entire album/release family.
- Blocking candidates from non-Mintarr indexers/download clients that Lidarr
  uses independently.

## 4. Semantics

### 4.1 Per-release cancel remains unchanged

When Lidarr removes a queued Mintarr item:

- active worker gets `cancel_requested`;
- exact release is blocklisted if Lidarr requested blocklist;
- job terminalizes as `cancelled`;
- no QC/import continues after cancel.

This behavior is correct and must not be weakened.

### 4.2 Album hold is additional operator intent

If Mintarr can infer the target `albumId`, it creates:

```text
album_hold:
  album_id
  reason = operator_cancel
  created_at
  expires_at
  source_jid
  source_release_guid/source_id
  actor = lidarr_remove_blocklist | user_dashboard
```

Default duration should be short enough to avoid hiding wanted albums forever,
but long enough to stop immediate candidate cascades. Suggested v1 default:

```text
MINTARR_ALBUM_HOLD_TTL_MINUTES=60
```

The TTL should be configurable. A dashboard clear action should remove the hold
immediately.

### 4.3 Unknown album id

If Mintarr cannot infer `albumId`, it must not create a broad hold. Per-release
cancel still works. Record an audit/log warning so the missing mapping can be
diagnosed.

## 5. Hold Application Point

Before a hold can be created, Mintarr needs a trustworthy Lidarr `albumId`.
Resolution should be conservative:

1. Prefer a `target_album_id` captured on the worker job when the grab was
   first enqueued.
2. If absent, infer from Lidarr queue/history while the row is still visible.
3. If still absent, do not create an album hold.

V1 should add `target_album_id` to queued jobs as early as possible. The cancel
event can arrive while the download is still active, before any verification
record exists, so sidecar/record metadata is too late for this feature. Guessing
from release title is not acceptable; a broad or wrong hold is worse than no
hold.

The hold should apply where Mintarr exposes candidates to Lidarr:

```text
Lidarr search/RSS -> Mintarr Newznab aggregation -> adapter candidates
                                               -> album hold filter
                                               -> Newznab response
```

For a held album, Mintarr should either:

1. suppress matching candidates from the feed, or
2. return them with an explicit rejection marker if the Newznab/Lidarr surface
   can represent it cleanly.

V1 should prefer suppression plus dashboard/audit visibility. It is simpler and
avoids depending on Lidarr interpreting custom rejection text from Newznab.

The hold must be checked after candidate-to-album matching is known. For TIDAL
adapter results, the target album is often known from the search context or
release metadata. For weaker sources where album mapping is unknown, do not
guess broadly; let the normal candidate flow proceed.

## 6. State Model

Add a state table:

```sql
album_holds (
  album_id INTEGER PRIMARY KEY,
  reason TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  source_jid TEXT,
  source_type TEXT,
  source_id TEXT,
  actor TEXT,
  cleared_at REAL,
  cleared_by TEXT,
  details_json TEXT
)
```

Active hold:

```text
cleared_at IS NULL AND expires_at > now
```

Historical holds stay queryable for audit.

Use state DB, not sidecars, because holds are album-scoped and affect future
search exposure, not one verification record.

## 7. Dashboard / API

Dashboard should show:

- active album holds;
- album id and best-known artist/album title;
- reason;
- source JID that created the hold;
- expiry time;
- clear action.

Suggested endpoints:

```text
GET    /dashboard/v1/album-holds
POST   /album-holds/<album_id>/clear
POST   /album-holds/<album_id>
```

Manual creation is useful when an operator knows they want to pause attempts for
an album without waiting for a cancel event.

## 8. Safety Rules

- Holds must never create `BLOCK` verification decisions by themselves.
- Holds must never delete library files.
- Holds must not clear or override exact-release blocklists.
- Holds must not hide unrelated albums, artists, or sources.
- Holds must expire automatically.
- Holds must be visible enough that the operator can understand why an album is
  not receiving new Mintarr candidates.

## 9. Interaction With Review Hold

Review-hold and album-hold solve different problems:

| Mechanism | Scope | Purpose |
|---|---|---|
| Review hold | one `downloadId` / candidate | keep pending review visible to Lidarr as paused |
| Album cancel hold | one Lidarr `albumId` | stop immediate same-album re-grabs after operator cancel |

If a candidate reaches `REVIEW_REQUIRED`, review-hold owns that candidate. Do
not create album-hold merely because a review exists. Album-hold is for explicit
operator cancel/hold intent.

If the operator discards a review, exact-release blocklisting remains enough in
v1. A future option may offer "discard and pause album" as an explicit dashboard
action.

## 10. Rollout Plan

1. **State slice:** album hold table + pure helpers (`create`, `active`,
   `clear`, `expire`) with tests. No search effect.
2. **Cancel integration slice:** create hold when Lidarr remove/blocklist
   cancels an active job and target `albumId` is known. Dashboard read-only
   visibility.
3. **Search filter slice:** suppress held-album candidates in Newznab results.
4. **Dashboard control slice:** clear/create hold actions.
5. **Dogfood:** repeat the Hank Williams cancel test and verify no immediate
   second candidate is grabbed while hold is active.

## 11. Open Questions

- Default TTL: 30 minutes, 60 minutes, or until manually cleared?
- Should a dashboard discard offer a second button: "discard and pause album"?
- Should holds suppress RSS results as well as interactive search? V1 should do
  both if they share the Newznab aggregation path.
- How should Mintarr derive display names for held albums without adding
  expensive Lidarr API calls to every dashboard render?
