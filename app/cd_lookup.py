"""AccurateRip lookup client (F5.3B, lookup slice).

Fetch and parse the AccurateRip response for a reconstructed TOC into an
advisory result. The disc-id/URL conventions are already validated against the
live DB (see cd_discid); this module adds the HTTP fetch (injectable + timeout),
a per-URL cache, and the binary `.bin` parser. **Advisory only** — it returns a
result object; no sensor registration, connector, or policy here.

The AccurateRip `.bin` format is a concatenation of per-pressing chunks:

    track_count : 1 byte
    discId1     : uint32 LE
    discId2     : uint32 LE
    cddbId      : uint32 LE
    per track   : confidence (1 byte) + crc (uint32 LE) + crc450 (uint32 LE)

so a chunk is ``13 + 9 * track_count`` bytes; multiple chunks mean multiple
community submissions of the same disc. (The real OK Computer response was
confirmed to be 121 bytes = 13 + 9×12 during disc-id validation.)
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field

from cd_discid import accuraterip_ids, accuraterip_url, cddb_id, ctdb_lookup_url
from cd_toc import CdToc

DEFAULT_TIMEOUT_SECONDS = 10.0
_CHUNK_HEADER = 13  # track_count(1) + discId1/discId2/cddbId(12)
_PER_TRACK = 9  # confidence(1) + crc(4) + crc450(4)

# A fetch returns the response body, or None on 404 / error / timeout.
Fetch = Callable[[str], bytes | None]


@dataclass(frozen=True)
class AccurateRipResult:
    """Advisory AccurateRip lookup result for a disc."""

    found: bool
    pressings: int = 0  # number of community submissions (chunks)
    max_confidence: int = 0  # highest per-track confidence seen
    track_count: int = 0
    per_track_confidence: tuple[int, ...] = field(default_factory=tuple)


def parse_accuraterip(
    data: bytes | None,
    *,
    expected_id1: int,
    expected_id2: int,
    expected_cddb: int,
    expected_track_count: int,
) -> AccurateRipResult:
    """Parse an AccurateRip ``.bin`` body, counting only chunks for *this* disc.

    Each chunk carries its own ``(discId1, discId2, cddbId, track_count)``. A
    chunk is counted only when all four match the expected values for the looked-
    up TOC; chunks for a different disc are skipped (the URL path encodes just a
    few digits of id1, so the body is not trusted blindly). No matching chunk →
    ``found=False``.
    """
    if not data:
        return AccurateRipResult(found=False)

    pressings = 0
    per_track_max: dict[int, int] = {}
    offset = 0
    length = len(data)
    while offset + _CHUNK_HEADER <= length:
        n = data[offset]
        chunk_len = _CHUNK_HEADER + _PER_TRACK * n
        if n == 0 or offset + chunk_len > length:
            break  # malformed or trailing garbage — stop, keep what parsed
        id1 = int.from_bytes(data[offset + 1 : offset + 5], "little")
        id2 = int.from_bytes(data[offset + 5 : offset + 9], "little")
        cddb = int.from_bytes(data[offset + 9 : offset + 13], "little")
        if (id1, id2, cddb, n) == (
            expected_id1,
            expected_id2,
            expected_cddb,
            expected_track_count,
        ):
            base = offset + _CHUNK_HEADER
            for track in range(n):
                confidence = data[base + track * _PER_TRACK]
                per_track_max[track] = max(per_track_max.get(track, 0), confidence)
            pressings += 1
        offset += chunk_len  # advance past matching and non-matching chunks alike

    if pressings == 0:
        return AccurateRipResult(found=False)
    per_track = tuple(per_track_max[i] for i in range(expected_track_count))
    return AccurateRipResult(
        found=True,
        pressings=pressings,
        max_confidence=max(per_track),
        track_count=expected_track_count,
        per_track_confidence=per_track,
    )


def lookup_accuraterip(
    toc: CdToc,
    *,
    fetch: Fetch | None = None,
    cache: MutableMapping[str, AccurateRipResult] | None = None,
) -> AccurateRipResult:
    """Look up a TOC in AccurateRip and return an advisory result.

    Never raises: a fetch error/timeout/404 yields ``found=False``. ``fetch`` is
    injectable for tests; ``cache`` (if given) is keyed by URL so re-lookups of
    the same disc do not re-hit the network.
    """
    url = accuraterip_url(toc)
    if cache is not None and url in cache:
        return cache[url]

    fetcher = fetch or _default_fetch
    try:
        body = fetcher(url)
    except Exception:
        body = None
    id1, id2 = accuraterip_ids(toc)
    result = parse_accuraterip(
        body,
        expected_id1=id1,
        expected_id2=id2,
        expected_cddb=cddb_id(toc),
        expected_track_count=toc.track_count,
    )

    if cache is not None:
        cache[url] = result
    return result


@dataclass(frozen=True)
class CtdbResult:
    """Advisory CTDB (CUETools DB) lookup result for a disc."""

    found: bool
    submissions: int = 0  # matching CTDB entries
    confidence: int = 0  # highest community confidence among matching entries


def _expected_entry_toc(toc: CdToc) -> str:
    """CTDB entry ``toc`` attribute form: 0-based offsets + lead-out, ':'-joined."""
    return ":".join(
        str(frame) for frame in (*toc.track_offsets_frames, toc.leadout_frames)
    )


def parse_ctdb(data: bytes | str | None, *, expected_toc: str) -> CtdbResult:
    """Parse a CTDB lookup response, counting only entries for *this* disc.

    Each ``<entry>`` carries a ``toc`` attribute (0-based offsets + lead-out).
    Entries whose ``toc`` does not match the looked-up disc are ignored, so a
    fuzzy/metadata match for a different pressing cannot be mistaken for a hit.
    No matching entry → ``found=False``.
    """
    if not data:
        return CtdbResult(found=False)
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return CtdbResult(found=False)

    submissions = 0
    best_confidence = 0
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]  # strip XML namespace
        if tag != "entry" or element.get("toc") != expected_toc:
            continue
        submissions += 1
        try:
            best_confidence = max(best_confidence, int(element.get("confidence") or 0))
        except ValueError:
            pass

    if submissions == 0:
        return CtdbResult(found=False)
    return CtdbResult(found=True, submissions=submissions, confidence=best_confidence)


def lookup_ctdb(
    toc: CdToc,
    *,
    fetch: Fetch | None = None,
    cache: MutableMapping[str, CtdbResult] | None = None,
) -> CtdbResult:
    """Look up a TOC in CTDB and return an advisory result. Never raises."""
    url = ctdb_lookup_url(toc)
    if cache is not None and url in cache:
        return cache[url]

    fetcher = fetch or _default_fetch
    try:
        body = fetcher(url)
    except Exception:
        body = None
    result = parse_ctdb(body, expected_toc=_expected_entry_toc(toc))

    if cache is not None:
        cache[url] = result
    return result


def _default_fetch(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> bytes | None:
    """Default HTTP GET: bytes on 200, None on any non-200/error/timeout."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "mintarr/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.getcode() != 200:
                return None
            return response.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None
