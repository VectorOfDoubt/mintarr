"""Tests for read-only library evidence measurement + storage (F5.4 slice 1)."""

from __future__ import annotations

import os

import library_evidence as le
import state_db


def _fake_prober(**probe):
    def _p(path):
        return {
            "codec": probe.get("codec", "flac"),
            "sample_rate": probe.get("sample_rate", 44100),
            "bit_depth": probe.get("bit_depth", 16),
            "channels": probe.get("channels", 2),
            "integrity_ok": probe.get("integrity_ok", True),
        }

    return _p


def _lib(tmp_path):
    root = tmp_path / "library"
    (root / "Artist" / "Album").mkdir(parents=True)
    f = root / "Artist" / "Album" / "01.flac"
    f.write_bytes(b"FLACDATA")
    return str(root)


# ---- path mapping + containment ----


def test_resolve_maps_lidarr_prefix(tmp_path):
    root = _lib(tmp_path)
    mapped, reason = le.resolve_library_path(
        "/music/Artist/Album/01.flac", library_root=root, lidarr_root="/music"
    )
    assert reason is None
    assert mapped == (tmp_path / "library" / "Artist" / "Album" / "01.flac")


def test_resolve_rejects_path_outside_lidarr_root(tmp_path):
    root = _lib(tmp_path)
    mapped, reason = le.resolve_library_path(
        "/other/Artist/Album/01.flac", library_root=root, lidarr_root="/music"
    )
    assert mapped is None
    assert "outside" in reason


def test_resolve_rejects_traversal(tmp_path):
    root = _lib(tmp_path)
    mapped, reason = le.resolve_library_path(
        "/music/../../etc/passwd", library_root=root, lidarr_root="/music"
    )
    assert mapped is None
    assert reason  # escaped root or not found


def test_resolve_rejects_symlink(tmp_path):
    root = _lib(tmp_path)
    outside = tmp_path / "outside.flac"
    outside.write_bytes(b"x")
    link = tmp_path / "library" / "Artist" / "Album" / "linked.flac"
    os.symlink(outside, link)
    mapped, reason = le.resolve_library_path(
        "/music/Artist/Album/linked.flac", library_root=root, lidarr_root="/music"
    )
    assert mapped is None
    assert "symlink" in reason or "escapes" in reason


def test_resolve_requires_lidarr_root(tmp_path):
    # Empty lidarr root must NOT map (wrong measurement is worse than none).
    root = _lib(tmp_path)
    mapped, reason = le.resolve_library_path(
        "/music/Artist/Album/01.flac", library_root=root, lidarr_root=""
    )
    assert mapped is None
    assert "lidarr root not configured" in reason


def test_resolve_missing_file(tmp_path):
    root = _lib(tmp_path)
    mapped, reason = le.resolve_library_path(
        "/music/Artist/Album/missing.flac", library_root=root, lidarr_root="/music"
    )
    assert mapped is None
    assert "not found" in reason


# ---- measurement ----


def test_measure_unmounted_is_unmeasured(monkeypatch):
    monkeypatch.delenv("MINTARR_LIBRARY_ROOT", raising=False)
    m = le.measure_trackfile("/music/x.flac")
    assert m.status == "unmeasured"
    assert "not mounted" in m.reason


def test_measure_flac_records_quality_vector(tmp_path):
    root = _lib(tmp_path)
    m = le.measure_trackfile(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="/music",
        prober=_fake_prober(codec="flac", sample_rate=96000, bit_depth=24),
    )
    assert m.status == "measured"
    assert m.codec == "flac"
    assert m.sample_rate == 96000
    assert m.bit_depth == 24
    assert m.lossless is True
    assert m.integrity_ok is True


def test_measure_lossy_is_not_lossless(tmp_path):
    root = _lib(tmp_path)
    m = le.measure_trackfile(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="/music",
        prober=_fake_prober(codec="mp3", integrity_ok=None),
    )
    assert m.status == "measured"
    assert m.lossless is False


def test_measure_no_audio_stream_is_unmeasured(tmp_path):
    # ffprobe returning no codec must be unmeasured, not measured-with-empty-vector.
    root = _lib(tmp_path)

    def _no_audio(path):
        return {"codec": "", "sample_rate": None, "bit_depth": None, "channels": None}

    m = le.measure_trackfile(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="/music",
        prober=_no_audio,
    )
    assert m.status == "unmeasured"
    assert "no audio stream" in m.reason


def test_measure_requires_lidarr_root(tmp_path):
    root = _lib(tmp_path)
    m = le.measure_trackfile(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="",
        prober=_fake_prober(),
    )
    assert m.status == "unmeasured"
    assert "lidarr root not configured" in m.reason


def test_measure_prober_failure_is_unmeasured(tmp_path):
    root = _lib(tmp_path)

    def _boom(path):
        raise RuntimeError("ffprobe blew up")

    m = le.measure_trackfile(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="/music",
        prober=_boom,
    )
    assert m.status == "unmeasured"
    assert "probe failed" in m.reason


# ---- storage ----


def test_library_evidence_storage_round_trip():
    state_db.upsert_library_evidence(
        {
            "trackfile_id": 42,
            "album_id": 7,
            "path": "/lib/a.flac",
            "size": 123,
            "mtime": 1.0,
            "status": "measured",
            "codec": "flac",
            "sample_rate": 96000,
            "bit_depth": 24,
            "channels": 2,
            "lossless": True,
            "integrity_ok": True,
            "sensor_version": le.SENSOR_VERSION,
            "evidence": {"x": 1},
        }
    )
    row = state_db.get_library_evidence(42)
    assert row["album_id"] == 7
    assert row["lossless"] == 1
    assert row["codec"] == "flac"
    assert state_db.get_album_library_evidence(7)[0]["trackfile_id"] == 42


def test_library_evidence_upsert_replaces():
    state_db.upsert_library_evidence(
        {"trackfile_id": 99, "album_id": 1, "status": "measured", "bit_depth": 16}
    )
    state_db.upsert_library_evidence(
        {"trackfile_id": 99, "album_id": 1, "status": "measured", "bit_depth": 24}
    )
    assert state_db.get_library_evidence(99)["bit_depth"] == 24


# ---- is_measured_row_fresh (F5.4 slice 3b decision-time guard) ----


def _fresh_row(tmp_path, monkeypatch):
    monkeypatch.setenv("MINTARR_LIBRARY_ROOT", str(tmp_path))
    f = tmp_path / "track.flac"
    f.write_bytes(b"DATA")
    st = f.stat()
    return {
        "status": "measured",
        "sensor_version": le.SENSOR_VERSION,
        "path": str(f),
        "size": st.st_size,
        "mtime": st.st_mtime,
    }


def test_fresh_row_is_fresh(tmp_path, monkeypatch):
    assert le.is_measured_row_fresh(_fresh_row(tmp_path, monkeypatch)) is True


def test_unmounted_is_not_fresh(tmp_path, monkeypatch):
    row = _fresh_row(tmp_path, monkeypatch)
    monkeypatch.delenv("MINTARR_LIBRARY_ROOT", raising=False)
    assert le.is_measured_row_fresh(row) is False


def test_old_sensor_version_is_not_fresh(tmp_path, monkeypatch):
    row = _fresh_row(tmp_path, monkeypatch)
    row["sensor_version"] = "mintarr-library-evidence OLD"
    assert le.is_measured_row_fresh(row) is False


def test_changed_file_is_not_fresh(tmp_path, monkeypatch):
    row = _fresh_row(tmp_path, monkeypatch)
    (tmp_path / "track.flac").write_bytes(b"DATA-CHANGED-bigger")  # size/mtime differ
    assert le.is_measured_row_fresh(row) is False


def test_unmeasured_row_is_not_fresh(tmp_path, monkeypatch):
    row = _fresh_row(tmp_path, monkeypatch)
    row["status"] = "unmeasured"
    assert le.is_measured_row_fresh(row) is False
