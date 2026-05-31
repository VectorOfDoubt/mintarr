# Mintarr — Development Setup

> **Type:** Development guide
> **Version:** 1.0 — 2026-05-26
> **Status:** Living document — updated as the dev workflow evolves.
> **Audience:** New contributors setting up a Mintarr development environment for the first time.

---

## Prerequisites

| Requirement | Why | How to install |
|---|---|---|
| Docker Engine 24+ | Tests and the dev container run inside Docker | [docs.docker.com](https://docs.docker.com/engine/install/) |
| Docker Compose v2 | Test orchestration uses `docker compose` | Bundled with Docker Desktop; on Linux usually `docker-compose-plugin` package |
| Git 2.40+ | Repo operations and per-feature branches | Distro package or [git-scm.com](https://git-scm.com/) |
| Python 3.12 (optional) | Not required — tests run in container. Useful for IDE intellisense. | [python.org](https://python.org/) or your distro |
| `pre-commit` (optional) | Local lint/format/typecheck before commit | `pip install pre-commit` |

On Windows: WSL2 is strongly recommended. The Mintarr development workflow assumes a POSIX shell. Running directly under PowerShell works but requires extra path-quoting care.

## Repo layout

```
mintarr/                       ← (post-cutover) root of the public repo
├── README.md
├── LICENSE                    ← AGPL-3.0-only
├── CHANGELOG.md
├── CODE_OF_CONDUCT.md
├── CONTRIBUTING.md
├── SECURITY.md
├── mkdocs.yml                 ← MkDocs Material config
├── docker-compose.yaml        ← production-style compose
├── docker-compose.test.yaml   ← test-suite runner
├── Dockerfile
├── Dockerfile.test
├── pyproject.toml             ← Python project metadata
├── requirements.txt           ← runtime deps
├── requirements-test.txt      ← test-only deps
├── requirements-docs.txt      ← MkDocs deps
├── app/                       ← Mintarr code
│   ├── server.py              ← Flask routes
│   ├── pipeline.py            ← 4-phase common pipeline
│   ├── verification.py        ← V2 policy
│   ├── state_db.py            ← SQLite state index
│   ├── worker.py              ← worker queue
│   ├── dashboard.py           ← dashboard endpoints
│   ├── sensor_registry.py     ← verifier sensor registry
│   ├── backfill_state.py      ← rebuild state_db from sidecars
│   └── adapters/              ← source/verifier/output adapters
│       ├── __init__.py
│       ├── base.py
│       ├── context.py
│       ├── runtime.py
│       ├── tidal.py
│       └── local_folder.py
├── tests/                     ← pytest suite
│   ├── conftest.py
│   └── test_*.py
└── docs/                      ← MkDocs Material source
    ├── MINTARR_DOCUMENTATION_INDEX.md
    ├── strategy/
    ├── architecture/
    ├── specs/
    ├── operations/
    ├── development/
    ├── design/
    └── community/
```

The Mintarr Python code is the entire `app/` directory. The test suite is everything under `tests/`. Documentation lives under `docs/`. There is no separate "library vs application" split — Mintarr is a single Flask application.

## First-time setup

```bash
# Clone the repo
git clone https://github.com/eivindsjursen-lab/mintarr.git
cd mintarr

# Run the test suite to verify your Docker environment works
docker compose -f docker-compose.test.yaml run --rm tests
```

If the test suite passes, your development environment is ready. The `docker compose ... run --rm tests` command builds the test image on first run (slow, ~3-5 minutes) and runs it in seconds afterwards.

There is **no local Python install needed** for the standard dev loop. Tests run in container; the dev container has all dependencies. Local Python is only useful for IDE features (autocomplete, type checking) and is not required.

## The dev loop

```
1. Pick a small, scoped change
2. Edit code under app/ or docs/
3. docker compose -f docker-compose.test.yaml run --rm tests
4. Iterate on (2)–(3) until green
5. Update docs in the same commit as the code change
6. Conventional commit: feat(scope): summary
7. Push to a feature branch and open a PR
```

This is the entire loop. Mintarr's test suite (currently 300+ tests, runs in ~3 seconds when cached) is the load-bearing check. If tests pass and docs are updated, the PR is in good shape.

## Running specific tests

```bash
# Run a single test file
docker compose -f docker-compose.test.yaml run --rm \
    --entrypoint pytest tests /tests/test_pipeline_phases.py -v

# Run a single test
docker compose -f docker-compose.test.yaml run --rm \
    --entrypoint pytest tests /tests/test_pipeline_phases.py::test_execute_source_grab_runs_phases_in_order -v

# Run with short traceback on failure
docker compose -f docker-compose.test.yaml run --rm \
    --entrypoint pytest tests /tests/ -v --tb=short
```

See [TESTING.md](TESTING.md) for the full test patterns.

## Running Mintarr locally for manual testing

```bash
docker compose up -d --build mintarr
docker logs mintarr --tail 50

# Health check
curl http://127.0.0.1:5025/health -H "X-Api-Key: <your-key>"

# Stop
docker compose down mintarr
```

You will need a `docker-compose.yaml` configured with your API key and mounted volumes (`/config`, `/downloads`, `/output`, optionally `/lidarr-config:ro`). A skeleton is in `docker-compose.example.yml`. The full configuration reference is [CONFIGURATION.md](../operations/CONFIGURATION.md).

To test against a real Lidarr, you need a running Lidarr that Mintarr can reach. Most contributors will use the existing Lidarr in their home stack; Mintarr does not ship a test-Lidarr container.

## IDE setup

Mintarr is a standard Python Flask project. Any IDE that understands Python 3.12+ works.

### VS Code (recommended)

1. Install the Python extension
2. Install the `mypy` extension for type checking
3. Open the Mintarr repo root
4. VS Code prompts to use a Python interpreter — point at a local Python 3.12 install for intellisense (does not need Mintarr's dependencies installed; just the interpreter)
5. Run tests via the Testing panel (uses Docker behind the scenes via a `pytest` shim — see `.vscode/settings.json`)

### PyCharm / IntelliJ

Same prerequisites. Configure the Python interpreter as a remote interpreter pointing at the test container, or use a local 3.12 install for intellisense.

### Other editors

Anything with LSP support works. `pylsp` or `pyright` provides language intelligence. Tests run via the command line as described above.

## Code style

Mintarr uses `ruff` for both linting and formatting. Configuration is in `pyproject.toml`. Run locally before committing:

```bash
# Lint
docker compose -f docker-compose.test.yaml run --rm \
    --entrypoint ruff tests check /app /tests

# Format
docker compose -f docker-compose.test.yaml run --rm \
    --entrypoint ruff tests format /app /tests
```

Type checks use `mypy`:

```bash
docker compose -f docker-compose.test.yaml run --rm \
    --entrypoint mypy tests /app
```

CI runs all three on every PR. Local pre-commit hooks save a CI round trip:

```bash
pip install pre-commit
pre-commit install
# Now ruff + mypy run automatically before every commit
```

Style conventions live in [STYLE_GUIDE.md](STYLE_GUIDE.md).

## Documentation development

The documentation site uses MkDocs Material. Local preview:

```bash
docker run --rm -it -p 8000:8000 \
    -v ${PWD}:/docs \
    squidfunk/mkdocs-material:9
```

Then browse `http://127.0.0.1:8000`. The site rebuilds automatically on file changes under `docs/`.

For full MkDocs Material configuration (themes, plugins, navigation), see `mkdocs.yml` at repo root. The decision to use MkDocs Material is recorded in [ADR-0006](../architecture/adr/0006-docs-tooling-mkdocs-material.md).

## Working with adapters

Source adapters live under `app/adapters/`. To add a new source:

1. Read [ADAPTER_PROTOCOL_v1.md](../specs/ADAPTER_PROTOCOL_v1.md) for the locked contract
2. Read [ADAPTER_TUTORIAL.md](ADAPTER_TUTORIAL.md) for a worked example
3. Create `app/adapters/<your_source>.py` implementing `SourceAdapter`
4. Register in `app/server.py` boot (search for `LocalFolderAdapter` to see the pattern)
5. Add a worker executor for the new job type (e.g., `your_source_grab`)
6. Add tests under `tests/test_<your_source>_adapter.py`
7. Add a design doc under `docs/design/F<number>_<source>_ADAPTER.md` if the change is non-trivial

Connector manifests (the operator-facing wrapper) come from [CONNECTOR_MANIFEST_v1.md](../specs/CONNECTOR_MANIFEST_v1.md). The two work together — see [ADR-0003](../architecture/adr/0003-connector-vs-adapter.md).

## Working with the pipeline

The four-phase pipeline (`download_raw` → `normalize_audio` → `verify` → `import_to_lidarr`) is in `app/pipeline.py`. Each phase has explicit invariants documented in [PIPELINE.md](../architecture/PIPELINE.md).

If your change affects the pipeline:

- Phase boundaries are stable — touching them without a design doc is rejected
- Each phase must be testable in isolation
- The pipeline does not know which adapter it is running — it works on `RawDownload` and the common `ctx`

Pipeline-level tests live in `tests/test_pipeline_phases.py`.

## Working with V2 verification

The V2 verification policy is in `app/verification.py`. It is currently source-agnostic; source-aware overrides are F5.2 work and require an ADR.

Score components, weights, and thresholds are constants at module top. Changing them requires:

- A regression test demonstrating the old behaviour
- An updated test demonstrating the new behaviour
- A note in the commit message explaining the operator-visible impact

Per-component score logic is in functions named `_compute_<component>`. Adding a new score component:

1. Add the constant and weight at module top
2. Add the `_compute_<component>` function
3. Wire it into `compute_components()`
4. Add tests covering the component
5. Update [PIPELINE.md](../architecture/PIPELINE.md) "Verify phase" section

## Working with state_db

The state_db (SQLite) is in `app/state_db.py`. Schema lives in the `SCHEMA` constant. Schema changes require:

- An additive migration (new columns via `ALTER TABLE` — see `_ensure_records_source_type_column` for the pattern)
- A `state_db.init()` regression test verifying idempotency
- An entry in [DATA_MODEL.md](../architecture/DATA_MODEL.md)

State_db is a **query index over sidecars**, not the source of truth. If state_db gets corrupted, it can be rebuilt from sidecars with `backfill_state.py`. Bear this in mind: do not put data in state_db that does not also exist in sidecars.

## Working with the dashboard

The dashboard backend is `app/dashboard.py` (a Flask blueprint). The frontend is currently vanilla HTML/CSS/JS rendered server-side. Phase 2 of the roadmap will redesign this with a sidebar layout matching arr-stack conventions.

For minor dashboard changes (e.g., adding a column to the records table):

1. Edit the Flask route in `app/dashboard.py`
2. Update the HTML template (embedded in the route as f-string today)
3. Add a test under `tests/test_dashboard.py`

For major dashboard changes (sidebar redesign, Connector tab, etc.):

- Open an issue describing the change
- Coordinate with the Phase 2 design doc

## Debugging

### Test failures

Most test failures show useful tracebacks. Add `--tb=long` for full tracebacks, `-x` to stop on first failure, `-s` to see stdout during the test:

```bash
docker compose -f docker-compose.test.yaml run --rm \
    --entrypoint pytest tests /tests/test_foo.py -v --tb=long -x -s
```

### Production container debugging

The Mintarr container logs to stdout. View with:

```bash
docker logs mintarr --tail 100 -f
```

State_db can be inspected:

```bash
docker exec mintarr sqlite3 /config/mintarr_state.db "SELECT * FROM records LIMIT 5"
```

Sidecars on disk are JSON files at `/output/<jid>/verification.json` (or `/config/blocked_decisions/<jid>.json` for terminated records). Read with `cat` or `python3 -m json.tool`.

### Worker queue inspection

```bash
docker exec mintarr sqlite3 /config/mintarr_state.db \
    "SELECT id, jid, type, state, result_state FROM jobs ORDER BY id DESC LIMIT 10"
```

If the worker thread crashes, the container logs show the traceback. Worker restarts automatically on the next gunicorn boot.

## What to read next

- [TESTING.md](TESTING.md) — patterns for writing tests
- [STYLE_GUIDE.md](STYLE_GUIDE.md) — code style conventions
- [COMMIT_CONVENTION.md](COMMIT_CONVENTION.md) — conventional-commits format
- [REVIEW_CHECKLIST.md](REVIEW_CHECKLIST.md) — what reviewers check on PRs
- [AGENT_HANDOVER.md](AGENT_HANDOVER.md) — context for AI-assisted contributions

---

> Last updated: 2026-05-26
