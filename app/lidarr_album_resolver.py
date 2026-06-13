"""Pure Lidarr-album resolver for #160 album-hold search suppression (slice 3).

Newznab search requests from Lidarr carry only text (``artist``/``album`` or a
combined ``q``), never Lidarr's internal ``albumId``. To suppress held albums in
Mintarr's own Newznab feed *safely*, we resolve a search request to exactly one
trusted Lidarr ``albumId`` against Lidarr's own album catalogue — Lidarr is the
source of truth, so this is a bridge, not a guess.

This module is intentionally pure: no Flask, requests, filesystem, env, or global
state. The runtime is responsible for loading the catalogue (read-only from
Lidarr's SQLite/API); this module only normalizes and matches it.

Hard rule: **abstain on ambiguity.** Zero matches, multiple matches, or missing
fields all return ``album_id=None``. The caller must never suppress on an abstain.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


def normalize(value: str | None) -> str:
    """Normalize an artist/album/query string for exact matching.

    Lowercase, strip diacritics, replace any run of non-alphanumeric characters
    with a single space, and collapse whitespace. Deliberately conservative — it
    does NOT strip edition words ("deluxe", "remaster"), articles, or years, so a
    different edition normalizes to a different string and is never collapsed into
    the standard album.
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = stripped.lower()
    spaced = re.sub(r"[^a-z0-9]+", " ", lowered)
    return " ".join(spaced.split())


@dataclass(frozen=True)
class AlbumCatalogueEntry:
    """One Lidarr album, with its artist/album strings pre-normalized."""

    album_id: int
    artist_norm: str
    album_norm: str

    @property
    def combined_norm(self) -> str:
        return f"{self.artist_norm} {self.album_norm}".strip()


def build_catalogue(
    rows: list[tuple[int, str, str]] | list[dict],
) -> list[AlbumCatalogueEntry]:
    """Build normalized catalogue entries from raw ``(album_id, artist, album)``.

    Accepts tuples or dicts (``album_id``/``artist``/``album`` keys). Rows with a
    non-positive/invalid album_id or an empty normalized album are dropped — they
    can never be a safe unique match.
    """
    entries: list[AlbumCatalogueEntry] = []
    for row in rows:
        if isinstance(row, dict):
            raw_id = row.get("album_id")
            artist = row.get("artist") or ""
            album = row.get("album") or ""
        else:
            raw_id, artist, album = row[0], row[1], row[2]
        if raw_id is None:
            continue
        try:
            album_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if album_id <= 0:
            continue
        album_norm = normalize(album)
        if not album_norm:
            continue
        entries.append(
            AlbumCatalogueEntry(
                album_id=album_id,
                artist_norm=normalize(artist),
                album_norm=album_norm,
            )
        )
    return entries


@dataclass(frozen=True)
class AlbumMatch:
    """Result of resolving a search request to a Lidarr album.

    ``album_id`` is ``None`` whenever the match is not a confident, unique one.
    ``method`` and ``candidate_count`` exist for audit/logging and to make the
    abstain reason (nothing vs. ambiguous) explicit and testable.
    """

    album_id: int | None
    method: str  # "artist_album" | "q_exact" | "none"
    candidate_count: int

    @property
    def resolved(self) -> bool:
        return self.album_id is not None


_ABSTAIN = AlbumMatch(album_id=None, method="none", candidate_count=0)


def _unique(album_ids: set[int], method: str) -> AlbumMatch:
    if len(album_ids) == 1:
        return AlbumMatch(
            album_id=next(iter(album_ids)), method=method, candidate_count=1
        )
    return AlbumMatch(album_id=None, method=method, candidate_count=len(album_ids))


def resolve_album_id(
    catalogue: list[AlbumCatalogueEntry],
    *,
    artist: str | None = "",
    album: str | None = "",
    q: str | None = "",
    allow_q_fallback: bool = False,
) -> AlbumMatch:
    """Resolve a Newznab search to exactly one Lidarr albumId, or abstain.

    Priority:
    1. ``artist`` + ``album`` (the ``t=music`` shape Lidarr uses for album search):
       match on exact normalized artist AND album. The authoritative path.
    2. ``q`` only, and only when ``allow_q_fallback`` is enabled: match the
       normalized query exactly against the catalogue's normalized
       ``"<artist> <album>"`` (and album-only as a secondary). Conservative,
       off by default, because a combined query is inherently weaker evidence.

    Any case that is not a single unique match abstains (``album_id=None``).
    """
    artist_norm = normalize(artist)
    album_norm = normalize(album)
    if artist_norm and album_norm:
        ids = {
            e.album_id
            for e in catalogue
            if e.artist_norm == artist_norm and e.album_norm == album_norm
        }
        return _unique(ids, "artist_album")

    if allow_q_fallback:
        q_norm = normalize(q)
        if q_norm:
            ids = {e.album_id for e in catalogue if e.combined_norm == q_norm}
            if not ids:
                ids = {e.album_id for e in catalogue if e.album_norm == q_norm}
            return _unique(ids, "q_exact")

    return _ABSTAIN
