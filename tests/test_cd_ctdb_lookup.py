"""Tests for the CTDB lookup client (F5.3B).

The fixture is a **real CTDB response** captured from the live CUETools DB for
Nirvana — Nevermind (disc rVkbymyibv8zjrKWYP0NXtplrbo-): a populated
``<entry confidence="22" toc="0:16440:…:212219" …/>``. Fetch is mocked, so no
network is touched.
"""

from __future__ import annotations

import cd_lookup
from cd_toc import CdToc

# Nevermind, cd_toc 0-based offsets (MusicBrainz absolute minus the 150 lead-in).
NEVERMIND = CdToc(
    track_offsets_frames=(
        0,
        16440,
        35909,
        50757,
        64354,
        81124,
        95944,
        105263,
        121970,
        139380,
        153709,
        166008,
        181118,
        200873,
    ),
    leadout_frames=212219,
    track_count=14,
)

# Real <entry> captured from db.cuetools.net for this disc (toc is 0-based).
_REAL_CTDB_XML = (
    '<ctdb xmlns="http://db.cuetools.net/ns/mmd-1.0#">'
    '<entry confidence="22" crc32="6973d814" id="167877" npar="8" stride="5880" '
    'toc="0:16440:35909:50757:64354:81124:95944:105263:121970:139380:153709:'
    '166008:181118:200873:212219" />'
    "</ctdb>"
)


def test_parse_real_ctdb_entry():
    expected = cd_lookup._expected_entry_toc(NEVERMIND)
    result = cd_lookup.parse_ctdb(_REAL_CTDB_XML, expected_toc=expected)
    assert result.found is True
    assert result.submissions == 1
    assert result.confidence == 22


def test_expected_entry_toc_matches_real_entry():
    # The 0-based offsets+leadout string must equal the real entry's toc attr.
    assert cd_lookup._expected_entry_toc(NEVERMIND) == (
        "0:16440:35909:50757:64354:81124:95944:105263:121970:139380:153709:"
        "166008:181118:200873:212219"
    )


def test_parse_entry_for_other_disc_is_not_found():
    result = cd_lookup.parse_ctdb(_REAL_CTDB_XML, expected_toc="0:111:222:333")
    assert result.found is False


def test_parse_empty_entry_is_not_found():
    xml = '<ctdb xmlns="http://db.cuetools.net/ns/mmd-1.0#"><entry /></ctdb>'
    assert cd_lookup.parse_ctdb(xml, expected_toc="0:1:2").found is False


def test_parse_empty_or_garbage_is_not_found():
    assert cd_lookup.parse_ctdb(b"", expected_toc="0:1").found is False
    assert cd_lookup.parse_ctdb(None, expected_toc="0:1").found is False
    assert cd_lookup.parse_ctdb("<not xml", expected_toc="0:1").found is False


def test_parse_takes_max_confidence_across_matching_entries():
    toc_attr = cd_lookup._expected_entry_toc(NEVERMIND)
    xml = (
        '<ctdb xmlns="http://db.cuetools.net/ns/mmd-1.0#">'
        f'<entry confidence="5" toc="{toc_attr}" />'
        f'<entry confidence="22" toc="{toc_attr}" />'
        '<entry confidence="99" toc="0:9:9" />'  # different disc — ignored
        "</ctdb>"
    )
    result = cd_lookup.parse_ctdb(xml, expected_toc=toc_attr)
    assert result.submissions == 2
    assert result.confidence == 22


def test_lookup_ctdb_uses_fetch_and_parses():
    result = cd_lookup.lookup_ctdb(NEVERMIND, fetch=lambda url: _REAL_CTDB_XML.encode())
    assert result.found is True
    assert result.confidence == 22


def test_lookup_ctdb_never_raises():
    def _boom(url):
        raise RuntimeError("network down")

    assert cd_lookup.lookup_ctdb(NEVERMIND, fetch=_boom).found is False


def test_lookup_ctdb_cache_avoids_refetch():
    calls = {"n": 0}

    def _counting(url):
        calls["n"] += 1
        return _REAL_CTDB_XML.encode()

    cache: dict = {}
    cd_lookup.lookup_ctdb(NEVERMIND, fetch=_counting, cache=cache)
    cd_lookup.lookup_ctdb(NEVERMIND, fetch=_counting, cache=cache)
    assert calls["n"] == 1


def test_lookup_ctdb_url_is_the_validated_query():
    import cd_discid

    seen = {}

    def _record(url):
        seen["url"] = url
        return None

    cd_lookup.lookup_ctdb(NEVERMIND, fetch=_record)
    assert seen["url"] == cd_discid.ctdb_lookup_url(NEVERMIND)
