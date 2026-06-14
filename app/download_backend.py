"""Mintarr-managed download backends — ADR-0014 slice 2 (SAB first).

Mintarr submits, monitors, and cancels backend transfer jobs **only** in its own
dedicated music category (ADR-0014). This module is the backend-client
abstraction: it has no Lidarr exposure, watches no shared folders, and stays
read/copy-oriented. Two safety rails are unit-tested here because the whole lane
depends on them:

* **Category containment** — a backend job must carry the configured dedicated
  category; an empty category fails closed (we never submit into "whatever").
* **Path containment** — a completed backend path is only trusted when it
  resolves *inside* the configured download root; traversal or outside-root
  paths are rejected, never imported from.

Secrets (SAB apikey, qBit password) are never logged: all outbound URLs/params
pass through :func:`redact` before they reach a log line.
"""

from __future__ import annotations

import enum
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import requests

log = logging.getLogger("tidalhires.download_backend")


# ---- Value objects -------------------------------------------------------


class BackendState(str, enum.Enum):
    """Normalized backend job state, independent of SAB/qBit vocabulary."""

    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BackendJob:
    """A job Mintarr submitted into its dedicated category."""

    backend_job_id: str
    category: str


@dataclass(frozen=True)
class BackendJobStatus:
    """A point-in-time backend status snapshot.

    ``completed_path`` is set only when the job is COMPLETED *and* its path
    resolved inside the configured download root; otherwise it stays ``None``.
    """

    backend_job_id: str
    state: BackendState
    progress: float  # 0..100
    completed_path: str | None
    raw_status: str


# ---- Secret redaction ----------------------------------------------------

_SECRET_PARAM_RE = re.compile(r"(?i)\b(apikey|api_key|password|passwd|pass)=([^&\s]+)")


def redact(text: str) -> str:
    """Mask secret query params (apikey/password/...) in a URL or string."""
    return _SECRET_PARAM_RE.sub(r"\1=<redacted>", text)


def redact_params(params: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``params`` with secret values masked, for logging.

    Masks both secret *keys* (the SAB ``apikey`` field) and secret *values*
    embedded in string params — e.g. a release/download URL passed as ``name``
    that itself carries ``apikey=...`` from an indexer. Without the second pass
    a debug log of the submit call would leak the indexer key.
    """
    out: dict[str, Any] = {}
    for key, value in params.items():
        if key.lower() in {"apikey", "api_key", "password", "passwd", "pass"}:
            out[key] = "<redacted>"
        elif isinstance(value, str):
            out[key] = redact(value)
        else:
            out[key] = value
    return out


# ---- Category / path containment ----------------------------------------


_GENERIC_CATEGORIES = {"music", "complete", "completed", "downloads", "finished", ""}


def ensure_category(category: str | None) -> str:
    """Return a non-empty, stripped category or raise — fail closed.

    Mintarr must never submit a backend job without a dedicated category; an
    empty/whitespace category is a hard error, not a silent default.
    """
    cat = (category or "").strip()
    if not cat:
        raise ValueError("backend category is required (fail closed; ADR-0014)")
    return cat


def is_generic_category(category: str | None) -> bool:
    """A generic name (``music``/``downloads``/...) is a containment warning."""
    return (category or "").strip().lower() in _GENERIC_CATEGORIES


def contained_path(
    root: str | os.PathLike, candidate: str | os.PathLike
) -> Path | None:
    """Resolve ``candidate`` and return it only if it sits inside ``root``.

    Returns ``None`` for traversal, the root itself, or any path outside the
    configured download root. Mintarr copies only from a contained path.
    """
    try:
        root_resolved = Path(root).resolve()
        candidate_resolved = Path(candidate).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if candidate_resolved == root_resolved:
        return None
    if root_resolved not in candidate_resolved.parents:
        return None
    return candidate_resolved


# ---- Remote path mapping -------------------------------------------------
# A Windows-native backend (SABnzbd/qBittorrent on Windows) reports completed
# paths in its own namespace (e.g. ``H:\Nedlasting\sabnzbd\complete\...``) while
# Mintarr runs in a Linux container that sees the same files at a mount path
# (e.g. ``/sab-backend-complete/...``). This rewrites the backend-reported path
# string into the container's view before containment — it moves no files.


def parse_path_map(raw: str | None) -> list[tuple[str, str]]:
    """Parse ``from=>to[;from=>to...]`` into normalized prefix pairs.

    Both sides are backslash-normalized and trailing-slash-stripped. Empty or
    malformed entries are skipped, so an unset/blank map yields ``[]`` (no-op).
    """
    pairs: list[tuple[str, str]] = []
    for entry in (raw or "").split(";"):
        entry = entry.strip()
        if "=>" not in entry:
            continue
        src, dst = entry.split("=>", 1)
        src = src.strip().replace("\\", "/").rstrip("/")
        dst = dst.strip().replace("\\", "/").rstrip("/")
        if src and dst:
            pairs.append((src, dst))
    return pairs


def apply_path_map(path: str, pairs: list[tuple[str, str]]) -> str:
    """Rewrite ``path`` by the first matching prefix pair, else return it as-is.

    Backend paths are compared backslash-insensitive and case-insensitive
    (Windows paths are both). With no pairs (the containerized-backend case) the
    path is returned unchanged, so this is backward compatible.
    """
    if not path or not pairs:
        return path
    norm = path.replace("\\", "/")
    lowered = norm.lower()
    for src, dst in pairs:
        src_l = src.lower()
        if lowered == src_l:
            return dst
        if lowered.startswith(src_l + "/"):
            return dst + norm[len(src) :]
    return path


# ---- Backend protocol ----------------------------------------------------


@runtime_checkable
class DownloadBackend(Protocol):
    """A transfer engine Mintarr drives for its own category (submit/poll/cancel)."""

    name: str
    source_type: str

    def is_enabled(self) -> bool: ...

    def submit(self, *, url: str) -> BackendJob: ...

    def status(self, backend_job_id: str) -> BackendJobStatus: ...

    def cancel(self, backend_job_id: str, *, delete_files: bool = False) -> bool: ...


# ---- SABnzbd backend client ---------------------------------------------


_SAB_FAILED_TOKENS = ("failed", "error")


class SabBackendClient:
    """Drive a backend SABnzbd instance for Mintarr's dedicated music category.

    HTTP is injected (``request=``) so tests never touch a real SAB. The apikey
    is sent as a query param (SAB's only auth) but is redacted from every log.
    """

    name = "sab"
    source_type = "sab_usenet_backend"

    def __init__(
        self,
        *,
        url: str | None = None,
        api_key: str | None = None,
        category: str | None = None,
        download_root: str | None = None,
        path_map: str | None = None,
        timeout: int | None = None,
        request: Callable[..., Any] | None = None,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._category = category
        self._download_root = download_root
        self._path_map = path_map
        self._timeout = timeout
        self._request = request or requests.get

    @property
    def path_map(self) -> list[tuple[str, str]]:
        """Backend→container path-prefix rewrites (for a Windows-native SAB)."""
        return parse_path_map(
            self._path_map or os.environ.get("MINTARR_SAB_BACKEND_PATH_MAP", "")
        )

    @property
    def api_url(self) -> str:
        return (self._url or os.environ.get("MINTARR_SAB_BACKEND_URL") or "").rstrip(
            "/"
        )

    @property
    def api_key(self) -> str:
        return self._api_key or os.environ.get("MINTARR_SAB_BACKEND_API_KEY") or ""

    @property
    def category(self) -> str:
        # No hardcoded default: an unset category must fail closed (ADR-0014),
        # never silently submit into an invented category. The operator sets
        # MINTARR_SAB_BACKEND_CATEGORY explicitly (design §6 suggests
        # "mintarr-music").
        return self._category or os.environ.get("MINTARR_SAB_BACKEND_CATEGORY") or ""

    @property
    def download_root(self) -> str:
        return (
            self._download_root
            or os.environ.get("MINTARR_SAB_BACKEND_DOWNLOAD_ROOT")
            or ""
        )

    @property
    def timeout(self) -> int:
        if self._timeout is not None:
            return self._timeout
        raw = os.environ.get("MINTARR_SAB_BACKEND_TIMEOUT", "30")
        try:
            return max(1, int(raw))
        except ValueError:
            return 30

    def is_enabled(self) -> bool:
        return bool(self.api_url and self.api_key and self.category)

    def _call(self, mode: str, **params: Any) -> dict[str, Any]:
        """Issue one SAB API call, returning parsed JSON. Never logs the apikey."""
        query = {"mode": mode, "output": "json", "apikey": self.api_key, **params}
        log.debug("SAB backend call mode=%s params=%s", mode, redact_params(query))
        response = self._request(
            f"{self.api_url}/api", params=query, timeout=self.timeout
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def submit(self, *, url: str) -> BackendJob:
        """Add a download by URL into the client's dedicated category.

        There is intentionally no per-call category override: a backend client
        owns exactly one dedicated category (ADR-0014), so callers cannot steer
        a job into a different one. An unset category fails closed here.
        """
        cat = ensure_category(self.category)
        payload = self._call("addurl", name=url, cat=cat)
        nzo_ids = payload.get("nzo_ids") or []
        if not (payload.get("status", True) and nzo_ids):
            raise RuntimeError("SAB backend addurl returned no nzo_id")
        return BackendJob(backend_job_id=str(nzo_ids[0]), category=cat)

    def status(self, backend_job_id: str) -> BackendJobStatus:
        """Map the SAB queue/history slot for ``backend_job_id`` to a snapshot."""
        queue = self._call("queue").get("queue", {})
        for slot in queue.get("slots", []) or []:
            if str(slot.get("nzo_id")) == backend_job_id:
                return self._queue_status(backend_job_id, slot)

        history = self._call("history").get("history", {})
        for slot in history.get("slots", []) or []:
            if str(slot.get("nzo_id")) == backend_job_id:
                return self._history_status(backend_job_id, slot)

        return BackendJobStatus(
            backend_job_id, BackendState.UNKNOWN, 0.0, None, "not_found"
        )

    def _queue_status(self, jid: str, slot: dict[str, Any]) -> BackendJobStatus:
        raw = str(slot.get("status", ""))
        progress = _safe_float(slot.get("percentage"))
        state = (
            BackendState.FAILED
            if _has_token(raw, _SAB_FAILED_TOKENS)
            else BackendState.DOWNLOADING
        )
        return BackendJobStatus(jid, state, progress, None, raw)

    def _history_status(self, jid: str, slot: dict[str, Any]) -> BackendJobStatus:
        raw = str(slot.get("status", ""))
        if _has_token(raw, _SAB_FAILED_TOKENS):
            return BackendJobStatus(jid, BackendState.FAILED, 0.0, None, raw)
        # A COMPLETED job whose path is not contained in our root yields
        # completed_path=None. Downstream slices (3/5) MUST treat COMPLETED with
        # completed_path=None as "finished but not safely importable" and never
        # ingest from an uncontained/unknown path.
        completed = None
        if self.download_root:
            # Translate the backend-reported (possibly Windows) path into the
            # container's view before the containment check.
            storage = apply_path_map(str(slot.get("storage", "")), self.path_map)
            completed_path = contained_path(self.download_root, storage)
            completed = str(completed_path) if completed_path else None
        return BackendJobStatus(jid, BackendState.COMPLETED, 100.0, completed, raw)

    def cancel(self, backend_job_id: str, *, delete_files: bool = False) -> bool:
        """Remove the job from queue and history. Returns True if SAB accepted."""
        del_files = 1 if delete_files else 0
        ok = False
        for mode in ("queue", "history"):
            try:
                payload = self._call(
                    mode, name="delete", value=backend_job_id, del_files=del_files
                )
                ok = ok or bool(payload.get("status", False))
            except Exception as exc:  # one mode failing must not mask the other
                log.warning(
                    "SAB backend cancel %s failed for %s: %s",
                    mode,
                    backend_job_id,
                    exc,
                )
        return ok


# ---- qBittorrent backend client -----------------------------------------


def _default_request(method: str, url: str, **kwargs: Any) -> Any:
    return requests.request(method, url, **kwargs)


def _expect_qbit_ok(response: Any, action: str) -> None:
    """Raise unless a qBit text-mutation response body is ``Ok.``.

    qBit answers some mutations (``/torrents/add``) with HTTP 200 and a body of
    ``Ok.`` or ``Fails.``; ``raise_for_status`` cannot see that soft failure.
    """
    text = (getattr(response, "text", "") or "").strip()
    if text.lower() != "ok.":
        raise RuntimeError(f"qBit {action} rejected: {text or 'empty response'}")


_MAGNET_BTIH_RE = re.compile(r"(?i)xt=urn:btih:([0-9a-z]+)")

# qBittorrent torrent states grouped to the normalized BackendState.
_QBIT_DOWNLOADING = {
    "downloading",
    "stalleddl",
    "metadl",
    "queueddl",
    "allocating",
    "checkingdl",
    "forceddl",
    "pauseddl",
    "checkingresumedata",
    "moving",
}
_QBIT_COMPLETED = {
    "uploading",
    "stalledup",
    "queuedup",
    "forcedup",
    "pausedup",
    "checkingup",
}
_QBIT_FAILED = {"error", "missingfiles"}


def magnet_btih(url: str) -> str | None:
    """Return the lowercased btih info-hash of a magnet URL, else None."""
    if not url or not url.lower().startswith("magnet:"):
        return None
    match = _MAGNET_BTIH_RE.search(url)
    return match.group(1).lower() if match else None


def map_qbit_state(raw: str) -> BackendState:
    lowered = (raw or "").lower()
    if lowered in _QBIT_FAILED:
        return BackendState.FAILED
    if lowered in _QBIT_COMPLETED:
        return BackendState.COMPLETED
    if lowered in _QBIT_DOWNLOADING:
        return BackendState.DOWNLOADING
    return BackendState.UNKNOWN


class QbitBackendClient:
    """Drive a backend qBittorrent instance for Mintarr's dedicated category.

    Differs from SAB on three points handled here: SID-cookie auth, hash
    resolution (qBit ``/torrents/add`` returns ``"Ok."`` not a hash — resolved
    from the magnet btih, else by a before/after category diff), and
    seeding-safe cancel (ADR-0014: never destroy seeding unless the operator
    explicitly opts in). The category, not a save path, is the ownership
    boundary, so submit does not override the category's configured path.
    """

    name = "qbittorrent"
    source_type = "qbittorrent_backend"

    def __init__(
        self,
        *,
        url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        category: str | None = None,
        download_root: str | None = None,
        cleanup_policy: str | None = None,
        timeout: int | None = None,
        request: Callable[..., Any] | None = None,
    ) -> None:
        self._url = url
        self._username = username
        self._password = password
        self._category = category
        self._download_root = download_root
        self._cleanup_policy = cleanup_policy
        self._timeout = timeout
        self._request = request or _default_request
        self._sid: str | None = None

    @property
    def api_url(self) -> str:
        return (self._url or os.environ.get("MINTARR_QBIT_BACKEND_URL") or "").rstrip(
            "/"
        )

    @property
    def username(self) -> str:
        return self._username or os.environ.get("MINTARR_QBIT_BACKEND_USERNAME") or ""

    @property
    def password(self) -> str:
        return self._password or os.environ.get("MINTARR_QBIT_BACKEND_PASSWORD") or ""

    @property
    def category(self) -> str:
        # Fail closed like SAB: no invented category default (ADR-0014).
        return self._category or os.environ.get("MINTARR_QBIT_BACKEND_CATEGORY") or ""

    @property
    def download_root(self) -> str:
        return (
            self._download_root
            or os.environ.get("MINTARR_QBIT_BACKEND_DOWNLOAD_ROOT")
            or ""
        )

    @property
    def cleanup_policy(self) -> str:
        return (
            self._cleanup_policy
            or os.environ.get("MINTARR_QBIT_BACKEND_CLEANUP")
            or "leave_seeding"
        )

    @property
    def timeout(self) -> int:
        if self._timeout is not None:
            return self._timeout
        raw = os.environ.get("MINTARR_QBIT_BACKEND_TIMEOUT", "30")
        try:
            return max(1, int(raw))
        except ValueError:
            return 30

    def is_enabled(self) -> bool:
        # Auth is optional (qBit may bypass it for localhost); url + dedicated
        # category are the minimum.
        return bool(self.api_url and self.category)

    def _login(self) -> None:
        response = self._request(
            "POST",
            f"{self.api_url}/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        response.raise_for_status()
        cookie = response.headers.get("Set-Cookie", "") if response.headers else ""
        match = re.search(r"SID=([^;]+)", cookie)
        self._sid = match.group(1) if match else None
        # Fail closed: if credentials were supplied but qBit returned no SID, the
        # login was rejected — do not proceed unauthenticated.
        if (self.username or self.password) and not self._sid:
            raise RuntimeError("qBit login was rejected (no SID returned)")

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        """Issue one authenticated qBit API call. Never logs the password."""
        if self._sid is None and (self.username or self.password):
            self._login()
        headers = dict(kwargs.pop("headers", {}) or {})
        if self._sid:
            headers["Cookie"] = f"SID={self._sid}"
        log.debug(
            "qBit backend call %s %s params=%s",
            method,
            path,
            redact_params(kwargs.get("params") or kwargs.get("data") or {}),
        )
        response = self._request(
            method,
            f"{self.api_url}{path}",
            headers=headers,
            timeout=self.timeout,
            **kwargs,
        )
        response.raise_for_status()
        return response

    def _category_hashes(self, category: str) -> set[str]:
        response = self._call(
            "GET", "/api/v2/torrents/info", params={"category": category}
        )
        items = response.json()
        if not isinstance(items, list):
            return set()
        return {
            str(t["hash"]).lower()
            for t in items
            if isinstance(t, dict) and t.get("hash")
        }

    def submit(self, *, url: str) -> BackendJob:
        """Add a torrent into the client's dedicated category; resolve its hash.

        No per-call category override (ADR-0014). The hash comes from the magnet
        btih when present, else from a before/after diff of the category listing.
        """
        cat = ensure_category(self.category)
        btih = magnet_btih(url)
        before: set[str] = set() if btih else self._category_hashes(cat)
        response = self._call(
            "POST",
            "/api/v2/torrents/add",
            data={"urls": url, "category": cat, "paused": "false"},
        )
        # qBit /torrents/add answers HTTP 200 with "Ok." or "Fails." — a soft
        # failure raise_for_status cannot catch. Validate the body so we never
        # mint a BackendJob (e.g. from a magnet btih) for a torrent qBit rejected.
        _expect_qbit_ok(response, "add")
        if btih:
            return BackendJob(backend_job_id=btih, category=cat)
        new = self._category_hashes(cat) - before
        if len(new) != 1:
            raise RuntimeError("qBit add: could not resolve a single new torrent hash")
        return BackendJob(backend_job_id=next(iter(new)), category=cat)

    def status(self, backend_job_id: str) -> BackendJobStatus:
        response = self._call(
            "GET", "/api/v2/torrents/info", params={"hashes": backend_job_id.lower()}
        )
        items = response.json()
        match = None
        if isinstance(items, list):
            match = next(
                (
                    t
                    for t in items
                    if isinstance(t, dict)
                    and str(t.get("hash", "")).lower() == backend_job_id.lower()
                ),
                None,
            )
        if match is None:
            return BackendJobStatus(
                backend_job_id, BackendState.UNKNOWN, 0.0, None, "not_found"
            )
        raw = str(match.get("state", ""))
        state = map_qbit_state(raw)
        progress = _safe_float(match.get("progress")) * 100.0  # qBit is 0..1
        completed = None
        if state is BackendState.COMPLETED and self.download_root:
            # As with SAB: COMPLETED + completed_path=None means finished but
            # NOT safely importable; slices 3/5 must not ingest an uncontained
            # path.
            path = match.get("content_path") or match.get("save_path") or ""
            contained = contained_path(self.download_root, path)
            completed = str(contained) if contained else None
        return BackendJobStatus(backend_job_id, state, progress, completed, raw)

    def cancel(self, backend_job_id: str, *, delete_files: bool = False) -> bool:
        """Cancel a grab on the backend — always propagates, never silently no-ops.

        ADR-0014 requires cancel to reach the backend *and* forbids destroying
        data unless the operator opts in:

        * ``delete_files=True`` — explicit opt-in: delete the torrent and its
          data.
        * ``cleanup_policy="remove"`` — remove the torrent but keep files on disk
          (stops seeding, no data loss).
        * default — **pause** the torrent: the grab stops propagating to the
          backend without deleting the torrent or its data (data-safe). This is
          deliberately not a no-op; an untracked torrent must not keep running.
          (``leave_seeding`` governs post-import cleanup, slice 5, not cancel.)
        """
        hashes = backend_job_id.lower()
        if delete_files:
            self._call(
                "POST",
                "/api/v2/torrents/delete",
                data={"hashes": hashes, "deleteFiles": "true"},
            )
            return True
        if self.cleanup_policy == "remove":
            self._call(
                "POST",
                "/api/v2/torrents/delete",
                data={"hashes": hashes, "deleteFiles": "false"},
            )
            return True
        self._call("POST", "/api/v2/torrents/pause", data={"hashes": hashes})
        return True


# ---- helpers -------------------------------------------------------------


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _has_token(text: str, tokens: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in tokens)
