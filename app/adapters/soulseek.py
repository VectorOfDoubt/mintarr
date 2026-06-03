"""Soulseek adapter.

F3.5a ingests folders already completed by slskd/Soulseek. F3.5b adds a
small slskd HTTP search/download path while preserving completed-folder ingest.
Source files are copied, never moved or deleted.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .base import RawDownload, ReleaseCandidate
from .context import PipelineContext
from .local_folder import hash_rel

log = logging.getLogger("tidalhires.adapter.soulseek")

_SUPPORTED_AUDIO_SUFFIXES = (".flac", ".m4a")
_PARTIAL_SUFFIXES = (
    ".part",
    ".partial",
    ".tmp",
    ".download",
    ".crdownload",
    ".incomplete",
)
_SLSKD_PREFIX = "slskd:"
_SLSKD_SUCCESS_STATES = ("Succeeded", "Completed")
_SLSKD_FAILURE_STATES = ("Errored", "Rejected", "Cancelled", "TimedOut", "Aborted")


@dataclass(frozen=True)
class SlskdDownloadFile:
    filename: str
    size: int


@dataclass(frozen=True)
class SlskdDownloadRequest:
    username: str
    files: tuple[SlskdDownloadFile, ...]
    title: str
    search_text: str


def _env_bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


class SoulseekCompletedAdapter:
    name = "soulseek"
    source_type = "soulseek"

    def __init__(
        self,
        *,
        download_root: str | None = None,
        enabled: bool | None = None,
        max_files: int | None = None,
        max_bytes: int | None = None,
        settle_seconds: float | None = None,
        search_enabled: bool | None = None,
        slskd_api_url: str | None = None,
        slskd_api_key: str | None = None,
        search_timeout: int | None = None,
        search_response_limit: int | None = None,
        search_file_limit: int | None = None,
        min_tracks: int | None = None,
        download_timeout: int | None = None,
        poll_seconds: float | None = None,
    ) -> None:
        self._download_root = Path(
            download_root or os.environ.get("SOULSEEK_DOWNLOAD_ROOT", "")
        )
        self._enabled_override = enabled
        self._max_files = max_files
        self._max_bytes = max_bytes
        self._settle_seconds = settle_seconds
        self._search_enabled = search_enabled
        self._slskd_api_url = slskd_api_url
        self._slskd_api_key = slskd_api_key
        self._search_timeout = search_timeout
        self._search_response_limit = search_response_limit
        self._search_file_limit = search_file_limit
        self._min_tracks = min_tracks
        self._download_timeout = download_timeout
        self._poll_seconds = poll_seconds

    @property
    def max_files(self) -> int:
        return (
            self._max_files
            if self._max_files is not None
            else _env_int("SOULSEEK_MAX_FILES", 300)
        )

    @property
    def max_bytes(self) -> int:
        return (
            self._max_bytes
            if self._max_bytes is not None
            else _env_int("SOULSEEK_MAX_BYTES", 0)
        )

    @property
    def settle_seconds(self) -> float:
        if self._settle_seconds is not None:
            return max(0.0, float(self._settle_seconds))
        return float(_env_int("SOULSEEK_SETTLE_SECONDS", 10))

    @property
    def slskd_api_url(self) -> str:
        raw = self._slskd_api_url or os.environ.get("SLSKD_API_URL", "")
        return raw.rstrip("/")

    @property
    def slskd_api_key(self) -> str:
        return self._slskd_api_key or os.environ.get("SLSKD_API_KEY", "")

    @property
    def search_timeout(self) -> int:
        value = self._search_timeout
        if value is None:
            value = _env_int("SOULSEEK_SEARCH_TIMEOUT", 8)
        return max(5, int(value))

    @property
    def search_response_limit(self) -> int:
        value = self._search_response_limit
        if value is None:
            value = _env_int("SOULSEEK_SEARCH_RESPONSE_LIMIT", 5)
        return max(1, int(value))

    @property
    def search_file_limit(self) -> int:
        value = self._search_file_limit
        if value is None:
            value = _env_int("SOULSEEK_SEARCH_FILE_LIMIT", 500)
        return max(1, int(value))

    @property
    def min_tracks(self) -> int:
        value = self._min_tracks
        if value is None:
            value = _env_int("SOULSEEK_MIN_TRACKS", 2)
        return max(1, int(value))

    @property
    def download_timeout(self) -> int:
        value = self._download_timeout
        if value is None:
            value = _env_int("SOULSEEK_DOWNLOAD_TIMEOUT", 3600)
        return max(30, int(value))

    @property
    def poll_seconds(self) -> float:
        value = self._poll_seconds
        if value is None:
            value = _env_int("SOULSEEK_POLL_SECONDS", 5)
        return max(0.1, float(value))

    def is_enabled(self) -> bool:
        enabled = self._enabled_override
        if enabled is None:
            enabled = _env_bool("SOULSEEK_ENABLED")
        return bool(enabled) and self._download_root.is_dir()

    def slskd_is_configured(self) -> bool:
        return bool(self.slskd_api_url and self.slskd_api_key)

    def search(
        self,
        query: str,
        artist: str = "",
        album: str = "",
        year: int | None = None,
    ) -> list[ReleaseCandidate]:
        """Search slskd and expose conservative folder-level candidates."""
        search_enabled = self._search_enabled
        if search_enabled is None:
            search_enabled = _env_bool("SOULSEEK_SEARCH_ENABLED")
        if (
            not search_enabled
            or not self.is_enabled()
            or not self.slskd_is_configured()
        ):
            return []
        search_text = self._search_text(query=query, artist=artist, album=album)
        if not search_text:
            return []

        try:
            search_payload = self._slskd_post(
                "/searches",
                {
                    "searchText": search_text,
                    "responseLimit": self.search_response_limit,
                    "fileLimit": self.search_file_limit,
                    "searchTimeout": self.search_timeout * 1000,
                    "minimumResponseFileCount": self.min_tracks,
                    "filterResponses": True,
                },
                timeout=self.search_timeout + 5,
            )
        except Exception:
            log.exception("Soulseek slskd search failed for %r", search_text)
            return []

        search_id = search_payload.get("id")
        if not search_id:
            return []
        responses = self._wait_for_search_responses(str(search_id))
        return self._responses_to_candidates(
            responses,
            search_text=search_text,
            artist=artist,
            album=album,
            year=year,
        )

    def normalize_candidate_id(self, rel_path: str) -> str:
        if rel_path.startswith(_SLSKD_PREFIX):
            self._decode_slskd_source_id(rel_path)
            return rel_path
        src = self.resolve_source_dir(rel_path, check_completed=True)
        return src.relative_to(self._download_root.resolve()).as_posix()

    def title_for_candidate_id(self, source_id: str) -> str:
        if source_id.startswith(_SLSKD_PREFIX):
            return self._decode_slskd_source_id(source_id).title
        return f"[Soulseek] {source_id}"

    def resolve_source_dir(self, rel_path: str, *, check_completed: bool) -> Path:
        if not rel_path or Path(rel_path).is_absolute():
            raise RuntimeError("soulseek path must be relative")
        root = self._download_root.resolve()
        raw_src = root / rel_path
        cur = root
        for part in Path(rel_path).parts:
            cur = cur / part
            if cur.is_symlink():
                raise RuntimeError(f"symlink blocked: {Path(rel_path)}")
        src = raw_src.resolve()
        if not src.is_relative_to(root):
            raise RuntimeError(f"path traversal blocked: {rel_path}")
        if not src.is_dir():
            raise RuntimeError(f"soulseek source not a directory: {src}")
        if check_completed:
            self._validate_completed_dir(src)
        return src

    def _snapshot(self, src: Path) -> tuple[dict[str, tuple[int, int]], int, int, int]:
        files: dict[str, tuple[int, int]] = {}
        audio_files = 0
        total_bytes = 0
        partial_files = 0
        root = self._download_root.resolve()
        for f in src.rglob("*"):
            if f.is_symlink():
                raise RuntimeError(f"symlink blocked: {f.relative_to(src)}")
            if not f.is_file():
                continue
            if not f.resolve().is_relative_to(root):
                raise RuntimeError(f"path escape blocked: {f}")
            rel = f.relative_to(src).as_posix()
            if f.name.lower().endswith(_PARTIAL_SUFFIXES):
                partial_files += 1
            stat = f.stat()
            files[rel] = (stat.st_size, stat.st_mtime_ns)
            total_bytes += stat.st_size
            if f.suffix.lower() in _SUPPORTED_AUDIO_SUFFIXES:
                audio_files += 1
        return files, audio_files, total_bytes, partial_files

    def _validate_completed_dir(self, src: Path) -> tuple[int, int, int]:
        first, audio_files, total_bytes, partial_files = self._snapshot(src)
        if partial_files:
            raise RuntimeError("soulseek folder has partial download markers")
        if not audio_files:
            raise RuntimeError(f"no supported audio files found in {src}")
        if len(first) > self.max_files:
            raise RuntimeError(
                f"soulseek folder has too many files: {len(first)} > {self.max_files}"
            )
        if self.max_bytes and total_bytes > self.max_bytes:
            raise RuntimeError(
                f"soulseek folder exceeds max bytes: {total_bytes} > {self.max_bytes}"
            )
        settle_seconds = self.settle_seconds
        if settle_seconds:
            time.sleep(settle_seconds)
            second, audio_files_2, total_bytes_2, partial_files_2 = self._snapshot(src)
            if partial_files_2:
                raise RuntimeError("soulseek folder has partial download markers")
            if (
                second != first
                or audio_files_2 != audio_files
                or total_bytes_2 != total_bytes
            ):
                raise RuntimeError("soulseek folder not settled")
        return len(first), audio_files, total_bytes

    def download_raw(
        self,
        candidate_id: str,
        ctx: PipelineContext,
    ) -> RawDownload:
        if candidate_id.startswith(_SLSKD_PREFIX):
            return self._download_raw_from_slskd(candidate_id, ctx)
        src = self.resolve_source_dir(candidate_id, check_completed=True)
        root_resolved = self._download_root.resolve()

        ctx.raw_dir.mkdir(parents=True, exist_ok=True)
        ctx.set_progress(stage="copying", percent=10, message="Copying Soulseek files")

        copied = 0
        audio_files = 0
        total_bytes = 0
        for f in src.rglob("*"):
            ctx.check_cancelled()
            if f.is_symlink():
                raise RuntimeError(f"symlink blocked: {f.relative_to(src)}")
            if not f.is_file():
                continue
            if not f.resolve().is_relative_to(root_resolved):
                raise RuntimeError(f"path escape blocked: {f}")
            rel = f.relative_to(src)
            dst = ctx.raw_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(f), str(dst))
            copied += 1
            if f.suffix.lower() in _SUPPORTED_AUDIO_SUFFIXES:
                audio_files += 1
            total_bytes += f.stat().st_size

        if audio_files == 0:
            raise RuntimeError(f"no supported audio files copied from {src}")

        ctx.set_progress(
            stage="copied",
            percent=45,
            message="Soulseek copy complete",
            file_count=copied,
            audio_files=audio_files,
            size_bytes=total_bytes,
        )
        return RawDownload(
            files_dir=ctx.raw_dir,
            file_count=copied,
            total_bytes=int(total_bytes),
        )

    def cleanup(self, jid: str, ctx: PipelineContext) -> None:
        return None

    def _search_text(self, *, query: str, artist: str, album: str) -> str:
        parts = [artist.strip(), album.strip()]
        text = " ".join(p for p in parts if p)
        if not text:
            text = query.strip()
        suffix = os.environ.get("SOULSEEK_SEARCH_SUFFIX", "").strip()
        if suffix and suffix.lower() not in text.lower():
            text = f"{text} {suffix}"
        return text

    def _api_headers(self) -> dict[str, str]:
        return {"X-API-Key": self.slskd_api_key}

    def _api_url(self, path: str) -> str:
        return f"{self.slskd_api_url}/api/v0{path}"

    def _slskd_get(self, path: str, *, timeout: int = 15):
        import requests

        response = requests.get(
            self._api_url(path), headers=self._api_headers(), timeout=timeout
        )
        response.raise_for_status()
        return response.json()

    def _slskd_post(self, path: str, payload, *, timeout: int = 15):
        import requests

        response = requests.post(
            self._api_url(path),
            headers={**self._api_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()

    def _wait_for_search_responses(self, search_id: str) -> list[dict]:
        deadline = time.monotonic() + self.search_timeout + 1
        last: dict | None = None
        while time.monotonic() < deadline:
            last = self._slskd_get(f"/searches/{search_id}", timeout=10)
            responses = last.get("responses") or []
            if last.get("isComplete") or responses:
                break
            time.sleep(0.5)
        if last and last.get("responses"):
            return list(last.get("responses") or [])
        try:
            responses = self._slskd_get(f"/searches/{search_id}/responses", timeout=10)
        except Exception:
            return []
        return list(responses or [])

    def _responses_to_candidates(
        self,
        responses: list[dict],
        *,
        search_text: str,
        artist: str,
        album: str,
        year: int | None,
    ) -> list[ReleaseCandidate]:
        candidates: list[ReleaseCandidate] = []
        seen: set[str] = set()
        for response in responses:
            username = str(
                response.get("username") or response.get("user") or ""
            ).strip()
            if not username:
                continue
            grouped: dict[str, list[dict]] = {}
            for file_info in response.get("files") or []:
                filename = str(file_info.get("filename") or "").strip()
                if (
                    not filename
                    or Path(filename).suffix.lower() not in _SUPPORTED_AUDIO_SUFFIXES
                ):
                    continue
                parent = self._remote_parent(filename)
                grouped.setdefault(parent, []).append(file_info)

            for parent, files in grouped.items():
                if len(files) < self.min_tracks:
                    continue
                files = sorted(
                    files,
                    key=lambda f: self._remote_filename_sort_key(
                        str(f.get("filename") or "")
                    ),
                )
                if len(files) > self.max_files:
                    files = files[: self.max_files]
                total = sum(int(f.get("size") or 0) for f in files)
                if self.max_bytes and total > self.max_bytes:
                    continue
                request = SlskdDownloadRequest(
                    username=username,
                    files=tuple(
                        SlskdDownloadFile(
                            str(f.get("filename") or ""), int(f.get("size") or 0)
                        )
                        for f in files
                    ),
                    title=self._candidate_title(parent, artist=artist, album=album),
                    search_text=search_text,
                )
                source_id = self._encode_slskd_source_id(request)
                if source_id in seen:
                    continue
                seen.add(source_id)
                quality = (
                    "FLAC"
                    if all(
                        Path(f.filename).suffix.lower() == ".flac"
                        for f in request.files
                    )
                    else "FLAC/M4A"
                )
                candidates.append(
                    ReleaseCandidate(
                        source_type=self.source_type,
                        source_id=source_id,
                        title=f"{request.title} ({quality}) [Soulseek]",
                        artist=artist or "Soulseek",
                        album=album or self._display_album(parent),
                        year=year,
                        quality_tag=quality,
                        size_bytes=total,
                        download_url=f"soulseek:{source_id}",
                        priority=10,
                        extra={"username": username, "file_count": len(request.files)},
                    )
                )
                if len(candidates) >= self.search_response_limit:
                    return candidates
        return candidates

    def _candidate_title(self, parent: str, *, artist: str, album: str) -> str:
        if artist and album:
            return f"{artist} - {album}"
        display_album = self._display_album(parent)
        if artist:
            return f"{artist} - {display_album}"
        return display_album

    def _display_album(self, parent: str) -> str:
        clean = parent.replace("\\", "/").strip("/")
        if not clean:
            return "Download"
        return clean.split("/")[-1]

    def _remote_parent(self, filename: str) -> str:
        normalized = filename.replace("\\", "/")
        if "/" not in normalized:
            return ""
        return normalized.rsplit("/", 1)[0]

    def _remote_filename_sort_key(self, filename: str) -> tuple[int, str]:
        name = Path(filename.replace("\\", "/")).name
        digits = ""
        for ch in name:
            if ch.isdigit():
                digits += ch
            elif digits:
                break
        return (int(digits) if digits else 9999, name.lower())

    def _encode_slskd_source_id(self, request: SlskdDownloadRequest) -> str:
        payload = self._slskd_request_payload(request)
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        token = hashlib.sha256(raw).hexdigest()[:24]
        self._store_slskd_request(token, payload)
        return _SLSKD_PREFIX + token

    def _slskd_request_payload(self, request: SlskdDownloadRequest) -> dict:
        return {
            "v": 1,
            "u": request.username,
            "t": request.title,
            "q": request.search_text,
            "f": [
                {"filename": item.filename, "size": item.size} for item in request.files
            ],
        }

    def _candidate_cache_path(self) -> Path:
        return Path(
            os.environ.get(
                "SOULSEEK_CANDIDATE_CACHE", "/config/soulseek_candidates.json"
            )
        )

    def _store_slskd_request(self, token: str, payload: dict) -> None:
        path = self._candidate_cache_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing: dict = {}
            if path.exists():
                existing = json.loads(path.read_text(encoding="utf-8") or "{}")
                if not isinstance(existing, dict):
                    existing = {}
            existing[token] = payload
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(existing, separators=(",", ":"), ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception:
            log.exception("failed to store Soulseek slskd candidate token")

    def _load_slskd_request_payload(self, token: str) -> dict | None:
        path = self._candidate_cache_path()
        try:
            if not path.exists():
                return None
            payloads = json.loads(path.read_text(encoding="utf-8") or "{}")
            if isinstance(payloads, dict):
                payload = payloads.get(token)
                if isinstance(payload, dict):
                    return payload
        except Exception:
            log.exception("failed to load Soulseek slskd candidate token")
        return None

    def _decode_legacy_slskd_source_id(self, token: str) -> dict:
        padded = token + ("=" * (-len(token) % 4))
        return json.loads(
            base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        )

    def _decode_slskd_source_id(self, source_id: str) -> SlskdDownloadRequest:
        if not source_id.startswith(_SLSKD_PREFIX):
            raise RuntimeError("soulseek slskd source_id must start with slskd:")
        token = source_id[len(_SLSKD_PREFIX) :]
        payload = self._load_slskd_request_payload(token)
        if payload is None:
            try:
                payload = self._decode_legacy_slskd_source_id(token)
            except Exception as exc:
                raise RuntimeError("bad soulseek slskd source_id") from exc
        return self._payload_to_slskd_request(payload)

    def _payload_to_slskd_request(self, payload: dict) -> SlskdDownloadRequest:
        if payload.get("v") != 1:
            raise RuntimeError("unsupported soulseek slskd source_id version")
        username = str(payload.get("u") or "").strip()
        if not username:
            raise RuntimeError("soulseek slskd source_id missing username")
        files = []
        for item in payload.get("f") or []:
            filename = str(item.get("filename") or "").strip()
            size = int(item.get("size") or 0)
            if (
                not filename
                or Path(filename).suffix.lower() not in _SUPPORTED_AUDIO_SUFFIXES
            ):
                raise RuntimeError("soulseek slskd source_id has invalid file")
            files.append(SlskdDownloadFile(filename, size))
        if not files:
            raise RuntimeError("soulseek slskd source_id has no files")
        if len(files) > self.max_files:
            raise RuntimeError(
                f"soulseek slskd source_id has too many files: {len(files)} > {self.max_files}"
            )
        total = sum(item.size for item in files)
        if self.max_bytes and total > self.max_bytes:
            raise RuntimeError(
                f"soulseek slskd source_id exceeds max bytes: {total} > {self.max_bytes}"
            )
        return SlskdDownloadRequest(
            username=username,
            files=tuple(files),
            title=str(payload.get("t") or "[Soulseek] slskd download"),
            search_text=str(payload.get("q") or ""),
        )

    def _download_raw_from_slskd(
        self, candidate_id: str, ctx: PipelineContext
    ) -> RawDownload:
        if not self.slskd_is_configured():
            raise RuntimeError("slskd API not configured")
        request = self._decode_slskd_source_id(candidate_id)
        ctx.raw_dir.mkdir(parents=True, exist_ok=True)
        ctx.set_progress(
            stage="slskd_queue",
            percent=8,
            message="Queueing Soulseek download",
            file_count=len(request.files),
        )
        baseline = self._matching_paths(request.files)
        self._queue_slskd_download(request)
        paths = self._wait_for_slskd_files(request, baseline=baseline, ctx=ctx)
        ctx.set_progress(stage="copying", percent=35, message="Copying Soulseek files")
        copied, total_bytes = self._copy_paths(paths, ctx)
        ctx.set_progress(
            stage="copied",
            percent=45,
            message="Soulseek copy complete",
            file_count=copied,
            audio_files=copied,
            size_bytes=total_bytes,
        )
        return RawDownload(
            files_dir=ctx.raw_dir, file_count=copied, total_bytes=total_bytes
        )

    def _queue_slskd_download(self, request: SlskdDownloadRequest) -> None:
        body = [
            {"filename": item.filename, "size": item.size} for item in request.files
        ]
        self._slskd_post(
            f"/transfers/downloads/{quote(request.username, safe='')}",
            body,
            timeout=30,
        )

    def _wait_for_slskd_files(
        self,
        request: SlskdDownloadRequest,
        *,
        baseline: set[Path],
        ctx: PipelineContext,
    ) -> list[Path]:
        deadline = time.monotonic() + self.download_timeout
        last_failure = ""
        while time.monotonic() < deadline:
            ctx.check_cancelled()
            paths = self._matching_paths(request.files, exclude=baseline)
            if len(paths) >= len(request.files):
                self._validate_paths_settled(paths)
                return paths
            failure = self._slskd_failure_reason(request)
            if failure:
                last_failure = failure
            ctx.set_progress(
                stage="slskd_wait",
                percent=20,
                message="Waiting for Soulseek download",
                found_files=len(paths),
                expected_files=len(request.files),
            )
            time.sleep(self.poll_seconds)
        detail = f": {last_failure}" if last_failure else ""
        raise RuntimeError(f"soulseek slskd download timed out{detail}")

    def _slskd_failure_reason(self, request: SlskdDownloadRequest) -> str:
        try:
            downloads = self._slskd_get(
                f"/transfers/downloads/{quote(request.username, safe='')}",
                timeout=15,
            )
        except Exception:
            return ""
        transfers = self._flatten_transfers(downloads)
        target_names = {item.filename for item in request.files}
        failed = []
        for transfer in transfers:
            filename = str(transfer.get("filename") or "")
            if filename not in target_names:
                continue
            state = str(transfer.get("state") or "")
            if any(marker in state for marker in _SLSKD_FAILURE_STATES):
                failed.append(f"{Path(filename).name}: {state}")
        return "; ".join(failed)

    def _flatten_transfers(self, value) -> list[dict]:
        if isinstance(value, dict):
            if "filename" in value and "state" in value:
                return [value]
            out: list[dict] = []
            for child in value.values():
                out.extend(self._flatten_transfers(child))
            return out
        if isinstance(value, list):
            out: list[dict] = []
            for child in value:
                out.extend(self._flatten_transfers(child))
            return out
        return []

    def _matching_paths(
        self,
        files: tuple[SlskdDownloadFile, ...],
        *,
        exclude: set[Path] | None = None,
    ) -> list[Path]:
        exclude = exclude or set()
        root = self._download_root.resolve()
        matches: list[Path] = []
        used: set[Path] = set()
        for item in files:
            basename = Path(item.filename.replace("\\", "/")).name
            candidates: list[Path] = []
            try:
                iterator = root.rglob(basename)
            except OSError:
                iterator = iter(())
            for path in iterator:
                if path in used or path.resolve() in exclude:
                    continue
                if path.is_symlink() or not path.is_file():
                    continue
                if path.name.lower().endswith(_PARTIAL_SUFFIXES):
                    continue
                if not path.resolve().is_relative_to(root):
                    continue
                if item.size and path.stat().st_size != item.size:
                    continue
                candidates.append(path)
            if not candidates:
                continue
            chosen = sorted(
                candidates, key=lambda p: p.stat().st_mtime_ns, reverse=True
            )[0]
            matches.append(chosen)
            used.add(chosen)
        return matches

    def _validate_paths_settled(self, paths: list[Path]) -> None:
        if not paths:
            raise RuntimeError("no soulseek files found")
        first = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in paths}
        if self.settle_seconds:
            time.sleep(self.settle_seconds)
            second = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in paths}
            if second != first:
                raise RuntimeError("soulseek files not settled")

    def _copy_paths(self, paths: list[Path], ctx: PipelineContext) -> tuple[int, int]:
        total = 0
        for idx, src in enumerate(paths, start=1):
            ctx.check_cancelled()
            suffix = src.suffix.lower()
            if suffix not in _SUPPORTED_AUDIO_SUFFIXES:
                continue
            dst = ctx.raw_dir / f"{idx:02d} {src.name}"
            shutil.copy2(str(src), str(dst))
            total += src.stat().st_size
        if not total:
            raise RuntimeError("no supported audio files copied from slskd download")
        return len(paths), total


__all__ = [
    "SoulseekCompletedAdapter",
    "SlskdDownloadRequest",
    "SlskdDownloadFile",
    "hash_rel",
]
