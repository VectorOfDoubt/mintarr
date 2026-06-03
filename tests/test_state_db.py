"""Tests for V2 F1 SQLite state index."""

from __future__ import annotations

import json

import pytest

import state_db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Re-init state_db with a temp DB file per test."""
    db_file = tmp_path / "state.db"
    state_db._initialized = False
    state_db.init(db_path=db_file)
    yield db_file
    # Cleanup global state for next test
    state_db._initialized = False


def _sidecar(jid="abc123", **overrides):
    base = {
        "jid": jid,
        "title": "Test Artist - Test Album",
        "album_ids": [100, 101],
        "ts": 1779600000.0,
        "v2_verification_decision": "ACCEPT",
        "v2_import_outcome": "MANUAL_IMPORTED",
        "v2_score": 85,
        "verdict": "AUTHENTIC",
        "lifecycle": {
            "state": "created",
            "created_at": 1779600000.0,
            "actor": None,
        },
    }
    base.update(overrides)
    return base


# ---------- Init + schema ----------


def test_init_creates_schema(fresh_db):
    """init() should be idempotent — calling twice doesn't error."""
    state_db.init(db_path=fresh_db)
    state_db.init(db_path=fresh_db)
    assert state_db._initialized


def test_init_creates_all_tables(fresh_db):
    import sqlite3

    conn = sqlite3.connect(str(fresh_db))
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "records",
        "sensor_runs",
        "file_evidence",
        "actions",
        "connector_config",
    }.issubset(tables)


# ---------- upsert_record ----------


def test_upsert_record_inserts(fresh_db):
    state_db.upsert_record(_sidecar(), derived_status="imported")
    rec = state_db.get_record("abc123")
    assert rec is not None
    assert rec["title"] == "Test Artist - Test Album"
    assert rec["verification_decision"] == "ACCEPT"
    assert rec["derived_status"] == "imported"
    assert json.loads(rec["album_ids_json"]) == [100, 101]


def test_upsert_record_updates_on_conflict(fresh_db):
    state_db.upsert_record(_sidecar(), derived_status="imported")
    state_db.upsert_record(
        _sidecar(
            v2_import_outcome="FAILED",
            lifecycle={"state": "created", "actor": None},
        ),
        derived_status="failed",
    )
    rec = state_db.get_record("abc123")
    assert rec["import_outcome"] == "FAILED"
    assert rec["derived_status"] == "failed"


def test_upsert_record_preserves_lifecycle_timestamps(fresh_db):
    """COALESCE in ON CONFLICT should not overwrite existing promoted_at with None."""
    promoted_sidecar = _sidecar(
        lifecycle={
            "state": "promoted",
            "actor": "user_promote",
            "promoted_at": 1779700000.0,
            "created_at": 1779600000.0,
        }
    )
    state_db.upsert_record(promoted_sidecar)
    rec = state_db.get_record("abc123")
    assert rec["promoted_at"] == 1779700000.0

    # Second upsert without promoted_at — should NOT clear it
    state_db.upsert_record(
        _sidecar(lifecycle={"state": "promoted", "actor": "user_promote"})
    )
    rec = state_db.get_record("abc123")
    assert rec["promoted_at"] == 1779700000.0  # preserved


def test_upsert_record_handles_missing_jid_gracefully(fresh_db):
    state_db.upsert_record({})  # no exception raised
    # No row created


# ---------- upsert_sensor_runs ----------


def test_upsert_sensor_runs_replaces_existing(fresh_db):
    sensors_v1 = [
        {
            "name": "ffprobe",
            "class": "hard_gate",
            "status": "pass",
            "confidence": 1.0,
            "duration_ms": 420,
        },
        {
            "name": "flac_t",
            "class": "hard_gate",
            "status": "pass",
            "confidence": 1.0,
            "duration_ms": 7100,
        },
    ]
    state_db.upsert_sensor_runs("xyz789", sensors_v1)

    import sqlite3

    conn = sqlite3.connect(str(fresh_db))
    rows = conn.execute(
        "SELECT sensor_name, status FROM sensor_runs WHERE jid = 'xyz789'"
    ).fetchall()
    assert len(rows) == 2
    assert {r[0] for r in rows} == {"ffprobe", "flac_t"}

    # Replace with new set
    sensors_v2 = [
        {
            "name": "ffprobe",
            "class": "hard_gate",
            "status": "fail",
            "confidence": 1.0,
            "duration_ms": 420,
        },
    ]
    state_db.upsert_sensor_runs("xyz789", sensors_v2)
    rows = conn.execute(
        "SELECT sensor_name, status FROM sensor_runs WHERE jid = 'xyz789'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "fail"


def test_upsert_sensor_runs_handles_empty(fresh_db):
    state_db.upsert_sensor_runs("xyz789", None)  # no-op, no exception
    state_db.upsert_sensor_runs("xyz789", [])


# ---------- upsert_file_evidence ----------


def test_upsert_file_evidence_stores_per_file(fresh_db):
    files = [
        {
            "filename": "01 - Track.flac",
            "sample_rate": 96000,
            "bit_depth": 24,
            "cutoff_hz": 42000,
            "detective_verdict": "AUTHENTIC",
            "is_fake_high_res": False,
            "estimated_mp3_bitrate": 0,
        },
        {
            "filename": "02 - Track.flac",
            "sample_rate": 44100,
            "bit_depth": 16,
            "cutoff_hz": 17000,
            "detective_verdict": "SUSPICIOUS",
            "is_fake_high_res": False,
            "estimated_mp3_bitrate": 192,
        },
    ]
    state_db.upsert_file_evidence("file123", files)

    import sqlite3

    conn = sqlite3.connect(str(fresh_db))
    rows = conn.execute(
        "SELECT filename, detective_verdict, is_fake_high_res FROM file_evidence WHERE jid = 'file123'"
    ).fetchall()
    assert len(rows) == 2
    verdicts = {r[0]: r[1] for r in rows}
    assert verdicts["02 - Track.flac"] == "SUSPICIOUS"


# ---------- log_action ----------


def test_log_action_appends_rows(fresh_db):
    state_db.log_action(
        "act123", "promote", "user_dashboard", "ok", {"status_code": 200}
    )
    state_db.log_action(
        "act123", "promote", "user_dashboard", "http_409", {"status_code": 409}
    )
    actions = state_db.list_actions(jid="act123")
    assert len(actions) == 2
    # Order by created_at DESC
    assert actions[0]["result"] in ("ok", "http_409")
    assert actions[1]["result"] in ("ok", "http_409")
    assert actions[0]["jid"] == "act123"


def test_log_action_global_list(fresh_db):
    state_db.log_action("j1", "discard", "user_dashboard", "ok")
    state_db.log_action("j2", "promote", "user_dashboard", "ok")
    all_actions = state_db.list_actions(limit=10)
    assert len(all_actions) == 2


def test_connector_config_round_trip(fresh_db):
    saved = state_db.set_connector_config(
        "tidal",
        enabled=True,
        mode="dry_run",
        actor="test",
    )

    assert saved is not None
    assert saved["connector_id"] == "tidal"
    assert saved["enabled"] is True
    assert saved["mode"] == "dry_run"
    assert state_db.get_connector_config("tidal")["mode"] == "dry_run"
    assert state_db.list_connector_config()["tidal"]["actor"] == "test"


def test_connector_config_update_overwrites_existing(fresh_db):
    state_db.set_connector_config("tidal", enabled=True, mode="dry_run", actor="test")
    state_db.set_connector_config(
        "tidal", enabled=False, mode="disabled", actor="test2"
    )

    row = state_db.get_connector_config("tidal")
    assert row["enabled"] is False
    assert row["mode"] == "disabled"
    assert row["actor"] == "test2"


# ---------- list_records (filtering) ----------


def test_list_records_filters_by_decision(fresh_db):
    state_db.upsert_record(_sidecar(jid="acc1", v2_verification_decision="ACCEPT"))
    state_db.upsert_record(
        _sidecar(jid="rev1", v2_verification_decision="REVIEW_REQUIRED")
    )
    state_db.upsert_record(_sidecar(jid="blk1", v2_verification_decision="BLOCK"))

    total, rows = state_db.list_records(decision=["ACCEPT", "BLOCK"])
    assert total == 2
    jids = {r["jid"] for r in rows}
    assert jids == {"acc1", "blk1"}


def test_list_records_filters_by_status(fresh_db):
    state_db.upsert_record(_sidecar(jid="imp1"), derived_status="imported")
    state_db.upsert_record(_sidecar(jid="rev1"), derived_status="needs_review")

    total, rows = state_db.list_records(status=["needs_review"])
    assert total == 1
    assert rows[0]["jid"] == "rev1"


def test_list_records_pagination(fresh_db):
    for i in range(15):
        state_db.upsert_record(_sidecar(jid=f"page{i:02d}", ts=1779600000.0 + i))
    total, rows = state_db.list_records(limit=5, offset=0)
    assert total == 15
    assert len(rows) == 5


def test_list_records_sort_ts_desc_default(fresh_db):
    state_db.upsert_record(
        _sidecar(
            jid="old",
            ts=1779600000.0,
            lifecycle={"state": "created", "created_at": 1779600000.0},
        )
    )
    state_db.upsert_record(
        _sidecar(
            jid="new",
            ts=1779700000.0,
            lifecycle={"state": "created", "created_at": 1779700000.0},
        )
    )
    total, rows = state_db.list_records()
    assert rows[0]["jid"] == "new"


# ---------- count_by_status ----------


def test_count_by_status_aggregates(fresh_db):
    state_db.upsert_record(_sidecar(jid="a"), derived_status="imported")
    state_db.upsert_record(_sidecar(jid="b"), derived_status="imported")
    state_db.upsert_record(_sidecar(jid="c"), derived_status="needs_review")
    counts = state_db.count_by_status()
    assert counts.get("imported") == 2
    assert counts.get("needs_review") == 1


# ---------- upsert_from_sidecar (full integration) ----------


def test_upsert_from_sidecar_writes_all_tables(fresh_db):
    sidecar = _sidecar(jid="full123")
    sidecar["sensors"] = [
        {"name": "ffprobe", "class": "hard_gate", "status": "pass", "confidence": 1.0},
    ]
    sidecar["files"] = [
        {
            "filename": "01 - Track.flac",
            "sample_rate": 96000,
            "bit_depth": 24,
            "detective_verdict": "AUTHENTIC",
        },
    ]
    state_db.upsert_from_sidecar(sidecar, derived_status="imported")

    rec = state_db.get_record("full123")
    assert rec is not None

    import sqlite3

    conn = sqlite3.connect(str(fresh_db))
    sensor_count = conn.execute(
        "SELECT COUNT(*) FROM sensor_runs WHERE jid='full123'"
    ).fetchone()[0]
    file_count = conn.execute(
        "SELECT COUNT(*) FROM file_evidence WHERE jid='full123'"
    ).fetchone()[0]
    assert sensor_count == 1
    assert file_count == 1


# ---------- Defensive: failures don't propagate ----------


def test_upsert_record_does_not_raise_on_init_failure(monkeypatch, tmp_path):
    """If DB init fails (e.g., permission denied), upserts return silently."""
    # Point to unwritable path
    state_db._initialized = False
    # Use a path that can't be created — simulate failure
    bad_path = tmp_path / "nonexistent" / "deep" / "path.db"
    state_db._db_path = bad_path

    # _ensure_initialized will try init(), which mkdir(parents=True) — so this will work
    # Better simulate: monkeypatch _connect to raise
    def _raise(*a, **kw):
        raise OSError("permission denied")

    monkeypatch.setattr(state_db, "_connect", _raise)

    # Should not raise
    state_db.upsert_record(_sidecar())
    state_db.log_action("jid", "test", "actor", "ok")
