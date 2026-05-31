# Adapter Tutorial — Build Your Own Source Adapter

> **Type:** Development / tutorial
> **Version:** 1.0 — 2026-05-26
> **Status:** Living document. Updates as the adapter contract evolves.
> **Audience:** Contributors building a source adapter for the first time.
> **Related:** [ADAPTER_PROTOCOL_v1.md](../specs/ADAPTER_PROTOCOL_v1.md) (the locked contract)

---

## 1. What we're building

This tutorial walks through building a source adapter end-to-end. We'll build a `FtpSourceAdapter` that fetches files from an FTP server — simple enough to focus on the pattern, realistic enough that the result could ship.

By the end, you will have:

- A working `FtpSourceAdapter` in `app/adapters/ftp.py`
- A connector manifest in `app/connectors/ftp.py` (Phase 4.1)
- Tests under `tests/test_ftp_adapter.py`
- The adapter wired into the registry, available to Lidarr via Newznab + addurl
- Files Lidarr can manually import

## 2. Plan

Before writing code, decide:

| Question | Answer for FTP adapter |
|---|---|
| What does the source contain? | Audio files in folder hierarchies under a configured FTP root |
| How do we identify a candidate? | Relative path within the FTP root, e.g., `Artist/Album/` |
| How do we authenticate? | Username + password from environment variables |
| Is the source modifiable? | No — Mintarr copies, FTP server is left untouched |
| What's the trust level? | Operator-controlled (operator chose what to put on the FTP) — semi-trusted |
| What's the priority relative to other sources? | Lower than TIDAL (no HiRes guarantee), comparable to LocalFolder (20-30) |

Document this in a design doc under `docs/design/F<N>_FTP_ADAPTER.md` for non-trivial adapters. For this tutorial, the question table above is the design.

## 3. Skeleton

Start with the minimum that satisfies the Protocol:

```python
# app/adapters/ftp.py

import os
from pathlib import Path

from .base import RawDownload, ReleaseCandidate
from .context import PipelineContext


class FtpSourceAdapter:
    name = "ftp"
    source_type = "ftp"

    def is_enabled(self) -> bool:
        return False  # placeholder — implement in §4

    def search(self, query, artist="", album="", year=None):
        return []  # placeholder — implement in §5

    def download_raw(self, candidate_id, ctx):
        raise NotImplementedError  # placeholder — implement in §6

    def cleanup(self, jid, ctx):
        return None
```

This is the smallest valid adapter. It satisfies the Protocol but does nothing useful. We'll fill in each method.

## 4. `is_enabled`

The adapter is enabled when its required configuration is present. For FTP:

- `FTP_HOST` environment variable
- `FTP_USER` environment variable
- `FTP_PASS` environment variable (or password file)
- Optional: `FTP_PORT` (default 21)

```python
def is_enabled(self) -> bool:
    return all(os.environ.get(v) for v in ("FTP_HOST", "FTP_USER", "FTP_PASS"))
```

Test:

```python
# tests/test_ftp_adapter.py

def test_ftp_adapter_disabled_without_env(monkeypatch):
    from adapters.ftp import FtpSourceAdapter
    monkeypatch.delenv("FTP_HOST", raising=False)
    adapter = FtpSourceAdapter()
    assert adapter.is_enabled() is False


def test_ftp_adapter_enabled_when_env_present(monkeypatch):
    from adapters.ftp import FtpSourceAdapter
    monkeypatch.setenv("FTP_HOST", "ftp.example.com")
    monkeypatch.setenv("FTP_USER", "user")
    monkeypatch.setenv("FTP_PASS", "pass")
    adapter = FtpSourceAdapter()
    assert adapter.is_enabled() is True
```

Run the tests:

```bash
docker compose -f docker-compose.test.yaml run --rm \
    --entrypoint pytest tests /tests/test_ftp_adapter.py -v
```

Both pass. We have an enable check.

## 5. `search`

The search method returns release candidates Lidarr can choose to grab. For FTP, search means "scan the FTP root for matching artist/album folders".

The implementation pattern:

1. If query, artist, album are all empty → return `[]` (Lidarr indexer-test)
2. Connect to FTP
3. List directories under the configured root
4. Filter by artist/album/query
5. For each match, return a `ReleaseCandidate`

```python
from ftplib import FTP


def search(self, query, artist="", album="", year=None):
    if not (query or artist or album):
        return []
    if not self.is_enabled():
        return []

    host = os.environ["FTP_HOST"]
    user = os.environ["FTP_USER"]
    pwd = os.environ["FTP_PASS"]
    port = int(os.environ.get("FTP_PORT", "21"))

    candidates = []
    try:
        with FTP() as ftp:
            ftp.connect(host, port, timeout=10)
            ftp.login(user, pwd)
            for artist_dir in ftp.nlst("/"):
                if artist and artist.lower() not in artist_dir.lower():
                    continue
                for album_dir in ftp.nlst(f"/{artist_dir}"):
                    if album and album.lower() not in album_dir.lower():
                        continue
                    if query:
                        haystack = f"{artist_dir} {album_dir}".lower()
                        if query.lower() not in haystack:
                            continue
                    rel = f"{artist_dir}/{album_dir}"
                    # Approximate size — refine later
                    candidates.append(ReleaseCandidate(
                        source_type="ftp",
                        source_id=rel,
                        title=f"{artist_dir} - {album_dir} (FLAC) [FTP]",
                        artist=artist_dir,
                        album=album_dir,
                        year=None,
                        quality_tag="FLAC",
                        size_bytes=500_000_000,  # placeholder
                        download_url=f"ftp:{rel}",
                        priority=25,
                    ))
                    if len(candidates) >= 100:
                        return candidates
    except Exception:
        log.exception("FTP search failed")
        return []
    return candidates
```

Tests for search:

```python
def test_ftp_search_empty_query_returns_empty(monkeypatch):
    from adapters.ftp import FtpSourceAdapter
    monkeypatch.setenv("FTP_HOST", "ftp.example.com")
    monkeypatch.setenv("FTP_USER", "user")
    monkeypatch.setenv("FTP_PASS", "pass")
    adapter = FtpSourceAdapter()
    assert adapter.search(query="", artist="", album="") == []


def test_ftp_search_returns_candidates(monkeypatch):
    from adapters.ftp import FtpSourceAdapter
    import adapters.ftp as ftp_mod

    class _FakeFTP:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def connect(self, *a, **kw): pass
        def login(self, *a, **kw): pass
        def nlst(self, path):
            if path == "/":
                return ["Artist A", "Artist B"]
            return ["Album 1"]

    monkeypatch.setattr(ftp_mod, "FTP", _FakeFTP)
    monkeypatch.setenv("FTP_HOST", "x")
    monkeypatch.setenv("FTP_USER", "x")
    monkeypatch.setenv("FTP_PASS", "x")

    adapter = FtpSourceAdapter()
    candidates = adapter.search(query="Artist A")
    assert len(candidates) == 1
    assert candidates[0].artist == "Artist A"
```

## 6. `download_raw`

This is where the real work happens. The pattern:

1. Validate the candidate_id (path traversal guard)
2. Connect to FTP
3. Walk the candidate folder, downloading files
4. Call `ctx.check_cancelled()` between files
5. Call `ctx.set_progress(...)` for dashboard updates
6. Return `RawDownload` with stats

```python
def download_raw(self, candidate_id, ctx):
    # Defence in depth: validate the candidate_id
    if not candidate_id or "/.." in candidate_id or candidate_id.startswith("/"):
        raise RuntimeError(f"invalid candidate path: {candidate_id}")

    if not self.is_enabled():
        raise RuntimeError("FTP adapter not enabled")

    ctx.check_cancelled()
    ctx.set_progress(stage="connecting", percent=5, message="Connecting to FTP")

    host = os.environ["FTP_HOST"]
    user = os.environ["FTP_USER"]
    pwd = os.environ["FTP_PASS"]
    port = int(os.environ.get("FTP_PORT", "21"))

    ctx.raw_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    audio_files = 0
    total_bytes = 0

    with FTP() as ftp:
        ftp.connect(host, port, timeout=30)
        ftp.login(user, pwd)

        ctx.set_progress(stage="downloading", percent=10, message="Fetching files")

        files = ftp.nlst(f"/{candidate_id}")
        for i, filename in enumerate(files):
            ctx.check_cancelled()

            # Progress update per file
            percent = 10 + int(35 * (i / max(len(files), 1)))
            ctx.set_progress(
                stage="downloading", percent=percent,
                message=f"Downloading {filename}",
            )

            target = ctx.raw_dir / filename
            with target.open("wb") as f:
                ftp.retrbinary(f"RETR /{candidate_id}/{filename}", f.write)

            copied += 1
            if filename.lower().endswith((".flac", ".m4a")):
                audio_files += 1
            total_bytes += target.stat().st_size

    if audio_files == 0:
        raise RuntimeError(f"no audio files in {candidate_id}")

    ctx.set_progress(
        stage="downloaded", percent=45, message="Download complete",
        file_count=copied, audio_files=audio_files, size_bytes=total_bytes,
    )
    return RawDownload(
        files_dir=ctx.raw_dir,
        file_count=copied,
        total_bytes=int(total_bytes),
    )
```

Tests:

```python
def test_ftp_download_raw_rejects_path_traversal(tmp_path):
    from adapters.ftp import FtpSourceAdapter
    adapter = FtpSourceAdapter()
    ctx = _FakeContext(jid="test", raw_dir=tmp_path / "raw", output_dir=tmp_path / "out")
    import pytest
    with pytest.raises(RuntimeError, match="invalid candidate path"):
        adapter.download_raw("../../etc", ctx)


def test_ftp_download_raw_copies_files(tmp_path, monkeypatch):
    # ... build a fake FTP that returns predetermined files
    # ... call adapter.download_raw
    # ... assert files appear in ctx.raw_dir
    pass  # exercise for the reader
```

## 7. Registration

The adapter is registered in `app/server.py` boot:

```python
# In app/server.py, after the existing adapter registrations
try:
    from adapters.ftp import FtpSourceAdapter
    if adapters.get_adapter("ftp") is None:
        adapters.register(FtpSourceAdapter())
except Exception:
    log.exception("FTP adapter registration failed")
```

The worker queue also needs an executor for the new job type:

```python
# In app/server.py, in the worker registration block
worker.register_executor(
    "ftp_grab",
    lambda job: _execute_source_grab_job(job, "ftp"),
)
```

That's it. The generic executor handles dispatch.

## 8. Connector manifest

If your adapter is for a community-distributed project, also add a connector manifest under `app/connectors/ftp.py`:

```python
from connectors.base import ConnectorManifest, ConnectorKind


class FtpConnector:
    manifest = ConnectorManifest(
        id="ftp",
        display_name="FTP Source",
        kind=ConnectorKind.SOURCE,
        api_version="1.0.0",
        adapter_class="adapters.ftp:FtpSourceAdapter",
        default_enabled=True,
        required=False,
        install_profile=None,
        docker_service=None,
        required_env=("FTP_HOST", "FTP_USER", "FTP_PASS"),
        optional_env=("FTP_PORT",),
        capabilities=("ftp_download", "http_download"),
        docs_url="connectors/ftp/",
        min_supported_version=None,
    )

    def __init__(self, adapter):
        self._adapter = adapter

    def is_installed(self) -> bool:
        return self._adapter.is_enabled()

    def is_enabled(self) -> bool:
        return True  # F4.1 — no per-connector config yet

    def health(self):
        import time
        from connectors.base import ConnectorHealth
        return ConnectorHealth(
            status="ok" if self.is_installed() else "missing",
            last_error=None if self.is_installed() else "FTP_HOST/USER/PASS not set",
            last_checked_at=time.time(),
        )

    def detected_version(self):
        return None
```

Register in `app/server.py`:

```python
from connectors.ftp import FtpConnector
connectors.register(FtpConnector(FtpSourceAdapter()))
```

## 9. End-to-end test

Once the adapter is wired in, run a smoke test:

1. Start Mintarr with `FTP_HOST`, `FTP_USER`, `FTP_PASS` set
2. Verify the dashboard shows the FTP connector as installed
3. Trigger a search via Lidarr — Lidarr should see FTP candidates
4. Trigger a grab — Mintarr should download the files, run the pipeline, attempt Lidarr import

If any step fails, check Mintarr's logs. The log lines are prefixed with the adapter name and jid for easy filtering.

## 10. What you skipped

A real FTP adapter would also handle:

- FTPS / SFTP variants
- Connection pooling (one connection per grab, not per file)
- Resume on connection failure
- Bandwidth limiting
- Listing performance for FTP servers with thousands of folders
- Authentication via SSH key instead of password
- Connection caching across multiple grabs

Mintarr's adapter contract does not constrain these — the adapter author decides. The tutorial keeps things simple to illustrate the pattern; a production adapter would expand.

## 11. Common mistakes

### 11.1 Catching `JobCancelled` in download_raw

`worker.JobCancelled` is the cancel signal. The adapter must NOT catch it:

```python
# Bad
try:
    ctx.check_cancelled()
    # ... work
except worker.JobCancelled:
    pass  # NO — propagate it

# Good — let it propagate
ctx.check_cancelled()
# ... work
```

### 11.2 Direct subprocess calls

Adapters use `ctx.run_subprocess`, not `subprocess.run`:

```python
# Bad
import subprocess
result = subprocess.run(["some-cli", "arg"], timeout=30)

# Good
result = ctx.run_subprocess(["some-cli", "arg"], timeout=30)
```

### 11.3 Returning empty RawDownload

If `download_raw` cannot produce files, it should raise, not return an empty `RawDownload`:

```python
# Bad
if not files:
    return RawDownload(files_dir=ctx.raw_dir, file_count=0, total_bytes=0)

# Good
if not files:
    raise RuntimeError(f"no files for candidate {candidate_id}")
```

### 11.4 Importing from server.py

Adapters depend only on `adapters.base` and `adapters.context`. Importing from `server.py` couples your adapter to internal Mintarr code:

```python
# Bad
from server import _set_worker_progress
_set_worker_progress(...)

# Good — go through the context
ctx.set_progress(...)
```

## 12. What to read next

- [ADAPTER_PROTOCOL_v1.md](../specs/ADAPTER_PROTOCOL_v1.md) for the locked contract details
- [PIPELINE.md](../architecture/PIPELINE.md) to understand the four phases and what the pipeline does with your adapter's output
- [CONNECTOR_MANIFEST_v1.md](../specs/CONNECTOR_MANIFEST_v1.md) for the manifest spec
- Existing adapters in `app/adapters/` — TIDAL and LocalFolder are the reference implementations

When you're ready to contribute your adapter back, follow [CONTRIBUTING.md](https://github.com/eivindsjursen-lab/mintarr/blob/main/CONTRIBUTING.md).

---

> Last updated: 2026-05-26
