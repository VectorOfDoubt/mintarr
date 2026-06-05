"""Restore zip validation/planning and staging.

This module validates the backup zip contract and can stage a restore marker for
future boot-time apply. It never extracts restore payloads into runtime state and
never replaces state_db, sidecars, or audit logs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import BinaryIO

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
    """Raised when restore staging/cancel state is not valid."""


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
        raise RestoreStateError("no staged restore")
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
