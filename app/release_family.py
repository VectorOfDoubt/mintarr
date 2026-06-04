"""Pure release-family identity helpers for F5.1.

This module intentionally has no Flask, requests, filesystem, global state, or
Lidarr client dependencies. Runtime code is responsible for collecting metadata;
this module scores the evidence it is handed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
import re

AUDIO_SUFFIXES = (".flac", ".m4a", ".mp3", ".ogg", ".aac")
RELEASE_FAMILY_REJECTION_MARKERS = (
    "match is not close enough",
    "missing tracks",
    "unmatched tracks",
)


class IdentityDecision(StrEnum):
    SAME_RELEASE = "SAME_RELEASE"
    SAME_FAMILY = "SAME_FAMILY"
    AMBIGUOUS_EDITION = "AMBIGUOUS_EDITION"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    WRONG_ALBUM = "WRONG_ALBUM"


@dataclass(frozen=True)
class ObservedRelease:
    file_count: int = 0
    track_titles: frozenset[str] = frozenset()
    artist_mbid: str | None = None
    release_group_mbid: str | None = None
    release_mbid: str | None = None


@dataclass(frozen=True)
class ExpectedRelease:
    album_id: int | None = None
    release_id: int | str | None = None
    track_count: int = 0
    track_titles: frozenset[str] = frozenset()
    artist_mbid: str | None = None
    release_group_mbid: str | None = None
    release_mbid: str | None = None
    is_current: bool = False


@dataclass(frozen=True)
class ReleaseFamilyEvidence:
    observed: ObservedRelease
    expected_releases: tuple[ExpectedRelease, ...] = ()
    current_release_id: int | str | None = None
    lidarr_rejections: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReleaseIdentityResult:
    decision: IdentityDecision
    confidence: float
    best_release_id: int | str | None = None
    current_release_id: int | str | None = None
    score: float = 0.0
    track_count_delta: int | None = None
    title_similarity: float | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)


def normalize_track_title_for_match(title: str) -> str:
    """Normalize remaster/edition noise while preserving alternate-version identity."""
    suffix = ""
    if "." in title:
        suffix = title[title.rfind(".") :].lower()
    normalized = title[: -len(suffix)] if suffix in AUDIO_SUFFIXES else title
    normalized = re.sub(
        r"^\s*(?:disc\s*)?\d{1,2}[\s._-]+(?:\d{1,2}[\s._-]+)?",
        "",
        normalized,
        flags=re.I,
    )
    if " - " in normalized:
        normalized = normalized.rsplit(" - ", 1)[-1]
    normalized = re.sub(
        r"[\[(]\s*(?:\d{4}\s+)?(?:remaster(?:ed)?|remix(?:ed)?|anniversary remaster)\s*[\])]",
        "",
        normalized,
        flags=re.I,
    )
    normalized = re.sub(r"\b(?:\d{4}\s+)?remaster(?:ed)?\b", "", normalized, flags=re.I)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized.lower())
    return re.sub(r"\s+", " ", normalized).strip()


def normalized_track_names_from_titles(titles: list[str] | tuple[str, ...]) -> set[str]:
    names = set()
    for title in titles:
        normalized = normalize_track_title_for_match(str(title or ""))
        if normalized:
            names.add(normalized)
    return names


def track_title_names(tracks: list[dict[str, Any]]) -> set[str]:
    return normalized_track_names_from_titles(
        [str(track.get("title") or "") for track in tracks]
    )


def score_release_match(
    file_count: int,
    observed_track_titles: set[str],
    *,
    expected_track_count: int,
    expected_track_titles: set[str],
) -> float:
    count_score = max(0, 100 - abs(expected_track_count - file_count) * 10)
    if observed_track_titles and expected_track_titles:
        matches = sum(
            1 for name in observed_track_titles if name in expected_track_titles
        )
        name_score = (matches / len(observed_track_titles)) * 100
    else:
        name_score = 50
    return count_score * 0.4 + name_score * 0.6


def score_lidarr_release_match(
    file_count: int,
    observed_track_titles: set[str],
    release: dict[str, Any],
    expected_track_titles: set[str],
) -> float:
    expected_track_count = int(
        release.get("trackCount") or len(expected_track_titles) or 0
    )
    return score_release_match(
        file_count,
        observed_track_titles,
        expected_track_count=expected_track_count,
        expected_track_titles=expected_track_titles,
    )


def rejection_reasons(item: dict[str, Any]) -> list[str]:
    return [str(r.get("reason", "")).lower() for r in item.get("rejections") or []]


def is_release_family_rejection(item: dict[str, Any]) -> bool:
    reasons = rejection_reasons(item)
    return bool(reasons) and all(
        any(marker in reason for marker in RELEASE_FAMILY_REJECTION_MARKERS)
        for reason in reasons
    )


def evaluate_release_identity(
    evidence: ReleaseFamilyEvidence,
    *,
    same_release_threshold: float = 95.0,
    same_family_threshold: float = 80.0,
) -> ReleaseIdentityResult:
    """Return a conservative identity decision from already-collected evidence.

    This first slice is deliberately small. It captures strong MBID disagreement
    and same-release/family/abstain outcomes without making runtime policy
    depend on the result yet.
    """
    observed = evidence.observed
    reasons: list[str] = []

    for expected in evidence.expected_releases:
        if (
            observed.artist_mbid
            and expected.artist_mbid
            and observed.artist_mbid != expected.artist_mbid
        ):
            return ReleaseIdentityResult(
                decision=IdentityDecision.WRONG_ALBUM,
                confidence=100.0,
                best_release_id=expected.release_id,
                current_release_id=evidence.current_release_id,
                reasons=("artist MBID mismatch",),
            )
        if (
            observed.release_group_mbid
            and expected.release_group_mbid
            and observed.release_group_mbid != expected.release_group_mbid
        ):
            return ReleaseIdentityResult(
                decision=IdentityDecision.WRONG_ALBUM,
                confidence=100.0,
                best_release_id=expected.release_id,
                current_release_id=evidence.current_release_id,
                reasons=("release-group MBID mismatch",),
            )

    if not evidence.expected_releases or (
        observed.file_count <= 0 and not observed.track_titles
    ):
        return ReleaseIdentityResult(
            decision=IdentityDecision.INSUFFICIENT_EVIDENCE,
            confidence=0.0,
            current_release_id=evidence.current_release_id,
            reasons=("insufficient release evidence",),
        )

    scored: list[tuple[float, ExpectedRelease, int, float | None]] = []
    for expected in evidence.expected_releases:
        score = score_release_match(
            observed.file_count,
            set(observed.track_titles),
            expected_track_count=expected.track_count,
            expected_track_titles=set(expected.track_titles),
        )
        delta = abs((expected.track_count or 0) - observed.file_count)
        if observed.track_titles and expected.track_titles:
            matches = sum(
                1 for name in observed.track_titles if name in expected.track_titles
            )
            title_similarity: float | None = matches / len(observed.track_titles)
        else:
            title_similarity = None
        scored.append((score, expected, delta, title_similarity))

    scored.sort(key=lambda row: row[0], reverse=True)
    best_score, best_expected, track_delta, title_similarity = scored[0]
    if (
        best_expected.release_id == evidence.current_release_id
        and best_score >= same_release_threshold
    ):
        decision = IdentityDecision.SAME_RELEASE
        reasons.append("matches current Lidarr release")
    elif best_score >= same_family_threshold:
        decision = IdentityDecision.SAME_FAMILY
        reasons.append("matches another Lidarr release in the same album family")
    else:
        decision = IdentityDecision.AMBIGUOUS_EDITION
        reasons.append("no release reached same-family confidence")

    return ReleaseIdentityResult(
        decision=decision,
        confidence=best_score,
        best_release_id=best_expected.release_id,
        current_release_id=evidence.current_release_id,
        score=best_score,
        track_count_delta=track_delta,
        title_similarity=title_similarity,
        reasons=tuple(reasons),
    )
