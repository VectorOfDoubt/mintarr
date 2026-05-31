# Mintarr Cutover Manifest

> **Type:** Architecture / release checklist
> **Version:** 0.1 — 2026-05-31
> **Status:** Required before creating the public `eivindsjursen-lab/mintarr` repository.
> **Audience:** Maintainers preparing the private `tidalhires` code for public Mintarr publication.

---

## 1. Purpose

The private monorepo is not the public Mintarr repository shape. Do not publish it with history intact and do not copy the `docs/` tree wholesale. This manifest defines the minimum assembly and validation steps needed before Phase 0 cutover.

## 2. Source Assembly

Target public layout:

```text
mintarr/
  app/                 # from tidalhires/app
  tests/               # from tidalhires/tests
  docs/                # selected Mintarr docs only
  .github/             # from mintarr/.github after validation
  Dockerfile
  docker-compose.example.yml
  requirements*.txt
  README.md
  LICENSE              # AGPL-3.0-only (locked 2026-05-31, ADR-0005)
  CHANGELOG.md
  CONTRIBUTING.md
  SECURITY.md
  mkdocs.yml
```

Required rename/compatibility checks:

- `tidalhires/app` imports still work until the Python package rename is done.
- `MINTARR_API_KEY` is accepted; legacy `TIDALHIRES_API_KEY` remains an alias for migration.
- `MINTARR_STATE_DB` is accepted; legacy `TIDALHIRES_STATE_DB` remains an alias for migration.
- `MINTARR_RESCUE_RESCAN_ENABLED` and `MINTARR_DISABLE_WORKER` are accepted; legacy names remain aliases.
- Dashboard-visible names and titles are updated intentionally, not by ad-hoc string replacement.

## 3. Documents Allowed In Public Repo

Allowed:

- `docs/strategy/**`
- `docs/architecture/{OVERVIEW,PIPELINE,DATA_MODEL,SECURITY_MODEL,CUTOVER_MANIFEST}.md`
- `docs/architecture/adr/0001-*.md` through the latest locked ADR
- `docs/specs/**` after contract validation
- `docs/operations/**` after runtime-vs-target wording is checked
- `docs/development/**` after tool references match the assembled repo
- `docs/community/**`
- Feature design docs that have been scrubbed and renamed away from `TIDALHIRES_` if public-facing

Not allowed without rewrite:

- Agent handovers from private dogfood sessions
- Incident reports with host-specific data
- Private publication audits with local paths
- Tracker-specific policy and credentials context
- Any document containing private LAN addresses, usernames, host drive letters, or real API keys

## 4. Contract Validation Gates

These must pass before any spec is marked locked:

1. `SIDECAR_FORMAT_v2.md` is validated against at least three real sidecars:
   - one imported record
   - one BLOCK/SKIPPED record
   - one REVIEW_REQUIRED or discarded record
2. `HTTP_API_v1.md` route list is generated from Flask route registration and compared to the spec.
3. `CONNECTOR_MANIFEST_v1.md` remains provisional until F4.1 implements the runtime registry and `/dashboard/v1/connectors`.
4. `LIDARR_INTEGRATION.md` is tested against the supported Lidarr version documented in that file.

## 5. Link And Docs Build Gates

Run these from the assembled public-repo root:

```bash
python -m pytest
mkdocs build --strict
python scripts/check_markdown_links.py docs README.md CONTRIBUTING.md SECURITY.md
python scripts/inventory_flask_routes.py > route-inventory.json
python scripts/validate_sidecar_format.py fixtures/sidecars
```

Broken links in the private monorepo are acceptable only when they point across the future cutover boundary; broken links in the assembled public repo are blockers.

## 6. Security Scrub Gates

Before first public push:

- No hardcoded private LAN IPs.
- No real API keys, OAuth tokens, passwords, cookie values, tracker names that imply private access, or host drive letters.
- No unredacted `apikey`, `token`, `password`, or `X-Api-Key` values in examples or logs.
- Catch-all/unknown HTTP routes require auth and redact logged request values.
- ~~License decision in ADR-0005 is locked and `LICENSE` exists.~~ Done 2026-05-31: AGPL-3.0-only.
- Dependency license table exists and has no unresolved blockers.

## 7. GitHub Infrastructure Gates

The workflows under `.github/workflows/` must be run against the assembled repo before publication:

- Paths in workflows match the public root (`app/`, `tests/`, `Dockerfile`, `requirements*.txt`).
- Missing tooling files are created or workflow steps are removed.
- CI does not reference private Docker Compose files or host paths.
- Issue templates and PR template link only to public docs.

## 8. Go / No-Go

Cutover is **no-go** if any of these are unresolved:

- ~~ADR-0005 license choice pending.~~ Resolved 2026-05-31: AGPL-3.0-only.
- Publication audit P0 findings open.
- HTTP or sidecar specs disagree with runtime.
- MkDocs strict build fails.
- CI workflows fail on a clean clone.
- Any public file contains private topology or secrets.

Cutover is **go** only when the assembled repo passes tests, docs build, link-check, security scrub and license validation in the target public layout.

---

> Last updated: 2026-05-31
