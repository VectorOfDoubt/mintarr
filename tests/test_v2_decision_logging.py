"""Regression tests for V2-aware decision logging."""

from __future__ import annotations

import json

import pytest

import server
from verification import VerificationResult


def _read_records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _verification_result(**overrides):
    defaults = {
        "jid": "abc12345",
        "score": 85,
        "verification_decision": "ACCEPT",
        "import_outcome": "MANUAL_IMPORTED",
        "components": {"ffprobe": 25, "flac_t": 25, "detective": 35, "complete": 0},
        "overrides": [],
        "verdict": "AUTHENTIC",
        "new_kbps": 3000,
        "existing_kbps": 320,
        "existing_label": "MP3-320",
        "album_ids": [101],
        "title": "Artist - Album",
    }
    defaults.update(overrides)
    return VerificationResult(**defaults)


def test_log_decision_keeps_legacy_shape_without_v2_result(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DECISIONS_LOG", tmp_path / "decisions.jsonl")
    monkeypatch.setenv("V2_VERIFICATION_ENABLED", "true")

    server._log_decision("legacy123", decision="BLOCKED", reason="no upgrade")

    [record] = _read_records(server.DECISIONS_LOG)
    assert record["jid"] == "legacy123"
    assert record["decision"] == "BLOCKED"
    assert record["reason"] == "no upgrade"
    assert "v2_verification_decision" not in record
    assert record["ts"] > 0
    assert record["ts_iso"]


def test_log_decision_strips_v2_fields_when_flag_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DECISIONS_LOG", tmp_path / "decisions.jsonl")
    monkeypatch.setenv("V2_VERIFICATION_ENABLED", "false")

    server._log_decision(
        "abc12345",
        v2_result=_verification_result(
            sensors=[{"name": "ffprobe", "status": "pass"}],
            files=[{"filename": "01.flac"}],
        ),
    )

    [record] = _read_records(server.DECISIONS_LOG)
    assert record["jid"] == "abc12345"
    assert record["decision"] == "IMPORTED_AUTHENTIC"
    assert record["reason"] == "upgrade from MP3-320"
    assert "v2_verification_decision" not in record
    assert "v2_import_outcome" not in record
    assert "sensors" not in record
    assert "files" not in record


@pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
def test_log_decision_writes_v2_fields_when_flag_enabled(value, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DECISIONS_LOG", tmp_path / "decisions.jsonl")
    monkeypatch.setenv("V2_VERIFICATION_ENABLED", value)

    server._log_decision("abc12345", v2_result=_verification_result(), error="extra context")

    [record] = _read_records(server.DECISIONS_LOG)
    assert record["jid"] == "abc12345"
    assert record["decision"] == "IMPORTED_AUTHENTIC"
    assert record["v2_verification_decision"] == "ACCEPT"
    assert record["v2_import_outcome"] == "MANUAL_IMPORTED"
    assert record["v2_score"] == 85
    assert record["v2_components"] == {"ffprobe": 25, "flac_t": 25, "detective": 35, "complete": 0}
    assert record["v2_overrides"] == []
    assert record["error"] == "extra context"
    assert record["ts"] > 0
    assert record["ts_iso"]


def test_log_decision_preserves_result_fields_over_extra_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DECISIONS_LOG", tmp_path / "decisions.jsonl")
    monkeypatch.setenv("V2_VERIFICATION_ENABLED", "true")

    server._log_decision(
        "abc12345",
        v2_result=_verification_result(),
        decision="BLOCKED",
        v2_score=0,
    )

    [record] = _read_records(server.DECISIONS_LOG)
    assert record["decision"] == "IMPORTED_AUTHENTIC"
    assert record["v2_score"] == 85


def test_log_decision_rejects_mismatched_v2_jid(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DECISIONS_LOG", tmp_path / "decisions.jsonl")

    with pytest.raises(ValueError):
        server._log_decision("other123", v2_result=_verification_result())

    assert not server.DECISIONS_LOG.exists()
