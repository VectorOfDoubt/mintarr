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
    # Spectral authenticity rollup (F5.4 slice 4a, §8b). Record-only here —
    # compare() does not read these yet (that is slice 4b). ``any_fake`` is True
    # iff a track was spectrally measured as fake; ``authenticity_known`` is True
    # only when *every* measured track has a spectral verdict, so unknown
    # authenticity is never silently read as authentic.
    any_fake: bool = False
    authenticity_known: bool = False
    # F5.4 integrity split: a track that decodes but whose stored FLAC MD5 is stale
    # (re-tagged/re-encoded after ripping). It is *not* corruption and must never
    # drive compare() — advisory only, surfaced in the ranking. ``any_invalid``
    # above stays strictly decode-invalid.
    any_md5_mismatch: bool = False


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
    # Spectral authenticity rolls up only over tracks that carry a spectral
    # verdict. A release is "authentic-known" only when every measured track was
    # spectrally measured; one fake track makes the release any_fake (§8b.1: the
    # rollup is scoped to the album's own trackfiles, never a directory).
    spectral = [r for r in measured if r.get("spectral_status") == "measured"]
    any_fake = any(r.get("authentic") == 0 for r in spectral)
    authenticity_known = len(spectral) == len(measured) and len(spectral) > 0
    # Stale-MD5 (decodes, checksum failed) is advisory: never feeds compare().
    any_md5_mismatch = any(r.get("checksum_ok") == 0 for r in measured)
    return AlbumQuality(
        measured_count=len(measured),
        track_count=len(rows),
        all_valid=all_valid,
        any_invalid=any_invalid,
        min_tier=min_tier,
        all_lossless=all_lossless,
        any_fake=any_fake,
        authenticity_known=authenticity_known,
        any_md5_mismatch=any_md5_mismatch,
    )


@dataclass(frozen=True)
class CandidateQuality:
    """The candidate side of the comparison (measured during the import flow)."""

    valid: bool  # passed hard gates (ffprobe + flac -t)
    authentic: bool  # not a measured fake (FLAC Detective)
    tier: int  # tier_rank of the candidate
    complete: bool  # fills/matches the expected track count
    tier_known: bool = True  # False when bit depth / sample rate is unknown


# Comparison verdicts — slice 3b maps these onto the ACCEPT/REVIEW decisions.
UPGRADE = "upgrade"  # candidate strictly better → may replace
EQUIVALENT = "equivalent"  # no meaningful quality gain
DOWNGRADE = "downgrade"  # candidate worse on a higher-precedence axis
REVIEW = "review"  # mixed/ambiguous → operator decides


def compare(
    candidate: CandidateQuality,
    existing: AlbumQuality | None,
    *,
    existing_incomplete: bool = False,
    consider_existing_authenticity: bool = False,
) -> str:
    """Compare a candidate against measured existing quality (§4 precedence).

    Lexicographic by precedence — hard validity, then authenticity, then lossless
    tier, then completeness. Never considers identity here (that stays on the
    identity axis / ADR-0013 in the caller). Returns one of UPGRADE / EQUIVALENT
    / DOWNGRADE / REVIEW. With no measured existing evidence, returns REVIEW so
    the caller falls back to its label-based path.

    ``existing_incomplete`` is the release-completeness signal and must come from
    Lidarr track stats (existing vs expected track count), supplied by the caller
    in slice 3b — it is NOT inferred from measurement coverage here, since some
    rows being unmeasured says nothing about whether the album is complete.

    ``consider_existing_authenticity`` (F5.4 slice 4b, gated on
    ``MINTARR_LIBRARY_SPECTRAL``) lets the *existing* release's spectral verdict
    move the decision. It must stay False unless spectral measurement is enabled:
    when it is off, ``existing.authenticity_known`` is False for every album, so
    reading it would route every non-upgrade to REVIEW — a regression. The two
    new effects rest on one asymmetry: **unknown existing authenticity can only
    inflate the existing tier (an upsampled fake reports hi-res), never deflate
    it.** So an UPGRADE verdict is always safe, while a DOWNGRADE/EQUIVALENT
    verdict — which trusts the existing tier — is suspect when authenticity is
    unverified, and abstains to REVIEW (symmetric with the candidate tier abstain).
    """
    if existing is None:
        return REVIEW

    # Highest precedence is the candidate's own usability: an invalid candidate
    # can never win; a fake/suspicious candidate never auto-wins (even over an
    # existing release that has an integrity defect) — route to review.
    if not candidate.valid:
        return DOWNGRADE
    if not candidate.authentic:
        return REVIEW

    # Validity of the existing release: a valid+authentic candidate that fixes an
    # existing integrity defect is an upgrade.
    if existing.any_invalid:
        return UPGRADE

    # Authenticity (axis 3) outranks tier (axis 4): a genuine candidate replacing
    # an existing release with a measured-fake track is an upgrade even if the fake
    # claims a higher nominal tier. Decided here, above the tier comparison and
    # above the candidate-tier abstain, because it lives on a higher axis.
    if consider_existing_authenticity and existing.any_fake:
        return UPGRADE

    # Abstain when the candidate's tier is unknown (no bit depth / sample rate):
    # we cannot judge an upgrade vs downgrade, so route to the operator rather
    # than guessing a tier.
    if not candidate.tier_known:
        return REVIEW

    # Lossless tier (existing is valid + measured here). A strictly higher
    # candidate tier is an upgrade regardless of existing authenticity — unknown
    # existing authenticity could only inflate the existing tier, so beating even
    # the inflated tier is unambiguous.
    if candidate.tier > existing.min_tier:
        return UPGRADE

    # Same tier: a complete candidate replacing an incomplete existing release is a
    # completeness upgrade (axis 5) — also safe regardless of existing authenticity.
    # Completeness comes from the caller (Lidarr stats), not measurement coverage.
    if (
        candidate.tier == existing.min_tier
        and candidate.complete
        and existing_incomplete
    ):
        return UPGRADE

    # All remaining outcomes (EQUIVALENT at the same tier, DOWNGRADE at a lower
    # one) *trust the existing tier*. If existing authenticity is unverified that
    # tier may be a fake-inflated hi-res hiding a real upgrade, so abstain to the
    # operator rather than silently keeping a possibly-fake existing release.
    if consider_existing_authenticity and not existing.authenticity_known:
        return REVIEW

    if candidate.tier < existing.min_tier:
        return DOWNGRADE
    return EQUIVALENT
