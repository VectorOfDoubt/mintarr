<!--
Before opening this PR:

1. Read CONTRIBUTING.md
2. Apply the boundary test from ADR-0008 (docs/architecture/adr/0008-strategic-positioning.md)
3. Ensure docs are updated in the same commit as code changes
4. Run the test suite locally: docker compose -f docker-compose.test.yaml run --rm tests

Delete sections that don't apply to your change.
-->

## Summary

<!-- One sentence: what does this PR do? -->

## What changed

<!-- Bullet list of changes -->

-
-
-

## Why

<!-- Reference the issue, design doc, or ADR motivating this change -->

Refs #

## Test plan

<!-- How did you verify the change works? -->

- [ ] Added unit tests for new logic
- [ ] Added regression tests for the bug fix (if applicable)
- [ ] All existing tests still pass
- [ ] Tested manually against a local Lidarr (describe scenario)

## Boundary test (ADR-0008)

<!-- Which step does this PR fall under? -->

- [ ] 1. Improves QC accuracy, source coverage, evidence, policy precision, audit clarity, or import safety
- [ ] 2. Improves operator experience of existing Mintarr scope
- [ ] 3. Crosses into Lidarr's territory (artist/album library, MusicBrainz, indexer config, tag writing) — REQUIRES PRIOR DISCUSSION
- [ ] 4. Reduces Lidarr coupling without expanding library-management scope

## Breaking changes

- [ ] No breaking changes
- [ ] Breaks an Adapter Protocol contract — requires `ADAPTER_PROTOCOL_v2.md`
- [ ] Breaks a Connector Manifest contract — requires `CONNECTOR_MANIFEST_v2.md`
- [ ] Breaks the Sidecar format — requires `SIDECAR_FORMAT_v3.md`
- [ ] Breaks the HTTP API — requires `HTTP_API_v2.md`
- [ ] Operator action required on upgrade — documented in `UPGRADE_GUIDE.md`

## Documentation

- [ ] Documentation updated in the same commit(s) as code
- [ ] New ADR added (if a major decision was made)
- [ ] CHANGELOG.md updated (if user-visible change)

## Security

- [ ] No security-relevant change
- [ ] Security model unchanged — change does not affect authentication, file I/O, subprocess invocation, or HTTP endpoints
- [ ] Security impact considered and documented (describe below)

<!-- If checked the last box, describe the security impact and rationale here -->

## Checklist

- [ ] Conventional commit format used
- [ ] `ruff format` and `ruff check` pass
- [ ] `mypy` passes
- [ ] AI co-authors attributed if applicable

## Notes for reviewer

<!-- Anything that would help the reviewer understand the change -->
