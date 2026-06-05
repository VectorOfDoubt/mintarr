"""Tests for opt-in outbound notifications (Phase 3 slice 4)."""

from __future__ import annotations

import sys
import types

import notifications
import server


def test_notify_is_noop_without_urls(monkeypatch):
    monkeypatch.delenv("MINTARR_NOTIFY_URLS", raising=False)
    assert notifications.is_enabled() is False
    assert notifications.notify("t", "b") is False


def test_notify_sends_when_configured(monkeypatch):
    monkeypatch.setenv("MINTARR_NOTIFY_URLS", "json://localhost/x , ntfy://h/topic")
    sent: dict = {}

    class _FakeAp:
        def add(self, url):
            sent.setdefault("urls", []).append(url)

        def notify(self, title, body):
            sent["title"], sent["body"] = title, body
            return True

    monkeypatch.setitem(
        sys.modules, "apprise", types.SimpleNamespace(Apprise=lambda: _FakeAp())
    )
    assert notifications.notify("hello", "world") is True
    assert sent["title"] == "hello"
    assert sent["urls"] == ["json://localhost/x", "ntfy://h/topic"]


def test_notify_event_for_maps_attention_events_only():
    assert (
        server._notify_event_for(
            {"verification_decision": "REVIEW_REQUIRED", "jid": "a", "title": "X"}
        )[0]
        == "Mintarr: review required"
    )
    assert (
        server._notify_event_for({"decision": "IMPORT_FAILED", "jid": "b"})[0]
        == "Mintarr: import failed"
    )
    assert (
        server._notify_event_for(
            {
                "verification_decision": "BLOCK",
                "import_outcome": "MANUAL_IMPORTED",
                "jid": "c",
            }
        )[0]
        == "Mintarr: policy violation"
    )
    # Normal successful import -> no notification.
    assert (
        server._notify_event_for(
            {"verification_decision": "ACCEPT", "decision": "IMPORTED_AUTHENTIC"}
        )
        is None
    )


def test_maybe_notify_decision_calls_notify_only_for_events(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        "notifications.notify", lambda title, body: calls.append((title, body)) or True
    )
    server._maybe_notify_decision(
        {"verification_decision": "REVIEW_REQUIRED", "jid": "z", "title": "Z"}
    )
    assert calls and calls[0][0] == "Mintarr: review required"
    calls.clear()
    server._maybe_notify_decision({"verification_decision": "ACCEPT", "jid": "z"})
    assert not calls


def test_log_decision_notifies_for_real_v2_review(monkeypatch, tmp_path):
    """End-to-end through _log_decision with a real VerificationResult.

    Guards the v2_-prefixed key names: V2 records use v2_verification_decision /
    v2_import_outcome, so a plain verification_decision lookup would silently miss.
    """
    from verification import VerificationResult

    monkeypatch.setattr(server, "DECISIONS_LOG", tmp_path / "decisions.jsonl")
    calls: list = []
    monkeypatch.setattr(
        "notifications.notify", lambda title, body: calls.append((title, body)) or True
    )
    result = VerificationResult(
        jid="rev12345",
        score=40,
        verification_decision="REVIEW_REQUIRED",
        import_outcome="PENDING",
        components={"ffprobe": 25, "flac_t": 25, "detective": 35, "complete": 0},
        overrides=[],
        verdict="SUSPICIOUS",
        new_kbps=1000,
        existing_kbps=0,
        existing_label="nothing",
        album_ids=[1],
        title="Some Album",
    )
    server._log_decision("rev12345", v2_result=result)
    assert calls and calls[0][0] == "Mintarr: review required"
    assert "Some Album" in calls[0][1]
