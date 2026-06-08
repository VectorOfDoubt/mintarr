"""Tests for F5.4 slice 5d library quality ranking views."""

from __future__ import annotations

import dashboard
import library_evidence
import server
import state_db

VALID_KEY = "tidalhires-test-api-key"


def _seed(trackfile_id: int, album_id: int, **overrides):
    row = {
        "trackfile_id": trackfile_id,
        "album_id": album_id,
        "path": f"/library/Artist/Album/{trackfile_id:02d}.flac",
        "status": "measured",
        "codec": "flac",
        "sample_rate": 96000,
        "bit_depth": 24,
        "lossless": True,
        "integrity_ok": True,
        "sensor_version": library_evidence.SENSOR_VERSION,
    }
    row.update(overrides)
    state_db.upsert_library_evidence(row)


def _fresh(monkeypatch):
    monkeypatch.setattr(library_evidence, "is_measured_row_fresh", lambda _row: True)
    monkeypatch.setattr(library_evidence, "is_spectral_row_fresh", lambda _row: True)


def test_library_quality_requires_auth():
    client = server.app.test_client()

    assert client.get("/dashboard/v1/library-quality").status_code == 401
    assert client.get("/dashboard/v1/library-quality/partial").status_code == 401


def test_bucket_precedence_is_worst_first(monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.setattr(library_evidence, "spectral_enabled", lambda: True)
    _seed(1, 100, integrity_ok=False)
    state_db.update_library_spectral(
        {
            "trackfile_id": 1,
            "album_id": 100,
            "authentic": False,
            "spectral_status": "measured",
            "spectral_sensor_version": library_evidence.SPECTRAL_SENSOR_VERSION,
        }
    )
    _seed(2, 101)
    state_db.update_library_spectral(
        {
            "trackfile_id": 2,
            "album_id": 101,
            "authentic": False,
            "spectral_status": "measured",
            "spectral_sensor_version": library_evidence.SPECTRAL_SENSOR_VERSION,
        }
    )

    view = dashboard._build_library_quality_view()
    by_album = {album["album_id"]: album for album in view["albums"]}

    assert by_album[100]["primary_bucket"] == "invalid"
    assert by_album[101]["primary_bucket"] == "measured_fake"


def test_stale_evidence_gets_own_bucket_without_disk_stat(monkeypatch):
    def fail_live_freshness(_row):
        raise AssertionError("dashboard ranking must not stat library files")

    monkeypatch.setattr(library_evidence, "is_measured_row_fresh", fail_live_freshness)
    monkeypatch.setattr(library_evidence, "is_spectral_row_fresh", fail_live_freshness)
    _seed(3, 102, integrity_ok=True, sensor_version="older")

    view = dashboard._build_library_quality_view()

    assert view["albums"][0]["primary_bucket"] == "stale"
    assert view["albums"][0]["stale_count"] == 1


def test_all_non_error_buckets_are_classified(monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.setattr(library_evidence, "spectral_enabled", lambda: True)
    _seed(10, 110, status="unmeasured", reason="library not mounted")
    _seed(11, 111, codec="mp3", lossless=False, integrity_ok=None)
    _seed(12, 112, bit_depth=24, sample_rate=48000)
    _seed(13, 112, bit_depth=24, sample_rate=96000)
    _seed(14, 113, bit_depth=24, sample_rate=96000)
    _seed(15, 114, bit_depth=24, sample_rate=96000)
    state_db.update_library_spectral(
        {
            "trackfile_id": 15,
            "album_id": 114,
            "authentic": True,
            "spectral_status": "measured",
            "spectral_sensor_version": library_evidence.SPECTRAL_SENSOR_VERSION,
        }
    )

    view = dashboard._build_library_quality_view()
    by_album = {album["album_id"]: album["primary_bucket"] for album in view["albums"]}

    assert by_album[110] == "unmeasured"
    assert by_album[111] == "lossy_or_low_tier"
    assert by_album[112] == "mixed_tier"
    assert by_album[113] == "unknown_authenticity"
    assert by_album[114] == "ok"


def test_library_quality_json_uses_basenames_only(monkeypatch):
    _fresh(monkeypatch)
    _seed(4, 103, path="/mnt/music/Artist/Album/01 - One.flac")
    _seed(5, 103, path="H:\\Music\\Artist\\Album\\02 - Two.flac")

    resp = server.app.test_client().get(
        f"/dashboard/v1/library-quality?apikey={VALID_KEY}"
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    files = payload["albums"][0]["sample_files"]
    names = {f["filename"] for f in files}
    assert names == {"01 - One.flac", "02 - Two.flac"}
    serialized = resp.get_data(as_text=True)
    assert "/mnt/music" not in serialized
    assert "H:\\Music" not in serialized


def test_library_quality_includes_scan_progress(monkeypatch):
    _fresh(monkeypatch)
    _seed(6, 104)
    run = state_db.enqueue_library_scan(mode="cheap")
    state_db.update_library_scan_run_state(
        run["id"],
        "running",
        totals={"total_items": 10, "processed_items": 4, "measured_items": 3},
    )

    resp = server.app.test_client().get(
        f"/dashboard/v1/library-quality?apikey={VALID_KEY}"
    )

    assert resp.status_code == 200
    active = resp.get_json()["scan"]["active"]
    assert active["id"] == run["id"]
    assert active["mode"] == "cheap"
    assert active["processed_items"] == 4
    assert active["percent"] == 40.0


def test_library_quality_partial_renders(monkeypatch):
    _fresh(monkeypatch)
    _seed(7, 105, bit_depth=16, sample_rate=44100)

    resp = server.app.test_client().get(
        f"/dashboard/v1/library-quality/partial?apikey={VALID_KEY}"
    )

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Library quality" in body
    assert "lossy / low tier" in body.lower()
    assert "07.flac" in body
