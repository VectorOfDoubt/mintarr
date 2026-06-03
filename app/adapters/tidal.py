"""TidalAdapter — TIDAL source extracted from server.py (F3.1).

Module-level helpers (get_session, search_albums, classify_quality,
release_title) live here so the adapter never imports from server.py.
server.py re-exports them as private aliases for backwards-compat with
existing call sites and tests.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from .base import RawDownload, ReleaseCandidate
from .context import PipelineContext

log = logging.getLogger("tidalhires.adapter.tidal")


# ---- TIDAL session (module-level cache, mirrors prior server.py state) ----

_session = None
_session_lock = threading.Lock()


def _use_pkce_oauth() -> bool:
    value = os.environ.get("TIDAL_OAUTH_PKCE", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def get_session():
    """Load OAuth token from TIDAL_DL_NG_CONFIG and create a tidalapi.Session.

    Cached at module level — first call performs the OAuth handshake,
    subsequent calls return the cached session. Thread-safe via _session_lock.
    """
    global _session
    with _session_lock:
        if _session is not None:
            return _session
        import tidalapi

        s = tidalapi.Session()
        config_dir = Path(
            os.environ.get("TIDAL_DL_NG_CONFIG", "/root/.config/tidal_dl_ng-dev")
        )
        token_file = config_dir / "token.json"
        if not token_file.exists():
            raise RuntimeError(
                f"No TIDAL token found at {token_file} — run 'tidal-dl-ng login' first"
            )
        t = json.loads(token_file.read_text())
        ok = s.load_oauth_session(
            t.get("token_type", "Bearer"),
            t["access_token"],
            t.get("refresh_token"),
            t.get("expiry_time"),
            is_pkce=_use_pkce_oauth(),
        )
        if not ok:
            raise RuntimeError(
                "tidalapi.load_oauth_session failed — token expired? Re-login."
            )
        _session = s
        log.info("TIDAL session loaded")
        return _session


def reset_session_cache() -> None:
    """Test helper: drop cached session so the next get_session() re-handshakes."""
    global _session
    with _session_lock:
        _session = None


def classify_quality(album) -> tuple[str, int]:
    """Return (quality_tag, estimated size in bytes).

    The TIDAL API always reports LOSSLESS for HiRes albums with our standard
    token (HiRes sits behind a specific client-ID that the Radexito fork
    spoofs on download). We therefore tag every TIDAL match optimistically
    as "FLAC 24bit" — Radexito consistently delivers 24-bit/88+ kHz when the
    album has a HiRes master.

    Consequence if the album is *actually* only 16/44: the file is stored as
    FLAC in the library, and Lidarr's own ffprobe detection at import time
    sets the correct quality based on the actual bits/sample-rate. With
    upgradeAllowed=true Lidarr will re-search later if the cutoff is not yet
    reached.
    """
    duration = album.duration or 0
    bitrate = 4608  # 24*2*96000 = 4.608 Mbps for stereo
    size = int(bitrate * 1000 * duration / 8)
    return ("FLAC 24bit", size)


def release_title(album, quality_tag: str) -> str:
    """Build a release title that Lidarr's quality parser recognises.
    Format: '<Artist> - <Album> (<Year>) [TIDAL] [<Quality>]'
    """
    artist = album.artist.name if album.artist else "Unknown"
    title = album.name
    year = album.release_date.year if album.release_date else 0
    return f"{artist} - {title} ({year}) [TIDAL] [{quality_tag}]"


def search_albums(
    query: str,
    artist_hint: str = "",
    album_hint: str = "",
    limit: int = 25,
) -> list:
    """Search Tidal albums. Returns a list of tidalapi.Album."""
    s = get_session()
    q = " ".join(filter(None, [artist_hint, album_hint, query])).strip()
    if not q:
        return []
    log.info("TIDAL search: %r", q)
    try:
        res = s.search(q, models=[s.album_model_class] if False else None)
    except Exception:
        log.exception("Search failed")
        return []
    albums = []
    if isinstance(res, dict):
        albums = list(res.get("albums") or [])
    elif hasattr(res, "albums"):
        albums = list(res.albums or [])
    if album_hint:
        ah = album_hint.lower()
        albums = [a for a in albums if a.name and ah in a.name.lower()]
    if artist_hint:
        artist_h = artist_hint.lower()
        albums = [a for a in albums if a.artist and artist_h in a.artist.name.lower()]

    def _album_rank(a):
        nt = getattr(a, "num_tracks", 0) or 0
        atype = (getattr(a, "type", "") or "").upper()
        type_bonus = {"ALBUM": 100, "EP": 50, "SINGLE": 0}.get(atype, 25)
        return -(type_bonus + nt)

    albums.sort(key=_album_rank)
    return albums[:limit]


# ---- Adapter ----------------------------------------------------------------


class TidalAdapter:
    """TIDAL HiRes via Radexito-fork of tidal-dl-ng.

    Tokens are managed externally (tidal-dl-ng-test container handles OAuth).
    This adapter only invokes `tidal-dl-ng dl <url>` and reports back the
    raw files-directory; the rest of the pipeline is source-agnostic.
    """

    name = "tidal"
    source_type = "tidal"

    def __init__(self, *, config_dir: str | None = None) -> None:
        self._config_dir = Path(
            config_dir
            or os.environ.get("TIDAL_DL_NG_CONFIG", "/root/.config/tidal_dl_ng-dev")
        )

    def is_enabled(self) -> bool:
        return (self._config_dir / "token.json").exists()

    def search(
        self,
        query: str,
        artist: str = "",
        album: str = "",
        year: int | None = None,
    ) -> list[ReleaseCandidate]:
        """F3.1: implemented but unused — newznab() endpoint still calls
        search_albums() directly. F3.3 will route through this method.
        """
        try:
            albums = search_albums(query, artist, album, limit=25)
        except Exception:
            log.exception("TIDAL search failed for query=%r", query)
            return []
        candidates: list[ReleaseCandidate] = []
        for a in albums:
            quality_tag, size = classify_quality(a)
            candidates.append(
                ReleaseCandidate(
                    source_type=self.source_type,
                    source_id=str(a.id),
                    title=release_title(a, quality_tag),
                    artist=a.artist.name if a.artist else "Unknown",
                    album=a.name or "",
                    year=a.release_date.year if a.release_date else None,
                    quality_tag=quality_tag,
                    size_bytes=int(size),
                    download_url=f"tidal:{a.id}",
                    priority=50,
                )
            )
        return candidates

    def download_raw(
        self,
        candidate_id: str,
        ctx: PipelineContext,
    ) -> RawDownload:
        """Run `tidal-dl-ng dl https://tidal.com/album/<id>` into ctx.raw_dir.

        Raises worker.JobCancelled if ctx.check_cancelled() trips.
        Raises RuntimeError on subprocess failure (treated as permanent by
        F2.5 retry policy unless the error string matches the transient
        allow-list).
        """
        album_id = int(candidate_id)
        url = f"https://tidal.com/album/{album_id}"
        ctx.raw_dir.mkdir(parents=True, exist_ok=True)

        ctx.check_cancelled()
        ctx.set_progress(
            stage="configuring", percent=5, message="Configuring tidal-dl-ng"
        )
        # cfg-step goes through ctx.run_subprocess so cancel signals reach it
        # and a stuck cfg doesn't ignore the worker's cancel state. Short
        # 30s timeout matches the original direct subprocess.run timeout.
        cfg = ctx.run_subprocess(
            ["tidal-dl-ng", "cfg", "download_base_path", str(ctx.raw_dir)],
            timeout=30,
        )
        if cfg.returncode != 0:
            raise RuntimeError(
                f"tidal-dl-ng cfg exited {cfg.returncode}: {(cfg.stderr or '')[-200:]}"
            )

        ctx.check_cancelled()
        ctx.set_progress(
            stage="downloading", percent=10, message="Downloading from TIDAL"
        )
        log.info("[%s] TIDAL dl album=%s → %s", ctx.jid, album_id, ctx.raw_dir)
        result = ctx.run_subprocess(
            ["tidal-dl-ng", "dl", url],
            timeout=3600,
        )
        log.info("[%s] tidal-dl-ng exit=%s", ctx.jid, result.returncode)
        if result.stderr:
            log.warning("[%s] tidal-dl-ng stderr: %s", ctx.jid, result.stderr[-500:])
        if result.returncode != 0:
            raise RuntimeError(
                f"tidal-dl-ng exited {result.returncode}: {(result.stderr or '')[-200:]}"
            )

        m4a_files = list(ctx.raw_dir.rglob("*.m4a"))
        flac_files = list(ctx.raw_dir.rglob("*.flac"))
        file_count = len(m4a_files) + len(flac_files)
        if file_count == 0:
            raise RuntimeError("no audio files downloaded")
        total_bytes = sum(
            f.stat().st_size for f in (*m4a_files, *flac_files) if f.is_file()
        )
        ctx.set_progress(
            stage="downloaded",
            percent=45,
            message="Download complete",
            m4a_files=len(m4a_files),
            flac_files=len(flac_files),
        )
        return RawDownload(
            files_dir=ctx.raw_dir,
            file_count=file_count,
            total_bytes=int(total_bytes),
        )

    def cleanup(self, jid: str, ctx: PipelineContext) -> None:
        # server._raise_if_job_cancelled already calls _mark_download_cancelled,
        # which rmtrees ctx.raw_dir / ctx.output_dir when appropriate.
        return None
