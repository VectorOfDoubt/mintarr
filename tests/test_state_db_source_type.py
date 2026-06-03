"""F3.1 tests for records.source_type column + migration idempotency."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


def _cols(db_path: Path) -> set[str]:
    with sqlite3.connect(str(db_path)) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(records)").fetchall()}


def test_migration_adds_source_type_column(tmp_path):
    """A pre-F3.1 records table (without source_type) gains the column on init."""
    import state_db

    db = tmp_path / "legacy.db"
    # Simulate pre-F3.1 schema by creating records without source_type
    with sqlite3.connect(str(db)) as conn:
        conn.execute("""
            CREATE TABLE records (
                jid TEXT PRIMARY KEY,
                title TEXT,
                album_ids_json TEXT,
                created_at REAL,
                updated_at REAL,
                verification_decision TEXT,
                import_outcome TEXT,
                derived_status TEXT,
                score INTEGER,
                verdict TEXT,
                lifecycle_state TEXT,
                actor TEXT,
                discarded_at REAL,
                promoted_at REAL,
                expired_at REAL
            )
        """)
        conn.execute(
            "INSERT INTO records (jid, title, created_at) VALUES ('legacyjid', 'old', ?)",
            (time.time(),),
        )
        conn.commit()
    assert "source_type" not in _cols(db)

    state_db._initialized = False
    state_db.init(db_path=db)

    cols = _cols(db)
    assert "source_type" in cols

    # Backfill: legacy row should now have 'tidal'
    with sqlite3.connect(str(db)) as conn:
        row = conn.execute(
            "SELECT source_type FROM records WHERE jid='legacyjid'"
        ).fetchone()
    assert row[0] == "tidal"


def test_migration_is_idempotent(tmp_path):
    """Running init twice on the same DB must not error or duplicate columns."""
    import state_db

    db = tmp_path / "fresh.db"

    state_db._initialized = False
    state_db.init(db_path=db)
    cols_first = _cols(db)
    assert "source_type" in cols_first

    state_db._initialized = False
    state_db.init(db_path=db)
    cols_second = _cols(db)
    assert cols_first == cols_second


def test_upsert_record_persists_source_type(tmp_path):
    import state_db

    db = tmp_path / "upsert.db"
    state_db._initialized = False
    state_db.init(db_path=db)

    sidecar = {
        "jid": "abc123",
        "title": "Album",
        "ts": time.time(),
        "source_type": "tidal",
        "v2_verification_decision": "ACCEPT",
        "v2_import_outcome": "PENDING",
    }
    state_db.upsert_record(sidecar, derived_status="imported")
    with sqlite3.connect(str(db)) as conn:
        row = conn.execute(
            "SELECT source_type FROM records WHERE jid='abc123'"
        ).fetchone()
    assert row[0] == "tidal"


def test_upsert_record_defaults_to_tidal_when_absent(tmp_path):
    """Sidecars without source_type are treated as legacy TIDAL."""
    import state_db

    db = tmp_path / "legacy_sidecar.db"
    state_db._initialized = False
    state_db.init(db_path=db)

    sidecar = {
        "jid": "legacysid",
        "title": "Old album",
        "ts": time.time(),
    }
    state_db.upsert_record(sidecar)
    with sqlite3.connect(str(db)) as conn:
        row = conn.execute(
            "SELECT source_type FROM records WHERE jid='legacysid'"
        ).fetchone()
    assert row[0] == "tidal"
