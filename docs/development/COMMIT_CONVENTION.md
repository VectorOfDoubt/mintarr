# Commit Convention

> **Type:** Development / process
> **Version:** 1.0 — 2026-05-26
> **Status:** Living document.
> **Audience:** Contributors writing commits. Maintainers reviewing PRs.

---

## 1. Conventional Commits

Mintarr uses [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/). The format:

```
<type>(<scope>): <subject>

<body>

<footer>
```

Example:

```
feat(adapters): add LocalFolder source adapter

Implements F3.4 design v0.3. Copy-semantics with symlink and
path-traversal guards. Source files left untouched after import.

13 new tests covering is_enabled, search returns empty, normalize
candidate path validation, download_raw copy + traversal rejection,
endpoint enqueue + dedupe + missing-adapter handling.

Closes #42
```

## 2. Types

| Type | Use for |
|---|---|
| `feat` | New feature visible to operators or contributors |
| `fix` | Bug fix |
| `docs` | Documentation only |
| `test` | Test-only changes (new tests, test refactoring) |
| `refactor` | Code restructuring without behaviour change |
| `chore` | Build, CI, dependency updates, repo housekeeping |
| `perf` | Performance improvement (with measurement) |
| `style` | Formatting / whitespace (rarely needed — ruff handles this) |
| `revert` | Reverting a previous commit |

If a commit fits multiple types, pick the dominant one. A feature with tests is `feat:`, not `feat:` and `test:` separately.

## 3. Scope

The scope is a hint at which subsystem changed. Optional but encouraged:

- `feat(adapters): ...` — anything under `app/adapters/`
- `feat(pipeline): ...` — pipeline code
- `feat(dashboard): ...` — dashboard frontend or backend
- `fix(worker): ...` — worker queue
- `fix(v2): ...` — V2 verification policy
- `feat(connectors): ...` — connector registry / manifests
- `docs(strategy): ...` — strategy docs
- `docs(architecture): ...` — architecture docs
- `chore(deps): ...` — dependency updates
- `chore(ci): ...` — GitHub Actions

For multi-area changes, omit the scope rather than picking the wrong one.

## 4. Subject line

- Imperative mood ("add", not "added" or "adds")
- Lowercase first letter
- No period at the end
- Max 72 characters (GitHub truncates longer lines on the commit-list view)
- Specific enough to identify the change without reading the body

Bad:

```
feat: stuff
fix: update
docs: edited the readme
```

Good:

```
feat(adapters): add LocalFolder source adapter
fix(v2): plug rescue-success sidecar leak (Codex follow-up)
docs(strategy): lock ADR-0007 no Lidarr fork + ADR-0008 positioning
```

## 5. Body

Use the body for:

- What the change does (one paragraph)
- Why the change is needed (one paragraph) — reference design doc or issue
- What is being deferred (one paragraph, if applicable)
- Notable trade-offs

Wrap at 72 characters per line. Blank line between paragraphs.

For trivial changes, skip the body. A typo fix doesn't need three paragraphs.

## 6. Footer

The footer carries metadata that tooling reads:

```
BREAKING CHANGE: <description>
Closes #<issue>
Refs #<issue>
Co-Authored-By: <name> <email>
```

`BREAKING CHANGE` is significant — it implies the next SemVer bump must be major. See §9.

## 7. SemVer impact

Conventional commit types map to SemVer bumps:

| Commit type | SemVer impact |
|---|---|
| `feat:` | Minor (`1.0.0` → `1.1.0`) |
| `fix:` | Patch (`1.0.0` → `1.0.1`) |
| `docs:`, `test:`, `chore:`, `refactor:`, `style:`, `perf:` | None (no release) |
| Any commit with `BREAKING CHANGE:` in footer | Major (`1.0.0` → `2.0.0`) |
| Any commit with `!` after type (e.g., `feat!:` or `fix(scope)!:`) | Major |

Release automation (Phase 0+ infra work) reads commit messages to determine the next version.

## 8. Examples

### 8.1 Simple feature

```
feat(adapters): support source_id with spaces in download URLs

Base64url-encode source_id in /download/<source>/<id>.nzb so
LocalFolder paths like 'Nightwish/Nemo (CD Single)' roundtrip
without breaking Flask routing.
```

### 8.2 Bug fix with regression test

```
fix(pipeline): clean up download_exit_code field in success path

The download_exit_code attribute was dropped from _jobs during the
F3.1 refactor. Audit consumers expected it to be present after a
clean run.

Added regression test ensuring the field is set to 0 in
test_pipeline_phases.py.

Refs #97
```

### 8.3 Documentation only

```
docs(architecture): add ADR-0007 no Lidarr fork

Records the decision to remain a Lidarr companion, with re-evaluation
triggers and rationale. Closes a recurring scope discussion.
```

### 8.4 Breaking change

```
feat(adapters)!: rename SourceAdapter.fetch_raw to download_raw

The method was confusingly named — fetch implies HTTP. Renamed to
download_raw to align with the four pipeline phases.

BREAKING CHANGE: SourceAdapter implementations must rename their
fetch_raw method to download_raw. Community adapters need updating.
A migration script will be published with v2.0.0.
```

### 8.5 Chore

```
chore(ci): pin ruff to 0.4.5 for reproducible builds

Floating to latest caused a formatting change to be flagged as new
when it was actually ruff being more strict in 0.5.x.
```

### 8.6 Co-authored

```
feat(connectors): add static connector registry (F4.1)

Implements docs/design/F4.1_STATIC_CONNECTOR_REGISTRY.md. ConnectorManifest
dataclass, registry, GET /dashboard/v1/connectors with runtime status.

7 connectors registered initially: tidal, local_folder, ffprobe, flac_t,
flac_detective, lidarr_manual_import, lidarr_rescue_rescan.

12 tests covering manifest validation, registry dedupe, required-connector
enforcement, individual connector health probes.

Co-Authored-By: Codex <noreply@anthropic.com>
```

## 9. Squashing

When merging a PR with multiple WIP commits, squash before merge. The final commit message uses the conventional-commits format. Force-push the squashed branch and merge as a single commit.

If your branch has multiple logical changes, split into multiple PRs rather than squashing them into one commit.

## 10. Co-authors

Mintarr development frequently uses AI assistance. When a commit was substantially produced via AI:

```
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
Co-Authored-By: Codex <noreply@anthropic.com>
```

This attribution lets future readers understand the development process and is GitHub-rendered as a co-author badge.

## 11. What not to do

- `fix: a thing`
- `update`
- `wip`
- `more changes`
- `merge branch 'foo'`
- `Revert "Revert "Revert..."`

These leave no record of what was done or why. They make `git log` useless and `git bisect` painful.

## 12. Tools

- `pre-commit` hooks include a commit-msg hook that validates conventional-commits format
- Local `commitizen` or `git-cliff` help compose conformant messages
- CI checks PR commits against the convention

If your commit message is rejected, fix it with `git commit --amend` (single commit) or `git rebase -i HEAD~<n>` (multiple).

---

> Last updated: 2026-05-26
