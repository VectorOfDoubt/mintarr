"""Read-only Lidarr album catalogue helpers.

Album-level holds need the same identity catalogue in multiple places:
Newznab suppression, dashboard labels, and manual hold validation. Keep the
contract in one module so SQLite/API fallback behavior cannot drift.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("tidalhires.lidarr_catalogue")

AlbumRow = tuple[int, str, str]


def lidarr_db_path() -> Path:
    """Resolve Lidarr's SQLite DB path using Mintarr's shared contract."""
    configured = os.environ.get("MINTARR_LIDARR_DB_PATH")
    if configured:
        return Path(configured)
    config_xml = os.environ.get("LIDARR_CONFIG_XML", "/lidarr-config/config.xml")
    return Path(config_xml).with_name("lidarr.db")


def lidarr_api_url() -> str:
    return os.environ.get("LIDARR_API_URL", "http://host.docker.internal:8686/api/v1")


def lidarr_request_timeout() -> int:
    raw = os.environ.get("MINTARR_LIDARR_REQUEST_TIMEOUT", "30")
    try:
        return max(1, int(raw))
    except ValueError:
        return 30


def lidarr_inventory_timeout() -> int:
    raw = os.environ.get("MINTARR_LIDARR_INVENTORY_TIMEOUT", "120")
    try:
        return max(1, int(raw))
    except ValueError:
        return 120


def lidarr_api_key() -> str:
    """Read Lidarr API key from env or mounted config.xml without logging it."""
    env_key = os.environ.get("LIDARR_API_KEY", "")
    if env_key:
        return env_key
    config_path = os.environ.get("LIDARR_CONFIG_XML", "/lidarr-config/config.xml")
    try:
        content = Path(config_path).read_text()
    except Exception:
        return ""
    match = re.search(r"<ApiKey>([a-f0-9]+)</ApiKey>", content)
    return match.group(1) if match else ""


def read_album_rows_sqlite(db_path: Path | None = None) -> list[AlbumRow]:
    """Read ``(album_id, artist, album)`` from Lidarr SQLite, read-only."""
    path = db_path or lidarr_db_path()
    if not path.exists():
        return []
    rows: list[AlbumRow] = []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            """
            SELECT Albums.Id AS album_id, Albums.Title AS album_title,
                   ArtistMetadata.Name AS artist_name
            FROM Albums
            LEFT JOIN ArtistMetadata
              ON ArtistMetadata.Id = Albums.ArtistMetadataId
            """
        ):
            rows.append(
                (
                    int(row["album_id"]),
                    row["artist_name"] or "",
                    row["album_title"] or "",
                )
            )
    return rows


def _wanted_album_ids(album_ids: list[int]) -> set[int]:
    wanted: set[int] = set()
    for raw in album_ids:
        try:
            album_id = int(raw)
        except (TypeError, ValueError):
            continue
        if album_id > 0:
            wanted.add(album_id)
    return wanted


def read_album_labels_sqlite(
    album_ids: list[int], db_path: Path | None = None
) -> dict[int, dict]:
    """Read dashboard labels for selected album IDs from Lidarr SQLite."""
    wanted = _wanted_album_ids(album_ids)
    if not wanted:
        return {}
    path = db_path or lidarr_db_path()
    if not path.exists():
        return {}

    placeholders = ",".join("?" for _ in wanted)
    rows: dict[int, dict] = {}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            f"""
            SELECT Albums.Id AS album_id, Albums.Title AS album_title,
                   ArtistMetadata.Name AS artist_name
            FROM Albums
            LEFT JOIN ArtistMetadata
              ON ArtistMetadata.Id = Albums.ArtistMetadataId
            WHERE Albums.Id IN ({placeholders})
            """,
            tuple(sorted(wanted)),
        ):
            album_id = int(row["album_id"])
            rows[album_id] = _label_entry(
                album_id,
                row["artist_name"] or "",
                row["album_title"] or "",
            )
    return rows


def _album_artist_name(album: dict[str, Any], fallback: str = "") -> str:
    artist = album.get("artist")
    if isinstance(artist, dict):
        for key in ("artistName", "name", "title"):
            value = artist.get(key)
            if value:
                return str(value)
    for key in ("artistName", "artist"):
        value = album.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


def _album_title(album: dict[str, Any]) -> str:
    value = album.get("title") or album.get("albumTitle")
    return str(value) if value else ""


def _album_id(album: dict[str, Any]) -> int | None:
    raw = album.get("id") or album.get("albumId")
    if raw is None:
        return None
    try:
        album_id = int(raw)
    except (TypeError, ValueError):
        return None
    return album_id if album_id > 0 else None


def _rows_from_albums(
    albums: list[dict[str, Any]], artist_name: str = ""
) -> list[AlbumRow]:
    rows: list[AlbumRow] = []
    seen: set[int] = set()
    for album in albums:
        if not isinstance(album, dict):
            continue
        album_id = _album_id(album)
        if album_id is None or album_id in seen:
            continue
        title = _album_title(album)
        if not title:
            continue
        seen.add(album_id)
        rows.append((album_id, _album_artist_name(album, artist_name), title))
    return rows


def read_album_rows_api(
    *,
    api: str | None = None,
    key: str | None = None,
    get: Callable[..., Any] | None = None,
) -> list[AlbumRow]:
    """Read ``(album_id, artist, album)`` via Lidarr API.

    Prefer the global ``/album`` endpoint. If that fails, fall back to
    ``/artist`` + per-artist ``/album?artistId=...`` so installs with stricter
    or older Lidarr behavior can still resolve holds.
    """
    api = (api or lidarr_api_url()).rstrip("/")
    key = key if key is not None else lidarr_api_key()
    get = get or requests.get
    headers = {"X-Api-Key": key}

    try:
        response = get(
            f"{api}/album",
            headers=headers,
            timeout=lidarr_inventory_timeout(),
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return _rows_from_albums(payload)
    except Exception as exc:
        log.warning("Lidarr /album catalogue failed; falling back to /artist: %s", exc)

    response = get(
        f"{api}/artist",
        headers=headers,
        timeout=lidarr_inventory_timeout(),
    )
    response.raise_for_status()
    artists = response.json()
    if not isinstance(artists, list):
        raise RuntimeError("Lidarr /artist returned non-list")

    rows: list[AlbumRow] = []
    seen: set[int] = set()
    for artist in artists:
        if not isinstance(artist, dict) or artist.get("id") is None:
            continue
        artist_id = artist.get("id")
        artist_name = str(
            artist.get("artistName") or artist.get("name") or artist.get("title") or ""
        )
        albums_resp = get(
            f"{api}/album?artistId={artist_id}",
            headers=headers,
            timeout=lidarr_request_timeout(),
        )
        albums_resp.raise_for_status()
        albums = albums_resp.json()
        if not isinstance(albums, list):
            continue
        for row in _rows_from_albums(albums, artist_name):
            if row[0] in seen:
                continue
            seen.add(row[0])
            rows.append(row)
    return rows


def read_album_labels_api(
    album_ids: list[int],
    *,
    api: str | None = None,
    key: str | None = None,
    get: Callable[..., Any] | None = None,
) -> dict[int, dict]:
    """Read dashboard labels for selected album IDs via targeted Lidarr API calls."""
    wanted = _wanted_album_ids(album_ids)
    if not wanted:
        return {}
    api = (api or lidarr_api_url()).rstrip("/")
    key = key if key is not None else lidarr_api_key()
    get = get or requests.get
    headers = {"X-Api-Key": key}

    rows: dict[int, dict] = {}
    for requested_id in sorted(wanted):
        try:
            response = get(
                f"{api}/album/{requested_id}",
                headers=headers,
                timeout=lidarr_request_timeout(),
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            log.warning(
                "Lidarr album label lookup failed for %s: %s", requested_id, exc
            )
            continue
        if not isinstance(payload, dict):
            continue
        album_id = _album_id(payload) or requested_id
        if album_id != requested_id:
            continue
        title = _album_title(payload)
        if not title:
            continue
        rows[requested_id] = _label_entry(
            requested_id,
            _album_artist_name(payload),
            title,
        )
    return rows


def read_album_rows(*, prefer_sqlite: bool = True) -> list[AlbumRow]:
    """Read Lidarr album catalogue using SQLite first, then API fallback."""
    if prefer_sqlite:
        try:
            rows = read_album_rows_sqlite()
            if rows:
                return rows
        except Exception as exc:
            log.warning("Lidarr SQLite catalogue failed; falling back to API: %s", exc)
    try:
        return read_album_rows_api()
    except Exception as exc:
        log.warning("Lidarr API catalogue failed; returning empty catalogue: %s", exc)
        return []


def label_map(album_ids: list[int]) -> dict[int, dict]:
    """Return dashboard labels for selected album IDs from the shared catalogue.

    Keep this lookup targeted. Dashboard rows and manual hold creation often need
    one album label; API fallback must not crawl the full Lidarr catalogue.
    """
    wanted = _wanted_album_ids(album_ids)
    if not wanted:
        return {}

    out: dict[int, dict] = {}
    try:
        out.update(read_album_labels_sqlite(sorted(wanted)))
    except Exception as exc:
        log.warning("Lidarr SQLite label lookup failed; falling back to API: %s", exc)

    missing = sorted(wanted - set(out))
    if missing:
        try:
            out.update(read_album_labels_api(missing))
        except Exception as exc:
            log.warning(
                "Lidarr API label lookup failed; returning partial labels: %s", exc
            )
    return out


def _label_entry(album_id: int, artist: str, album: str) -> dict:
    return {
        "artist": artist,
        "album": album,
        "lidarr_url": f"{_lidarr_web_base()}/album/{album_id}",
    }


def _lidarr_web_base() -> str:
    return os.environ.get("LIDARR_WEB_URL", "http://127.0.0.1:8686").rstrip("/")
