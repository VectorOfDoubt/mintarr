# Style Guide

> **Type:** Development / conventions
> **Version:** 1.0 — 2026-05-26
> **Status:** Living document.
> **Audience:** Contributors writing Mintarr code.

---

## 1. Tooling

Mintarr uses these tools enforced in CI:

| Tool | Purpose | Config file |
|---|---|---|
| `ruff` | Linting + formatting | `pyproject.toml` |
| `mypy` | Static type checking | `pyproject.toml` |
| `pytest` | Test runner | `pyproject.toml` |

All three run on every PR via GitHub Actions. Local pre-commit hooks save a CI round-trip:

```bash
pip install pre-commit
pre-commit install
```

## 2. Python version

Mintarr targets Python 3.12. New code may use:

- PEP 604 union types (`int | str` instead of `Union[int, str]`)
- PEP 695 type parameter syntax (the new generic-function declaration form using square brackets after the function name)
- Structural pattern matching (`match` / `case`)
- `from __future__ import annotations` is permitted but not required (3.12 supports `int | None` without it)

Avoid 3.13-only features. CI runs against 3.12.

## 3. Formatting

`ruff format` is the source of truth. Run:

```bash
docker compose -f docker-compose.test.yaml run --rm \
    --entrypoint ruff tests format /app /tests
```

Settings worth knowing:

- Line length 100 (not 88)
- Double quotes (not single)
- Trailing commas in multi-line structures
- Imports sorted by `ruff` (`isort` semantics)

If `ruff format` and your IDE disagree, fix the IDE.

## 4. Naming

### 4.1 Functions and variables

- `lowercase_with_underscores` for functions, variables, modules
- `_leading_underscore` for module-private helpers
- `_double_leading_underscore` only for dunder cases (not for "more private")

### 4.2 Classes

- `CapitalizedWords` for classes, dataclasses, Protocols
- Acronyms keep their case: `HTTPClient`, not `HttpClient`. Common short ones (`Url`, `Json`) lowercase the second word.
- Protocol classes named after what they describe: `SourceAdapter`, `PipelineContext`. Not `ISourceAdapter` — no Hungarian prefixes.

### 4.3 Constants

- `UPPER_SNAKE_CASE` for module-level constants
- Group at top of module, before class/function definitions

### 4.4 Type aliases

```python
type JobId = str
type SourceType = str
```

Use `type` (PEP 695) syntax. Plain `JobId = str` works too but the explicit `type` keyword reads better.

## 5. Imports

### 5.1 Order (handled by ruff)

1. Standard library
2. Third-party
3. First-party (`from app...`, `from adapters...`)
4. Local relative (`from .base import ...`)

### 5.2 Style

```python
# Good
import json
import logging
from pathlib import Path

import requests
from flask import Flask, jsonify

from adapters.base import RawDownload
from .context import PipelineContext

# Avoid star imports
from module import *  # NO

# Avoid relative imports across major package boundaries
from ...server import _trigger_lidarr_import  # NO — this couples adapter to server
```

### 5.3 Lazy imports

For breaking circular imports or deferring expensive imports:

```python
def some_function():
    import server  # lazy — avoids circular import at module load
    server.do_thing()
```

Use sparingly. Lazy imports cost runtime and obscure dependencies. Prefer restructuring the dependency.

## 6. Type hints

### 6.1 Public functions

All public functions (not `_leading_underscore`) must have complete type hints:

```python
def compute_v2_score(
    sensors: list[SensorResult],
    existing_kbps: int,
    new_kbps: int,
) -> int:
    ...
```

### 6.2 Private helpers

`_leading_underscore` helpers should have type hints, but mypy may tolerate gaps. Use judgement; helpers used in many places benefit from explicit types.

### 6.3 `Protocol` for duck typing

```python
class PipelineContext(Protocol):
    jid: str
    raw_dir: Path

    def check_cancelled(self) -> None: ...
```

Use `Protocol` for adapter contracts and dependency-injection handles. Not `ABCMeta` — Mintarr does not need runtime inheritance checks.

### 6.4 `Any` is acceptable in narrow contexts

Sometimes `dict[str, Any]` is the honest type. Don't write `dict[str, str | int | bool | None | dict[str, Any]]` to avoid `Any` — that's worse.

## 7. Docstrings

### 7.1 Public functions and classes

Triple-quoted docstring, one-line summary on the first line, optional blank line + extended description:

```python
def execute_source_grab(job: dict, adapter: SourceAdapter, ctx: PipelineContext) -> None:
    """Run the full pipeline for a source-grab job.

    Mirrors the previous _run_download_job flow but factored into named
    phases. State and Lidarr-call side effects remain in server.py helpers
    (called via ctx.set_progress, _trigger_lidarr_import, etc.).
    """
```

### 7.2 Private helpers

Single-line docstring or none. Don't write 10-line docstrings for 5-line helpers.

### 7.3 Style

- Imperative mood ("Run the pipeline", not "Runs the pipeline")
- No restating the type signature in prose
- Document WHY for non-obvious code, not WHAT

## 8. Comments

### 8.1 When to comment

- Why the code does something non-obvious
- Hidden invariants the reader cannot see
- Workarounds for external bugs
- References to ADRs, design docs, issues

### 8.2 When NOT to comment

- What the code does (the code is the source of truth)
- Restating the function name
- Marking removed code (`# removed for v2`)
- "Used by X" — that rots fast

### 8.3 Style

```python
# Codec gate: ffprobe must confirm audio stream is FLAC or ALAC.
# Otherwise ffmpeg copy/re-encode would silently produce lossy FLAC.
probe = subprocess.run(...)
```

vs.

```python
# Run ffprobe to check the codec
probe = subprocess.run(...)
```

The first explains the *why*; the second is noise.

## 9. Error handling

### 9.1 Be specific

```python
try:
    result = some_call()
except requests.Timeout:
    log.warning("Timeout from %s — retrying", endpoint)
    raise
except requests.ConnectionError as exc:
    raise RuntimeError(f"unreachable: {exc}") from exc
```

Not:

```python
try:
    result = some_call()
except Exception:
    pass  # NO
```

### 9.2 Don't swallow exceptions

`except Exception: pass` is almost always a bug. The exception should at minimum be logged.

### 9.3 Raise with context

```python
raise RuntimeError(f"tidal-dl-ng exited {result.returncode}: {result.stderr[-200:]}")
```

Not:

```python
raise RuntimeError("tidal-dl-ng failed")
```

The first lets the operator debug; the second sends them to read code.

## 10. Logging

### 10.1 Use the module logger

```python
import logging
log = logging.getLogger("mintarr.<module>")

log.info("Started worker (worker_id=%s)", worker_id)
log.warning("[%s] Lidarr unreachable: %s", jid, exc)
log.exception("[%s] Unexpected error", jid)  # includes traceback
```

### 10.2 Use %-formatting for log calls

```python
log.info("Job %s completed in %.2fs", job_id, duration)
```

Not:

```python
log.info(f"Job {job_id} completed in {duration:.2f}s")
```

The %-formatting style lets the logging library skip formatting when the level is suppressed.

### 10.3 Include context

Format `[<jid>]` at the start of log lines for record-scoped events. This is how Mintarr's existing logs are formatted; it makes log greps trivially scoped:

```python
log.info("[%s] Pipeline phase %s started", jid, phase_name)
```

### 10.4 Redact secrets

Use `_redact_request_values` for any log line that includes HTTP request arguments. Secrets in logs are a security issue.

## 11. Database access

### 11.1 Use the helpers

`state_db.py` provides typed helpers. Use them:

```python
# Good
job = state_db.get_job(job_id)

# Avoid
with sqlite3.connect(...) as conn:
    conn.execute("SELECT ... FROM jobs ...")
```

Direct SQL is fine in `state_db.py` itself; everywhere else, route through the helper functions.

### 11.2 Migrations

Schema changes are additive `ALTER TABLE` in `state_db.init()`. See [DATA_MODEL.md §4](../architecture/DATA_MODEL.md#4-migrations).

## 12. Subprocess

### 12.1 Use `ctx.run_subprocess` in adapters

```python
# Good (adapter)
result = ctx.run_subprocess(["tidal-dl-ng", "dl", url], timeout=3600)

# Avoid (adapter)
result = subprocess.run(["tidal-dl-ng", "dl", url], timeout=3600)
```

The context-bound version handles cancel and timeout uniformly.

### 12.2 Use list of strings, never shell

```python
# Good
subprocess.run(["ffprobe", "-v", "error", file_path])

# Never
subprocess.run(f"ffprobe -v error {file_path}", shell=True)
```

`shell=True` is a shell-injection surface. There is no legitimate use of it in Mintarr.

### 12.3 Always pass `timeout`

```python
# Good
subprocess.run([...], timeout=30)

# Bad (can hang forever)
subprocess.run([...])
```

## 13. Flask routes

### 13.1 Always decorate with `@require_apikey`

Unauthenticated endpoints are `/health` and a couple of static assets. Every other endpoint is protected:

```python
@app.route("/dashboard/v1/summary", methods=["GET"])
@require_apikey
def dashboard_summary():
    ...
```

### 13.2 Return JSON via `jsonify`

```python
return jsonify({"status": True, "data": ...})
```

Not bare dict (Flask handles dict-return but `jsonify` is explicit).

### 13.3 Return appropriate status codes

See [HTTP_API_v1.md §11](../specs/HTTP_API_v1.md#11-errors). 400 for client errors, 503 for unavailable, etc.

## 14. Avoid

- Global mutable state outside `_jobs` dict and module-level caches
- `__slots__` for "performance" (not needed at our scale)
- `@property` for trivial attribute access
- Premature optimisation (profile first)
- Threading primitives beyond what's already in `server.py` (the worker queue is the concurrency model)
- New dependencies without an issue first

---

> Last updated: 2026-05-26
