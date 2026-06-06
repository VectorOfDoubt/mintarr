"""Tests for AccurateRip/CDDB disc-id computation (F5.3B sub-slice 2a).

The headline vector is **validated against the live AccurateRip database** (not
self-derived): the computed URL for this TOC resolves with HTTP 200, so these
expected values pin the disc-id conventions. The test itself is fully offline.
"""

from __future__ import annotations

import cd_discid
from cd_toc import CdToc

# MusicBrainz disc 1zOfzhFgYPD05pEMZa8j5o.dR.g- (Radiohead — OK Computer),
# offsets converted to cd_toc's 0-based frames (absolute minus the 150 lead-in).
OK_COMPUTER = CdToc(
    track_offsets_frames=(
        0,
        21482,
        50326,
        70547,
        90559,
        113095,
        132869,
        141821,
        159263,
        180805,
        198046,
        217551,
    ),
    leadout_frames=241891,
    track_count=12,
)
# These are the ids of the AccurateRip URL that the real DB returns 200 for.
EXPECTED_ID1 = 0x0018B14F
EXPECTED_ID2 = 0x00E28D21
EXPECTED_CDDB = 0xA20C990C


def test_accuraterip_ids_match_validated_vector():
    assert cd_discid.accuraterip_ids(OK_COMPUTER) == (EXPECTED_ID1, EXPECTED_ID2)


def test_cddb_id_matches_validated_vector():
    assert cd_discid.cddb_id(OK_COMPUTER) == EXPECTED_CDDB


def test_accuraterip_url_matches_validated_vector():
    assert cd_discid.accuraterip_url(OK_COMPUTER) == (
        "http://www.accuraterip.com/accuraterip/f/4/1/"
        "dBAR-012-0018b14f-00e28d21-a20c990c.bin"
    )


def test_ids_are_unsigned_32bit():
    id1, id2 = cd_discid.accuraterip_ids(OK_COMPUTER)
    assert 0 <= id1 <= 0xFFFFFFFF
    assert 0 <= id2 <= 0xFFFFFFFF
    assert 0 <= cd_discid.cddb_id(OK_COMPUTER) <= 0xFFFFFFFF


def test_single_track_toc_does_not_crash():
    toc = CdToc(track_offsets_frames=(0,), leadout_frames=10000, track_count=1)
    id1, id2 = cd_discid.accuraterip_ids(toc)
    # id2 treats a zero offset as 1: 1*1 + 10000*2 = 20001
    assert id1 == 10000
    assert id2 == 20001
