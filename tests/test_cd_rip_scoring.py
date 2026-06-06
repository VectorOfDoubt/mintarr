"""Tests for opt-in CD-rip decision scoring (F5.3 slice 4)."""

from __future__ import annotations

import cd_rip_evidence as cre
import server


def _ev(status, *, accurate=False, log="rip.log"):
    return cre.CdRipEvidence(
        detected=True,
        status=status,
        summary="x",
        ripper="eac",
        log_filename=log,
        accuraterip=cre.AccurateRipResult(
            present=accurate, accurate=accurate, matched=2 if accurate else 0, total=2
        ),
    )


def test_scoring_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MINTARR_CD_RIP_SCORING", raising=False)
    assert server._cd_rip_scoring_enabled() is False


def test_verified_rip_rescues_review_to_provisional():
    ev = _ev("pass", accurate=True)
    assert server._apply_cd_rip_scoring("REVIEW_REQUIRED", ev) == "ACCEPT_PROVISIONAL"


def test_verified_rip_never_touches_block():
    ev = _ev("pass", accurate=True)
    assert server._apply_cd_rip_scoring("BLOCK", ev) == "BLOCK"


def test_verified_rip_leaves_accept_unchanged():
    ev = _ev("pass", accurate=True)
    assert server._apply_cd_rip_scoring("ACCEPT", ev) == "ACCEPT"


def test_pass_without_accuraterip_does_not_rescue_review():
    # "Copy OK" but no AccurateRip match is not strong enough to rescue review.
    ev = _ev("pass", accurate=False)
    assert server._apply_cd_rip_scoring("REVIEW_REQUIRED", ev) == "REVIEW_REQUIRED"


def test_log_backed_mismatch_routes_accept_to_review():
    ev = _ev("warn", log="rip.log")
    assert server._apply_cd_rip_scoring("ACCEPT", ev) == "REVIEW_REQUIRED"
    assert server._apply_cd_rip_scoring("ACCEPT_PROVISIONAL", ev) == "REVIEW_REQUIRED"


def test_log_backed_mismatch_never_touches_block():
    ev = _ev("warn", log="rip.log")
    assert server._apply_cd_rip_scoring("BLOCK", ev) == "BLOCK"


def test_cue_only_warn_does_not_downgrade():
    # warn with no rip log (cue-only) is weak/absent evidence, not a mismatch.
    ev = _ev("warn", log=None)
    assert server._apply_cd_rip_scoring("ACCEPT", ev) == "ACCEPT"
