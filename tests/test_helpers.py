"""Baseline tests for pure helpers in server.py.

These are the 5 baseline tests required by Batch D build gate
(SPEC §5). Coverage targets _sanitize_path_segment (path-safety),
_classify_quality (size estimation), _release_title (Lidarr-parseable
release title format).
"""

from __future__ import annotations

import server


def test_sanitize_path_segment_strips_separators():
    assert server._sanitize_path_segment("foo/bar") == "foo_bar"
    assert server._sanitize_path_segment("foo\\bar") == "foo_bar"
    assert server._sanitize_path_segment("a/b\\c") == "a_b_c"


def test_sanitize_path_segment_rejects_parent_refs():
    assert server._sanitize_path_segment("") == "Unknown"
    assert server._sanitize_path_segment(".") == "Unknown"
    assert server._sanitize_path_segment("..") == "Unknown"
    assert server._sanitize_path_segment("   ") == "Unknown"


def test_sanitize_path_segment_strips_leading_dots_and_nul():
    assert server._sanitize_path_segment(".hidden") == "hidden"
    assert server._sanitize_path_segment("foo\x00bar") == "foobar"
    assert server._sanitize_path_segment("...rel") == "rel"


def test_classify_quality_returns_flac_24bit_with_size_estimate(fake_album):
    tag, size = server._classify_quality(fake_album)
    assert tag == "FLAC 24bit"
    # 4608 kbps * 4500s / 8 = ~2.59 GB
    assert size == int(4608 * 1000 * 4500 / 8)
    # Zero-duration album defaults to size 0
    fake_album.duration = 0
    _, size_zero = server._classify_quality(fake_album)
    assert size_zero == 0


def test_release_title_format_matches_lidarr_parser(fake_album):
    title = server._release_title(fake_album, "FLAC 24bit")
    assert title == "Daft Punk - Random Access Memories (2013) [TIDAL] [FLAC 24bit]"
    # Fallback when artist/release_date missing
    fake_album.artist = None
    fake_album.release_date = None
    title2 = server._release_title(fake_album, "FLAC")
    assert title2 == "Unknown - Random Access Memories (0) [TIDAL] [FLAC]"


def test_track_title_normalization_ignores_remaster_noise():
    assert (
        server._normalize_track_title_for_match("01 - Celice (2026 Remaster).flac")
        == "celice"
    )
    assert (
        server._normalize_track_title_for_match("1-01. Cosy Prisons [Remastered]")
        == "cosy prisons"
    )
    assert (
        server._normalize_track_title_for_match("Celice (Early Version)")
        == "celice early version"
    )


def test_release_match_score_prefers_full_remaster_release():
    downloaded = {
        server._normalize_track_title_for_match(name)
        for name in [
            "01 - Celice (2026 Remaster).flac",
            "02 - Don't Do Me Any Favours (2026 Remaster).flac",
            "03 - Cosy Prisons (2026 Remaster).flac",
            "04 - Minor Key Sonata (Analogue).flac",
        ]
    }
    full_release_tracks = server._track_title_names(
        [
            {"title": "Celice"},
            {"title": "Don't Do Me Any Favours"},
            {"title": "Cosy Prisons"},
            {"title": "Minor Key Sonata (Analogue)"},
        ]
    )
    standard_release_tracks = server._track_title_names(
        [
            {"title": "Celice"},
            {"title": "Don't Do Me Any Favours"},
        ]
    )

    full = server._score_release_match(
        4, downloaded, {"trackCount": 4}, full_release_tracks
    )
    standard = server._score_release_match(
        4, downloaded, {"trackCount": 2}, standard_release_tracks
    )

    assert full == 100
    assert standard < 70
