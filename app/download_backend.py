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
        timeout: int | None = None,
        request: Callable[..., Any] | None = None,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._category = category
        self._download_root = download_root
        self._timeout = timeout
        self._request = request or requests.get

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
            completed_path = contained_path(self.download_root, slot.get("storage", ""))
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


# ---- helpers -------------------------------------------------------------


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _has_token(text: str, tokens: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in tokens)
