"""Pytest fixtures for tidalhires.

Sets up minimal env vars so `import server` doesn't crash on missing
paths. Real Lidarr/Tidal interactions are mocked in individual tests.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

_TMPDIR = tempfile.mkdtemp(prefix="tidalhires-test-")
os.environ["TIDALHIRES_API_KEY"] = "tidalhires-test-api-key"
os.environ["DOWNLOAD_BASE"] = str(Path(_TMPDIR) / "downloads")
os.environ["OUTPUT_BASE"] = str(Path(_TMPDIR) / "output")
os.environ["TIDAL_DL_NG_CONFIG"] = str(Path(_TMPDIR) / "tidal-config")
# F1.6: isoler tester fra prod state-DB
os.environ["TIDALHIRES_STATE_DB"] = str(Path(_TMPDIR) / "state.db")
# F2.1: do not start auto-worker in tests (pytest controls lifecycle)
os.environ["TIDALHIRES_DISABLE_WORKER"] = "1"

APP_DIR = (
    Path("/app")
    if Path("/app/server.py").exists()
    else Path(__file__).resolve().parents[1] / "app"
)
sys.path.insert(0, str(APP_DIR))


@pytest.fixture(autouse=True)
def _reset_state_db():
    """Reset state_db between tests so _initialized + _db_path are fresh.

    F3.1: also re-seed the adapter registry with built-in adapters so tests
    that call reset_registry() do not leave subsequent tests with no
    adapter registered. F4.1 does the same for the connector registry.
    """
    try:
        import state_db

        state_db._initialized = False
        if state_db._db_path.exists():
            state_db._db_path.unlink()
    except Exception:
        pass
    try:
        import adapters
        import connectors
        from adapters.completed_folder import (
            QBittorrentCompletedAdapter,
            SabUsenetCompletedAdapter,
        )
        from adapters.tidal import TidalAdapter
        from adapters.local_folder import LocalFolderAdapter
        from adapters.soulseek import SoulseekCompletedAdapter

        adapters.reset_registry()
        adapters.register(TidalAdapter())
        adapters.register(LocalFolderAdapter())
        adapters.register(SoulseekCompletedAdapter())
        adapters.register(SabUsenetCompletedAdapter())
        adapters.register(QBittorrentCompletedAdapter())
        connectors.reset_registry()
        connectors.register_builtin_connectors(warn_missing_required=False)
    except Exception:
        pass
    yield


@pytest.fixture
def fake_album():
    """Minimal stand-in for tidalapi.Album used by _classify_quality / _release_title."""
    return SimpleNamespace(
        id=12345,
        name="Random Access Memories",
        duration=4500,  # 75 min
        artist=SimpleNamespace(name="Daft Punk"),
        release_date=SimpleNamespace(year=2013),
        num_tracks=13,
        type="ALBUM",
    )
