"""Restore zip validation/planning (Phase 3 restore slice 1).

This module is deliberately pure and non-mutating. It validates the backup zip
contract and returns a restore plan, but it does not stage, extract, or replace
runtime state. Endpoint staging and boot-time apply are later slices.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

_JID_RE = re.compile(r"^[a-f0-9]{12}$")
_ARCHIVE_LABELS = {"blocked", "discarded", "expired"}
_LOG_NAMES = {"decisions.jsonl", "release_switch_audit.jsonl"}

DEFAULT_MAX_ENTRIES = 100_000
DEFAULT_MAX_TOTAL_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_ENTRY_UNCOMPRESSED_BYTES = 256 * 1024 * 1024


class RestoreValidationError(ValueError):
    """Raised when a restore zip does not match Mintarr's restore contract."""


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
