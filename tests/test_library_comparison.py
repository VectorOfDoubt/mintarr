"""Tests for the F5.4 quality-vector comparison + album rollup (pure, slice 3a)."""

from __future__ import annotations

import library_comparison as lc


def _row(**over):
    base = {
        "status": "measured",
        "lossless": True,
        "bit_depth": 16,
        "sample_rate": 44100,
        "integrity_ok": True,
    }
    base.update(over)
    return base


# ---- tier_rank ----


def test_tier_rank_orders_lossless_tiers():
    assert lc.tier_rank(lossless=False, bit_depth=None, sample_rate=None) == 0
    assert lc.tier_rank(lossless=True, bit_depth=16, sample_rate=44100) == 1
    assert lc.tier_rank(lossless=True, bit_depth=24, sample_rate=48000) == 2
    assert lc.tier_rank(lossless=True, bit_depth=24, sample_rate=96000) == 3


# ---- album_quality rollup ----


def test_album_quality_none_without_measured():
    assert lc.album_quality([]) is None
    assert lc.album_quality([{"status": "unmeasured"}]) is None


def test_album_quality_min_tier_is_weakest_track():
    rows = [
        _row(bit_depth=24, sample_rate=96000),  # tier 3
        _row(bit_depth=16, sample_rate=44100),  # tier 1
    ]
    q = lc.album_quality(rows)
    assert q.min_tier == 1  # only as good as the weakest
    assert q.all_lossless is True
    assert q.all_valid is True
    assert q.any_invalid is False


def test_album_quality_flags_invalid_track():
    q = lc.album_quality([_row(), _row(integrity_ok=False)])
    assert q.any_invalid is True
    assert q.all_valid is False


def test_album_quality_counts_unmeasured_in_total():
    q = lc.album_quality([_row(), {"status": "unmeasured"}])
    assert q.measured_count == 1
    assert q.track_count == 2


# ---- compare (precedence) ----


def _cand(valid=True, authentic=True, tier=1, complete=True):
    return lc.CandidateQuality(
        valid=valid, authentic=authentic, tier=tier, complete=complete
    )


def test_compare_no_existing_is_review():
    assert lc.compare(_cand(), None) == lc.REVIEW


def test_compare_fixes_invalid_existing_is_upgrade():
    existing = lc.album_quality([_row(integrity_ok=False)])
    assert lc.compare(_cand(tier=1), existing) == lc.UPGRADE


def test_compare_invalid_candidate_is_downgrade():
    existing = lc.album_quality([_row()])
    assert lc.compare(_cand(valid=False), existing) == lc.DOWNGRADE


def test_compare_fake_candidate_is_review():
    existing = lc.album_quality([_row()])
    assert lc.compare(_cand(authentic=False), existing) == lc.REVIEW


def test_compare_higher_tier_is_upgrade():
    existing = lc.album_quality([_row(bit_depth=16, sample_rate=44100)])  # tier 1
    assert lc.compare(_cand(tier=3), existing) == lc.UPGRADE


def test_compare_lower_tier_is_downgrade():
    existing = lc.album_quality([_row(bit_depth=24, sample_rate=96000)])  # tier 3
    assert lc.compare(_cand(tier=1), existing) == lc.DOWNGRADE


def test_compare_same_tier_is_equivalent():
    existing = lc.album_quality([_row(bit_depth=16, sample_rate=44100)])  # tier 1
    assert lc.compare(_cand(tier=1), existing) == lc.EQUIVALENT


def test_compare_same_tier_completes_partial_is_upgrade():
    # existing measured 1 of 2 tracks; same tier candidate that is complete wins.
    existing = lc.AlbumQuality(
        measured_count=1,
        track_count=2,
        all_valid=True,
        any_invalid=False,
        min_tier=1,
        all_lossless=True,
    )
    assert lc.compare(_cand(tier=1, complete=True), existing) == lc.UPGRADE
