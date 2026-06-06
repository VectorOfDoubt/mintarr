"""Restore zip validation/planning and staging.

This module validates the backup zip contract and can stage a restore marker for
future boot-time apply. It never extracts restore payloads into runtime state and
never replaces state_db, sidecars, or audit logs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import BinaryIO

log = logging.getLogger("tidalhires.restore")

_JID_RE = re.compile(r"^[a-f0-9]{12}$")
_ARCHIVE_LABELS = {"blocked", "discarded", "expired"}
_LOG_NAMES = {"decisions.jsonl", "release_switch_audit.jsonl"}

DEFAULT_MAX_ENTRIES = 250_000
DEFAULT_MAX_TOTAL_UNCOMPRESSED_BYTES = 25 * 1024 * 1024 * 1024
DEFAULT_MAX_ENTRY_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
RESTORE_REQUEST_FILE = "restore_request.json"
RESTORE_STATUS_FILE = "restore_status.json"


class RestoreValidationError(ValueError):
    """Raised when a restore zip does not match Mintarr's restore contract."""


class RestoreStateError(RuntimeError):
    """Raised when restore staging/cancel state conflicts with current state."""


class RestoreNotFoundError(RestoreStateError):
    """Raised when no staged restore exists to act on."""


@dataclass(frozen=True)
class RestoreEntry:
    """A validated restore zip member."""

    name: str
    kind: str
    size: int
    target_key: str
    jid: str | None = None


@dataclass(frozen=True)
class RestorePlan:
    """Validated restore plan derived from a backup zip."""

    entries: tuple[RestoreEntry, ...]
    total_uncompressed_bytes: int
    has_state_db: bool


@dataclass(frozen=True)
class RestoreLimits:
    max_entries: int = DEFAULT_MAX_ENTRIES
    max_total_uncompressed_bytes: int = DEFAULT_MAX_TOTAL_UNCOMPRESSED_BYTES
    max_entry_uncompressed_bytes: int = DEFAULT_MAX_ENTRY_UNCOMPRESSED_BYTES


@dataclass(frozen=True)
class RestoreTargets:
    """Filesystem destinations for restoring each zip member kind."""

    state_db_path: Path
    output_base: Path
    archive_dirs: dict[str, Path]
    log_paths: dict[str, Path]


@dataclass(frozen=True)
class RestoreApplyResult:
    """Outcome of a boot-time restore apply attempt.

    ``start_workers`` is the only signal the boot sequence needs: it is False
    only when restore failed mid-replacement (``failed_partial``) and the process
    must come up with workers off so an operator can recover.
    """

    outcome: (
        str  # no_pending | disabled_skip | applied | failed_preflight | failed_partial
    )
    start_workers: bool
    restore_id: str | None = None
    detail: str = ""


def validate_restore_zip(
    zip_path: str,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_total_uncompressed_bytes: int = DEFAULT_MAX_TOTAL_UNCOMPRESSED_BYTES,
    max_entry_uncompressed_bytes: int = DEFAULT_MAX_ENTRY_UNCOMPRESSED_BYTES,
) -> RestorePlan:
    """Validate a Mintarr backup zip and return its restore plan.

    Validation is intentionally strict: unknown members, path traversal, symlink
    entries, unsafe jid segments, invalid SQLite, malformed sidecar JSON, and
    zip-bomb limits all fail before a later staging slice can write a marker.
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            if len(infos) > max_entries:
                raise RestoreValidationError("restore zip has too many entries")

            entries: list[RestoreEntry] = []
            total = 0
            has_state_db = False
            seen_names: set[str] = set()

            for info in infos:
                entry = _validate_info(info)
                if entry.name in seen_names:
                    raise RestoreValidationError(
                        f"duplicate restore zip entry: {entry.name}"
                    )
                seen_names.add(entry.name)

                if entry.size > max_entry_uncompressed_bytes:
                    raise RestoreValidationError(
                        f"restore zip entry too large: {entry.name}"
                    )
                total += entry.size
                if total > max_total_uncompressed_bytes:
                    raise RestoreValidationError(
                        "restore zip total uncompressed size too large"
                    )

                if entry.kind == "state_db":
                    _validate_sqlite_member(zf, entry.name)
                    has_state_db = True
                elif entry.kind in {"sidecar", "archive"}:
                    _validate_json_member(zf, entry.name)
                elif entry.kind == "log":
                    _validate_jsonl_member(zf, entry.name)

                entries.append(entry)

            return RestorePlan(
                entries=tuple(entries),
                total_uncompressed_bytes=total,
                has_state_db=has_state_db,
            )
    except zipfile.BadZipFile as exc:
        raise RestoreValidationError("restore input is not a valid zip") from exc


def stage_restore_from_path(
    backup_path: Path | str,
    *,
    allowed_roots: tuple[Path | str, ...],
    staging_dir: Path | str,
    limits: RestoreLimits | None = None,
    actor: str = "api",
    now: float | None = None,
) -> dict:
    """Validate and stage a restore zip selected from an allowed backup dir."""
    source = _resolve_allowed_backup_path(backup_path, allowed_roots=allowed_roots)
    return _stage_restore_source(
        source,
        staging_dir=staging_dir,
        limits=limits or RestoreLimits(),
        actor=actor,
        source_name=source.name,
        now=now,
    )


def stage_restore_upload(
    data: bytes,
    *,
    filename: str,
    staging_dir: Path | str,
    limits: RestoreLimits | None = None,
    actor: str = "api",
    now: float | None = None,
) -> dict:
    """Stage an uploaded restore zip without trusting the client filename."""
    root = Path(staging_dir)
    root.mkdir(parents=True, exist_ok=True)
    _ensure_no_pending_restore(root)
    safe_name = Path(filename or "upload.zip").name
    restore_id = _restore_id(now=now)
    staged_zip = root / f"{restore_id}.zip"
    tmp = root / f".{restore_id}.upload.tmp"
    try:
        tmp.write_bytes(data)
        tmp.replace(staged_zip)
        return _write_restore_marker(
            staged_zip=staged_zip,
            staging_dir=root,
            limits=limits or RestoreLimits(),
            actor=actor,
            source_name=safe_name,
            restore_id=restore_id,
            now=now,
        )
    except Exception:
        _unlink_if_exists(tmp)
        _unlink_if_exists(staged_zip)
        raise


def stage_restore_upload_stream(
    stream: BinaryIO,
    *,
    filename: str,
    staging_dir: Path | str,
    limits: RestoreLimits | None = None,
    actor: str = "api",
    now: float | None = None,
) -> dict:
    """Stage an uploaded restore zip from a file-like stream."""
    root = Path(staging_dir)
    root.mkdir(parents=True, exist_ok=True)
    _ensure_no_pending_restore(root)
    safe_name = Path(filename or "upload.zip").name
    restore_id = _restore_id(now=now)
    staged_zip = root / f"{restore_id}.zip"
    tmp = root / f".{restore_id}.upload.tmp"
    try:
        with tmp.open("wb") as out:
            shutil.copyfileobj(stream, out)
        tmp.replace(staged_zip)
        return _write_restore_marker(
            staged_zip=staged_zip,
            staging_dir=root,
            limits=limits or RestoreLimits(),
            actor=actor,
            source_name=safe_name,
            restore_id=restore_id,
            now=now,
        )
    except Exception:
        _unlink_if_exists(tmp)
        _unlink_if_exists(staged_zip)
        raise


def restore_status(staging_dir: Path | str) -> dict:
    """Return staged restore status from marker/status files."""
    root = Path(staging_dir)
    request_path = root / RESTORE_REQUEST_FILE
    status_path = root / RESTORE_STATUS_FILE
    status = _read_json_file(status_path) if status_path.is_file() else None
    request = _read_json_file(request_path) if request_path.is_file() else None
    return {
        "pending": request is not None,
        "restore_id": (request or status or {}).get("restore_id"),
        "state": (request or status or {}).get("state"),
        "created_at": (request or status or {}).get("created_at"),
        "last_apply": status,
    }


def cancel_staged_restore(staging_dir: Path | str) -> dict:
    """Cancel a staged restore before boot-time apply starts."""
    root = Path(staging_dir)
    request_path = root / RESTORE_REQUEST_FILE
    if not request_path.is_file():
        raise RestoreNotFoundError("no staged restore")
    request = _read_json_file(request_path)
    state = str(request.get("state") or "")
    if state != "staged":
        raise RestoreStateError("restore cannot be cancelled after apply starts")
    staged_zip = Path(str(request.get("staged_zip") or ""))
    request_path.unlink()
    if staged_zip.is_file() and _is_relative_to(staged_zip.resolve(), root.resolve()):
        staged_zip.unlink()
    return {"restore_id": request.get("restore_id"), "state": "cancelled"}


def _stage_restore_source(
    source: Path,
    *,
    staging_dir: Path | str,
    limits: RestoreLimits,
    actor: str,
    source_name: str,
    now: float | None,
) -> dict:
    root = Path(staging_dir)
    root.mkdir(parents=True, exist_ok=True)
    _ensure_no_pending_restore(root)
    restore_id = _restore_id(now=now)
    staged_zip = root / f"{restore_id}.zip"
    tmp = root / f".{restore_id}.copy.tmp"
    try:
        shutil.copyfile(source, tmp)
        tmp.replace(staged_zip)
        return _write_restore_marker(
            staged_zip=staged_zip,
            staging_dir=root,
            limits=limits,
            actor=actor,
            source_name=source_name,
            restore_id=restore_id,
            now=now,
        )
    except Exception:
        _unlink_if_exists(tmp)
        _unlink_if_exists(staged_zip)
        raise


def _write_restore_marker(
    *,
    staged_zip: Path,
    staging_dir: Path,
    limits: RestoreLimits,
    actor: str,
    source_name: str,
    restore_id: str,
    now: float | None,
) -> dict:
    plan = validate_restore_zip(
        str(staged_zip),
        max_entries=limits.max_entries,
        max_total_uncompressed_bytes=limits.max_total_uncompressed_bytes,
        max_entry_uncompressed_bytes=limits.max_entry_uncompressed_bytes,
    )
    payload = {
        "restore_id": restore_id,
        "state": "staged",
        "created_at": now or time.time(),
        "actor": actor,
        "source_name": Path(source_name).name,
        "staged_zip": str(staged_zip),
        "limits": {
            "max_entries": limits.max_entries,
            "max_total_uncompressed_bytes": limits.max_total_uncompressed_bytes,
            "max_entry_uncompressed_bytes": limits.max_entry_uncompressed_bytes,
        },
        "plan": {
            "entry_count": len(plan.entries),
            "total_uncompressed_bytes": plan.total_uncompressed_bytes,
            "has_state_db": plan.has_state_db,
        },
    }
    marker = staging_dir / RESTORE_REQUEST_FILE
    tmp = staging_dir / f".{RESTORE_REQUEST_FILE}.tmp"
    try:
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp.replace(marker)
    finally:
        _unlink_if_exists(tmp)
    return payload


def _resolve_allowed_backup_path(
    backup_path: Path | str, *, allowed_roots: tuple[Path | str, ...]
) -> Path:
    try:
        candidate = Path(backup_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        # Missing path (FileNotFoundError) or symlink loop (RuntimeError) — a bad
        # request, not a server fault, so surface it as a validation error.
        raise RestoreValidationError("backup_path does not exist") from exc
    if not candidate.is_file():
        raise RestoreValidationError("backup_path is not a file")
    roots = tuple(
        Path(root).expanduser().resolve(strict=False) for root in allowed_roots
    )
    if not any(_is_relative_to(candidate, root) for root in roots):
        raise RestoreValidationError(
            "backup_path is outside allowed backup directories"
        )
    return candidate


def _ensure_no_pending_restore(staging_dir: Path) -> None:
    if (staging_dir / RESTORE_REQUEST_FILE).exists():
        raise RestoreStateError("restore already staged")


def _restore_id(*, now: float | None = None) -> str:
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now or time.time()))
    return f"{ts}-{uuid.uuid4().hex[:6]}"


def _read_json_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state": "unreadable", "path": str(path)}
    return (
        data if isinstance(data, dict) else {"state": "unreadable", "path": str(path)}
    )


def _unlink_if_exists(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def apply_pending_restore(
    *,
    staging_dir: Path | str,
    targets: RestoreTargets,
    safety_backup_dir: Path | str,
    build_safety_zip: Callable[[], bytes],
    enabled: bool,
    limits: RestoreLimits | None = None,
    now: float | None = None,
) -> RestoreApplyResult:
    """Apply a staged restore at boot, before state_db/workers/scheduler start.

    Crash-safe, not just exception-safe: the marker is moved to ``applying``
    before the first destructive write, so a marker still in ``applying`` at boot
    means a prior apply was interrupted — we fail closed (workers off) and never
    blindly retry. Any failure *before* replacement leaves current state intact.
    """
    limits = limits or RestoreLimits()
    root = Path(staging_dir)
    marker_path = root / RESTORE_REQUEST_FILE
    if not marker_path.is_file():
        return RestoreApplyResult("no_pending", start_workers=True)

    request = _read_json_file(marker_path)
    restore_id = request.get("restore_id")
    state = str(request.get("state") or "")

    # An interrupted prior apply (crash or caught mid-replacement) must fail
    # closed on every subsequent boot until an operator intervenes — regardless
    # of whether restore is currently enabled.
    if state == "applying":
        _safe_write_status(
            root,
            restore_id,
            "failed_partial",
            "interrupted apply detected; recover from safety backup and clear marker",
            now,
        )
        return RestoreApplyResult(
            "failed_partial", start_workers=False, restore_id=restore_id
        )

    if not enabled:
        # Leave the staged marker untouched for when the operator re-enables.
        return RestoreApplyResult(
            "disabled_skip", start_workers=True, restore_id=restore_id
        )

    if state != "staged":
        _unlink_if_exists(marker_path)
        _safe_write_status(
            root,
            restore_id,
            "failed_preflight",
            f"unexpected marker state: {state}",
            now,
        )
        return RestoreApplyResult(
            "failed_preflight", start_workers=True, restore_id=restore_id
        )

    staged_zip = Path(str(request.get("staged_zip") or ""))

    # Preflight: re-validate the staged zip (staging-time validation is not
    # trusted) and confirm it still lives inside the staging dir.
    try:
        if not staged_zip.is_file() or not _is_relative_to(
            staged_zip.resolve(), root.resolve()
        ):
            raise RestoreValidationError("staged restore zip missing or out of staging")
        plan = validate_restore_zip(
            str(staged_zip),
            max_entries=limits.max_entries,
            max_total_uncompressed_bytes=limits.max_total_uncompressed_bytes,
            max_entry_uncompressed_bytes=limits.max_entry_uncompressed_bytes,
        )
    except RestoreValidationError as exc:
        _unlink_if_exists(marker_path)
        _unlink_if_exists(staged_zip)
        _safe_write_status(
            root, restore_id, "failed_preflight", f"revalidation failed: {exc}", now
        )
        return RestoreApplyResult(
            "failed_preflight", start_workers=True, restore_id=restore_id
        )

    # Preflight: snapshot current state. If we cannot capture a recovery point,
    # abort before touching anything.
    try:
        safety_path = _write_safety_backup(
            Path(safety_backup_dir), restore_id, build_safety_zip
        )
    except Exception as exc:
        _unlink_if_exists(marker_path)
        _unlink_if_exists(staged_zip)
        _safe_write_status(
            root, restore_id, "failed_preflight", f"safety backup failed: {exc}", now
        )
        return RestoreApplyResult(
            "failed_preflight", start_workers=True, restore_id=restore_id
        )

    # Commit-marker write is still pre-destructive: if we cannot persist the
    # "applying" state, no files have been replaced, so abort with state intact.
    try:
        _set_marker_state(marker_path, request, "applying", now)
    except Exception as exc:
        _unlink_if_exists(marker_path)
        _unlink_if_exists(staged_zip)
        _safe_write_status(
            root, restore_id, "failed_preflight", f"could not start apply: {exc}", now
        )
        return RestoreApplyResult(
            "failed_preflight", start_workers=True, restore_id=restore_id
        )

    # Past this point state may be partially replaced. Nothing here may escape:
    # any failure — including a status-write failure — must fail closed (workers
    # off), and the marker is left in "applying" so the next boot also does.
    try:
        _apply_entries(staged_zip, plan, targets)
    except Exception as exc:
        _safe_write_status(
            root,
            restore_id,
            "failed_partial",
            f"apply failed mid-replacement: {exc}; recover from {safety_path.name}",
            now,
            safety_backup=str(safety_path),
        )
        return RestoreApplyResult(
            "failed_partial", start_workers=False, restore_id=restore_id
        )

    _unlink_if_exists(marker_path)
    _unlink_if_exists(staged_zip)
    _safe_write_status(
        root,
        restore_id,
        "applied",
        "restore applied",
        now,
        safety_backup=str(safety_path),
    )
    return RestoreApplyResult("applied", start_workers=True, restore_id=restore_id)


def _apply_entries(
    staged_zip: Path, plan: RestorePlan, targets: RestoreTargets
) -> None:
    """Write restored members to their targets. state_db first, then evidence.

    Sidecars, archive sidecars, and logs are written/overwritten from the backup;
    nothing else is deleted, so audio under ``output_base/<jid>/`` is never
    touched. The state DB is fully replaced (with stale WAL/SHM removed).
    """
    with zipfile.ZipFile(staged_zip) as zf:
        for entry in plan.entries:
            if entry.kind != "state_db":
                continue
            _atomic_write(targets.state_db_path, zf.read(entry.name))
            _remove_wal_shm(targets.state_db_path)

        for entry in plan.entries:
            if entry.kind == "state_db":
                continue
            dest = _entry_target(entry, targets)
            _atomic_write(dest, zf.read(entry.name))


def _entry_target(entry: RestoreEntry, targets: RestoreTargets) -> Path:
    parts = entry.target_key.split("/")
    if entry.kind == "sidecar":
        return targets.output_base / parts[1] / "verification.json"
    if entry.kind == "archive":
        return targets.archive_dirs[parts[1]] / parts[2]
    if entry.kind == "log":
        return targets.log_paths[parts[1]]
    raise RestoreValidationError(f"unrestorable entry kind: {entry.kind}")


def _atomic_write(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.restore.tmp"
    try:
        tmp.write_bytes(data)
        tmp.replace(dest)
    finally:
        _unlink_if_exists(tmp)


def _remove_wal_shm(db_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        _unlink_if_exists(Path(f"{db_path}{suffix}"))


def _write_safety_backup(
    safety_backup_dir: Path,
    restore_id: str | None,
    build_safety_zip: Callable[[], bytes],
) -> Path:
    target_dir = safety_backup_dir / (restore_id or "unknown")
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / "pre-restore-state.zip"
    tmp = target_dir / ".pre-restore-state.zip.tmp"
    try:
        tmp.write_bytes(build_safety_zip())
        tmp.replace(dest)
    finally:
        _unlink_if_exists(tmp)
    return dest


def _set_marker_state(
    marker_path: Path, request: dict, state: str, now: float | None
) -> None:
    payload = dict(request)
    payload["state"] = state
    payload["state_changed_at"] = now or time.time()
    tmp = marker_path.parent / f".{RESTORE_REQUEST_FILE}.tmp"
    try:
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp.replace(marker_path)
    finally:
        _unlink_if_exists(tmp)


def _write_status_file(
    staging_dir: Path,
    restore_id: str | None,
    state: str,
    detail: str,
    now: float | None,
    *,
    safety_backup: str | None = None,
) -> None:
    payload = {
        "restore_id": restore_id,
        "state": state,
        "detail": detail,
        "applied_at": now or time.time(),
    }
    if safety_backup is not None:
        payload["safety_backup"] = safety_backup
    status_path = staging_dir / RESTORE_STATUS_FILE
    tmp = staging_dir / f".{RESTORE_STATUS_FILE}.tmp"
    try:
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp.replace(status_path)
    finally:
        _unlink_if_exists(tmp)


def _safe_write_status(
    staging_dir: Path,
    restore_id: str | None,
    state: str,
    detail: str,
    now: float | None,
    *,
    safety_backup: str | None = None,
) -> None:
    """Best-effort status write that never raises.

    The boot-apply outcome (especially fail-closed `failed_partial`) must not
    depend on our ability to persist a status file — a write failure here must
    never turn into an escaping exception that the server wrapper would treat as
    "start workers".
    """
    try:
        _write_status_file(
            staging_dir, restore_id, state, detail, now, safety_backup=safety_backup
        )
    except Exception:
        log.warning("failed to write restore status file in %s", staging_dir)


def _validate_info(info: zipfile.ZipInfo) -> RestoreEntry:
    name = info.filename
    if not name or name.endswith("/"):
        raise RestoreValidationError(f"restore zip contains directory entry: {name}")
    if info.file_size < 0:
        raise RestoreValidationError(f"restore zip entry has invalid size: {name}")
    if _is_symlink_or_special(info):
        raise RestoreValidationError(f"restore zip entry is not a regular file: {name}")
    parts = _safe_parts(name)

    if parts == ("state_db.sqlite",):
        return RestoreEntry(
            name=name, kind="state_db", size=info.file_size, target_key="state_db"
        )

    if len(parts) == 3 and parts[0] == "sidecars" and parts[2] == "verification.json":
        jid = parts[1]
        if not _JID_RE.match(jid):
            raise RestoreValidationError(f"unsafe restore jid segment: {jid}")
        return RestoreEntry(
            name=name,
            kind="sidecar",
            size=info.file_size,
            target_key=f"sidecars/{jid}/verification.json",
            jid=jid,
        )

    if len(parts) == 3 and parts[0] == "archive" and parts[1] in _ARCHIVE_LABELS:
        filename = parts[2]
        if "/" in filename or not filename.endswith(".json") or filename in {".json"}:
            raise RestoreValidationError(f"invalid archive sidecar name: {name}")
        return RestoreEntry(
            name=name,
            kind="archive",
            size=info.file_size,
            target_key=f"archive/{parts[1]}/{filename}",
        )

    if len(parts) == 2 and parts[0] == "logs" and parts[1] in _LOG_NAMES:
        return RestoreEntry(
            name=name,
            kind="log",
            size=info.file_size,
            target_key=f"logs/{parts[1]}",
        )

    raise RestoreValidationError(f"unknown restore zip entry: {name}")


def _safe_parts(name: str) -> tuple[str, ...]:
    path = PurePosixPath(name)
    parts = path.parts
    if name.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise RestoreValidationError(f"unsafe restore zip path: {name}")
    return parts


def _is_symlink_or_special(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o777777
    file_type = mode & 0o170000
    if file_type == 0:
        return False
    return not stat.S_ISREG(mode)


def _validate_sqlite_member(zf: zipfile.ZipFile, name: str) -> None:
    data = zf.read(name)
    tmp_name = ""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    try:
        tmp_name = tmp.name
        tmp.write(data)
        tmp.close()
        conn = sqlite3.connect(f"file:{tmp_name}?mode=ro", uri=True)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
        if not result or result[0] != "ok":
            raise RestoreValidationError("restore state_db failed integrity_check")
    except sqlite3.DatabaseError as exc:
        raise RestoreValidationError("restore state_db is not valid SQLite") from exc
    finally:
        if not tmp.closed:
            tmp.close()
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _validate_json_member(zf: zipfile.ZipFile, name: str) -> None:
    try:
        json.loads(zf.read(name))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RestoreValidationError(f"restore JSON entry is invalid: {name}") from exc


def _validate_jsonl_member(zf: zipfile.ZipFile, name: str) -> None:
    try:
        text = zf.read(name).decode("utf-8")
        for line in text.splitlines():
            if line.strip():
                json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RestoreValidationError(f"restore JSONL entry is invalid: {name}") from exc
