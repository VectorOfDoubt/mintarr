"""Tests for measured-existing decision adjustment (F5.4 slice 3b)."""

from __future__ import annotations

import library_evidence
import server
import state_db


def test_flag_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MINTARR_MEASURED_EXISTING", raising=False)
    assert server._measured_existing_enabled() is False


# ---- candidate vector construction ----


def test_candidate_quality_from_clean_flac():
    c = server._candidate_quality(
        files=[{"bit_depth": 24, "sample_rate": 96000}],
        normalized_verdict="AUTHENTIC",
        overrides=[],
        new_track_count=10,
        expected_track_count=10,
    )
    assert c.valid is True
    assert c.authentic is True
    assert c.tier == 3
    assert c.complete is True


def test_candidate_quality_codec_mismatch_is_invalid():
    c = server._candidate_quality(
        files=[{"bit_depth": 16, "sample_rate": 44100}],
        normalized_verdict="AUTHENTIC",
        overrides=["codec_mismatch"],
        new_track_count=10,
        expected_track_count=10,
    )
    assert c.valid is False


def test_candidate_quality_fake_hi_res_is_inauthentic():
    c = server._candidate_quality(
        files=[{"bit_depth": 24, "sample_rate": 96000}],
        normalized_verdict="SUSPICIOUS",
        overrides=["fake_hi_res"],
        new_track_count=10,
        expected_track_count=10,
    )
    assert c.authentic is False


def test_candidate_quality_tier_is_weakest_file():
    c = server._candidate_quality(
        files=[
            {"bit_depth": 24, "sample_rate": 96000},  # tier 3
            {"bit_depth": 16, "sample_rate": 44100},  # tier 1
        ],
        normalized_verdict="AUTHENTIC",
        overrides=[],
        new_track_count=10,
        expected_track_count=10,
    )
    assert c.tier == 1


# ---- verdict mapping (default-safe) ----


def test_map_block_stays_block():
    assert server._map_measured_verdict("BLOCK", "downgrade") == "BLOCK"


def test_map_downgrade_routes_accept_to_review():
    assert server._map_measured_verdict("ACCEPT", "downgrade") == "REVIEW_REQUIRED"
    assert (
        server._map_measured_verdict("ACCEPT_PROVISIONAL", "review")
        == "REVIEW_REQUIRED"
    )


def test_map_upgrade_rescues_review():
    assert (
        server._map_measured_verdict("REVIEW_REQUIRED", "upgrade")
        == "ACCEPT_PROVISIONAL"
    )


def test_map_equivalent_and_accept_unchanged():
    assert server._map_measured_verdict("ACCEPT", "equivalent") == "ACCEPT"
    assert server._map_measured_verdict("ACCEPT", "upgrade") == "ACCEPT"


# ---- full adjustment over the index (only FRESH measured rows may decide) ----


def _seed_real(tmp_path, album_id, bit_depth, sample_rate, *, trackfile_id):
    """Seed a fresh measured row backed by a real file under the mount."""
    f = tmp_path / f"{trackfile_id}.flac"
    f.write_bytes(b"X" * 100)
    st = f.stat()
    state_db.upsert_library_evidence(
        {
            "trackfile_id": trackfile_id,
            "album_id": album_id,
            "path": str(f),
            "size": st.st_size,
            "mtime": st.st_mtime,
            "status": "measured",
            "codec": "flac",
            "bit_depth": bit_depth,
            "sample_rate": sample_rate,
            "lossless": True,
            "integrity_ok": True,
            "sensor_version": library_evidence.SENSOR_VERSION,
        }
    )
    return f


def _apply(
    decision, album_ids, *, cand_bits, cand_rate, new_tc=10, exist_tc=10, exp_tc=10
):
    return server._apply_measured_existing(
        decision,
        album_ids=album_ids,
        files=[{"bit_depth": cand_bits, "sample_rate": cand_rate}],
        normalized_verdict="AUTHENTIC",
        overrides=[],
        new_track_count=new_tc,
        existing_track_count=exist_tc,
        expected_track_count=exp_tc,
    )


def test_no_measured_evidence_leaves_decision_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("MINTARR_LIBRARY_ROOT", str(tmp_path))
    assert _apply("ACCEPT", [424242], cand_bits=16, cand_rate=44100) == "ACCEPT"


def test_lower_tier_candidate_routes_accept_to_review(monkeypatch, tmp_path):
    monkeypatch.setenv("MINTARR_LIBRARY_ROOT", str(tmp_path))
    _seed_real(tmp_path, 810, 24, 96000, trackfile_id=8101)  # existing tier 3
    assert _apply("ACCEPT", [810], cand_bits=16, cand_rate=44100) == "REVIEW_REQUIRED"


def test_higher_tier_candidate_rescues_review(monkeypatch, tmp_path):
    monkeypatch.setenv("MINTARR_LIBRARY_ROOT", str(tmp_path))
    _seed_real(tmp_path, 811, 16, 44100, trackfile_id=8111)  # existing tier 1
    assert (
        _apply("REVIEW_REQUIRED", [811], cand_bits=24, cand_rate=96000)
        == "ACCEPT_PROVISIONAL"
    )


def test_same_tier_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("MINTARR_LIBRARY_ROOT", str(tmp_path))
    _seed_real(tmp_path, 812, 16, 44100, trackfile_id=8121)
    assert _apply("ACCEPT", [812], cand_bits=16, cand_rate=44100) == "ACCEPT"


def test_mount_unconfigured_falls_back_to_label(monkeypatch, tmp_path):
    # Fresh row exists, but the library is not mounted now → must NOT decide on it.
    monkeypatch.setenv("MINTARR_LIBRARY_ROOT", str(tmp_path))
    _seed_real(tmp_path, 813, 24, 96000, trackfile_id=8131)  # tier 3
    monkeypatch.delenv("MINTARR_LIBRARY_ROOT", raising=False)
    assert _apply("ACCEPT", [813], cand_bits=16, cand_rate=44100) == "ACCEPT"


def test_stale_row_falls_back_to_label(monkeypatch, tmp_path):
    # Row's stored size/mtime no longer match the file (it changed) → not fresh.
    monkeypatch.setenv("MINTARR_LIBRARY_ROOT", str(tmp_path))
    f = _seed_real(tmp_path, 814, 24, 96000, trackfile_id=8141)  # tier 3
    f.write_bytes(b"Y" * 5000)  # size + mtime now differ from the stored row
    assert _apply("ACCEPT", [814], cand_bits=16, cand_rate=44100) == "ACCEPT"


def test_candidate_tier_unknown_when_no_file_metadata():
    c = server._candidate_quality(
        files=[{"bit_depth": None, "sample_rate": None}],
        normalized_verdict="AUTHENTIC",
        overrides=[],
        new_track_count=10,
        expected_track_count=10,
    )
    assert c.tier_known is False


def test_candidate_tier_known_with_metadata():
    c = server._candidate_quality(
        files=[{"bit_depth": 24, "sample_rate": 96000}],
        normalized_verdict="AUTHENTIC",
        overrides=[],
        new_track_count=10,
        expected_track_count=10,
    )
    assert c.tier_known is True
