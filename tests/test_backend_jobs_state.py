"""ADR-0014 slice 3 — Mintarr-managed download-backend queue state."""

from __future__ import annotations

import pytest

import state_db


def _fresh_db(tmp_path):
    state_db._initialized = False
    state_db.init(db_path=tmp_path / "state.db")


# ---- schema --------------------------------------------------------------


def test_backend_jobs_schema_is_created(tmp_path):
    _fresh_db(tmp_path)
    with state_db._connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    assert "backend_jobs" in tables
    assert "idx_backend_jobs_backend_lookup" in indexes
    assert "idx_backend_jobs_state" in indexes


# ---- create / get --------------------------------------------------------


def test_create_and_get_round_trip(tmp_path):
    _fresh_db(tmp_path)
    job = state_db.create_backend_job(
        "jid-1",
        source_type="sab_usenet_backend",
        category="mintarr-music",
        backend_job_id="NZO-1",
        target_album_id=42,
        release_title="Artist - Album",
        details={"indexer": "x"},
    )
    assert job is not None
    assert job["jid"] == "jid-1"
    assert job["backend_job_id"] == "NZO-1"
    assert job["category"] == "mintarr-music"
    assert job["state"] == "queued"
    assert job["target_album_id"] == 42
    assert job["details"] == {"indexer": "x"}

    fetched = state_db.get_backend_job("jid-1")
    assert fetched["release_title"] == "Artist - Album"
    assert fetched["finished_at"] is None


def test_create_rejects_bad_input(tmp_path):
    _fresh_db(tmp_path)
    assert state_db.create_backend_job("", source_type="sab", category="c") is None
    assert state_db.create_backend_job("j", source_type="", category="c") is None
    assert state_db.create_backend_job("j", source_type="s", category="") is None
    assert (
        state_db.create_backend_job("j", source_type="s", category="c", state="bogus")
        is None
    )


def test_create_upserts_on_same_jid(tmp_path):
    _fresh_db(tmp_path)
    state_db.create_backend_job("jid-1", source_type="s", category="c")
    again = state_db.create_backend_job(
        "jid-1",
        source_type="s",
        category="c",
        backend_job_id="NZO-9",
        state="downloading",
    )
    assert again["backend_job_id"] == "NZO-9"
    assert again["state"] == "downloading"
    total, _ = state_db.list_backend_jobs()
    assert total == 1


def test_get_by_backend_id(tmp_path):
    _fresh_db(tmp_path)
    state_db.create_backend_job(
        "jid-1",
        source_type="qbittorrent_backend",
        category="c",
        backend_job_id="deadbeef",
    )
    job = state_db.get_backend_job_by_backend_id(
        "deadbeef", source_type="qbittorrent_backend"
    )
    assert job["jid"] == "jid-1"
    # scoped by source_type: the same id under a different backend never matches
    assert (
        state_db.get_backend_job_by_backend_id(
            "deadbeef", source_type="sab_usenet_backend"
        )
        is None
    )
    assert (
        state_db.get_backend_job_by_backend_id(
            "nope", source_type="qbittorrent_backend"
        )
        is None
    )
    assert (
        state_db.get_backend_job_by_backend_id("", source_type="qbittorrent_backend")
        is None
    )


# ---- update --------------------------------------------------------------


def test_update_patches_only_given_fields(tmp_path):
    _fresh_db(tmp_path)
    state_db.create_backend_job("jid-1", source_type="s", category="c")
    updated = state_db.update_backend_job(
        "jid-1",
        state="completed",
        backend_path="/backend/complete/Album",
        target_album_id=9829,
        finished=True,
    )
    assert updated["state"] == "completed"
    assert updated["backend_path"] == "/backend/complete/Album"
    assert updated["target_album_id"] == 9829
    assert updated["finished_at"] is not None
    # untouched fields preserved
    assert updated["category"] == "c"


@pytest.mark.parametrize("terminal", sorted(state_db.BACKEND_JOB_TERMINAL_STATES))
def test_create_does_not_reopen_terminal_job(tmp_path, terminal):
    _fresh_db(tmp_path)
    state_db.create_backend_job("jid-1", source_type="s", category="c")
    state_db.update_backend_job("jid-1", state=terminal)
    # an upsert must not resurrect ANY terminal job (imported/failed/cancelled);
    # the guard is built from BACKEND_JOB_TERMINAL_STATES so it cannot drift.
    again = state_db.create_backend_job(
        "jid-1", source_type="s", category="c", state="queued"
    )
    assert again["state"] == terminal
    assert state_db.get_backend_job("jid-1")["state"] == terminal


def test_update_rejects_bad_state_and_missing_jid(tmp_path):
    _fresh_db(tmp_path)
    state_db.create_backend_job("jid-1", source_type="s", category="c")
    assert state_db.update_backend_job("jid-1", state="bogus") is None
    assert state_db.update_backend_job("nope", state="failed") is None


# ---- list / active -------------------------------------------------------


def test_list_orders_and_filters_active(tmp_path):
    _fresh_db(tmp_path)
    state_db.create_backend_job("a", source_type="s", category="c", now=1.0)
    state_db.create_backend_job("b", source_type="s", category="c", now=2.0)
    state_db.update_backend_job("a", state="failed", now=3.0)  # terminal

    total, rows = state_db.list_backend_jobs()
    assert total == 2
    assert rows[0]["jid"] == "a"  # updated_at newest first

    active_total, active_rows = state_db.list_backend_jobs(active_only=True)
    assert active_total == 1
    assert [r["jid"] for r in active_rows] == ["b"]

    assert [j["jid"] for j in state_db.list_active_backend_jobs()] == ["b"]


def test_list_imported_backend_jobs_with_pending_records(tmp_path):
    _fresh_db(tmp_path)
    state_db.create_backend_job(
        "imported-pending",
        source_type="s",
        category="c",
        state="imported",
        release_title="Imported Pending",
    )
    state_db.create_backend_job(
        "imported-done",
        source_type="s",
        category="c",
        state="imported",
        release_title="Imported Done",
    )
    state_db.create_backend_job(
        "active-pending",
        source_type="s",
        category="c",
        state="completed",
        release_title="Active Pending",
    )
    state_db.upsert_record(
        {
            "jid": "imported-pending",
            "v2_import_outcome": "PENDING",
            "v2_verification_decision": "ACCEPT",
        },
        derived_status="pending",
    )
    state_db.upsert_record(
        {
            "jid": "imported-done",
            "v2_import_outcome": "MANUAL_IMPORTED",
            "v2_verification_decision": "ACCEPT",
        },
        derived_status="imported",
    )
    state_db.upsert_record(
        {
            "jid": "active-pending",
            "v2_import_outcome": "PENDING",
            "v2_verification_decision": "ACCEPT",
        },
        derived_status="pending",
    )

    rows = state_db.list_imported_backend_jobs_with_pending_records()

    assert [row["jid"] for row in rows] == ["imported-pending"]


# ---- reconciliation mapping (pure) ---------------------------------------


@pytest.mark.parametrize(
    ("current", "backend", "expected"),
    [
        ("queued", "downloading", "downloading"),
        ("downloading", "completed", "completed"),
        ("downloading", "failed", "failed"),
        ("queued", "queued", "queued"),
        ("downloading", "unknown", "downloading"),  # transient unknown: no flip
        ("failed", "downloading", "failed"),  # terminal never reopened
        ("cancelled", "completed", "cancelled"),
        ("importing", "completed", "importing"),  # worker owns post-completion
        ("review", "completed", "review"),
    ],
)
def test_next_lifecycle_state(current, backend, expected):
    assert state_db.next_lifecycle_state(current, backend) == expected
