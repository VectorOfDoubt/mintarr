"""Unit tests for the pure Lidarr-album resolver (#160 slice 3)."""

from __future__ import annotations

import pytest

from lidarr_album_resolver import (
    AlbumCatalogueEntry,
    AlbumMatch,
    build_catalogue,
    normalize,
    resolve_album_id,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Björk", "bjork"),
        ("The Beatles!", "the beatles"),
        ("AC/DC", "ac dc"),
        ("  Sigur   Rós  ", "sigur ros"),
        ("Motörhead — Ace of Spades", "motorhead ace of spades"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_normalize_keeps_edition_words_distinct():
    # A different edition must normalize to a different string so it is never
    # collapsed into the standard album.
    assert normalize("Black Celebration") != normalize("Black Celebration (Deluxe)")


def test_build_catalogue_drops_invalid_rows():
    cat = build_catalogue(
        [
            (10, "Depeche Mode", "Black Celebration"),
            (0, "Bad", "Zero Id"),  # non-positive id
            (-1, "Bad", "Negative Id"),
            ("x", "Bad", "Bad Id"),  # non-int id
            (11, "Artist", ""),  # empty album → dropped
        ]
    )
    assert [e.album_id for e in cat] == [10]
    assert cat[0].artist_norm == "depeche mode"
    assert cat[0].album_norm == "black celebration"
    assert cat[0].combined_norm == "depeche mode black celebration"


def test_build_catalogue_accepts_dicts():
    cat = build_catalogue(
        [{"album_id": 5, "artist": "A-ha", "album": "Memorial Beach"}]
    )
    assert cat == [
        AlbumCatalogueEntry(album_id=5, artist_norm="a ha", album_norm="memorial beach")
    ]


_CATALOGUE = build_catalogue(
    [
        (10, "Depeche Mode", "Black Celebration"),
        (11, "Depeche Mode", "Violator"),
        (12, "Depeche Mode", "Black Celebration (Deluxe)"),
        (20, "Various Artists", "Now 10"),
        (21, "Various Artists", "Now 10"),  # duplicate title under same artist
    ]
)


def test_resolve_artist_album_unique():
    m = resolve_album_id(_CATALOGUE, artist="Depeche Mode", album="Black Celebration")
    assert m == AlbumMatch(album_id=10, method="artist_album", candidate_count=1)
    assert m.resolved is True


def test_resolve_artist_album_normalizes_both_sides():
    m = resolve_album_id(_CATALOGUE, artist="depeche  mode", album="VIOLATOR")
    assert m.album_id == 11


def test_resolve_abstains_on_ambiguous_artist_album():
    # Two albums share the same normalized artist+album → never guess.
    m = resolve_album_id(_CATALOGUE, artist="Various Artists", album="Now 10")
    assert m.album_id is None
    assert m.method == "artist_album"
    assert m.candidate_count == 2


def test_resolve_abstains_on_no_match():
    m = resolve_album_id(_CATALOGUE, artist="Nobody", album="Nothing")
    assert m.album_id is None
    assert m.candidate_count == 0


def test_resolve_does_not_collapse_deluxe_into_standard():
    # The deluxe is a distinct album entry; searching the standard resolves only
    # the standard, never the deluxe.
    m = resolve_album_id(_CATALOGUE, artist="Depeche Mode", album="Black Celebration")
    assert m.album_id == 10  # not 12 (the deluxe)


def test_resolve_abstains_when_album_missing():
    assert (
        resolve_album_id(_CATALOGUE, artist="Depeche Mode", album="").album_id is None
    )
    assert resolve_album_id(_CATALOGUE, artist="", album="Violator").album_id is None


def test_resolve_q_only_ignored_without_fallback():
    m = resolve_album_id(_CATALOGUE, q="Depeche Mode Violator")
    assert m.album_id is None
    assert m.method == "none"


def test_resolve_q_only_exact_unique_with_fallback():
    m = resolve_album_id(_CATALOGUE, q="Depeche Mode Violator", allow_q_fallback=True)
    assert m == AlbumMatch(album_id=11, method="q_exact", candidate_count=1)


def test_resolve_q_only_album_only_fallback():
    m = resolve_album_id(_CATALOGUE, q="violator", allow_q_fallback=True)
    assert m.album_id == 11


def test_resolve_q_only_abstains_on_ambiguous():
    m = resolve_album_id(_CATALOGUE, q="Various Artists Now 10", allow_q_fallback=True)
    assert m.album_id is None
    assert m.candidate_count == 2


def test_resolve_empty_inputs_abstain():
    assert resolve_album_id(_CATALOGUE).album_id is None
    assert resolve_album_id([], artist="A", album="B").album_id is None
