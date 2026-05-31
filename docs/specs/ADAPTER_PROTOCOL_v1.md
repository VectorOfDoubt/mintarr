# Adapter Protocol — v1

> **Type:** Spec / contract
> **Version:** 1.0.1 — runtime-validated 2026-05-31
> **Status:** Locked for the current runtime contract after validation against `tidalhires/app/adapters/base.py` and `tidalhires/app/adapters/context.py`. Import paths remain pre-cutover aliases until the public repo assembly renames `tidalhires/app` to `app`.
> **Audience:** Anyone writing a community-contributed `SourceAdapter`. Mintarr maintainers when reviewing adapter-related PRs.

---

## 1. Status and stability promise

This specification is the **stable contract** community-contributed source adapters can rely on after Phase 0 cutover. Current private builds use the same protocol objects but may expose them through the legacy top-level `adapters` import path.

- Any code conforming to this spec will continue to work across all Mintarr `1.x.y` releases
- Breaking changes — to method signatures, dataclass field names, dataclass field types, Protocol method behaviour — require a new spec file (`ADAPTER_PROTOCOL_v2.md`) and a successor ADR
- Editorial changes (typos, clarifications, additional examples) are applied to this file without version bump

If you find a behavioural ambiguity in this spec, file an issue. We treat ambiguity as a documentation bug.

## 2. What this specifies

This document specifies the `SourceAdapter` Protocol. Two related contracts have their own versioned specs:

- [`CONNECTOR_MANIFEST_v1.md`](CONNECTOR_MANIFEST_v1.md) — operator-facing wrapper
- [`SIDECAR_FORMAT_v2.md`](SIDECAR_FORMAT_v2.md) — `verification.json` schema

`VerifierAdapter` and `OutputAdapter` Protocols are tracked separately when they land (currently verifiers are processes invoked from the pipeline, not adapter-shaped).

## 3. Imports

A v1 adapter imports only from the adapter protocol modules. In the target public repo these imports are:

```python
from app.adapters.base import (
    SourceAdapter,
    ReleaseCandidate,
    RawDownload,
)
from app.adapters.context import PipelineContext
```

Pre-cutover private builds expose the same modules as top-level `adapters.base` and `adapters.context` because `tidalhires/app` is mounted as the Python path in tests. The protocol objects and signatures are identical; only the package prefix changes during cutover.

The contract permits these protocol imports and only these. **Adapters must not import from `app.server`, `app.pipeline`, `app.state_db`, or their legacy pre-cutover equivalents (`server`, `pipeline`, `state_db`), or any module that does.** Violating this is a spec violation that PR review rejects.

## 4. The `SourceAdapter` Protocol

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class SourceAdapter(Protocol):
    """Source adapter contract. Implementations register with the
    Mintarr connector registry at boot.

    Adapters own only `download_raw()`. Normalization, verification, and
    import are handled by the common pipeline; adapters do not see them.
    """

    name: str
    """Unique adapter identifier. Used as:
       - prefix in /sabnzbd/api?mode=addurl&name=<name>:<source_id>
       - prefix in /download/<name>/<base64url_source_id>.nzb
       - source_type column value in state_db.jobs and state_db.records
       - source_type field in verification.json sidecars
       Must match the regex ^[a-z0-9_]+$ (lower-case ASCII, digits, underscore)."""

    source_type: str
    """Typically equal to `name`. Persisted in jobs, records, and sidecars.
    Separate from `name` to allow future scenarios where one adapter class
    serves multiple source_type values; v1 adapters set source_type == name."""

    def is_enabled(self) -> bool:
        """Returns True if this adapter can currently service grabs.

        Returning False means the adapter is dormant — its source-prefix
        addurl requests return HTTP 503 'source not enabled', its search()
        is not called by newznab(), and its connector reports
        installed=True but enabled=False in the dashboard.

        Cheap, synchronous, idempotent. Called on every boot, every
        connector health check, and every addurl request.

        Common is_enabled() conditions:
          - Required environment variable is set
          - Required token file exists
          - Required mounted directory exists and is readable
          - Network endpoint for the source is reachable (cheap probe)

        Returning False is the recommended initial state for any adapter
        whose configuration is incomplete. Mintarr treats it as 'operator
        has not finished setting this up' rather than 'broken'."""
        ...

    def search(
        self,
        query: str,
        artist: str = "",
        album: str = "",
        year: int | None = None,
    ) -> list[ReleaseCandidate]:
        """Search for release candidates matching the query.

        Called by the newznab() handler when Lidarr searches for releases.
        Returns a list of ReleaseCandidate, possibly empty, never None.

        Should be fast (< 5 seconds end-to-end). Lidarr's indexer timeout
        is 10 seconds by default; exceeding it surfaces as
        "indexer unreachable" in Lidarr's UI.

        Empty query handling: when query, artist, and album are all empty,
        the adapter SHOULD return [] rather than scanning everything.
        This is Lidarr's indexer-health-check pattern and Mintarr does
        not surface arbitrary source contents in that case.

        Exceptions: raise on actual failure (network down, auth expired).
        The newznab() handler catches exceptions per-adapter for failure
        isolation; one adapter failing does not block the others."""
        ...

    def download_raw(
        self,
        candidate_id: str,
        ctx: PipelineContext,
    ) -> RawDownload:
        """Fetch the candidate identified by `candidate_id` into ctx.raw_dir.

        `candidate_id` is the adapter's `source_id` (the second part of
        a guid `f"{source_type}:{source_id}"`). For TIDAL it is the album
        ID as a string; for LocalFolder it is a normalised POSIX relative
        path; for future adapters, whatever form the adapter chooses.

        Must:
          - Place fetched files under ctx.raw_dir
          - Call ctx.check_cancelled() at natural checkpoints in long loops
          - Use ctx.run_subprocess(...) for any subprocess work (so cancel
            and timeout are handled uniformly)
          - Emit progress via ctx.set_progress(stage, percent, message, ...)
          - Validate path-traversal-style inputs at copy time (defence in
            depth — Mintarr also validates at the addurl/ingest endpoints)

        Must not:
          - Modify the source (LocalFolder must copy, not move)
          - Write outside ctx.raw_dir / ctx.output_dir
          - Mount or read the Docker socket
          - Bind a port or open a listening socket (the runtime expects
            adapters to be invoked synchronously from the worker)

        Returns RawDownload. The pipeline takes over from there
        (normalize_audio → verify → import_to_lidarr).

        Raises:
          - worker.JobCancelled — when ctx.check_cancelled() trips. The
            pipeline propagates this and the worker marks the job
            cancelled. The adapter SHOULD NOT catch JobCancelled.
          - RuntimeError — for permanent failures (auth, 404, unknown
            error). The worker treats these as permanent unless they
            match the retry allow-list.
          - TransientSourceError (future, planned) — for explicit
            transient failures the retry policy should re-queue. Until
            this type exists, raise RuntimeError with a message matching
            the allow-list patterns in worker._is_transient_failure."""
        ...

    def cleanup(self, jid: str, ctx: PipelineContext) -> None:
        """Adapter-specific teardown after download_raw raises or after
        the pipeline finishes its other phases.

        Default for most adapters is no-op (`return None`). The pipeline
        already cleans ctx.raw_dir and ctx.output_dir on cancel/failure.

        Called even if download_raw succeeded — cleanup is unconditional
        from the adapter's perspective. Implementations should be
        idempotent and tolerate being called when there is nothing to
        clean.

        Examples of adapter-specific cleanup:
          - Releasing an external lock or transfer slot
          - Cancelling an in-flight HTTP transfer on a peer-to-peer source
          - Decrementing an adapter-internal in-flight counter
        """
        ...
```

## 5. Dataclasses

### 5.1 `ReleaseCandidate`

```python
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ReleaseCandidate:
    """A single result from adapter.search(), surfaced to Lidarr via
    the Newznab response.

    Title contract: must be parseable by Lidarr's quality detector.
    Adding a source tag like '[TIDAL]' or '[Local]' is permitted as long
    as the quality detector still extracts FLAC/24bit/MP3-320/etc
    correctly. Verifying this against Lidarr is the adapter author's
    responsibility before declaring the adapter ready.
    """

    source_type: str               # Globally unique with source_id
    source_id: str                 # Adapter-internal id; survives base64url roundtrip
    title: str                     # Lidarr-parseable release title
    artist: str
    album: str
    year: int | None
    quality_tag: str               # 'FLAC 24bit', 'FLAC', 'MP3 320', etc.
    size_bytes: int                # Estimated file size; not authoritative
    download_url: str              # Passed verbatim through addurl; adapter chooses format
    priority: int = 50             # 0-100, higher wins ties; adapter chooses
    extra: dict = field(default_factory=dict)  # Adapter-specific metadata
```

Field constraints:

| Field | Constraint |
|---|---|
| `source_type` | Matches `^[a-z0-9_]+$`; same as the adapter's `source_type` attribute |
| `source_id` | UTF-8 string; may contain spaces, slashes, parentheses; survives base64url roundtrip |
| `title` | Non-empty; tested against Lidarr's parser before adapter ships |
| `artist`, `album` | Non-empty for normal use; empty allowed only for adapters that legitimately have no artist (rare) |
| `year` | Integer 1900-2100 or `None` |
| `quality_tag` | Free-form; common values listed above |
| `size_bytes` | Non-negative integer |
| `download_url` | Adapter-specific; for sources reached via Mintarr's `/download/` endpoint, format is `f"{source_type}:{source_id}"` |
| `priority` | Integer sort key; higher wins ties. Convention: 50 = strong source (TIDAL), 30 = medium (LocalFolder), -10 to 0 = weak/untrusted source. Negative values are allowed. |
| `extra` | Free-form dict; not interpreted by the core |

The `guid` derived from this is `f"{source_type}:{source_id}"` and is the globally unique identifier surfaced in Newznab XML.

### 5.2 `RawDownload`

```python
@dataclass(frozen=True)
class RawDownload:
    """Result of adapter.download_raw(). Marks the boundary between
    'source has produced files' and 'common pipeline takes over'."""

    files_dir: Path                # Where files landed; typically ctx.raw_dir
    file_count: int                # Total files placed (audio + non-audio)
    total_bytes: int               # Total bytes across all files
```

Field constraints:

| Field | Constraint |
|---|---|
| `files_dir` | Existing directory; typically equal to `ctx.raw_dir` |
| `file_count` | At least 1; raise rather than return zero |
| `total_bytes` | Non-negative integer |

## 6. `PipelineContext` (the runtime handle)

`PipelineContext` is what the pipeline passes to `download_raw()` and `cleanup()`. Adapters do not construct it; they receive it.

```python
from typing import Protocol
from pathlib import Path
import subprocess


class PipelineContext(Protocol):
    """The dependency-injected runtime handle for an adapter call.

    Adapters depend only on this Protocol — they never import from
    server.py. The runtime injects a concrete implementation
    (`RuntimePipelineContext`) that bridges back to server.py's worker
    queue, cancel state, progress tracking, and logging.
    """

    jid: str
    """The job ID for the current download. 12-char hex."""

    worker_job_id: int | None
    """The state_db.jobs row ID for this worker invocation. None when
    the call is from the sync addurl-fallback thread."""

    raw_dir: Path
    """Where the adapter should write fetched files. Already created
    by the runtime before download_raw() is called."""

    output_dir: Path
    """Where the pipeline will move files after normalize_audio.
    Adapters do not write here directly."""

    def check_cancelled(self) -> None:
        """Raises worker.JobCancelled if the operator has requested
        cancel. Adapters must call this at natural checkpoints in long
        loops (per-file, per-chunk in large transfers, between subprocess
        invocations).

        Sub-second response to cancel is not required, but cancel
        ignored for more than 30 seconds is a poor experience."""
        ...

    def run_subprocess(
        self,
        argv: list[str],
        *,
        timeout: int,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run a subprocess with cancellation and timeout handled
        uniformly. Returns a CompletedProcess; raises on timeout or
        cancel. Adapters use this for any subprocess work."""
        ...

    def set_progress(
        self,
        *,
        stage: str,
        percent: int,
        message: str = "",
        **extra,
    ) -> None:
        """Emit progress to the worker queue and dashboard. `percent`
        is required (use 0-100; the runtime clamps). `stage` is a
        short string the dashboard surfaces. `message` is a human-
        readable status line. `**extra` is folded into the
        sidecar/dashboard payload."""
        ...

    def log(
        self,
        level: str,
        msg: str,
        *args,
        **fields,
    ) -> None:
        """Structured logging with the adapter name auto-prefixed.
        `level` is 'debug' / 'info' / 'warning' / 'error' / 'exception'.
        `msg` is the log message; `*args` are %-format substitutions.
        `**fields` becomes structured log fields (Phase 3)."""
        ...
```

## 7. Registration

Adapters register at boot via:

```python
# In your adapter file
class MySourceAdapter:
    name = "my_source"
    source_type = "my_source"

    # ... implement the Protocol methods


# In app/server.py (Mintarr-maintainer side)
import adapters
from adapters.my_source import MySourceAdapter

if adapters.get_adapter("my_source") is None:
    adapters.register(MySourceAdapter())
```

Connector-level registration (manifest, install hints, dashboard surface) is separate; see [`CONNECTOR_MANIFEST_v1.md`](CONNECTOR_MANIFEST_v1.md).

## 8. Job-type and worker integration

For an adapter named `my_source`, the worker recognises a job type `my_source_grab`. The pipeline's generic executor (`_execute_source_grab_job(job, "my_source")`) handles dispatch.

Worker registration in `app/server.py`:

```python
worker.register_executor(
    "my_source_grab",
    lambda job: _execute_source_grab_job(job, "my_source"),
)
```

Custom executors are only needed for non-standard cases (e.g., promote/retry actions). The generic executor handles every source-grab adapter.

## 9. addurl name format

Lidarr-initiated grabs flow through `/sabnzbd/api?mode=addurl&name=<name>:<source_id>`. The dispatcher parses by exact prefix.

For a source named `my_source`, valid addurl names are:

```
my_source:abc123
my_source:Artist/Album (Deluxe Edition)
my_source:any-string-that-survives-utf8
```

The dispatcher passes everything after the first colon as `source_id` to `_addurl_canonicalize(adapter, raw_id, name)`. Adapters that need per-source canonicalisation (e.g., LocalFolder normalising the path) provide a helper used at that point.

## 10. Newznab download URL format

Adapters set `download_url` on their `ReleaseCandidate` to the format Lidarr will POST back. The recommended format leverages Mintarr's `/download/<source>/<base64url_source_id>.nzb` endpoint, which roundtrips arbitrary source_id values through base64url:

```python
candidate.download_url = f"{adapter.source_type}:{candidate.source_id}"
```

The newznab() handler wraps this in the encoded URL using `_addurl_callback_url(base_url, candidate)`. Adapters do not need to construct the URL themselves; setting `download_url` to the `source:source_id` form is sufficient.

## 11. Worked example — minimal source adapter

```python
# app/adapters/example_static.py
"""Trivial example adapter for spec illustration. Returns a fixed
candidate; download_raw() copies a test file. Not for production use."""

from pathlib import Path

from .base import RawDownload, ReleaseCandidate
from .context import PipelineContext


class ExampleStaticAdapter:
    name = "example_static"
    source_type = "example_static"

    def is_enabled(self) -> bool:
        return True

    def search(self, query, artist="", album="", year=None):
        if not (query or artist or album):
            return []  # Skip empty/indexer-test queries

        return [
            ReleaseCandidate(
                source_type="example_static",
                source_id="hardcoded-example",
                title="Example - Static Album (FLAC) [Example]",
                artist="Example Artist",
                album="Static Album",
                year=2024,
                quality_tag="FLAC",
                size_bytes=10 * 1024 * 1024,  # 10 MB
                download_url="example_static:hardcoded-example",
                priority=10,
            ),
        ]

    def download_raw(self, candidate_id, ctx):
        ctx.check_cancelled()
        ctx.set_progress(stage="preparing", percent=5, message="Setting up")

        if candidate_id != "hardcoded-example":
            raise RuntimeError(f"unknown candidate: {candidate_id}")

        ctx.raw_dir.mkdir(parents=True, exist_ok=True)
        target = ctx.raw_dir / "01 example.flac"

        ctx.set_progress(stage="downloading", percent=50, message="Generating file")
        target.write_bytes(b"FAKE_FLAC" + bytes(2048))  # placeholder

        ctx.set_progress(stage="downloaded", percent=45, message="Complete")
        return RawDownload(
            files_dir=ctx.raw_dir,
            file_count=1,
            total_bytes=target.stat().st_size,
        )

    def cleanup(self, jid, ctx):
        # Static adapter has nothing to clean up
        return None
```

This is intentionally tiny. A real adapter (`TidalAdapter`, `LocalFolderAdapter`) is 100-200 lines; `SoulseekAdapter` will be larger because of the completed-folder checks.

## 12. Testing your adapter

Mintarr ships `FakePipelineContext` for unit tests. The pattern:

```python
# tests/test_my_source_adapter.py

class _FakeContext:
    def __init__(self, *, jid, raw_dir, output_dir):
        self.jid = jid
        self.worker_job_id = None
        self.raw_dir = raw_dir
        self.output_dir = output_dir
        self.progress_calls = []
        self.subprocess_calls = []

    def check_cancelled(self): pass
    def run_subprocess(self, argv, *, timeout, text=True):
        self.subprocess_calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")
    def set_progress(self, **kwargs):
        self.progress_calls.append(kwargs)
    def log(self, *args, **kwargs): pass


def test_my_adapter_download_raw_copies_files(tmp_path):
    from adapters.my_source import MySourceAdapter
    adapter = MySourceAdapter(...)
    raw_dir = tmp_path / "raw"
    ctx = _FakeContext(jid="test", raw_dir=raw_dir, output_dir=tmp_path / "out")
    result = adapter.download_raw("test_candidate", ctx)
    assert result.file_count > 0
```

End-to-end pipeline tests go further by running `pipeline.execute_source_grab(job, adapter, ctx)` against a fake context and asserting on the resulting `_jobs` state and sidecar contents. See [`TESTING.md`](../development/TESTING.md) for full patterns.

## 13. Invariants

The Mintarr core enforces these about adapters. Adapter authors should not attempt to violate them:

1. **No imports from `app.server`, `app.pipeline`, `app.state_db` or their pre-cutover legacy equivalents.** Tested at import time for built-in adapters; reviewed for external adapters.
2. **`name` and `source_type` are lowercase ASCII underscore.** Tested at registration for built-in adapters.
3. **`is_enabled()` returns a bool quickly.** Caller times out at 5s.
4. **`search()` returns a list, possibly empty.** Returning None is a spec violation.
5. **`download_raw()` writes only inside `ctx.raw_dir`.** Path-escape attempts raise from the adapter.
6. **`download_raw()` calls `ctx.check_cancelled()` periodically.** Adapters ignoring cancel for >30s are user-hostile.
7. **`cleanup()` is idempotent.** Called even when nothing was created.

PR review applies these checks. Tests cover the testable ones.

## 14. Future changes (planned for v2)

These items are tracked for a hypothetical `ADAPTER_PROTOCOL_v2.md`. They are not v1; do not implement against them yet.

- **`TransientSourceError` exception type.** Today retry classification is string-matching in `worker._is_transient_failure`. v2 introduces explicit exception types.
- **`adapter.health()` method.** Today connector-level health is separate from the adapter; v2 may move per-source health into the adapter for tools without a separate Connector wrapper.
- **`download_raw()` returning generators for streaming.** Today the method is synchronous; v2 may add a streaming variant for very large transfers.
- **Per-source policy hints.** Today V2 verification is source-agnostic; v2 may surface adapter-declared confidence/quality hints into the policy.

If you are interested in driving any of these, open an issue describing the proposal. v2 will be a coordinated release with maintainer sign-off, not an incremental drift from v1.

## 15. Changelog

| Version | Date | Change |
|---|---|---|
| 1.0.0 | 2026-05-26 | Initial locked spec. |
| 1.0.1 | 2026-05-31 | Validated method signatures/dataclass fields against runtime; clarified pre-cutover import paths and priority semantics. |

---

> Last updated: 2026-05-31
