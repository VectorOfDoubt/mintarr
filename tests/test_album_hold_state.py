"""Tests for #160 album-level cancel/hold state primitives."""

from __future__ import annotations

import sqlite3

import state_db


def _fresh_db(tmp_path):
    db_file = tmp_path / "state.db"
    state_db._initialized = False
    state_db.init(db_path=db_file)
    return db_file


def test_album_hold_schema_is_created(tmp_path):
    _fresh_db(tmp_path)

    with state_db._connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }

    assert "album_holds" in tables
    assert "idx_album_holds_active" in indexes


def test_create_album_hold_returns_active_row_with_details(tmp_path):
    _fresh_db(tmp_path)

    hold = state_db.create_album_hold(
        123,
        reason="operator_cancelled_active_grab",
        now=1000.0,
        ttl_seconds=600.0,
        source_jid="abc123",
        source_type="tidal",
        source_id="release-1",
        actor="lidarr_remove",
        details={"artist": "Artist", "album_title": "Album"},
    )

    assert hold is not None
    assert hold["album_id"] == 123
    assert hold["reason"] == "operator_cancelled_active_grab"
    assert hold["expires_at"] == 1600.0
    assert hold["source_jid"] == "abc123"
    assert hold["details"] == {"artist": "Artist", "album_title": "Album"}
    assert state_db.is_album_held(123, now=1200.0) is True


def test_expired_album_hold_is_inactive_but_queryable_as_history(tmp_path):
    _fresh_db(tmp_path)
    state_db.create_album_hold(123, reason="cancel", now=1000.0, ttl_seconds=10.0)

    assert state_db.get_album_hold(123, now=1011.0) is None

    historical = state_db.get_album_hold(123, include_inactive=True, now=1011.0)
    assert historical is not None
    assert historical["album_id"] == 123


def test_list_album_holds_separates_active_from_history(tmp_path):
    _fresh_db(tmp_path)
    state_db.create_album_hold(1, reason="cancel", now=1000.0, ttl_seconds=500.0)
    state_db.create_album_hold(2, reason="cancel", now=1000.0, ttl_seconds=1.0)
    state_db.clear_album_hold(1, actor="operator", now=1005.0)
    state_db.create_album_hold(3, reason="cancel", now=1000.0, ttl_seconds=500.0)

    total_active, active = state_db.list_album_holds(active_only=True, now=1010.0)
    total_all, rows = state_db.list_album_holds(active_only=False, now=1010.0)

    assert total_active == 1
    assert [row["album_id"] for row in active] == [3]
    assert total_all == 3
    assert {row["album_id"] for row in rows} == {1, 2, 3}


def test_clear_album_hold_marks_hold_inactive(tmp_path):
    _fresh_db(tmp_path)
    state_db.create_album_hold(123, reason="cancel", now=1000.0, ttl_seconds=600.0)

    assert state_db.clear_album_hold(123, actor="operator", now=1005.0) is True
    assert state_db.clear_album_hold(123, actor="operator", now=1006.0) is False

    assert state_db.get_album_hold(123, now=1007.0) is None
    row = state_db.get_album_hold(123, include_inactive=True, now=1007.0)
    assert row is not None
    assert row["cleared_at"] == 1005.0
    assert row["cleared_by"] == "operator"


def test_expire_album_holds_clears_only_expired_active_rows(tmp_path):
    _fresh_db(tmp_path)
    state_db.create_album_hold(1, reason="cancel", now=1000.0, ttl_seconds=10.0)
    state_db.create_album_hold(2, reason="cancel", now=1000.0, ttl_seconds=100.0)
    state_db.create_album_hold(3, reason="cancel", now=1000.0, ttl_seconds=10.0)
    state_db.clear_album_hold(3, actor="operator", now=1005.0)

    assert state_db.expire_album_holds(now=1011.0) == 1

    assert state_db.get_album_hold(1, now=1012.0) is None
    expired = state_db.get_album_hold(1, include_inactive=True, now=1012.0)
    assert expired is not None
    assert expired["cleared_by"] == "auto_expire"
    assert state_db.get_album_hold(2, now=1012.0) is not None
    already_cleared = state_db.get_album_hold(3, include_inactive=True, now=1012.0)
    assert already_cleared is not None
    assert already_cleared["cleared_by"] == "operator"


def test_create_album_hold_reactivates_existing_row(tmp_path):
    _fresh_db(tmp_path)
    state_db.create_album_hold(123, reason="cancel", now=1000.0, ttl_seconds=10.0)
    state_db.clear_album_hold(123, actor="operator", now=1001.0)

    recreated = state_db.create_album_hold(
        123,
        reason="cancel_again",
        now=2000.0,
        ttl_seconds=50.0,
        details={"album_title": "New snapshot"},
    )

    assert recreated is not None
    assert recreated["reason"] == "cancel_again"
    assert recreated["created_at"] == 1000.0
    assert recreated["updated_at"] == 2000.0
    assert recreated["cleared_at"] is None
    assert recreated["cleared_by"] is None
    assert recreated["details"] == {"album_title": "New snapshot"}
    total, rows = state_db.list_album_holds(active_only=False, now=2001.0)
    assert total == 1
    assert rows[0]["album_id"] == 123


def test_album_hold_helpers_are_defensive_for_invalid_album_id(tmp_path):
    _fresh_db(tmp_path)

    assert state_db.create_album_hold(0, reason="cancel") is None
    assert state_db.create_album_hold("not-int", reason="cancel") is None
    assert state_db.get_album_hold("not-int") is None
    assert state_db.clear_album_hold("not-int") is False


def test_album_hold_details_json_parse_failure_returns_empty_details(tmp_path):
    _fresh_db(tmp_path)
    with state_db._connect() as conn:
        conn.execute(
            """
            INSERT INTO album_holds
              (album_id, reason, created_at, updated_at, expires_at, details_json)
            VALUES (1, 'cancel', 1, 1, 999, '{broken')
            """
        )

    row = state_db.get_album_hold(1, now=2)

    assert row is not None
    assert row["details"] == {}


def test_existing_database_gets_album_hold_table_on_init(tmp_path):
    db_file = tmp_path / "state.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """
            CREATE TABLE records (
                jid TEXT PRIMARY KEY,
                derived_status TEXT,
                created_at REAL,
                verification_decision TEXT
            )
            """
        )

    state_db._initialized = False
    state_db.init(db_path=db_file)

    hold = state_db.create_album_hold(123, reason="cancel", now=1000.0)
    assert hold is not None
