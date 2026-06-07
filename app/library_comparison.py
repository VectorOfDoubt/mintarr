"""Quality-vector comparison + album rollup for the library evidence index (F5.4).

Pure logic only — no decision wiring (that is slice 3b, behind
``MINTARR_MEASURED_EXISTING``). Implements the precedence vector (§4) and the
album/release rollup (§5) of the F5.4 design so a candidate can be compared
against the *measured* existing library instead of Lidarr's quality label.
"""

from __future__ import annotations

from dataclasses import dataclass


def tier_rank(
    *, lossless: bool | None, bit_depth: int | None, sample_rate: int | None
) -> int:
    """Coarse, comparable lossless-tier rank (higher is better).

    0 = lossy / unknown, 1 = lossless 16-bit, 2 = lossless 24-bit ≤48 kHz,
    3 = lossless 24-bit hi-res (>48 kHz). Deliberately coarse: it ranks audio
    tiers, it is not a bitrate.
    """
    if not lossless:
        return 0
    depth = bit_depth or 16
    rate = sample_rate or 44100
    if depth >= 24 and rate > 48000:
        return 3
    if depth >= 24:
        return 2
    return 1


@dataclass(frozen=True)
class AlbumQuality:
    """Rollup of a release's measured per-track evidence (§5)."""

    measured_count: int
    track_count: int
    all_valid: bool  # every measured track passed integrity (none failed)
    any_invalid: bool  # at least one measured track failed integrity
    min_tier: int  # weakest track tier (a release is only as good as its worst)
    all_lossless: bool


def album_quality(rows: list[dict]) -> AlbumQuality | None:
    """Roll measured per-track rows up to a release-level quality (§5).

    ``rows`` are ``library_evidence`` records. Returns None when nothing is
    measured (caller falls back to the Lidarr label). Unmeasured rows are
    ignored for tier/validity but counted in ``track_count``.
    """
    if not rows:
        return None
    measured = [r for r in rows if r.get("status") == "measured"]
    if not measured:
        return None

    any_invalid = any(r.get("integrity_ok") is False for r in measured)
    all_valid = all(r.get("integrity_ok") is not False for r in measured)
    all_lossless = all(bool(r.get("lossless")) for r in measured)
    min_tier = min(
        tier_rank(
            lossless=bool(r.get("lossless")),
            bit_depth=r.get("bit_depth"),
            sample_rate=r.get("sample_rate"),
        )
        for r in measured
    )
    return AlbumQuality(
        measured_count=len(measured),
        track_count=len(rows),
        all_valid=all_valid,
        any_invalid=any_invalid,
        min_tier=min_tier,
        all_lossless=all_lossless,
    )


@dataclass(frozen=True)
class CandidateQuality:
    """The candidate side of the comparison (measured during the import flow)."""

    valid: bool  # passed hard gates (ffprobe + flac -t)
    authentic: bool  # not a measured fake (FLAC Detective)
    tier: int  # tier_rank of the candidate
    complete: bool  # fills/matches the expected track count


# Comparison verdicts — slice 3b maps these onto the ACCEPT/REVIEW decisions.
UPGRADE = "upgrade"  # candidate strictly better → may replace
EQUIVALENT = "equivalent"  # no meaningful quality gain
DOWNGRADE = "downgrade"  # candidate worse on a higher-precedence axis
REVIEW = "review"  # mixed/ambiguous → operator decides


def compare(candidate: CandidateQuality, existing: AlbumQuality | None) -> str:
    """Compare a candidate against measured existing quality (§4 precedence).

    Lexicographic by precedence — hard validity, then authenticity, then lossless
    tier, then completeness. Never considers identity here (that stays on the
    identity axis / ADR-0013 in the caller). Returns one of UPGRADE / EQUIVALENT
    / DOWNGRADE / REVIEW. With no measured existing evidence, returns REVIEW so
    the caller falls back to its label-based path.
    """
    if existing is None:
        return REVIEW

    # Validity dominates: a candidate that fixes an invalid existing release wins;
    # an existing valid release is not displaced by a candidate that is no better.
    if existing.any_invalid and candidate.valid:
        return UPGRADE
    if not candidate.valid:
        return DOWNGRADE

    # Authenticity: a measured-fake candidate is never an upgrade over a clean
    # existing release; route to review rather than silently replacing.
    if not candidate.authentic:
        return REVIEW

    # Lossless tier (only meaningful once existing is valid + measured).
    if candidate.tier > existing.min_tier:
        return UPGRADE
    if candidate.tier < existing.min_tier:
        return DOWNGRADE

    # Same tier: completeness can still justify a replacement.
    if candidate.complete and existing.measured_count < existing.track_count:
        return UPGRADE
    return EQUIVALENT
