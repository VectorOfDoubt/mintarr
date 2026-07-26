# Contributing to Mintarr

> Read this **before** opening a pull request. PRs that miss the scope boundary or skip the test suite get held in review and often need significant rework.

---

## Before you start

1. **Read [VISION.md](docs/strategy/VISION.md)** and **[ADR-0008 Strategic positioning](docs/architecture/adr/0008-strategic-positioning.md)**.

   Mintarr has a deliberately narrow scope. Many otherwise-reasonable contributions are out of scope because they pull Mintarr toward Lidarr's territory (artist/album library, MusicBrainz model, indexer configuration, tag writing). ADR-0008's boundary test tells you which side of the line a proposed feature sits on.

2. **For non-trivial changes, open an issue first.**

   "Non-trivial" means anything beyond a typo fix, a minor docstring update, or a single-file bug fix. New features, architecture changes, new dependencies — open an issue describing the change before writing code. This saves wasted work when the maintainers see a scope conflict.

3. **Read the relevant design document.**

   Mintarr's foundation work is tracked in [docs/MINTARR_DOCUMENTATION_INDEX.md](docs/MINTARR_DOCUMENTATION_INDEX.md). Per-feature design docs live under [docs/design/](docs/design/). If you're working on something the design doc covers, read it first.

## Setting up the development environment

See [docs/development/DEVELOPMENT.md](docs/development/DEVELOPMENT.md) for the full setup. Short version:

```bash
git clone https://github.com/eivindsjursen-lab/mintarr.git
cd mintarr
# Tests run in Docker — no local Python install needed
docker compose -f docker-compose.test.yaml run --rm tests
```

The test suite must be green before opening a PR. If you cannot run Docker locally, the GitHub Actions CI will run it on your PR; expect to iterate based on CI failures.

## Running tests

Tests are the load-bearing constraint on Mintarr's evolution. The test suite for any given PR includes:

- All existing tests (currently 300+, must remain green)
- New tests for any new logic in the PR
- Regression tests for any bug fix

Read [docs/development/TESTING.md](docs/development/TESTING.md) for test patterns, fixtures, and how to write new tests.

## Style and conventions

Read [docs/development/STYLE_GUIDE.md](docs/development/STYLE_GUIDE.md) and [docs/development/COMMIT_CONVENTION.md](docs/development/COMMIT_CONVENTION.md).

Short version:

- Python 3.12+ syntax
- `ruff` formats and lints; configuration in `pyproject.toml`
- Imports sorted by `ruff` (which uses `isort` semantics)
- Type hints on public functions; `mypy` is configured but the CI job is
  disabled until the v0.2.0 type-cleanup issue is resolved
- Conventional commits (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`)
- One commit per logical change; squash branches with many WIP commits before merge

## The boundary test

ADR-0008 defines a four-step test for whether a feature is in scope. Apply it before opening a PR:

1. **Does the feature improve QC accuracy, source coverage, verifier evidence, policy precision, audit clarity, or import safety?** → In scope.
2. **Does the feature improve operator experience of Mintarr's existing scope (dashboard, REVIEW_REQUIRED workflow, configuration, observability)?** → In scope.
3. **Does the feature require Mintarr to start tracking, managing or owning something Lidarr already owns (library, MusicBrainz model, indexer config, profiles, import targets)?** → **Out of scope.** Use Lidarr, or send a PR to Lidarr upstream.
4. **Does the feature reduce Lidarr coupling without expanding Mintarr's library-management scope?** → In scope.

If your proposed feature lands in step 3, please don't open the PR. Open an issue describing the use case, and we'll either find a way to address it inside Mintarr's scope or point at the upstream Lidarr path.

## Writing adapters

Mintarr is designed for community-contributed adapters. The contract is:

- **`SourceAdapter`** — see [ADAPTER_PROTOCOL_v1.md](docs/specs/ADAPTER_PROTOCOL_v1.md) for the locked contract
- **`VerifierAdapter`** — same spec, separate Protocol
- **`OutputAdapter`** — same spec, separate Protocol
- **Tutorial:** [ADAPTER_TUTORIAL.md](docs/development/ADAPTER_TUTORIAL.md)

Adapter contracts are SemVer-versioned via filenames ([ADR-0004](docs/architecture/adr/0004-api-versioning-semver.md)). Adapters declare which version they target. Mintarr supports multiple adapter API versions in the same container.

If your adapter has dependencies (external HTTP services, system binaries, Python packages), document them in your adapter's docstring and reference the [Connector model](docs/design/CONNECTOR_PLUGIN_ARCHITECTURE.md) for how operators will see them in Mintarr's dashboard.

## Writing documentation

Documentation is a deliverable, not an afterthought. Every PR that changes behaviour must update documentation in the same commit.

Documentation conventions:

- English only (Mintarr ships in English; translation infrastructure deferred — [Documentation Index §9](docs/MINTARR_DOCUMENTATION_INDEX.md#9-open-questions))
- Markdown, ASCII-friendly characters
- Diagrams as Mermaid in fenced code blocks
- Locked decisions get an ADR ([docs/architecture/adr/](docs/architecture/adr/))
- Spec versions are immutable once locked — breaking changes get a successor file ([ADR-0004](docs/architecture/adr/0004-api-versioning-semver.md))

If you're not sure which document a change belongs in, the [Documentation Index](docs/MINTARR_DOCUMENTATION_INDEX.md) maps every Mintarr document to its purpose.

## PR review process

Maintainers look for:

- **Scope.** Does the change pass the boundary test from ADR-0008?
- **Tests.** Are new tests added for new logic? Are existing tests still green?
- **Documentation.** Is the docs change in the same commit as the behaviour change?
- **Backwards compatibility.** Does the change preserve existing adapter contracts, sidecar format, and HTTP API surfaces? Breaking changes require a successor version (per [ADR-0004](docs/architecture/adr/0004-api-versioning-semver.md)).
- **Security.** Does the change touch authentication, file I/O, subprocess invocation, or HTTP endpoints? If yes, [SECURITY_MODEL.md](docs/architecture/SECURITY_MODEL.md) review applies.

A more detailed reviewer checklist is in [docs/development/REVIEW_CHECKLIST.md](docs/development/REVIEW_CHECKLIST.md).

Mintarr is paused indefinitely. New contributions may remain open as historical
input, but maintainers are not reviewing them while the pause remains in effect
and no response or merge timeline is promised. If the project resumes, review
will continue to favor correctness over speed as described in
[VISION.md](docs/strategy/VISION.md) §"Documentation pace and contribution
rhythm".

## When your contribution will not be accepted

Be prepared for these outcomes:

- **Out of scope.** ADR-0008 boundary test fails. The PR is closed with a reference to the ADR. Re-opening requires a successor ADR proposal.
- **Existing ADR forbids the change.** E.g., a proposal to add multi-user RBAC is rejected by ADR-0002. The PR is closed with a reference.
- **Dependency is not compatible with AGPL-3.0.** Mintarr is AGPL-3.0-only ([ADR-0005](docs/architecture/adr/0005-license.md)). New dependencies must be AGPL-compatible: permissive licenses (MIT/BSD/Apache-2.0), GPL-2.0-or-later, GPL-3.0, LGPL, or AGPL-3.0 itself are fine. GPL-2.0-only is not compatible. Proprietary or unspecified-license dependencies are rejected.
- **Security regression.** Any change that weakens existing authentication, exposes secrets in browser-visible config, mounts the Docker socket, or violates the locked invariants in [Connector architecture §15](docs/design/CONNECTOR_PLUGIN_ARCHITECTURE.md) is rejected.
- **Breaks the test suite without addressing the underlying issue.** Tests are not negotiable. If a test no longer reflects current behaviour, update the test in the same PR and explain why in the commit message.

When in doubt, open an issue first. The cost of a rejected issue is small; the cost of a rejected 500-line PR is much higher for everyone involved.

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By participating, you agree to abide by its terms.

## License

Contributions to Mintarr are licensed under the same license as the project: **AGPL-3.0-only** ([LICENSE](LICENSE), [ADR-0005](docs/architecture/adr/0005-license.md)). By submitting a contribution, you certify that you have the right to license your contribution under AGPL-3.0-only.

The AGPL §13 "Remote Network Interaction" clause means anyone running Mintarr as a network-accessible service is obligated to offer the source to their users. Contributors should be aware that their code becomes part of this obligation.

## Questions

- General questions: GitHub Discussions (link added at public-repo cutover)
- Bug reports: GitHub Issues with the `bug` template
- Feature requests: GitHub Issues with the `feature` template
- Security issues: see [SECURITY.md](SECURITY.md)

We're glad you're here.
