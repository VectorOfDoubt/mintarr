"""Tests for F1.5 backfill script."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import backfill_state
import state_db


@pytest.fixture
def fresh_db(tmp_path):
    db_file = tmp_path / "state.db"
    state_db._initialized = False
    state_db.init(db_path=db_file)
    yield db_file
    state_db._initialized = False


def _write_sidecar(directory: Path, jid: str, **overrides) -> Path:
    """Write a verification.json sidecar in either live (output/<jid>/) or archived (flat) layout."""
    base = {
        "jid": jid,
        "title": f"Artist {jid}",
        "album_ids": [hash(jid) % 10000],
        "ts": 1779600000.0,
        "v2_verification_decision": "ACCEPT",
        "v2_import_outcome": "MANUAL_IMPORTED",
        "v2_score": 85,
        "verdict": "AUTHENTIC",
        "lifecycle": {"state": "created", "created_at": 1779600000.0},
    }
    base.update(overrides)
    # Detect layout: if directory ends in jid (live), use verification.json
    if directory.name == jid:
        path = directory / "verification.json"
    else:
        path = directory / f"{jid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(base))
    return path


def test_backfill_from_empty_dirs(fresh_db, tmp_path):
    counts = backfill_state.backfill(
        output_base=tmp_path / "output",
        blocked_dir=tmp_path / "blocked",
        discarded_dir=tmp_path / "discarded",
        expired_dir=tmp_path / "expired",
    )
    assert counts["total"] == 0


def test_backfill_ingests_live_sidecars(fresh_db, tmp_path):
    output_base = tmp_path / "output"
    _write_sidecar(output_base / "abc123", "abc123")
    _write_sidecar(output_base / "def456", "def456",
                   v2_verification_decision="REVIEW_REQUIRED",
                   lifecycle={"state": "pending_review", "created_at": 1779600000.0})

    counts = backfill_state.backfill(
        output_base=output_base,
        blocked_dir=tmp_path / "blocked",
        discarded_dir=tmp_path / "discarded",
        expired_dir=tmp_path / "expired",
    )
    assert counts["output"] == 2
    assert counts["total"] == 2

    rec1 = state_db.get_record("abc123")
    rec2 = state_db.get_record("def456")
    assert rec1["derived_status"] == "imported"
    assert rec2["derived_status"] == "needs_review"


def test_backfill_ingests_archived_sidecars(fresh_db, tmp_path):
    discarded_dir = tmp_path / "discarded"
    _write_sidecar(discarded_dir, "disc111",
                   lifecycle={"state": "discarded", "actor": "user_discard", "discarded_at": 1779700000.0})

    expired_dir = tmp_path / "expired"
    _write_sidecar(expired_dir, "exp222",
                   lifecycle={"state": "expired", "actor": "auto_expire", "expired_at": 1779800000.0})

    counts = backfill_state.backfill(
        output_base=tmp_path / "output",
        blocked_dir=tmp_path / "blocked",
        discarded_dir=discarded_dir,
        expired_dir=expired_dir,
    )
    assert counts["discarded"] == 1
    assert counts["expired"] == 1
    assert state_db.get_record("disc111")["derived_status"] == "discarded"
    assert state_db.get_record("exp222")["derived_status"] == "expired"


def test_backfill_live_sidecar_wins_over_archive(fresh_db, tmp_path):
    """If same jid in both live + archived, live (output) should win — scanned first."""
    output_base = tmp_path / "output"
    discarded_dir = tmp_path / "discarded"
    _write_sidecar(output_base / "dup123", "dup123",
                   v2_verification_decision="ACCEPT",
                   v2_import_outcome="MANUAL_IMPORTED")
    _write_sidecar(discarded_dir, "dup123",
                   v2_verification_decision="REVIEW_REQUIRED",
                   lifecycle={"state": "discarded", "actor": "user_discard"})

    counts = backfill_state.backfill(
        output_base=output_base,
        blocked_dir=tmp_path / "blocked",
        discarded_dir=discarded_dir,
        expired_dir=tmp_path / "expired",
    )
    # Live wins, archive skipped
    assert counts["output"] == 1
    assert counts["skipped"] == 1
    rec = state_db.get_record("dup123")
    assert rec["verification_decision"] == "ACCEPT"


def test_backfill_dry_run_does_not_write(fresh_db, tmp_path):
    output_base = tmp_path / "output"
    _write_sidecar(output_base / "dry123", "dry123")
    counts = backfill_state.backfill(
        output_base=output_base,
        blocked_dir=tmp_path / "blocked",
        discarded_dir=tmp_path / "discarded",
        expired_dir=tmp_path / "expired",
        dry_run=True,
    )
    assert counts["output"] == 1
    assert state_db.get_record("dry123") is None  # not written


def test_backfill_handles_malformed_sidecar(fresh_db, tmp_path):
    output_base = tmp_path / "output"
    bad_dir = output_base / "bad123"
    bad_dir.mkdir(parents=True)
    (bad_dir / "verification.json").write_text("{not valid json")

    counts = backfill_state.backfill(
        output_base=output_base,
        blocked_dir=tmp_path / "blocked",
        discarded_dir=tmp_path / "discarded",
        expired_dir=tmp_path / "expired",
    )
    assert counts["errors"] == 1
    assert counts["output"] == 0


def test_derive_status_for_backfill_policy_violation(fresh_db, tmp_path):
    """BLOCK decision + MANUAL_IMPORTED outcome = policy violation (audit-record)."""
    sidecar = {
        "jid": "pv123",
        "v2_verification_decision": "BLOCK",
        "v2_import_outcome": "MANUAL_IMPORTED",
        "lifecycle": {"state": "created"},
    }
    assert backfill_state._derive_status_for_backfill(sidecar) == "policy_violation"
