from __future__ import annotations

from release_family import (
    ExpectedRelease,
    IdentityDecision,
    ObservedRelease,
    ReleaseFamilyEvidence,
    evaluate_release_identity,
    is_release_family_rejection,
    normalize_track_title_for_match,
    score_release_match,
    track_title_names,
)


def test_track_title_normalization_ignores_release_noise():
    assert (
        normalize_track_title_for_match("01 - Celice (2026 Remaster).flac") == "celice"
    )
    assert (
        normalize_track_title_for_match("1-01. Cosy Prisons [Remastered]")
        == "cosy prisons"
    )
    assert (
        normalize_track_title_for_match("Celice (Early Version)")
        == "celice early version"
    )


def test_score_release_match_prefers_full_release():
    observed = {
        normalize_track_title_for_match(name)
        for name in [
            "01 - Celice (2026 Remaster).flac",
            "02 - Don't Do Me Any Favours (2026 Remaster).flac",
            "03 - Cosy Prisons (2026 Remaster).flac",
            "04 - Minor Key Sonata (Analogue).flac",
        ]
    }
    full_release_tracks = track_title_names(
        [
            {"title": "Celice"},
            {"title": "Don't Do Me Any Favours"},
            {"title": "Cosy Prisons"},
            {"title": "Minor Key Sonata (Analogue)"},
        ]
    )
    standard_release_tracks = track_title_names(
        [
            {"title": "Celice"},
            {"title": "Don't Do Me Any Favours"},
        ]
    )

    full = score_release_match(
        4,
        observed,
        expected_track_count=4,
        expected_track_titles=full_release_tracks,
    )
    standard = score_release_match(
        4,
        observed,
        expected_track_count=2,
        expected_track_titles=standard_release_tracks,
    )

    assert full == 100
    assert standard < 70


def test_release_family_rejection_requires_only_family_markers():
    assert is_release_family_rejection(
        {
            "rejections": [
                {"reason": "Album match is not close enough: 56.3 % vs 80 %"},
                {"reason": "Has unmatched tracks"},
            ]
        }
    )
    assert not is_release_family_rejection(
        {
            "rejections": [
                {"reason": "Album match is not close enough: 56.3 % vs 80 %"},
                {"reason": "Existing file has higher quality"},
            ]
        }
    )


def test_identity_evaluation_same_current_release():
    observed = ObservedRelease(
        file_count=2,
        track_titles=frozenset({"celice", "cosy prisons"}),
    )
    expected = ExpectedRelease(
        release_id=30,
        track_count=2,
        track_titles=frozenset({"celice", "cosy prisons"}),
        is_current=True,
    )

    result = evaluate_release_identity(
        ReleaseFamilyEvidence(
            observed=observed,
            expected_releases=(expected,),
            current_release_id=30,
        )
    )

    assert result.decision == IdentityDecision.SAME_RELEASE
    assert result.confidence == 100
    assert result.best_release_id == 30


def test_identity_evaluation_same_family_for_better_non_current_release():
    observed = ObservedRelease(
        file_count=4,
        track_titles=frozenset({"a", "b", "c", "d"}),
    )
    current = ExpectedRelease(
        release_id=10,
        track_count=2,
        track_titles=frozenset({"a", "b"}),
        is_current=True,
    )
    deluxe = ExpectedRelease(
        release_id=20,
        track_count=4,
        track_titles=frozenset({"a", "b", "c", "d"}),
    )

    result = evaluate_release_identity(
        ReleaseFamilyEvidence(
            observed=observed,
            expected_releases=(current, deluxe),
            current_release_id=10,
        )
    )

    assert result.decision == IdentityDecision.SAME_FAMILY
    assert result.best_release_id == 20
    assert result.current_release_id == 10


def test_identity_evaluation_wrong_album_requires_strong_mbid_evidence():
    result = evaluate_release_identity(
        ReleaseFamilyEvidence(
            observed=ObservedRelease(file_count=1, artist_mbid="artist-a"),
            expected_releases=(
                ExpectedRelease(release_id=1, track_count=1, artist_mbid="artist-b"),
            ),
            current_release_id=1,
        )
    )

    assert result.decision == IdentityDecision.WRONG_ALBUM
    assert result.confidence == 100
    assert result.reasons == ("artist MBID mismatch",)


def test_identity_evaluation_abstains_on_weak_metadata():
    result = evaluate_release_identity(
        ReleaseFamilyEvidence(
            observed=ObservedRelease(),
            expected_releases=(ExpectedRelease(release_id=1, track_count=10),),
            current_release_id=1,
        )
    )

    assert result.decision == IdentityDecision.INSUFFICIENT_EVIDENCE
    assert result.confidence == 0


def test_identity_evaluation_ambiguous_when_no_release_reaches_threshold():
    result = evaluate_release_identity(
        ReleaseFamilyEvidence(
            observed=ObservedRelease(file_count=4, track_titles=frozenset({"x", "y"})),
            expected_releases=(
                ExpectedRelease(
                    release_id=1,
                    track_count=10,
                    track_titles=frozenset({"a", "b"}),
                ),
            ),
            current_release_id=1,
        )
    )

    assert result.decision == IdentityDecision.AMBIGUOUS_EDITION
    assert result.confidence < 80
