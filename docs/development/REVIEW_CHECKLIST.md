# Review Checklist

> **Type:** Development / process
> **Version:** 1.0 — 2026-05-26
> **Status:** Living document. Add new items as recurring review concerns emerge.
> **Audience:** PR reviewers (maintainers and community). Contributors who want to predict what reviewers will check.

---

## 1. How to use this checklist

This document lists the questions a Mintarr PR reviewer asks. Use it as:

- **As an author:** check your PR against this before requesting review. Most are easily fixable; doing them yourself shortens the review cycle.
- **As a reviewer:** run through this for non-trivial PRs. Skip the obviously-not-applicable sections.

The list is intentionally long. Most PRs only touch 3-5 sections. The exhaustive form helps newer reviewers know what to look for.

## 2. Scope

- [ ] Does the change pass the [ADR-0008 boundary test](../architecture/adr/0008-strategic-positioning.md)?
- [ ] If the change crosses into Lidarr's territory (library, MusicBrainz, indexer config, etc.), is there an issue first discussing why?
- [ ] Is the change tightly scoped, or has it grown into "did everything"? Should it be split?
- [ ] Does the change conflict with a locked ADR? If yes, is there a successor ADR proposal?

## 3. Tests

- [ ] Are tests added for new logic?
- [ ] Are tests added for the bug being fixed (regression tests)?
- [ ] Do all existing tests still pass?
- [ ] Are the new tests fast (<100ms each)?
- [ ] Are the new tests isolated (no shared state between tests)?
- [ ] Are the new tests deterministic (no randomness, no timing dependence)?
- [ ] Are assertion messages specific (assert `x == 5`, not `assert x is not None`)?

## 4. Documentation

- [ ] Is documentation updated in the same commit/PR as the code change?
- [ ] Is the right document updated? See [Documentation Index](../MINTARR_DOCUMENTATION_INDEX.md) for mapping.
- [ ] If a contract changes (Adapter / Connector / Sidecar / HTTP API), is the spec file version bumped per [ADR-0004](../architecture/adr/0004-api-versioning-semver.md)?
- [ ] If a major decision is made, is there an ADR?
- [ ] Are examples accurate (no copy-paste errors, no outdated references)?

## 5. Boundaries

- [ ] Adapter code does not import from `app/server.py`, `app/pipeline.py`, or `app/state_db.py`
- [ ] Pipeline code does not know which adapter ran (operates on `RawDownload` + `ctx`)
- [ ] state_db is not used as a source of truth (sidecars are)
- [ ] Lidarr-facing code goes through the (planned) `LidarrClient` interface, not direct `requests.get(f"{api}/...")` calls
- [ ] Connector code wraps adapter code, not the other way around

## 6. Security

- [ ] Are secrets handled correctly? (Not in browser-visible config, not in logs, not in responses)
- [ ] Is path traversal checked at endpoints AND adapters (defence in depth)?
- [ ] Are symlinks rejected outright in source folders?
- [ ] Is API key comparison constant-time (`hmac.compare_digest`)?
- [ ] Are subprocess invocations using list-of-strings (not shell=True)?
- [ ] Do all endpoints have `@require_apikey` (except `/health`)?
- [ ] Is logged data redacted for sensitive query parameters?
- [ ] If the change affects authentication, network surface, or filesystem access, has [SECURITY_MODEL.md](../architecture/SECURITY_MODEL.md) been reviewed?

## 7. Error handling

- [ ] Are exceptions caught specifically (not bare `except Exception:`)?
- [ ] Are exceptions logged before being swallowed?
- [ ] Are error messages specific enough to debug?
- [ ] Are HTTP status codes appropriate (400 for client error, 503 for unavailable, etc.)?
- [ ] Does the failure path leave state consistent (no half-written sidecars, no orphan worker rows)?

## 8. Concurrency

- [ ] Does the change introduce shared mutable state?
- [ ] If shared state is added, is it lock-protected?
- [ ] Does the change affect the worker queue mechanics?
- [ ] If queue mechanics change, are lease + heartbeat behaviour preserved?
- [ ] Does the change introduce a race condition (e.g., between job enqueue and worker pickup)?

## 9. Performance

- [ ] Does the change add a database query inside a loop (N+1 query problem)?
- [ ] Does the change introduce O(n²) behaviour on a path that could see n=1000+?
- [ ] Does the change add unbounded memory growth (caches without eviction, lists that grow forever)?
- [ ] If the change touches a hot path, is there a benchmark or profile?

Mintarr is not a high-throughput application. "Slow but correct" is generally fine. "Slow and incorrect" is not.

## 10. Style

- [ ] Does the code pass `ruff format`?
- [ ] Does the code pass `ruff check`?
- [ ] Does the code pass `mypy`?
- [ ] Are public functions type-hinted?
- [ ] Are imports ordered correctly?
- [ ] Are docstrings imperative-mood and explaining the why (not restating the what)?

These are usually caught by CI. The checklist item is just "did CI pass".

## 11. Commit hygiene

- [ ] Are commits Conventional-Commits-formatted?
- [ ] Is the SemVer impact correctly indicated (`feat:` = minor, `fix:` = patch, `feat!:` = major)?
- [ ] Is the commit message body explaining the *why*, not the *what*?
- [ ] Is the commit history clean (no WIP commits, no merge commits unless intentional)?
- [ ] Are AI co-authors attributed if applicable?

## 12. Compatibility

- [ ] Does the change break the SourceAdapter Protocol? If yes, is it documented as a v2 transition?
- [ ] Does the change modify state_db schema? If yes, is the migration additive and idempotent?
- [ ] Does the change modify the sidecar format? If yes, is `SIDECAR_FORMAT_v2.md` updated or is `v3.md` introduced?
- [ ] Does the change affect operator-visible defaults (env vars, port mappings)? If yes, is [UPGRADE_GUIDE.md](../operations/UPGRADE_GUIDE.md) updated?

## 13. Operator impact

- [ ] Is the change safe to deploy to existing installations?
- [ ] Are new env vars documented in [CONFIGURATION.md](../operations/CONFIGURATION.md)?
- [ ] If the change requires operator action on upgrade, is that documented?
- [ ] Are the new features discoverable (dashboard surface, log output, docs)?

## 14. Reviewer-side guardrails

When reviewing, also ask yourself:

- [ ] Am I familiar with this subsystem? If not, can someone else review?
- [ ] Have I run the tests locally?
- [ ] Have I read the linked design doc / issue / ADR?
- [ ] If I were the operator, would I understand what changed and what to do about it?

These are not pass/fail; they are reviewer self-checks.

## 15. When to approve

A PR is approvable when:

- All applicable checklist items pass
- The code reviewer has understood the change (not just glanced at the diff)
- CI is green
- The author has responded to any blocking comments

A PR is NOT approvable just because the reviewer is tired of looking at it. If something is wrong, say so, even at the cost of more iterations.

## 16. When to request changes

If a PR has any of:

- Failing tests
- Missing documentation
- Scope conflicts with an ADR
- Security regression
- Breaking changes without version bump

then request changes — do not "approve with comments".

For style nits, "approve with comments" is fine.

## 17. When to close without merging

Rarely needed, but appropriate when:

- The PR's scope is fundamentally out of bounds (ADR-0008 boundary test fails decisively)
- The PR duplicates work from another PR
- The author has abandoned the PR (no response for 60+ days after review feedback)
- The PR is hostile or made in bad faith

Closing should be polite, reference the relevant ADR or pattern, and leave the door open for a revised proposal.

---

> Last updated: 2026-05-26
