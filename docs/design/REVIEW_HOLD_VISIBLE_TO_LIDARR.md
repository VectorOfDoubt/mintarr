# Review Hold Visible to Lidarr

> **Type:** Design document
> **Version:** 1.0 - 2026-06-11
> **Status:** Implemented
> **Related:** [ADR-0012 QC import-gate scope](../architecture/adr/0012-qc-import-gate-scope.md), [Lidarr integration](../specs/LIDARR_INTEGRATION.md), [Lidarr dogfood validation](../development/LIDARR_DOGFOOD_VALIDATION.md)

## 1. Problem

When Mintarr decides `REVIEW_REQUIRED`, it previously **removed the grab from
Lidarr's view**: `_mark_review_required` set `hidden_from_lidarr=True` and
`_create_review_required` called `_cleanup_lidarr_queue` (deleting the Lidarr queue
row). Because review does **not** blocklist (review ≠ rejection), Lidarr then saw a
monitored, still-wanted album with no download in progress and **re-grabbed it
automatically** — before the operator ever saw the review.

Observed in the 2026-06-11 dogfood: a deluxe edition held for review was followed,
~10 s later, by an automatic Lidarr re-grab of another release for the same album.

## 2. Invariant

> **A pending review must remain visible to the emulated download client until
> promote / discard / expire resolves it.**

`REVIEW_REQUIRED` means *Mintarr has taken responsibility for the candidate, but a
human must decide*. While that is true, Lidarr must keep seeing an active download
for the same `downloadId`, so it does not re-grab the album. Ownership stays with
Mintarr; Lidarr gets a stable "download still active (paused)" signal.

This is an explicit policy (`lidarr_hold`), not a side effect. "Paused" is only how
the hold is *presented* over the SAB protocol — the internal concept is the hold.

## 3. Mechanism

- **`_mark_review_required`** sets `status="review_required"`, **`lidarr_hold=True`**,
  `percent=100`, `warning`, and does **not** set `hidden_from_lidarr`.
- **`_create_review_required`** does **not** call `_cleanup_lidarr_queue` while
  pending review; the Lidarr queue row is left in place.
- **Emulated SAB queue** (`mode=queue`) includes `lidarr_hold` jobs; `_sab_queue_slot`
  presents them as **`Paused`** (percent 100, no time left). They never appear in
  `mode=history` (completed/failed), so Lidarr never treats the hold as ready to
  import.
- **Operator / lifecycle resolution** clears the hold and cleans the queue:
  - **promote** → ManualImport → `_mark_import_completed` (hidden) → queue settles.
  - **discard** → `_blocklist_grab` (stops re-grab of this exact release) +
    `_cleanup_lidarr_queue`.
  - **expire** → `_blocklist_grab` + `_cleanup_lidarr_queue`, clears `lidarr_hold`.
- **`_mark_import_completed` / `_mark_import_failed`** clear `lidarr_hold` (defence;
  `hidden_from_lidarr` already removes the slot).

## 4. Recovery

`_jobs` is persisted (`JOBS_FILE`). A review-held job keeps `lidarr_hold=True` and
`status="review_required"` across restart, so the emulated SAB queue still shows the
paused hold and Lidarr still does not re-grab. `_expire_review_required_jobs` only
expires holds older than `REVIEW_RETENTION_DAYS`, so a fresh hold survives a restart.

## 5. Non-goals / notes

- **Do not blocklist on review.** Review is "wait for a human", not rejection;
  blocklisting would wrongly stop the operator from ever promoting it.
- The dogfood invariant *"a blocked/review-held record must not remain stuck in
  Lidarr's queue as an active retry loop"* still holds: a paused hold is **not** a
  retry loop — it is an intentional, stable hold cleared by the operator.
- A discarded/expired release is blocklisted, so Lidarr will not re-grab that exact
  release; it may still grab a *different* release for the album, which is then
  re-gated by Mintarr (correct).
