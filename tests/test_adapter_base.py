"""F3.1 contract tests for adapter base types + registry."""

from __future__ import annotations


import sys
from types import SimpleNamespace

import pytest


def test_release_candidate_guid_is_source_colon_id():
    from adapters.base import ReleaseCandidate
    rc = ReleaseCandidate(
        source_type="tidal",
        source_id="12345",
        title="Daft Punk - Random Access Memories (2013) [TIDAL] [FLAC 24bit]",
        artist="Daft Punk",
        album="Random Access Memories",
        year=2013,
        quality_tag="FLAC 24bit",
        size_bytes=300_000_000,
        download_url="tidal:12345",
    )
    assert rc.guid == "tidal:12345"
    assert rc.priority == 50  # default per F3 design v0.3 §8


def test_raw_download_holds_paths_and_counts(tmp_path):
    from adapters.base import RawDownload
    rd = RawDownload(files_dir=tmp_path, file_count=12, total_bytes=42)
    assert rd.files_dir == tmp_path
    assert rd.file_count == 12
    assert rd.total_bytes == 42


def test_registry_register_get_and_enabled_filter():
    import adapters
    adapters.reset_registry()

    class _DummyAdapter:
        name = "dummy"
        source_type = "dummy"
        def is_enabled(self): return True
        def search(self, *a, **kw): return []
        def download_raw(self, *a, **kw): raise NotImplementedError
        def cleanup(self, *a, **kw): return None

    class _OffAdapter(_DummyAdapter):
        name = "off"
        source_type = "off"
        def is_enabled(self): return False

    adapters.register(_DummyAdapter())
    adapters.register(_OffAdapter())
    assert adapters.get_adapter("dummy") is not None
    assert adapters.get_adapter("off") is not None
    assert adapters.get_adapter("missing") is None
    enabled_names = {a.name for a in adapters.enabled_adapters()}
    assert enabled_names == {"dummy"}


def test_registry_register_duplicate_raises():
    import adapters
    adapters.reset_registry()

    class _A:
        name = "dup"
        source_type = "dup"
        def is_enabled(self): return True
        def search(self, *a, **kw): return []
        def download_raw(self, *a, **kw): raise NotImplementedError
        def cleanup(self, *a, **kw): return None

    adapters.register(_A())
    with pytest.raises(ValueError, match="already registered"):
        adapters.register(_A())


def test_runtime_context_satisfies_protocol(tmp_path):
    """RuntimePipelineContext must satisfy the PipelineContext Protocol shape."""
    from adapters.runtime import RuntimePipelineContext

    ctx = RuntimePipelineContext(
        jid="testjid",
        worker_job_id=None,
        raw_dir=tmp_path / "raw",
        output_dir=tmp_path / "out",
        adapter_name="tidal",
    )
    # Structural-typing check — Protocol w/o runtime_checkable, so verify by attr/method
    for name in ("check_cancelled", "run_subprocess", "set_progress", "log"):
        assert callable(getattr(ctx, name)), f"missing method: {name}"
    assert ctx.jid == "testjid"
    assert ctx.raw_dir == tmp_path / "raw"
    assert ctx.output_dir == tmp_path / "out"


def test_tidal_adapter_is_enabled_when_token_present(tmp_path):
    """is_enabled() must reflect token.json presence."""
    from adapters.tidal import TidalAdapter
    cfg = tmp_path / "tidal-config"
    cfg.mkdir()
    adapter = TidalAdapter(config_dir=str(cfg))
    assert adapter.is_enabled() is False  # no token yet
    (cfg / "token.json").write_text('{"access_token": "x"}')
    assert adapter.is_enabled() is True


def test_tidal_adapter_name_and_source_type():
    from adapters.tidal import TidalAdapter
    a = TidalAdapter()
    assert a.name == "tidal"
    assert a.source_type == "tidal"


def test_tidal_get_session_loads_token_as_pkce(tmp_path, monkeypatch):
    from adapters import tidal

    config_dir = tmp_path / "tidal-config"
    config_dir.mkdir()
    (config_dir / "token.json").write_text(
        '{"token_type":"Bearer","access_token":"access","refresh_token":"refresh","expiry_time":1}'
    )
    seen = {}

    class _FakeSession:
        def load_oauth_session(self, token_type, access_token, refresh_token, expiry_time, *, is_pkce=False):
            seen["is_pkce"] = is_pkce
            self.user = SimpleNamespace(username="redacted")
            return True

    monkeypatch.setenv("TIDAL_DL_NG_CONFIG", str(config_dir))
    monkeypatch.delenv("TIDAL_OAUTH_PKCE", raising=False)
    monkeypatch.setitem(sys.modules, "tidalapi", SimpleNamespace(Session=_FakeSession))
    tidal.reset_session_cache()
    try:
        tidal.get_session()
        assert seen["is_pkce"] is True
    finally:
        tidal.reset_session_cache()


def test_tidal_get_session_pkce_can_be_disabled(tmp_path, monkeypatch):
    from adapters import tidal

    config_dir = tmp_path / "tidal-config"
    config_dir.mkdir()
    (config_dir / "token.json").write_text(
        '{"token_type":"Bearer","access_token":"access","refresh_token":"refresh","expiry_time":1}'
    )
    seen = {}

    class _FakeSession:
        def load_oauth_session(self, token_type, access_token, refresh_token, expiry_time, *, is_pkce=False):
            seen["is_pkce"] = is_pkce
            self.user = SimpleNamespace(username="redacted")
            return True

    monkeypatch.setenv("TIDAL_DL_NG_CONFIG", str(config_dir))
    monkeypatch.setenv("TIDAL_OAUTH_PKCE", "0")
    monkeypatch.setitem(sys.modules, "tidalapi", SimpleNamespace(Session=_FakeSession))
    tidal.reset_session_cache()
    try:
        tidal.get_session()
        assert seen["is_pkce"] is False
    finally:
        tidal.reset_session_cache()
