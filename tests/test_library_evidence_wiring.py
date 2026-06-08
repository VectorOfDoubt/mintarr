"""Tests for record-only library-evidence wiring in the import precheck (F5.4 1b)."""

from __future__ import annotations

import library_evidence
import server
import state_db


def _measured(**over):
    base = dict(status="measured", codec="flac", lossless=True, integrity_ok=True)
    base.update(over)
    return library_evidence.TrackMeasurement(**base)


def _enable(monkeypatch, *, stat, measure_calls=None):
    """Enable the lane with a controlled stat + a counting measure."""
    monkeypatch.setattr(library_evidence, "configured_library_root", lambda: "/lib")
    monkeypatch.setattr(library_evidence, "stat_for_freshness", lambda path, **k: stat)
    counter = measure_calls if measure_calls is not None else {"n": 0}

    def _measure(path):
        counter["n"] += 1
        return _measured(sample_rate=96000, bit_depth=24)

    monkeypatch.setattr(library_evidence, "measure_trackfile", _measure)
    return counter


def test_noop_when_library_root_unset(monkeypatch):
    monkeypatch.setattr(library_evidence, "configured_library_root", lambda: None)
    calls = {"n": 0}
    monkeypatch.setattr(
        library_evidence,
        "measure_trackfile",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
    )
    server._record_existing_library_evidence(5, [{"id": 1, "path": "/m/a.flac"}])
    assert calls["n"] == 0


def test_records_measured_evidence(monkeypatch):
    _enable(monkeypatch, stat=("/lib/Artist/01.flac", 123, 1000.0))
    server._record_existing_library_evidence(
        7, [{"id": 101, "path": "/music/Artist/01.flac"}]
    )
    row = state_db.get_library_evidence(101)
    assert row is not None
    assert row["album_id"] == 7
    assert row["status"] == "measured"
    assert row["path"] == "/lib/Artist/01.flac"
    assert row["size"] == 123
    assert row["mtime"] == 1000.0
    assert row["bit_depth"] == 24
    assert row["sensor_version"] == library_evidence.SENSOR_VERSION


def test_skips_when_fully_fresh(monkeypatch):
    calls = _enable(monkeypatch, stat=("/lib/y.flac", 999, 5.0))
    tf = [{"id": 202, "path": "/music/y.flac"}]
    server._record_existing_library_evidence(1, tf)
    server._record_existing_library_evidence(1, tf)  # path+size+mtime+version match
    assert calls["n"] == 1


def test_remeasures_when_mtime_changes(monkeypatch):
    calls = {"n": 0}
    _enable(monkeypatch, stat=("/lib/y.flac", 999, 5.0), measure_calls=calls)
    server._record_existing_library_evidence(1, [{"id": 303, "path": "/m/y.flac"}])
    # mtime changes (same size) — must re-measure (the reported blocker).
    _enable(monkeypatch, stat=("/lib/y.flac", 999, 6.0), measure_calls=calls)
    server._record_existing_library_evidence(1, [{"id": 303, "path": "/m/y.flac"}])
    assert calls["n"] == 2


def test_remeasures_when_path_changes(monkeypatch):
    calls = {"n": 0}
    _enable(monkeypatch, stat=("/lib/old.flac", 10, 1.0), measure_calls=calls)
    server._record_existing_library_evidence(1, [{"id": 404, "path": "/m/y.flac"}])
    _enable(monkeypatch, stat=("/lib/new.flac", 10, 1.0), measure_calls=calls)
    server._record_existing_library_evidence(1, [{"id": 404, "path": "/m/y.flac"}])
    assert calls["n"] == 2


def test_remeasures_when_sensor_version_changes(monkeypatch):
    # A row measured by older sensor logic must be re-measured.
    state_db.upsert_library_evidence(
        {
            "trackfile_id": 505,
            "album_id": 1,
            "path": "/lib/z.flac",
            "size": 10,
            "mtime": 1.0,
            "status": "measured",
            "sensor_version": "mintarr-library-evidence OLD",
        }
    )
    calls = _enable(monkeypatch, stat=("/lib/z.flac", 10, 1.0))
    server._record_existing_library_evidence(1, [{"id": 505, "path": "/m/z.flac"}])
    assert calls["n"] == 1  # stale sensor_version → re-measured


def test_never_raises_on_measure_error(monkeypatch):
    monkeypatch.setattr(library_evidence, "configured_library_root", lambda: "/lib")
    monkeypatch.setattr(
        library_evidence,
        "stat_for_freshness",
        lambda path, **k: ("/lib/z.flac", 1, 1.0),
    )

    def _boom(path):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(library_evidence, "measure_trackfile", _boom)
    server._record_existing_library_evidence(1, [{"id": 606, "path": "/m/z.flac"}])


# ---- F5.4 slice 4a: spectral record-only wiring ----


def test_spectral_noop_when_disabled(monkeypatch):
    _enable(monkeypatch, stat=("/lib/s.flac", 1, 1.0))
    monkeypatch.setattr(library_evidence, "spectral_enabled", lambda: False)
    calls = {"n": 0}
    monkeypatch.setattr(
        library_evidence,
        "measure_trackfile_spectral",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
    )
    server._record_existing_library_evidence(1, [{"id": 700, "path": "/m/s.flac"}])
    assert calls["n"] == 0  # spectral tier never invoked when opt-out


def test_spectral_records_verdict(monkeypatch):
    _enable(monkeypatch, stat=("/lib/s.flac", 1, 1.0))
    monkeypatch.setattr(library_evidence, "spectral_enabled", lambda: True)
    monkeypatch.setattr(library_evidence, "is_spectral_row_fresh", lambda row: False)
    monkeypatch.setattr(
        library_evidence,
        "measure_trackfile_spectral",
        lambda *a, **k: library_evidence.SpectralMeasurement(
            status="measured", authentic=False, verdict="FAKE"
        ),
    )
    server._record_existing_library_evidence(9, [{"id": 701, "path": "/m/s.flac"}])
    row = state_db.get_library_evidence(701)
    assert row["spectral_status"] == "measured"
    assert row["authentic"] == 0
    assert row["spectral_verdict"] == "FAKE"
    assert row["spectral_sensor_version"] == library_evidence.SPECTRAL_SENSOR_VERSION
    # The cheap-tier evidence is preserved (partial upsert did not clobber it).
    assert row["status"] == "measured"
    assert row["bit_depth"] == 24


def test_spectral_skips_when_fresh(monkeypatch):
    _enable(monkeypatch, stat=("/lib/s.flac", 1, 1.0))
    monkeypatch.setattr(library_evidence, "spectral_enabled", lambda: True)
    monkeypatch.setattr(library_evidence, "is_spectral_row_fresh", lambda row: True)
    calls = {"n": 0}
    monkeypatch.setattr(
        library_evidence,
        "measure_trackfile_spectral",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
    )
    server._record_existing_library_evidence(1, [{"id": 702, "path": "/m/s.flac"}])
    assert calls["n"] == 0  # fresh spectral verdict → no re-measure
