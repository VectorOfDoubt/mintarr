"""AccurateRip / CDDB disc-ID computation (F5.3B sub-slice 2a).

Pure: turn a reconstructed :class:`cd_toc.CdToc` into the disc identifiers the
AccurateRip lookup needs. No network and no policy here — the HTTP lookup client
and the CTDB id land in later slices.

**Convention (validated against the live AccurateRip DB, not self-derived).**
``cd_toc`` offsets are 0-based (track 1 audio starts at frame 0). AccurateRip
keys on those 0-based frame offsets plus the lead-out. The CDDB/FreeDB id keys
on *absolute* offsets, i.e. with the standard 150-frame (2 s) lead-in added.
This was confirmed empirically: for MusicBrainz disc
``1zOfzhFgYPD05pEMZa8j5o.dR.g-`` (Radiohead — OK Computer) the 0-based offsets
produce an AccurateRip URL that the real DB resolves (HTTP 200), while the
absolute-offset variant 404s. The frozen vector is asserted in the tests.
"""

from __future__ import annotations

from cd_toc import CdToc

_LEAD_IN_FRAMES = 150  # standard 2-second CD lead-in (75 frames/s)


def accuraterip_ids(toc: CdToc) -> tuple[int, int]:
    """Return AccurateRip (discId1, discId2) from a 0-based TOC."""
    all_offsets = list(toc.track_offsets_frames) + [toc.leadout_frames]
    disc_id1 = 0
    disc_id2 = 0
    for position, offset in enumerate(all_offsets, start=1):
        disc_id1 += offset
        disc_id2 += (offset or 1) * position
    return disc_id1 & 0xFFFFFFFF, disc_id2 & 0xFFFFFFFF


def cddb_id(toc: CdToc) -> int:
    """Return the FreeDB/CDDB disc id (the third AccurateRip URL component)."""
    absolute = [offset + _LEAD_IN_FRAMES for offset in toc.track_offsets_frames]
    leadout_abs = toc.leadout_frames + _LEAD_IN_FRAMES
    checksum = sum(_digit_sum(offset // 75) for offset in absolute)
    total_seconds = (leadout_abs // 75) - (absolute[0] // 75)
    return ((checksum % 0xFF) << 24 | total_seconds << 8 | toc.track_count) & 0xFFFFFFFF


def accuraterip_url(toc: CdToc) -> str:
    """Build the AccurateRip ``.bin`` lookup URL for a TOC."""
    disc_id1, disc_id2 = accuraterip_ids(toc)
    cddb = cddb_id(toc)
    return (
        "http://www.accuraterip.com/accuraterip/"
        f"{disc_id1 & 0xF:x}/{(disc_id1 >> 4) & 0xF:x}/{(disc_id1 >> 8) & 0xF:x}/"
        f"dBAR-{toc.track_count:03d}-{disc_id1:08x}-{disc_id2:08x}-{cddb:08x}.bin"
    )


def ctdb_toc_string(toc: CdToc) -> str:
    """Return the CTDB (CUETools DB) TOC string for a TOC.

    CTDB keys on **absolute** frame offsets (0-based + the 150-frame lead-in),
    each track plus the lead-out, joined by ``:`` — the same shape AccurateRip's
    CDDB id uses. Validated against the live CTDB: this string returns the
    correct disc metadata (see the tests / ``ctdb_lookup_url``).
    """
    absolute = [offset + _LEAD_IN_FRAMES for offset in toc.track_offsets_frames]
    absolute.append(toc.leadout_frames + _LEAD_IN_FRAMES)
    return ":".join(str(frame) for frame in absolute)


def ctdb_lookup_url(toc: CdToc) -> str:
    """Build the CTDB lookup URL for a TOC.

    Confirmed against the live CUETools DB: for the OK Computer TOC this URL
    returns ``200`` with the correct disc metadata. Response parsing (the
    ``<entry>`` confidence record) belongs to the lookup-client slice.
    """
    return (
        "http://db.cuetools.net/lookup2.php"
        f"?version=3&ctdb=1&metadata=fast&fuzzy=1&toc={ctdb_toc_string(toc)}"
    )


def _digit_sum(value: int) -> int:
    total = 0
    while value > 0:
        total += value % 10
        value //= 10
    return total
