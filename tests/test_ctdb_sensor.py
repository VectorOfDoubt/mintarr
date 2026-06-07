"""Tests for the advisory CTDB SensorResult emission (F5.3B emission slice)."""

from __future__ import annotations

import cd_lookup
import cd_toc
import server
from cd_toc import CdToc

_CDRIP = object()  # a truthy "detected" cd-rip evidence stand-in
_TOC = object()  # reconstruct_toc is mocked, so contents don't matter here


def _enable(monkeypatch, *, toc=_TOC):
    monkeypatch.setattr(server, "_ctdb_enabled", lambda: True)
    monkeypatch.setattr(cd_toc, "reconstruct_toc", lambda *a, **k: toc)


def test_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(server, "_ctdb_enabled", lambda: False)
    assert server._build_ctdb_sensor(object(), _CDRIP) is None


def test_returns_none_when_not_a_cd_rip(monkeypatch):
    monkeypatch.setattr(server, "_ctdb_enabled", lambda: True)
    assert server._build_ctdb_sensor(object(), None) is None


def test_no_toc_is_skipped_info(monkeypatch):
    _enable(monkeypatch, toc=None)
    sensor = server._build_ctdb_sensor(object(), _CDRIP)
    assert sensor["name"] == "ctdb"
    assert sensor["class"] == "source_specific_proof"
    assert sensor["status"] == "skipped"
    assert sensor["severity"] == "info"
    assert sensor["evidence"]["reason"] == "no_toc"


def test_found_is_pass_with_evidence(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(
        cd_lookup,
        "lookup_accuraterip",
        lambda toc, **kw: cd_lookup.AccurateRipResult(
            found=True, pressings=3, max_confidence=22, track_count=12
        ),
    )
    monkeypatch.setattr(
        cd_lookup,
        "lookup_ctdb",
        lambda toc, **kw: cd_lookup.CtdbResult(
            found=True, submissions=2, confidence=22
        ),
    )

    sensor = server._build_ctdb_sensor(object(), _CDRIP)
    assert sensor["status"] == "pass"
    assert sensor["severity"] == "info"  # advisory, never blocker
    assert sensor["evidence"]["accuraterip"]["found"] is True
    assert sensor["evidence"]["ctdb"]["confidence"] == 22


def test_not_found_is_skipped_info(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(
        cd_lookup,
        "lookup_accuraterip",
        lambda toc, **kw: cd_lookup.AccurateRipResult(False),
    )
    monkeypatch.setattr(
        cd_lookup, "lookup_ctdb", lambda toc, **kw: cd_lookup.CtdbResult(False)
    )

    sensor = server._build_ctdb_sensor(object(), _CDRIP)
    assert sensor["status"] == "skipped"
    assert sensor["severity"] == "info"
    assert sensor["evidence"]["accuraterip"]["found"] is False


def test_lookup_failure_is_skipped_warning(monkeypatch):
    _enable(monkeypatch)

    def _boom(toc, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(cd_lookup, "lookup_accuraterip", _boom)
    monkeypatch.setattr(cd_lookup, "lookup_ctdb", _boom)

    sensor = server._build_ctdb_sensor(object(), _CDRIP)
    assert sensor["status"] == "skipped"
    assert sensor["severity"] == "warning"
    assert sensor["severity"] != "blocker"  # advisory: never blocks


def test_ctdb_enabled_is_false_by_default():
    # The connector is default-off, so the gate is closed unless opted in.
    assert server._ctdb_enabled() is False


def test_repeated_builds_use_lookup_cache(monkeypatch):
    # Re-verifying the same disc must not re-hit AccurateRip/CTDB (bounded fetches).
    toc = CdToc(
        track_offsets_frames=(0, 10000, 20000), leadout_frames=30000, track_count=3
    )
    monkeypatch.setattr(server, "_ctdb_enabled", lambda: True)
    monkeypatch.setattr(cd_toc, "reconstruct_toc", lambda *a, **k: toc)
    server._CTDB_AR_LOOKUP_CACHE.clear()
    server._CTDB_LOOKUP_CACHE.clear()

    calls = {"n": 0}

    def _counting_fetch(url):
        calls["n"] += 1
        return None  # disc not found; result still gets cached

    monkeypatch.setattr(cd_lookup, "_default_fetch", _counting_fetch)

    server._build_ctdb_sensor(object(), _CDRIP)
    server._build_ctdb_sensor(object(), _CDRIP)

    # 2 fetches total (AccurateRip + CTDB) on the first build; second is cached.
    assert calls["n"] == 2
