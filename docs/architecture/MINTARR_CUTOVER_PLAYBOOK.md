# Mintarr Cutover Playbook

> **Type:** Architecture / operational runbook
> **Version:** 1.0 — 2026-05-31
> **Status:** Authoritative. Eivind follows this verbatim when creating the public `eivindsjursen-lab/mintarr` repository.
> **Related:** [`CUTOVER_MANIFEST.md`](CUTOVER_MANIFEST.md) (what must be true), this document (how to make it true).
> **Audience:** Eivind, with Claude/Codex assisting from the private `sjursen-mediastack` side.

---

## 0. Preface

This playbook executes the **clean new repo, no inherited history** cutover strategy. Reasons recorded in Eivind's 2026-05-31 decision:

- The private monorepo has months of dogfood logs, incident reports, local IPs, real API keys, Windows/WSL paths, private tracker context
- `git filter-repo` makes it easy to scrub "almost correctly"; almost correctly is unsafe for a public repo
- Mintarr does not need historical commit history to be credible — it needs clean code, clean docs, license, tests, and a clear changelog
- The private monorepo continues as Eivind's lab; public Mintarr is product

The alternative (history-preserving `git filter-repo --path tidalhires/ --path docs/` with text replacement, secret scanners and gated pre-push validation) is explicitly **not chosen**. Anyone considering it later needs to re-open this playbook and the CUTOVER_MANIFEST.

## 1. Preconditions

Before starting the playbook, all of these must be true:

- [x] ADR-0005 locked (AGPL-3.0-only) — done 2026-05-31, commit `8d6828e`
- [x] `mintarr/LICENSE` exists — done 2026-05-31
- [x] HTTP_API_v1 and SIDECAR_FORMAT_v2 validated against fixtures via Codex's `scripts/inventory_flask_routes.py` and `scripts/validate_sidecar_format.py` — done 2026-05-31; specs locked.
- [ ] `git status` on `main` is clean; latest commit is on `origin/main`
- [ ] Test suite passes locally: `docker compose -f tidalhires/docker-compose.test.yaml run --rm tests` returns "X passed" (currently 308)
- [ ] You have publish-level access to the `eivindsjursen-lab` GitHub organisation (or the org Mintarr will live under)
- [ ] You have `gh` CLI installed and authenticated, OR you are comfortable creating repos via the GitHub web UI

If any precondition fails, stop and resolve it before continuing.

## 2. Phase A — Local cutover-tree assembly

**Goal:** build a `/tmp/mintarr-cutover/` directory that exactly mirrors what the public repo will contain. No more, no less.

### A.1 Create scratch directory

```bash
SCRATCH=/tmp/mintarr-cutover-$(date +%Y%m%d-%H%M%S)
mkdir -p "$SCRATCH"
echo "Scratch dir: $SCRATCH"
```

Keep this path in scope for the rest of the playbook.

### A.2 Copy source tree (Python application)

```bash
SRC=/path/to/private/sjursen-mediastack  # adjust to your private repo checkout

# Application code
mkdir -p "$SCRATCH/app"
cp -r "$SRC/tidalhires/app/." "$SCRATCH/app/"

# Tests
mkdir -p "$SCRATCH/tests"
cp -r "$SRC/tidalhires/tests/." "$SCRATCH/tests/"

# Build / runtime files
cp "$SRC/tidalhires/Dockerfile"               "$SCRATCH/Dockerfile"
cp "$SRC/tidalhires/Dockerfile.test"          "$SCRATCH/Dockerfile.test"
cp "$SRC/tidalhires/docker-compose.test.yaml" "$SCRATCH/docker-compose.test.yaml"
cp "$SRC/tidalhires/requirements-test.txt"    "$SCRATCH/requirements-test.txt"

# Keep the public tree source-only. A direct cp from a working tree may carry
# Python cache files created by local test runs.
find "$SCRATCH/app" "$SCRATCH/tests" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$SCRATCH/app" "$SCRATCH/tests" -type d -name ".pytest_cache" -prune -exec rm -rf {} +
```

`requirements.txt` (runtime deps) lives inline in the Dockerfile today. After cutover, extract to `requirements.txt` as a follow-up; for the first public push the Dockerfile-pinned version is sufficient.

### A.3 Copy root files

```bash
# AGPL LICENSE (verbatim from gnu.org, ADR-0005)
cp "$SRC/mintarr/LICENSE"                  "$SCRATCH/LICENSE"

# Operator-facing root docs
cp "$SRC/mintarr/README.md"                "$SCRATCH/README.md"
cp "$SRC/mintarr/CHANGELOG.md"             "$SCRATCH/CHANGELOG.md"
cp "$SRC/mintarr/CODE_OF_CONDUCT.md"       "$SCRATCH/CODE_OF_CONDUCT.md"
cp "$SRC/mintarr/CONTRIBUTING.md"          "$SCRATCH/CONTRIBUTING.md"
cp "$SRC/mintarr/SECURITY.md"              "$SCRATCH/SECURITY.md"
cp "$SRC/mintarr/mkdocs.yml"               "$SCRATCH/mkdocs.yml"

# Example compose (placeholders only, never the real values)
cp "$SRC/mintarr/docker-compose.example.yml" "$SCRATCH/docker-compose.example.yml"

# Commit lint config
cp "$SRC/mintarr/.commitlintrc.yaml"       "$SCRATCH/.commitlintrc.yaml"
cp "$SRC/mintarr/.gitignore"              "$SCRATCH/.gitignore"
```

### A.4 Copy GitHub infrastructure

```bash
mkdir -p "$SCRATCH/.github"
cp -r "$SRC/mintarr/.github/." "$SCRATCH/.github/"
```

### A.5 Copy scripts

```bash
mkdir -p "$SCRATCH/scripts"
cp "$SRC/scripts/check_markdown_links.py"     "$SCRATCH/scripts/check_markdown_links.py"
cp "$SRC/scripts/inventory_flask_routes.py"   "$SCRATCH/scripts/inventory_flask_routes.py"
cp "$SRC/scripts/validate_sidecar_format.py"  "$SCRATCH/scripts/validate_sidecar_format.py"
```

If additional scripts have landed under `scripts/` since this playbook was written, include them too.

### A.6 Copy documentation (selectively)

This is the most error-prone step. **Allowed Mintarr docs only.** No legacy `tidalhires/`-prefixed files, no handover or incident or dogfood docs.

```bash
mkdir -p "$SCRATCH/docs"

# Home page + supplementary stylesheet (referenced by mkdocs.yml)
cp "$SRC/docs/index.md" "$SCRATCH/docs/index.md"
mkdir -p "$SCRATCH/docs/stylesheets"
cp -r "$SRC/docs/stylesheets/." "$SCRATCH/docs/stylesheets/"

# Index
cp "$SRC/docs/MINTARR_DOCUMENTATION_INDEX.md" "$SCRATCH/docs/MINTARR_DOCUMENTATION_INDEX.md"

# Strategy
mkdir -p "$SCRATCH/docs/strategy"
cp -r "$SRC/docs/strategy/." "$SCRATCH/docs/strategy/"

# Architecture (including ADRs and CUTOVER_MANIFEST)
mkdir -p "$SCRATCH/docs/architecture"
cp "$SRC/docs/architecture/OVERVIEW.md"           "$SCRATCH/docs/architecture/OVERVIEW.md"
cp "$SRC/docs/architecture/PIPELINE.md"           "$SCRATCH/docs/architecture/PIPELINE.md"
cp "$SRC/docs/architecture/DATA_MODEL.md"         "$SCRATCH/docs/architecture/DATA_MODEL.md"
cp "$SRC/docs/architecture/SECURITY_MODEL.md"     "$SCRATCH/docs/architecture/SECURITY_MODEL.md"
cp "$SRC/docs/architecture/CUTOVER_MANIFEST.md"   "$SCRATCH/docs/architecture/CUTOVER_MANIFEST.md"
cp "$SRC/docs/architecture/MINTARR_CUTOVER_PLAYBOOK.md" "$SCRATCH/docs/architecture/MINTARR_CUTOVER_PLAYBOOK.md"

# ADRs — all 9 locked + 0005 locked
mkdir -p "$SCRATCH/docs/architecture/adr"
cp -r "$SRC/docs/architecture/adr/." "$SCRATCH/docs/architecture/adr/"

# Specs (all four, with their current draft/locked status preserved)
mkdir -p "$SCRATCH/docs/specs"
cp -r "$SRC/docs/specs/." "$SCRATCH/docs/specs/"

# Operations
mkdir -p "$SCRATCH/docs/operations"
cp -r "$SRC/docs/operations/." "$SCRATCH/docs/operations/"

# Development
mkdir -p "$SCRATCH/docs/development"
cp -r "$SRC/docs/development/." "$SCRATCH/docs/development/"

# Community
mkdir -p "$SCRATCH/docs/community"
cp -r "$SRC/docs/community/." "$SCRATCH/docs/community/"

# Design docs — Mintarr-era F-series + the connector architecture.
# Other legacy TIDALHIRES_-prefixed design docs (F2 worker queue, F3.2
# Newznab routing, F3.5 Soulseek, F3.x source adapters, F3.4 local
# folder, DASHBOARD_*, PIPELINE_REVIEW, QUALITY_STACK_ROADMAP) remain
# in the private monorepo and are NOT copied in v0.1.0. They contain
# legacy TidalHires branding and Norwegian content that needs cleanup
# before public release; migration planned for v0.2.0.
mkdir -p "$SCRATCH/docs/design"
cp "$SRC/docs/design/CONNECTOR_PLUGIN_ARCHITECTURE.md"      "$SCRATCH/docs/design/CONNECTOR_PLUGIN_ARCHITECTURE.md"
cp "$SRC/docs/design/F4.1_STATIC_CONNECTOR_REGISTRY.md"     "$SCRATCH/docs/design/F4.1_STATIC_CONNECTOR_REGISTRY.md"
```

### A.7 What is deliberately NOT copied

These files exist in the private monorepo and **must not** end up in the public assembly:

- `docs/AGENT_HANDOVER.md` (legacy private handover, not the new `docs/development/AGENT_HANDOVER.md`)
- `docs/CODEX_TO_CLAUDE_MINTARR_HARDENING_2026-05-31.md` (private agent coordination)
- `docs/CLAUDE_TO_CODEX_HARDENING_FOLLOWUP_2026-05-31.md` (private agent coordination)
- `docs/MINTARR_PUBLICATION_AUDIT_2026-05-26.md` (private audit; references private paths)
- `docs/INCIDENT_*.md` (host-specific diagnostics)
- `docs/V2_SMOKE_TESTS.md` and similar runbooks tied to Eivind's host
- `docs/TIDALHIRES_*.md` (legacy design docs; superseded by the Mintarr F-series. If valuable, rename them away from the `TIDALHIRES_` prefix in a follow-up commit before copying.)
- `tidalhires/docker-compose.yaml` (production compose with real API keys / mounts)
- `tidalhires/tidal-config/` (real OAuth tokens)
- `tidalhires/config/` (real state_db, sidecars, host paths)
- `lidarr/`, `seerr/`, `maintainerr/`, `uptime-kuma/`, `homepage/`, `slskd/`, etc. (the rest of Eivind's mediastack)
- `Gluetun & qbittorrent/`, `tidal-dl-ng-test/`, `flac-detective/`, `beets/`, `lidatube/`, `soularr/`, etc.

If `cp` ever pulls one of these in, abort and start over from A.1 with a fresh scratch directory.

### A.8 Inventory check

```bash
cd "$SCRATCH"
find . -type f | sort > /tmp/mintarr-cutover-inventory.txt
wc -l /tmp/mintarr-cutover-inventory.txt
```

Expected file count is roughly 60-80 depending on adapter count. Spot-check the list — anything that looks like a private artefact, remove and re-run.

## 3. Phase B — Cutover gates (local validation)

Run every gate against the assembled tree. **All gates must pass before pushing.** Failed gates abort cutover.

### B.1 Secret scan

```bash
cd "$SCRATCH"

# IP addresses (private LAN ranges)
grep -rnE "192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\." . \
    --exclude-dir=.git \
    --exclude=LICENSE \
    | grep -v "example.com\|reverse_proxy\|host.docker.internal\|127.0.0.1\|0.0.0.0"
# Should be empty (or only legitimate examples)

# Real API keys
grep -rn "tidalhires-local-api-key" . --exclude-dir=.git --exclude=MINTARR_CUTOVER_PLAYBOOK.md
# Should be empty

# Personal paths
grep -rnE "/mnt/[a-z]/|C:\\\\Users\\\\|/home/esj006" . --exclude-dir=.git --exclude=MINTARR_CUTOVER_PLAYBOOK.md
# Should be empty

# Personal identifiers
grep -rn "esj006\|C:\\\\Users\\\\Eivind\|/home/esj006" . --exclude-dir=.git --exclude=LICENSE \
    --exclude=MINTARR_CUTOVER_PLAYBOOK.md
# Should be empty. Eivind Sjursen attribution is expected in LICENSE/SECURITY/ADRs.

# Private tracker names
grep -rniE "norbits|nzbgeek" . --exclude-dir=.git --exclude=MINTARR_CUTOVER_PLAYBOOK.md
# Should be empty

# OAuth token paths
find . -name "token.json" -type f -not -path "./.git/*"
# Should be empty. Documentation may mention token.json, but no token file may be present.
```

If any unacceptable hit appears, **stop**, fix it in the private repo first, re-copy that file to the scratch tree, re-run scan.

### B.2 Markdown link check

```bash
cd "$SCRATCH"
python3 scripts/check_markdown_links.py \
    README.md CONTRIBUTING.md SECURITY.md CODE_OF_CONDUCT.md CHANGELOG.md \
    docs/
```

Exit 0 means all relative links resolve. Any non-zero exit lists broken links — fix in private repo first.

### B.3 MkDocs strict build

```bash
cd "$SCRATCH"

# Ensure MkDocs Material is installed locally:
# pip install mkdocs-material mkdocs-mermaid2-plugin

mkdocs build --strict
```

`--strict` fails on any unresolved reference, broken link, or warning. The site builds to `site/`. Inspect `site/index.html` in a browser if you want.

### B.4 Test suite

```bash
cd "$SCRATCH"
docker compose -f docker-compose.test.yaml run --rm tests
```

Currently 308 tests should pass. Lower number → some test was tied to a removed file.

### B.5 Route inventory vs HTTP API spec

```bash
cd "$SCRATCH"
python3 scripts/inventory_flask_routes.py > /tmp/routes.json
# Compare against docs/specs/HTTP_API_v1.md manually for now;
# a comparison script is a follow-up task.
```

### B.6 Sidecar format validation

If you have sample sidecars to test against (export from your private deployment, with sensitive fields redacted):

```bash
cd "$SCRATCH"
python3 scripts/validate_sidecar_format.py /path/to/sample-sidecars/
```

If you do not have sample sidecars, this gate is deferred to first post-cutover release.

### B.7 License compatibility spot-check

```bash
cd "$SCRATCH"
grep -rn "License" requirements*.txt Dockerfile* 2>/dev/null | head
# Manual review: each dependency must be AGPL-compatible per ADR-0005
```

## 4. Phase C — Create the public repository

Only after **every gate in Phase B passes**.

### C.1 Verify the org / username and repo name

```bash
gh api user
# Confirm logged in as expected user
```

Repo name: `mintarr`. Owner: `eivindsjursen-lab` (or a new dedicated `mintarr` org if Eivind has decided to delegate later).

### C.2 Create the repo via `gh`

```bash
gh repo create eivindsjursen-lab/mintarr \
    --public \
    --description "Quality control and import orchestration layer for Lidarr" \
    --license AGPL-3.0 \
    --confirm
```

The `--license AGPL-3.0` flag asks GitHub to add a LICENSE; we will overwrite it with our verbatim copy in C.4 to ensure exact bit-for-bit match.

If `gh` isn't available, create via the web UI:

- Owner: `eivindsjursen-lab`
- Name: `mintarr`
- Visibility: Public
- Add README: **No** (we have our own)
- Add `.gitignore`: **No**
- License: GNU Affero General Public License v3.0

### C.3 Clone the empty repo locally

```bash
cd /tmp
gh repo clone eivindsjursen-lab/mintarr mintarr-public
cd mintarr-public
```

### C.4 Replace contents with cutover scratch

```bash
# Remove anything GitHub auto-added
rm -rf README.md LICENSE .gitignore

# Copy the entire scratch tree in
cp -r "$SCRATCH/." .

# Verify the file layout
find . -type f -not -path './.git/*' | sort | head -30
```

### C.5 Initial commit

```bash
git add -A
git status

# Should show all your assembled files as new

git commit -m "$(cat <<'EOF'
chore: initial public Mintarr release foundation

Mintarr is a quality control and import orchestration layer for Lidarr.
It verifies and orchestrates music imports across multiple sources
(TIDAL, LocalFolder, future Soulseek / SAB / qBit / CD-rip) before
they reach Lidarr.

This is the first public commit. The project was previously developed
in private under the name "tidalhires". The rename to Mintarr and the
public release foundation are documented in:

  docs/architecture/adr/0001-rename-from-tidalhires.md
  docs/architecture/adr/0007-no-lidarr-fork.md
  docs/architecture/adr/0008-strategic-positioning.md

This commit imports the validated public assembly. Historical
development commits are preserved in the private upstream lab repo
and are not part of the public history by design.

License: AGPL-3.0-only (ADR-0005)

Foundation includes:
  - Python source code under app/
  - Test suite under tests/ (308 passing)
  - Complete documentation set under docs/ (~30 documents)
  - GitHub Actions workflows under .github/workflows/
  - Issue and PR templates under .github/
  - MkDocs Material site config (mkdocs.yml)
  - Container build (Dockerfile)
  - Operator-facing example compose (docker-compose.example.yml)
  - Cutover tooling under scripts/
EOF
)"
```

### C.6 Push

```bash
git push origin main
```

Watch for the push to succeed. If any large file fails (>100 MB GitHub limit), abort and investigate — Mintarr should not have any file that big.

## 5. Phase D — Post-push verification

### D.1 GitHub Actions

Open the Actions tab on the new repo and watch:

- **CI workflow** runs against the first commit. Expected: green.
- **Docs workflow** builds MkDocs Material site. Expected: green.
- **Build workflow** does **not** fire on first commit (it triggers on tags). Will fire later.

If CI fails on the first run, diagnose locally before attempting a fix-up push. Common first-run failures:

- Missing `requirements.txt` (Dockerfile inlines pip installs; CI's lint step may want a separate file)
- `mypy` config drift between local and CI environments
- `commitlint` rejecting the first commit (allow-list initial commit if needed)

### D.2 GitHub Pages

The docs workflow should deploy to `https://eivindsjursen-lab.github.io/mintarr/` on first success. Configure Pages source:

1. Repo Settings → Pages
2. Source: GitHub Actions
3. Save

Wait ~2 minutes after the docs workflow succeeds, then load the URL in a browser. Verify navigation, search, and rendering.

### D.3 Container image build (manual trigger for now)

```bash
gh workflow run build.yml --repo eivindsjursen-lab/mintarr
```

Watch for it to publish `ghcr.io/eivindsjursen-lab/mintarr:main`. First successful build is the precondition for tagged releases.

### D.4 Smoke-test the container

```bash
docker pull ghcr.io/eivindsjursen-lab/mintarr:main
docker run --rm -e MINTARR_API_KEY=test-only-not-real-keys-1234567890 \
    -p 5025:8000 ghcr.io/eivindsjursen-lab/mintarr:main \
    --version 2>&1 | head
```

The container should at minimum boot to the Flask startup banner.

## 6. Phase E — Tag v0.1.0

After Phase D succeeds and any first-run hiccups are fixed:

```bash
cd /tmp/mintarr-public

git tag -a v0.1.0 -m "$(cat <<'EOF'
Mintarr v0.1.0 — first public release

This tag marks the first publishable Mintarr release. The codebase has
been running in private production for several months; this is the
first version that is publicly available, documented, and licensed.

License: AGPL-3.0-only

Foundation documentation is complete (~30 documents). The connector
architecture is designed; F4.1 static connector registry will land in
v0.2.0. Soulseek source adapter (F3.5a) is designed and queued for
v0.3.0.

See CHANGELOG.md for details.
EOF
)"

git push origin v0.1.0
```

Tagging triggers the `build.yml` workflow to publish `ghcr.io/eivindsjursen-lab/mintarr:0.1.0`, `:0.1`, `:0`, and `:latest`.

## 7. Phase F — Legacy repo handling

The private `eivindsjursen-lab/sjursen-mediastack` monorepo continues to exist as Eivind's lab. Two specific updates:

### F.1 Stub README pointing at the public repo

Create or update `tidalhires/README.md` (private repo) to say:

```markdown
# tidalhires (renamed to Mintarr)

This subdirectory previously hosted the `tidalhires` project, which has been
renamed to **Mintarr** and is now developed at:

  https://github.com/eivindsjursen-lab/mintarr

The Mintarr public repo is the canonical source going forward. This directory
remains in the private monorepo as the upstream lab where dogfood, incident
notes, and integration with the rest of Eivind's media stack happen.

For installation, configuration, and contribution information, see the
public Mintarr repo.
```

Commit, push.

### F.2 Lock down the legacy private docs

Mark the private `docs/MINTARR_*`-prefixed files as "private working drafts that supersede the public Mintarr docs only for Eivind's lab purposes". They should not be considered authoritative once the public repo exists.

A short header on `docs/MINTARR_DOCUMENTATION_INDEX.md` in the private repo:

```markdown
> **Private-lab notice (2026-XX-XX after cutover):** The public Mintarr documentation
> lives at https://eivindsjursen-lab.github.io/mintarr/. This file in the private
> monorepo is a working draft used during lab work and may diverge from public docs.
> When in doubt, the public site is canonical.
```

## 8. Rollback

If anything goes wrong before Phase E (tag push), recovery is straightforward:

1. **Delete the public repo:** Settings → Danger Zone → Delete this repository
2. **Audit what leaked:** if private content reached the public repo even briefly, treat it as exposed. Rotate any keys that were visible. The git history of a deleted repo is unrecoverable to outsiders after deletion, but cached views (Google, archive.org) may persist for some time.
3. **Restart from Phase A:** fix the root cause that caused the leak, re-assemble, re-run gates, re-push.

After Phase E (tag push), rollback is much harder. The tagged container image is published; container caches may persist on operator hosts. Treat tag pushes as one-way doors.

## 9. Post-cutover follow-up

These can wait until after the public repo exists:

- F4.1 connector registry implementation
- CONNECTOR_MANIFEST_v1 lock once F4.1 lands
- Operator-facing announcement (r/lidarr, r/selfhosted, etc.) if Eivind chooses to surface
- Custom Format presets for Lidarr operators (Mintarr-tidal, Mintarr-local)
- Documentation site custom domain (currently `<org>.github.io/mintarr`)

## 10. Roles during cutover

| Phase | Eivind | Claude | Codex |
|---|---|---|---|
| Preconditions | Verifies items 1-6 | Reviews status | Reviews status |
| Phase A — Assembly | Operates the commands | Available for questions | Available for questions |
| Phase B — Gates | Runs all gates | Diagnoses failures | Diagnoses failures |
| Phase C — Repo create + initial commit | Operates `gh` / web UI | Drafts commit message refinements | Drafts commit message refinements |
| Phase D — Post-push verification | Watches CI / Pages | On standby | On standby |
| Phase E — Tag v0.1.0 | Tags and pushes | Writes release notes | Writes release notes |
| Phase F — Legacy handling | Updates private README | Drafts the stub content | Drafts the stub content |
| Rollback (if needed) | Operates delete | Diagnoses root cause | Diagnoses root cause |

Eivind owns every command that touches the public repo. Claude and Codex can prepare scripts, draft commit messages, and diagnose failures — but the push button is operator-only.

---

> Last updated: 2026-05-31
