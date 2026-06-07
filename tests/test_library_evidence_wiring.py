"""Tests for record-only library-evidence wiring in the import precheck (F5.4 1b)."""

from __future__ import annotations

import library_evidence
import server
import state_db


def test_noop_when_library_root_unset(monkeypatch):
    monkeypatch.setattr(library_evidence, "configured_library_root", lambda: None)
    called = {"n": 0}
    monkeypatch.setattr(
        library_evidence,
        "measure_trackfile",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )
    server._record_existing_library_evidence(5, [{"id": 1, "path": "/m/a.flac"}])
    assert called["n"] == 0  # measured nothing


def test_records_measured_evidence(monkeypatch):
    monkeypatch.setattr(library_evidence, "configured_library_root", lambda: "/lib")
    monkeypatch.setattr(
        library_evidence,
        "measure_trackfile",
        lambda path: library_evidence.TrackMeasurement(
            status="measured",
            codec="flac",
            sample_rate=96000,
            bit_depth=24,
            channels=2,
            lossless=True,
            integrity_ok=True,
        ),
    )

    server._record_existing_library_evidence(
        7, [{"id": 101, "path": "/music/Artist/Album/01.flac", "size": 123}]
    )

    row = state_db.get_library_evidence(101)
    assert row is not None
    assert row["album_id"] == 7
    assert row["status"] == "measured"
    assert row["bit_depth"] == 24
    assert row["lossless"] == 1


def test_skips_remeasure_when_size_unchanged(monkeypatch):
    monkeypatch.setattr(library_evidence, "configured_library_root", lambda: "/lib")
    calls = {"n": 0}

    def _measure(path):
        calls["n"] += 1
        return library_evidence.TrackMeasurement(status="measured", codec="flac")

    monkeypatch.setattr(library_evidence, "measure_trackfile", _measure)

    tf = [{"id": 202, "path": "/music/x.flac", "size": 999}]
    server._record_existing_library_evidence(1, tf)
    server._record_existing_library_evidence(1, tf)  # same size → cached
    assert calls["n"] == 1


def test_remeasures_when_size_changes(monkeypatch):
    monkeypatch.setattr(library_evidence, "configured_library_root", lambda: "/lib")
    calls = {"n": 0}

    def _measure(path):
        calls["n"] += 1
        return library_evidence.TrackMeasurement(status="measured", codec="flac")

    monkeypatch.setattr(library_evidence, "measure_trackfile", _measure)

    server._record_existing_library_evidence(
        1, [{"id": 303, "path": "/m/y.flac", "size": 10}]
    )
    server._record_existing_library_evidence(
        1, [{"id": 303, "path": "/m/y.flac", "size": 20}]
    )
    assert calls["n"] == 2


def test_never_raises_on_measure_error(monkeypatch):
    monkeypatch.setattr(library_evidence, "configured_library_root", lambda: "/lib")

    def _boom(path):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(library_evidence, "measure_trackfile", _boom)
    # Must not raise.
    server._record_existing_library_evidence(1, [{"id": 404, "path": "/m/z.flac"}])
