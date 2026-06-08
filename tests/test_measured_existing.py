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
            "sensor_version": library_evidence.METADATA_SENSOR_VERSION,
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


def test_candidate_tier_unknown_when_any_file_lacks_metadata():
    # Album tier = weakest track, so one metadata-less file makes the tier unknown.
    c = server._candidate_quality(
        files=[
            {"bit_depth": 24, "sample_rate": 96000},  # known
            {"bit_depth": None, "sample_rate": None},  # unknown
        ],
        normalized_verdict="AUTHENTIC",
        overrides=[],
        new_track_count=10,
        expected_track_count=10,
    )
    assert c.tier_known is False


def test_candidate_tier_unknown_with_partial_metadata():
    # sample rate present but bit depth missing → not trustworthy (would assume 16-bit).
    c = server._candidate_quality(
        files=[{"bit_depth": None, "sample_rate": 96000}],
        normalized_verdict="AUTHENTIC",
        overrides=[],
        new_track_count=10,
        expected_track_count=10,
    )
    assert c.tier_known is False


def test_candidate_tier_known_when_all_files_complete():
    c = server._candidate_quality(
        files=[
            {"bit_depth": 24, "sample_rate": 96000},
            {"bit_depth": 16, "sample_rate": 44100},
        ],
        normalized_verdict="AUTHENTIC",
        overrides=[],
        new_track_count=10,
        expected_track_count=10,
    )
    assert c.tier_known is True


# ---- F5.4 slice 4b: existing authenticity plumbed through _apply_measured_existing ----


def test_existing_fake_rescues_review_only_when_spectral_on(monkeypatch, tmp_path):
    # Existing is a measured-fake hi-res (nominal tier 3); candidate is genuine
    # tier 1. With spectral OFF the fake tier is trusted (DOWNGRADE → review stays
    # review). With spectral ON, any_fake → UPGRADE lifts review to provisional.
    monkeypatch.setenv("MINTARR_LIBRARY_ROOT", str(tmp_path))
    _seed_real(tmp_path, 820, 24, 96000, trackfile_id=8201)  # nominal tier 3
    state_db.update_library_spectral(
        {
            "trackfile_id": 8201,
            "album_id": 820,
            "authentic": False,
            "spectral_status": "measured",
            "spectral_verdict": "FAKE",
            "spectral_sensor_version": library_evidence.SPECTRAL_SENSOR_VERSION,
        }
    )
    monkeypatch.delenv("MINTARR_LIBRARY_SPECTRAL", raising=False)
    assert (
        _apply("REVIEW_REQUIRED", [820], cand_bits=16, cand_rate=44100)
        == "REVIEW_REQUIRED"
    )
    monkeypatch.setenv("MINTARR_LIBRARY_SPECTRAL", "true")
    assert (
        _apply("REVIEW_REQUIRED", [820], cand_bits=16, cand_rate=44100)
        == "ACCEPT_PROVISIONAL"
    )


def test_unknown_existing_authenticity_routes_to_review_only_when_spectral_on(
    monkeypatch, tmp_path
):
    # Existing measured but NOT spectrally verified, same tier as candidate. Off:
    # ACCEPT stays ACCEPT. On: unverified existing tier → abstain to review.
    monkeypatch.setenv("MINTARR_LIBRARY_ROOT", str(tmp_path))
    _seed_real(
        tmp_path, 821, 16, 44100, trackfile_id=8211
    )  # tier 1, no spectral verdict
    monkeypatch.delenv("MINTARR_LIBRARY_SPECTRAL", raising=False)
    assert _apply("ACCEPT", [821], cand_bits=16, cand_rate=44100) == "ACCEPT"
    monkeypatch.setenv("MINTARR_LIBRARY_SPECTRAL", "true")
    assert _apply("ACCEPT", [821], cand_bits=16, cand_rate=44100) == "REVIEW_REQUIRED"


def test_verified_genuine_existing_keeps_normal_behaviour(monkeypatch, tmp_path):
    # Spectral ON but existing is verified genuine same tier → no abstain (ACCEPT).
    monkeypatch.setenv("MINTARR_LIBRARY_ROOT", str(tmp_path))
    monkeypatch.setenv("MINTARR_LIBRARY_SPECTRAL", "true")
    _seed_real(tmp_path, 822, 16, 44100, trackfile_id=8221)
    state_db.update_library_spectral(
        {
            "trackfile_id": 8221,
            "album_id": 822,
            "authentic": True,
            "spectral_status": "measured",
            "spectral_verdict": "AUTHENTIC",
            "spectral_sensor_version": library_evidence.SPECTRAL_SENSOR_VERSION,
        }
    )
    assert _apply("ACCEPT", [822], cand_bits=16, cand_rate=44100) == "ACCEPT"


# ---- F5.4 slice 4b: stale spectral verdict must not be decision-active (#118) ----


def _seed_spectral(trackfile_id, album_id, authentic, *, version=None):
    state_db.update_library_spectral(
        {
            "trackfile_id": trackfile_id,
            "album_id": album_id,
            "authentic": authentic,
            "spectral_status": "measured",
            "spectral_verdict": "FAKE" if authentic == 0 else "AUTHENTIC",
            "spectral_sensor_version": version
            or library_evidence.SPECTRAL_SENSOR_VERSION,
        }
    )


def test_fresh_fake_rescues_review(monkeypatch, tmp_path):
    # Baseline partner for the stale tests: a *fresh* fake existing release lifts a
    # review to provisional (candidate genuine, existing measured-fake hi-res).
    monkeypatch.setenv("MINTARR_LIBRARY_ROOT", str(tmp_path))
    monkeypatch.setenv("MINTARR_LIBRARY_SPECTRAL", "true")
    _seed_real(tmp_path, 830, 24, 96000, trackfile_id=8301)  # nominal tier 3
    _seed_spectral(8301, 830, 0)  # fresh fake
    assert (
        _apply("REVIEW_REQUIRED", [830], cand_bits=16, cand_rate=44100)
        == "ACCEPT_PROVISIONAL"
    )


def test_stale_spectral_version_fake_is_ignored(monkeypatch, tmp_path):
    # Cheap tier is fresh, but the spectral verdict was produced by an older
    # spectral sensor version. The stale fake must not drive an UPGRADE: it is
    # stripped, the existing reads as unverified, and a lower-tier candidate
    # abstains to review instead of rescuing it.
    monkeypatch.setenv("MINTARR_LIBRARY_ROOT", str(tmp_path))
    monkeypatch.setenv("MINTARR_LIBRARY_SPECTRAL", "true")
    _seed_real(
        tmp_path, 831, 24, 96000, trackfile_id=8311
    )  # nominal tier 3, cheap-fresh
    _seed_spectral(8311, 831, 0, version="mintarr-library-spectral OLD")
    # Not rescued to provisional (stale fake ignored) — stays review (abstain).
    assert (
        _apply("REVIEW_REQUIRED", [831], cand_bits=16, cand_rate=44100)
        == "REVIEW_REQUIRED"
    )


def test_changed_file_drops_stale_fake_entirely(monkeypatch, tmp_path):
    # Changing the file invalidates BOTH sensors (shared size/mtime): the whole row
    # drops from the fresh set, so a stale fake can never rescue a review.
    monkeypatch.setenv("MINTARR_LIBRARY_ROOT", str(tmp_path))
    monkeypatch.setenv("MINTARR_LIBRARY_SPECTRAL", "true")
    f = _seed_real(tmp_path, 832, 24, 96000, trackfile_id=8321)
    _seed_spectral(8321, 832, 0)  # fresh-looking fake at seed time
    f.write_bytes(b"Z" * 9000)  # size + mtime now differ → row no longer fresh
    assert (
        _apply("REVIEW_REQUIRED", [832], cand_bits=16, cand_rate=44100)
        == "REVIEW_REQUIRED"
    )


def test_stale_spectral_strip_preserves_cheap_tier(monkeypatch, tmp_path):
    # Stripping stale authenticity must keep the fresh cheap-tier evidence: a
    # genuinely higher-tier candidate still upgrades on tier alone.
    monkeypatch.setenv("MINTARR_LIBRARY_ROOT", str(tmp_path))
    monkeypatch.setenv("MINTARR_LIBRARY_SPECTRAL", "true")
    _seed_real(tmp_path, 833, 16, 44100, trackfile_id=8331)  # cheap-fresh tier 1
    _seed_spectral(
        8331, 833, 1, version="mintarr-library-spectral OLD"
    )  # stale genuine
    # Tier still readable from the fresh cheap row → higher candidate rescues review.
    assert (
        _apply("REVIEW_REQUIRED", [833], cand_bits=24, cand_rate=96000)
        == "ACCEPT_PROVISIONAL"
    )
