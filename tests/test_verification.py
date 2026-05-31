"""Unit tests for the pure V2 verification policy."""

from __future__ import annotations

import json

import pytest

from verification import (
    VerificationResult,
    apply_overrides,
    compute_components,
    decide,
)


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("AUTHENTIC", 35),
        ("WARNING", 18),
        ("SUSPICIOUS", 5),
        ("FAKE_CERTAIN", 0),
        ("FAKE", 0),
        ("UNKNOWN", 0),
    ],
)
def test_compute_components_detective_scores(verdict, expected):
    components = compute_components(True, True, verdict, True)

    assert components["detective"] == expected


@pytest.mark.parametrize(
    ("ffprobe_ok", "flac_t_ok", "complete_album", "expected"),
    [
        (True, True, True, {"ffprobe": 25, "flac_t": 25, "complete": 15}),
        (False, True, True, {"ffprobe": 0, "flac_t": 25, "complete": 15}),
        (True, False, True, {"ffprobe": 25, "flac_t": 0, "complete": 15}),
        (True, True, False, {"ffprobe": 25, "flac_t": 25, "complete": 0}),
        (False, False, False, {"ffprobe": 0, "flac_t": 0, "complete": 0}),
    ],
)
def test_compute_components_objective_components(ffprobe_ok, flac_t_ok, complete_album, expected):
    components = compute_components(ffprobe_ok, flac_t_ok, "AUTHENTIC", complete_album)

    for key, value in expected.items():
        assert components[key] == value


def test_compute_components_max_score_is_100():
    components = compute_components(True, True, "AUTHENTIC", True)

    assert sum(components.values()) == 100


@pytest.mark.parametrize(
    ("kwargs", "expected_override"),
    [
        ({"codec_mismatch": True, "flac_t_failed": False, "validator_error": False}, "codec_mismatch"),
        ({"codec_mismatch": False, "flac_t_failed": True, "validator_error": False}, "flac_t_fail"),
        ({"codec_mismatch": False, "flac_t_failed": False, "validator_error": True}, "validator_error"),
    ],
)
def test_apply_overrides_hard_overrides_force_zero(kwargs, expected_override):
    score, overrides = apply_overrides(
        {"ffprobe": 25, "flac_t": 25, "detective": 35, "complete": 15},
        fake_hi_res=True,
        detective_verdict="AUTHENTIC",
        **kwargs,
    )

    assert score == 0
    assert overrides == [expected_override]


def test_apply_overrides_hard_override_precedence():
    score, overrides = apply_overrides(
        {"ffprobe": 25, "flac_t": 25, "detective": 35, "complete": 15},
        codec_mismatch=True,
        flac_t_failed=True,
        validator_error=True,
        fake_hi_res=True,
        detective_verdict="AUTHENTIC",
    )

    assert score == 0
    assert overrides == ["codec_mismatch"]


def test_apply_overrides_suspicious_cap_applies_above_60():
    score, overrides = apply_overrides(
        {"ffprobe": 25, "flac_t": 25, "detective": 5, "complete": 15},
        codec_mismatch=False,
        flac_t_failed=False,
        validator_error=False,
        fake_hi_res=False,
        detective_verdict="SUSPICIOUS",
    )

    assert score == 60
    assert overrides == ["suspicious_cap_60"]


def test_apply_overrides_suspicious_cap_not_added_at_60():
    score, overrides = apply_overrides(
        {"ffprobe": 25, "flac_t": 25, "detective": 5, "complete": 5},
        codec_mismatch=False,
        flac_t_failed=False,
        validator_error=False,
        fake_hi_res=False,
        detective_verdict="SUSPICIOUS",
    )

    assert score == 60
    assert overrides == []


def test_apply_overrides_fake_hi_res_does_not_change_score():
    score, overrides = apply_overrides(
        {"ffprobe": 25, "flac_t": 25, "detective": 35, "complete": 15},
        codec_mismatch=False,
        flac_t_failed=False,
        validator_error=False,
        fake_hi_res=True,
        detective_verdict="AUTHENTIC",
    )

    assert score == 100
    assert overrides == ["fake_hi_res"]


def test_apply_overrides_returns_plain_sum_without_overrides():
    score, overrides = apply_overrides(
        {"ffprobe": 25, "flac_t": 0, "detective": 18, "complete": 15},
        codec_mismatch=False,
        flac_t_failed=False,
        validator_error=False,
        fake_hi_res=False,
        detective_verdict="WARNING",
    )

    assert score == 58
    assert overrides == []


@pytest.mark.parametrize("override", ["codec_mismatch", "flac_t_fail", "validator_error"])
def test_decide_hard_overrides_block(override):
    assert decide(100, 0, 3000, "AUTHENTIC", [override]) == "BLOCK"


def test_decide_fake_hi_res_review_required_regardless_of_score():
    assert decide(100, 0, 3000, "AUTHENTIC", ["fake_hi_res"]) == "REVIEW_REQUIRED"


@pytest.mark.parametrize("verdict", ["FAKE_CERTAIN", "FAKE"])
def test_decide_fake_without_existing_goes_review_required(verdict):
    assert decide(0, 0, 3000, verdict, []) == "REVIEW_REQUIRED"


@pytest.mark.parametrize("verdict", ["FAKE_CERTAIN", "FAKE"])
def test_decide_fake_with_existing_blocks(verdict):
    assert decide(0, 320, 3000, verdict, []) == "BLOCK"


def test_decide_suspicious_existing_flac_blocks():
    assert decide(60, 1411, 3000, "SUSPICIOUS", []) == "BLOCK"


def test_decide_suspicious_existing_near_flac_blocks():
    assert decide(60, 1410, 3000, "SUSPICIOUS", []) == "BLOCK"


def test_decide_suspicious_without_existing_accepts_provisional():
    assert decide(60, 0, 320, "SUSPICIOUS", []) == "ACCEPT_PROVISIONAL"


def test_decide_suspicious_upgrade_accepts_provisional():
    assert decide(60, 128, 320, "SUSPICIOUS", []) == "ACCEPT_PROVISIONAL"


def test_decide_suspicious_same_quality_blocks():
    assert decide(60, 320, 320, "SUSPICIOUS", []) == "BLOCK"


def test_decide_suspicious_strict_20_percent_boundary_blocks():
    assert decide(60, 320, 384, "SUSPICIOUS", []) == "BLOCK"


def test_decide_suspicious_above_20_percent_boundary_accepts_provisional():
    assert decide(60, 320, 385, "SUSPICIOUS", []) == "ACCEPT_PROVISIONAL"


def test_decide_authentic_high_score_accepts():
    assert decide(85, 320, 3000, "AUTHENTIC", []) == "ACCEPT"


def test_decide_authentic_mid_score_accepts_provisional():
    assert decide(50, 320, 3000, "AUTHENTIC", []) == "ACCEPT_PROVISIONAL"


def test_decide_authentic_low_score_blocks():
    assert decide(15, 320, 3000, "AUTHENTIC", []) == "BLOCK"


def test_decide_score_19_blocks():
    assert decide(19, 0, 3000, "WARNING", []) == "BLOCK"


def test_decide_score_20_accepts_provisional():
    assert decide(20, 0, 3000, "WARNING", []) == "ACCEPT_PROVISIONAL"


def test_decide_score_69_accepts_provisional():
    assert decide(69, 0, 3000, "WARNING", []) == "ACCEPT_PROVISIONAL"


def test_decide_score_70_accepts():
    assert decide(70, 0, 3000, "WARNING", []) == "ACCEPT"


def test_decide_warning_score_83_accepts():
    assert decide(83, 0, 3000, "WARNING", []) == "ACCEPT"


def test_decide_is_deterministic_for_same_input():
    args = (60, 128, 320, "SUSPICIOUS", ["suspicious_cap_60"])

    assert decide(*args) == decide(*args)


def _result(**overrides):
    defaults = {
        "jid": "abc123",
        "score": 85,
        "verification_decision": "ACCEPT",
        "import_outcome": "MANUAL_IMPORTED",
        "components": {"ffprobe": 25, "flac_t": 25, "detective": 35},
        "overrides": [],
        "verdict": "AUTHENTIC",
        "new_kbps": 3000,
        "existing_kbps": 320,
        "existing_label": "MP3-320",
        "album_ids": [1, 2],
        "title": "Artist - Album",
        "timestamp": 123.4,
        "timestamp_iso": "2026-05-22T19:30:00",
    }
    defaults.update(overrides)
    return VerificationResult(**defaults)


def test_verification_result_decisions_log_contains_legacy_and_v2_fields():
    record = _result(
        sensors=[{"name": "ffprobe", "status": "pass"}],
        files=[{"filename": "01.flac", "cutoff_hz": 42000}],
    ).to_decisions_log()

    assert record["decision"] == "IMPORTED_AUTHENTIC"
    assert record["reason"] == "upgrade from MP3-320"
    assert record["v2_verification_decision"] == "ACCEPT"
    assert record["v2_import_outcome"] == "MANUAL_IMPORTED"
    assert record["v2_score"] == 85
    assert record["v2_components"] == {"ffprobe": 25, "flac_t": 25, "detective": 35}
    assert record["v2_overrides"] == []
    assert record["sensors"] == [{"name": "ffprobe", "status": "pass"}]
    assert record["files"] == [{"filename": "01.flac", "cutoff_hz": 42000}]
    json.dumps(record)


def test_legacy_decision_rescued_takes_precedence():
    record = _result(import_outcome="RESCUED", verification_decision="BLOCK").to_decisions_log()

    assert record["decision"] == "RESCUED_BY_RESCAN"


@pytest.mark.parametrize(
    ("verification_decision", "verdict", "expected"),
    [
        ("ACCEPT", "WARNING", "IMPORTED_WITH_WARNING"),
        ("ACCEPT_PROVISIONAL", "SUSPICIOUS", "IMPORTED_DESPITE_FAKE"),
        ("BLOCK", "AUTHENTIC", "BLOCKED"),
        ("REVIEW_REQUIRED", "FAKE_CERTAIN", "REVIEW_REQUIRED"),
    ],
)
def test_legacy_decision_label_mappings(verification_decision, verdict, expected):
    record = _result(verification_decision=verification_decision, verdict=verdict).to_decisions_log()

    assert record["decision"] == expected


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (["codec_mismatch", "flac_t_fail"], "codec mismatch"),
        (["flac_t_fail", "validator_error"], "flac -t failed"),
        (["validator_error"], "validator unavailable"),
    ],
)
def test_legacy_reason_override_precedence(overrides, expected):
    record = _result(overrides=overrides).to_decisions_log()

    assert record["reason"] == expected


def test_legacy_reason_no_existing():
    record = _result(existing_kbps=0, existing_label="nothing").to_decisions_log()

    assert record["reason"] == "nothing pre-existing"


def test_legacy_reason_score_fallback():
    record = _result(existing_kbps=320, new_kbps=384, score=69).to_decisions_log()

    assert record["reason"] == "score=69"


# ---- V2.1 completeness rule (added 2026-05-23) ----


def test_completeness_rule_backwards_compatible_without_track_counts():
    """Default track counts = 0 means no-op, all existing logic applies."""
    # AUTHENTIC + low score → BLOCK (unchanged behavior)
    assert decide(15, 0, 3000, "AUTHENTIC", []) == "BLOCK"
    # SUSPICIOUS no upgrade → BLOCK (unchanged)
    assert decide(60, 320, 320, "SUSPICIOUS", []) == "BLOCK"
    # SUSPICIOUS w/ lossless existing → BLOCK (unchanged)
    assert decide(60, 1411, 3000, "SUSPICIOUS", []) == "BLOCK"


def test_completeness_overrides_suspicious_no_kbps_upgrade():
    """existing=5/10 + new=10/10 + SUSPICIOUS no kbps upgrade → ACCEPT_PROVISIONAL."""
    decision = decide(
        60, 320, 320, "SUSPICIOUS", [],
        existing_track_count=5, new_track_count=10, expected_track_count=10,
    )
    assert decision == "ACCEPT_PROVISIONAL"


def test_completeness_overrides_lossless_protection_when_incomplete():
    """existing=5/10 lossless + new=10/10 SUSPICIOUS → ACCEPT_PROVISIONAL (completeness wins).

    Critical case: even when existing is real lossless, an incomplete album can
    be replaced by a complete-but-fake-hi-res candidate because completeness
    matters more than per-track quality (per maintainer rule 2026-05-23).
    """
    decision = decide(
        60, 1411, 3000, "SUSPICIOUS", [],
        existing_track_count=5, new_track_count=10, expected_track_count=10,
    )
    assert decision == "ACCEPT_PROVISIONAL"


def test_completeness_overrides_low_score_when_incomplete():
    """Low score (<20) gets ACCEPT_PROVISIONAL when completeness improves."""
    decision = decide(
        15, 320, 320, "WARNING", [],
        existing_track_count=3, new_track_count=10, expected_track_count=10,
    )
    assert decision == "ACCEPT_PROVISIONAL"


def test_completeness_fake_certain_with_existing_goes_to_review():
    """FAKE_CERTAIN + existing>0 normally BLOCK, but completeness routes to REVIEW."""
    decision = decide(
        0, 1411, 0, "FAKE_CERTAIN", [],
        existing_track_count=5, new_track_count=10, expected_track_count=10,
    )
    assert decision == "REVIEW_REQUIRED"


def test_completeness_does_not_override_fake_hi_res():
    """fake_hi_res always REVIEW_REQUIRED — completeness does not change that."""
    decision = decide(
        100, 320, 3000, "AUTHENTIC", ["fake_hi_res"],
        existing_track_count=5, new_track_count=10, expected_track_count=10,
    )
    assert decision == "REVIEW_REQUIRED"


def test_completeness_does_not_override_hard_overrides():
    """codec_mismatch / flac_t_fail / validator_error alltid BLOCK uavhengig."""
    for hard in ("codec_mismatch", "flac_t_fail", "validator_error"):
        decision = decide(
            0, 320, 320, "AUTHENTIC", [hard],
            existing_track_count=5, new_track_count=10, expected_track_count=10,
        )
        assert decision == "BLOCK", f"{hard} should still BLOCK"


def test_completeness_no_advantage_when_existing_complete():
    """existing=10/10 + new=10/10 + SUSPICIOUS no kbps upgrade → BLOCK (no completeness gain)."""
    decision = decide(
        60, 320, 320, "SUSPICIOUS", [],
        existing_track_count=10, new_track_count=10, expected_track_count=10,
    )
    assert decision == "BLOCK"


def test_completeness_partial_improvement_does_not_trigger():
    """existing=5/10 + new=7/10 (still incomplete) → no completeness rule, existing logic."""
    decision = decide(
        60, 320, 320, "SUSPICIOUS", [],
        existing_track_count=5, new_track_count=7, expected_track_count=10,
    )
    assert decision == "BLOCK"  # neither kbps upgrade nor full completeness


def test_completeness_when_existing_already_zero_kbps():
    """existing_kbps=0 (nothing pre-existing) — completeness rule redundant, score-based."""
    # AUTHENTIC + high score + nothing existing → ACCEPT regardless of track counts
    decision = decide(
        85, 0, 3000, "AUTHENTIC", [],
        existing_track_count=0, new_track_count=10, expected_track_count=10,
    )
    assert decision == "ACCEPT"


def test_completeness_deluxe_edition_extra_tracks():
    """new=12 tracks vs expected=10 (deluxe edition) — counts as complete upgrade if existing<10."""
    decision = decide(
        60, 1411, 3000, "SUSPICIOUS", [],
        existing_track_count=5, new_track_count=12, expected_track_count=10,
    )
    assert decision == "ACCEPT_PROVISIONAL"
