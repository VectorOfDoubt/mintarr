"""Tests for the AccurateRip lookup client (F5.3B lookup slice).

The disc-id/URL convention is already DB-validated (test_cd_discid). Here the
``.bin`` bytes are synthetic but **format-accurate** (the documented chunk
layout, whose real size for 12 tracks — 121 bytes — was confirmed live during
disc-id validation). Fetch is mocked, so no network is touched.
"""

from __future__ import annotations

import struct

import cd_discid
import cd_lookup
from cd_toc import CdToc

_TOC = CdToc(
    track_offsets_frames=(0, 10000, 20000), leadout_frames=30000, track_count=3
)
# Expected disc identity for _TOC (the values a chunk must carry to count).
_ID1, _ID2 = cd_discid.accuraterip_ids(_TOC)
_CDDB = cd_discid.cddb_id(_TOC)


def _chunk(confidences, *, id1=_ID1, id2=_ID2, cddb=_CDDB):
    """Build one AccurateRip pressing chunk for the given per-track confidences."""
    out = bytearray()
    out.append(len(confidences))
    out += struct.pack("<III", id1, id2, cddb)
    for conf in confidences:
        out.append(conf)
        out += struct.pack("<II", 0xDEADBEEF, 0xCAFEBABE)  # crc, crc450
    return bytes(out)


def _parse(data):
    """parse_accuraterip bound to _TOC's expected identity."""
    return cd_lookup.parse_accuraterip(
        data,
        expected_id1=_ID1,
        expected_id2=_ID2,
        expected_cddb=_CDDB,
        expected_track_count=_TOC.track_count,
    )


def test_parse_single_pressing():
    result = _parse(_chunk([5, 8, 3]))
    assert result.found is True
    assert result.pressings == 1
    assert result.track_count == 3
    assert result.per_track_confidence == (5, 8, 3)
    assert result.max_confidence == 8


def test_parse_chunk_size_matches_format():
    # 3 tracks -> 13 + 9*3 = 40 bytes (the format the parser assumes).
    assert len(_chunk([1, 1, 1])) == 13 + 9 * 3


def test_parse_multiple_pressings_takes_per_track_max():
    data = _chunk([5, 2, 9]) + _chunk([1, 7, 4])
    result = _parse(data)
    assert result.pressings == 2
    assert result.per_track_confidence == (5, 7, 9)
    assert result.max_confidence == 9


def test_parse_empty_is_not_found():
    assert _parse(b"").found is False
    assert _parse(None).found is False


def test_parse_truncated_chunk_is_not_found():
    truncated = _chunk([5, 8, 3])[:20]  # less than one full 40-byte chunk
    assert _parse(truncated).found is False


def test_parse_wrong_disc_chunk_is_not_found():
    # A well-formed chunk for a DIFFERENT disc must not count as a match.
    wrong = _chunk([9, 9, 9], id1=_ID1 ^ 0xFFFF, id2=_ID2, cddb=_CDDB)
    assert _parse(wrong).found is False


def test_parse_mixed_body_counts_only_matching_chunk():
    # Wrong-disc chunk + correct-disc chunk -> only the correct one is counted.
    data = _chunk([1, 1, 1], cddb=_CDDB ^ 0x1234) + _chunk([5, 6, 7])
    result = _parse(data)
    assert result.found is True
    assert result.pressings == 1
    assert result.per_track_confidence == (5, 6, 7)


def test_parse_track_count_mismatch_is_not_found():
    # Matching ids but a different track count is not our disc.
    mismatch = _chunk([4, 4, 4, 4])  # 4 tracks, expected 3
    assert _parse(mismatch).found is False


def test_lookup_uses_fetch_and_parses():
    result = cd_lookup.lookup_accuraterip(_TOC, fetch=lambda url: _chunk([9, 9, 9]))
    assert result.found is True
    assert result.max_confidence == 9


def test_lookup_missing_disc_is_not_found():
    result = cd_lookup.lookup_accuraterip(_TOC, fetch=lambda url: None)
    assert result.found is False


def test_lookup_wrong_disc_body_is_not_found():
    wrong = _chunk([9, 9, 9], id1=_ID1 + 1)
    assert cd_lookup.lookup_accuraterip(_TOC, fetch=lambda url: wrong).found is False


def test_lookup_never_raises_on_fetch_error():
    def _boom(url):
        raise RuntimeError("network down")

    assert cd_lookup.lookup_accuraterip(_TOC, fetch=_boom).found is False


def test_lookup_cache_avoids_refetch():
    calls = {"n": 0}

    def _counting(url):
        calls["n"] += 1
        return _chunk([4, 4, 4])

    cache: dict = {}
    cd_lookup.lookup_accuraterip(_TOC, fetch=_counting, cache=cache)
    cd_lookup.lookup_accuraterip(_TOC, fetch=_counting, cache=cache)
    assert calls["n"] == 1  # second lookup served from cache


def test_lookup_url_is_the_validated_accuraterip_url():
    seen = {}

    def _record(url):
        seen["url"] = url
        return None

    cd_lookup.lookup_accuraterip(_TOC, fetch=_record)
    assert seen["url"] == cd_discid.accuraterip_url(_TOC)
