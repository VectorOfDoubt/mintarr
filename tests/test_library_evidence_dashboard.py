"""Tests for measured library evidence in the record-detail payload (F5.4 slice 2)."""

from __future__ import annotations

import dashboard
import state_db


def _seed(trackfile_id, album_id, **over):
    row = {
        "trackfile_id": trackfile_id,
        "album_id": album_id,
        "path": f"/lib/Artist/Album/{trackfile_id}.flac",
        "status": "measured",
        "codec": "flac",
        "sample_rate": 96000,
        "bit_depth": 24,
        "lossless": True,
        "integrity_ok": True,
    }
    row.update(over)
    state_db.upsert_library_evidence(row)


def test_available_false_without_albums():
    assert dashboard._library_evidence_detail({"album_ids": []})["available"] is False


def test_available_false_when_no_rows():
    assert (
        dashboard._library_evidence_detail({"album_ids": [12345]})["available"] is False
    )


def test_surfaces_measured_tracks_basename_only():
    _seed(11, 900, path="/lib/Artist/Album/track11.flac")
    _seed(12, 900, status="unmeasured", reason="no audio stream", codec=None)

    detail = dashboard._library_evidence_detail({"album_ids": [900]})

    assert detail["available"] is True
    assert detail["track_count"] == 2
    assert detail["measured_count"] == 1
    names = {t["filename"] for t in detail["tracks"]}
    assert "track11.flac" in names
    # secret-safety: only basenames, never the full mount path
    assert all("/" not in (t["filename"] or "") for t in detail["tracks"])
    measured = next(t for t in detail["tracks"] if t["filename"] == "track11.flac")
    assert measured["lossless"] is True
    assert measured["bit_depth"] == 24


def test_windows_path_basename_only():
    # An unmeasured row may carry the raw Lidarr path; a Windows path must still
    # be reduced to its basename (no drive, no backslashes) — the secret-safety
    # promise must hold regardless of separator.
    _seed(
        31,
        902,
        status="unmeasured",
        reason="library not mounted",
        codec=None,
        path="H:\\Music\\Artist\\Album\\01 - Song.flac",
    )
    detail = dashboard._library_evidence_detail({"album_ids": [902]})
    name = detail["tracks"][0]["filename"]
    assert name == "01 - Song.flac"
    assert "\\" not in name and ":" not in name


def test_basename_any_handles_both_separators():
    assert dashboard._basename_any("/a/b/c.flac") == "c.flac"
    assert dashboard._basename_any("H:\\a\\b\\c.flac") == "c.flac"
    assert dashboard._basename_any("") is None
    assert dashboard._basename_any(None) is None


def test_lossy_track_flagged_not_lossless():
    _seed(21, 901, codec="mp3", lossless=False, integrity_ok=None, bit_depth=None)
    detail = dashboard._library_evidence_detail({"album_ids": [901]})
    t = detail["tracks"][0]
    assert t["lossless"] is False
