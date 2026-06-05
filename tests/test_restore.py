"""Tests for restore zip validation/planning."""

from __future__ import annotations

import sqlite3
import stat
import zipfile

import pytest


def _make_sqlite_file(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE records (jid TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO records VALUES ('abcdef123456')")
    conn.commit()
    conn.close()


def _write_zip(path, entries):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data, *rest in entries:
            info = zipfile.ZipInfo(name)
            if rest:
                info.external_attr = rest[0]
            zf.writestr(info, data)


def test_validate_restore_zip_accepts_backup_contract(tmp_path):
    import restore

    db = tmp_path / "state.db"
    _make_sqlite_file(db)
    zip_path = tmp_path / "backup.zip"
    _write_zip(
        zip_path,
        [
            ("state_db.sqlite", db.read_bytes()),
            ("sidecars/abcdef123456/verification.json", b'{"jid":"abcdef123456"}'),
            ("archive/blocked/blocked.json", b'{"jid":"blocked"}'),
            ("archive/discarded/discarded.json", b'{"jid":"discarded"}'),
            ("archive/expired/expired.json", b'{"jid":"expired"}'),
            ("logs/decisions.jsonl", b'{"decision":"ACCEPT"}\n'),
            ("logs/release_switch_audit.jsonl", b'{"result":"ok"}\n'),
        ],
    )

    plan = restore.validate_restore_zip(str(zip_path))

    assert plan.has_state_db is True
    assert len(plan.entries) == 7
    assert {entry.kind for entry in plan.entries} == {
        "state_db",
        "sidecar",
        "archive",
        "log",
    }


def test_validate_restore_zip_accepts_real_backup_builder_output(tmp_path):
    import backup
    import restore

    db = tmp_path / "state.db"
    _make_sqlite_file(db)

    output = tmp_path / "output"
    (output / "abcdef123456").mkdir(parents=True)
    (output / "abcdef123456" / "verification.json").write_text('{"jid":"abcdef123456"}')
    # The backup builder must still exclude audio, and the restore validator
    # must accept the external_attr values zipfile writes for real filesystem
    # members rather than only synthetic ZipInfo entries.
    (output / "abcdef123456" / "track.flac").write_bytes(b"AUDIO")

    blocked = tmp_path / "blocked"
    discarded = tmp_path / "discarded"
    expired = tmp_path / "expired"
    for directory, jid in (
        (blocked, "blocked"),
        (discarded, "discarded"),
        (expired, "expired"),
    ):
        directory.mkdir()
        (directory / f"{jid}.json").write_text(f'{{"jid":"{jid}"}}')

    decisions = tmp_path / "decisions.jsonl"
    decisions.write_text('{"decision":"ACCEPT"}\n')
    release_switch = tmp_path / "release_switch_audit.jsonl"
    release_switch.write_text('{"result":"ok"}\n')

    data = backup.build_backup_zip(
        state_db_path=db,
        output_base=output,
        archive_dirs={
            "blocked": blocked,
            "discarded": discarded,
            "expired": expired,
        },
        log_files={
            "decisions.jsonl": decisions,
            "release_switch_audit.jsonl": release_switch,
        },
    )
    zip_path = tmp_path / "mintarr-backup.zip"
    zip_path.write_bytes(data)

    plan = restore.validate_restore_zip(str(zip_path))

    names = {entry.name for entry in plan.entries}
    assert "sidecars/abcdef123456/verification.json" in names
    assert "state_db.sqlite" in names
    assert "logs/decisions.jsonl" in names
    assert "logs/release_switch_audit.jsonl" in names
    assert not any(name.endswith(".flac") for name in names)
    assert plan.has_state_db is True


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("../state_db.sqlite", "unsafe restore zip path"),
        ("/state_db.sqlite", "unsafe restore zip path"),
        ("sidecars/abcdef123456/../../verification.json", "unsafe restore zip path"),
        ("audio/track.flac", "unknown restore zip entry"),
        ("sidecars/not-safe/verification.json", "unsafe restore jid segment"),
        ("sidecars/abcdef123456/extra/verification.json", "unknown restore zip entry"),
        ("archive/blocked/../evil.json", "unsafe restore zip path"),
        ("logs/unknown.jsonl", "unknown restore zip entry"),
    ],
)
def test_validate_restore_zip_rejects_unsafe_or_unknown_paths(tmp_path, name, message):
    import restore

    zip_path = tmp_path / "backup.zip"
    _write_zip(zip_path, [(name, b"{}")])

    with pytest.raises(restore.RestoreValidationError, match=message):
        restore.validate_restore_zip(str(zip_path))


def test_validate_restore_zip_rejects_symlink_entries(tmp_path):
    import restore

    zip_path = tmp_path / "backup.zip"
    symlink_attr = (stat.S_IFLNK | 0o777) << 16
    _write_zip(zip_path, [("logs/decisions.jsonl", b"target", symlink_attr)])

    with pytest.raises(restore.RestoreValidationError, match="not a regular file"):
        restore.validate_restore_zip(str(zip_path))


def test_validate_restore_zip_rejects_invalid_sqlite(tmp_path):
    import restore

    zip_path = tmp_path / "backup.zip"
    _write_zip(zip_path, [("state_db.sqlite", b"not sqlite")])

    with pytest.raises(restore.RestoreValidationError, match="not valid SQLite"):
        restore.validate_restore_zip(str(zip_path))


def test_validate_restore_zip_rejects_invalid_json_sidecar(tmp_path):
    import restore

    zip_path = tmp_path / "backup.zip"
    _write_zip(zip_path, [("sidecars/abcdef123456/verification.json", b"{")])

    with pytest.raises(restore.RestoreValidationError, match="JSON entry is invalid"):
        restore.validate_restore_zip(str(zip_path))


def test_validate_restore_zip_rejects_invalid_jsonl_log(tmp_path):
    import restore

    zip_path = tmp_path / "backup.zip"
    _write_zip(zip_path, [("logs/decisions.jsonl", b'{"ok":true}\n{')])

    with pytest.raises(restore.RestoreValidationError, match="JSONL entry is invalid"):
        restore.validate_restore_zip(str(zip_path))


def test_validate_restore_zip_rejects_zip_bomb_limits(tmp_path):
    import restore

    zip_path = tmp_path / "backup.zip"
    _write_zip(
        zip_path,
        [
            ("logs/decisions.jsonl", b"{}\n"),
            ("logs/release_switch_audit.jsonl", b"{}\n"),
        ],
    )

    with pytest.raises(restore.RestoreValidationError, match="too many entries"):
        restore.validate_restore_zip(str(zip_path), max_entries=1)

    with pytest.raises(restore.RestoreValidationError, match="entry too large"):
        restore.validate_restore_zip(str(zip_path), max_entry_uncompressed_bytes=1)

    with pytest.raises(
        restore.RestoreValidationError, match="total uncompressed size too large"
    ):
        restore.validate_restore_zip(str(zip_path), max_total_uncompressed_bytes=5)


def test_validate_restore_zip_rejects_duplicate_names(tmp_path):
    import restore

    zip_path = tmp_path / "backup.zip"
    with pytest.warns(UserWarning, match="Duplicate name"):
        _write_zip(
            zip_path,
            [
                ("logs/decisions.jsonl", b"{}\n"),
                ("logs/decisions.jsonl", b"{}\n"),
            ],
        )

    with pytest.raises(restore.RestoreValidationError, match="duplicate"):
        restore.validate_restore_zip(str(zip_path))
