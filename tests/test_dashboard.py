"""Tests for V2.2 dashboard endpoints + HTML page."""

from __future__ import annotations

import json
from pathlib import Path

import server
import state_db
from verification import VerificationResult

VALID_KEY = "tidalhires-test-api-key"


def _result(jid="abc12345", decision="ACCEPT", outcome="MANUAL_IMPORTED"):
    return VerificationResult(
        jid=jid,
        score=85,
        verification_decision=decision,
        import_outcome=outcome,
        components={"ffprobe": 25, "flac_t": 25, "detective": 35, "complete": 0},
        overrides=[],
        verdict="AUTHENTIC",
        new_kbps=3000,
        existing_kbps=0,
        existing_label="nothing",
        album_ids=[100],
        title="Test Artist - Test Album",
    )


def _patch_paths(monkeypatch, tmp_path):
    output_base = tmp_path / "output"
    monkeypatch.setattr(server, "OUTPUT_BASE", output_base)
    monkeypatch.setattr(server, "BLOCKED_DECISIONS_DIR", tmp_path / "blocked")
    monkeypatch.setattr(server, "DISCARDED_DIR", tmp_path / "discarded")
    monkeypatch.setattr(server, "EXPIRED_REVIEW_DIR", tmp_path / "expired")
    return output_base


def test_dashboard_summary_requires_apikey():
    client = server.app.test_client()
    assert client.get("/dashboard/v1/summary").status_code == 401


def test_dashboard_records_requires_apikey():
    client = server.app.test_client()
    assert client.get("/dashboard/v1/records").status_code == 401


def test_dashboard_action_requires_apikey():
    client = server.app.test_client()
    assert client.post("/dashboard/v1/action/abc").status_code == 401


def test_dashboard_html_does_not_require_apikey(monkeypatch, tmp_path):
    """HTML shell loads without auth — JS handles auth via localStorage."""
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.content_type
    body = resp.get_data(as_text=True)
    assert "TidalHires V2 Dashboard" in body
    assert "summary-grid" in body
    assert "drawer" in body
    assert "integrations-body" in body
    # Assets are extracted to static files (#45); the shell only references them
    # and injects the one server-side value via the bootstrap script.
    assert '<link rel="stylesheet" href="/static/dashboard.css">' in body
    assert '<script src="/static/dashboard.js"></script>' in body
    assert "window.LIDARR_WEB_BASE =" in body
    assert "<style>" not in body


def test_dashboard_static_assets_served_without_apikey(monkeypatch, tmp_path):
    """CSS/JS are served by Flask static, unauthenticated, with sane types."""
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    css = client.get("/static/dashboard.css")
    assert css.status_code == 200
    assert "text/css" in css.content_type
    js = client.get("/static/dashboard.js")
    assert js.status_code == 200
    assert "javascript" in js.content_type


def test_dashboard_js_contains_connector_rendering(monkeypatch, tmp_path):
    """Connector/install-guidance logic lives in the extracted dashboard.js."""
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    body = client.get("/static/dashboard.js").get_data(as_text=True)
    assert "api('/connectors')" in body
    assert "function renderIntegrations" in body
    assert "connector-card" in body
    assert "saveConnectorConfig" in body
    assert "renderInstallGuidance" in body
    assert "Install guidance" in body
    assert "/connectors/' + encodeURIComponent(connectorId) + '/config" in body
    assert "Required env" in body
    # The server placeholder was rewired to a window global during extraction.
    assert "const LIDARR_WEB_BASE = window.LIDARR_WEB_BASE;" in body
    assert "__LIDARR_WEB_BASE__" not in body


def test_dashboard_js_contains_release_identity_drawer(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    body = client.get("/static/dashboard.js").get_data(as_text=True)

    assert "function renderReleaseIdentity" in body
    assert "Release identity" in body
    assert "identityBadgeClass" in body
    assert "Observed metadata" in body


def test_dashboard_vendors_alpine_with_sri(monkeypatch, tmp_path):
    """Alpine is vendored (no Node) and referenced with an SRI hash."""
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    asset = client.get("/static/vendor/alpine-3.14.1.min.js")
    assert asset.status_code == 200
    assert "javascript" in asset.content_type
    shell = client.get("/dashboard").get_data(as_text=True)
    assert "vendor/alpine-3.14.1.min.js" in shell
    assert 'integrity="sha384-' in shell


def test_dashboard_theme_switch_present(monkeypatch, tmp_path):
    """Theme switch: early bootstrap, Alpine toggle, and a light palette."""
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    shell = client.get("/dashboard").get_data(as_text=True)
    # Early no-FOUC bootstrap applies the persisted preference before paint.
    assert 'localStorage.getItem("mintarr_theme")' in shell
    assert 'setAttribute("data-theme"' in shell
    # Alpine-driven toggle control.
    assert 'class="theme-switch"' in shell
    assert 'class="theme-btn"' in shell
    assert "cycle()" in shell
    # Light palette exists in the stylesheet (dark stays the :root default).
    css = client.get("/static/dashboard.css").get_data(as_text=True)
    assert '[data-theme="light"]' in css


def test_dashboard_sidebar_shell_present(monkeypatch, tmp_path):
    """Sidebar nav (#50) replaces the tab bar with seven sections."""
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    shell = client.get("/dashboard").get_data(as_text=True)
    # Sidebar shell with Alpine-driven section state, no old tab bar.
    assert 'class="sidebar"' in shell
    assert "localStorage.getItem('mintarr_section')" in shell
    assert 'class="tab-bar"' not in shell
    assert "showView(" not in shell
    # All seven sections are navigable and rendered.
    for sec in (
        "overview",
        "queue",
        "history",
        "review",
        "connectors",
        "settings",
        "system",
    ):
        assert f"go('{sec}')" in shell
        assert f"section==='{sec}'" in shell
    # Existing content kept its ids inside the new sections.
    assert 'id="summary-grid"' in shell
    assert 'id="records-table"' in shell
    assert 'id="integrations-body"' in shell
    # Responsive collapse hook.
    css = client.get("/static/dashboard.css").get_data(as_text=True)
    assert "@media (max-width: 768px)" in css
    assert ".nav-toggle" in css


def test_queue_partial_requires_apikey():
    client = server.app.test_client()
    assert client.get("/dashboard/v1/queue/partial").status_code == 401


def test_queue_partial_renders_html_fragment(monkeypatch, tmp_path):
    """The HTMX queue partial returns an HTML fragment, not JSON (#50/slice 3)."""
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/queue/partial?apikey={VALID_KEY}")
    assert resp.status_code == 200
    assert "text/html" in resp.content_type
    body = resp.get_data(as_text=True)
    assert "queue depth" in body
    # A fresh state_db has no active jobs.
    assert "No active worker jobs." in body
    assert "<html" not in body  # fragment, not a full document


def test_dashboard_vendors_htmx_and_wires_queue(monkeypatch, tmp_path):
    """HTMX is vendored (no Node) and drives the live Queue section."""
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    asset = client.get("/static/vendor/htmx-2.0.3.min.js")
    assert asset.status_code == 200
    assert "javascript" in asset.content_type
    shell = client.get("/dashboard").get_data(as_text=True)
    assert "vendor/htmx-2.0.3.min.js" in shell
    assert 'integrity="sha384-' in shell
    assert 'hx-get="/dashboard/v1/queue/partial"' in shell
    assert "hx-trigger=" in shell
    # HTMX requests carry the stored API key.
    js = client.get("/static/dashboard.js").get_data(as_text=True)
    assert "htmx:configRequest" in js


def test_history_partial_requires_apikey():
    client = server.app.test_client()
    assert client.get("/dashboard/v1/history/partial").status_code == 401


def test_history_partial_renders_html_fragment(monkeypatch, tmp_path):
    """The HTMX history partial returns an HTML fragment of terminal jobs."""
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/history/partial?apikey={VALID_KEY}")
    assert resp.status_code == 200
    assert "text/html" in resp.content_type
    body = resp.get_data(as_text=True)
    assert "recent job" in body
    assert "No completed jobs yet." in body  # fresh state_db
    assert "<html" not in body  # fragment, not a full document


def test_dashboard_wires_history_section(monkeypatch, tmp_path):
    """History section is HTMX-driven like Queue (#50/slice 4)."""
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    shell = client.get("/dashboard").get_data(as_text=True)
    assert 'hx-get="/dashboard/v1/history/partial"' in shell
    assert 'id="history-live"' in shell


def test_system_partial_requires_apikey():
    client = server.app.test_client()
    assert client.get("/dashboard/v1/system/partial").status_code == 401


def test_system_partial_renders_status_and_workers(monkeypatch, tmp_path):
    """The System partial surfaces stack health + worker cards (slice 5)."""
    _patch_paths(monkeypatch, tmp_path)
    from dashboard_cache import clear

    clear()
    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/system/partial?apikey={VALID_KEY}")
    assert resp.status_code == 200
    assert "text/html" in resp.content_type
    body = resp.get_data(as_text=True)
    assert "Status" in body
    assert "Workers" in body
    assert "Active jobs" in body
    assert "health-badge" in body  # stack components rendered with a status badge
    assert "tidalhires" in body
    assert "<html" not in body  # fragment, not a full document


def test_dashboard_wires_system_section(monkeypatch, tmp_path):
    """System section is HTMX-driven (slice 5)."""
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    shell = client.get("/dashboard").get_data(as_text=True)
    assert 'hx-get="/dashboard/v1/system/partial"' in shell
    assert 'id="system-live"' in shell


def test_dashboard_records_search_present(monkeypatch, tmp_path):
    """Review has a client-side records search (frontend-only, no endpoint)."""
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    shell = client.get("/dashboard").get_data(as_text=True)
    assert 'id="records-search"' in shell
    js = client.get("/static/dashboard.js").get_data(as_text=True)
    assert "function applyRecordsSearch()" in js
    assert "records-search" in js


def test_actions_download_requires_apikey():
    client = server.app.test_client()
    assert client.get("/dashboard/v1/actions/download").status_code == 401


def test_actions_download_csv(monkeypatch, tmp_path):
    """Audit trail downloads as a CSV attachment (the viewer's download half)."""
    _patch_paths(monkeypatch, tmp_path)
    import state_db

    state_db.log_action("dl123456", "promote", "operator", "ok", {})
    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/actions/download?apikey={VALID_KEY}")
    assert resp.status_code == 200
    assert "text/csv" in resp.content_type
    assert "attachment" in resp.headers.get("Content-Disposition", "")
    body = resp.get_data(as_text=True)
    assert "created_at,jid,action,actor,result" in body  # header row
    assert "promote" in body
    assert "dl123456" in body


def test_dashboard_topbar_search(monkeypatch, tmp_path):
    """Topbar search jumps to Review (Alpine event) and drives the records filter."""
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    shell = client.get("/dashboard").get_data(as_text=True)
    assert 'id="topbar-search"' in shell
    # Decoupled nav: a window event flips the Alpine section, no JS<->Alpine coupling.
    assert "@goto-section.window=" in shell
    assert "go($event.detail)" in shell
    js = client.get("/static/dashboard.js").get_data(as_text=True)
    assert "topbar-search" in js
    assert "goto-section" in js


def test_record_detail_includes_release_switch_events(monkeypatch, tmp_path):
    """Release-switch audit trail surfaces in the record detail (read-only, #70 req 10)."""
    _patch_paths(monkeypatch, tmp_path)
    import state_db
    from dashboard_cache import clear

    output_dir = tmp_path / "output" / "sw123456"
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar("sw123456", _result(jid="sw123456"), output_dir)
    state_db.log_action(
        jid="sw123456",
        action="release_switch",
        actor="mintarr_auto_high_confidence",
        result="ok",
        details={
            "event": "applied",
            "mode": "auto_high_confidence",
            "old_release_id": 11,
            "new_release_id": 22,
            "reasons": ["best score 96.0 >= 95.0"],
        },
    )
    clear()
    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/record/sw123456?apikey={VALID_KEY}")
    assert resp.status_code == 200
    events = resp.get_json().get("release_switch_events")
    assert events and len(events) >= 1
    assert events[0]["event"] == "applied"
    assert events[0]["new_release_id"] == 22
    assert events[0]["mode"] == "auto_high_confidence"


def test_dashboard_js_renders_release_switch_events(monkeypatch, tmp_path):
    """The drawer renders the release-switch audit section."""
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    js = client.get("/static/dashboard.js").get_data(as_text=True)
    assert "function renderReleaseSwitchEvents" in js
    assert "d.release_switch_events" in js


def test_system_partial_renders_audit_events(monkeypatch, tmp_path):
    """System Events card surfaces the recent audit feed (slice 7)."""
    _patch_paths(monkeypatch, tmp_path)
    import state_db
    from dashboard_cache import clear

    state_db.log_action("evt12345", "promote", "operator", "ok", {"note": "x"})
    clear()
    client = server.app.test_client()
    body = client.get(f"/dashboard/v1/system/partial?apikey={VALID_KEY}").get_data(
        as_text=True
    )
    assert "Events" in body
    assert "event-list" in body
    assert "promote" in body
    assert "evt12345" in body
    assert "Event feed lands in a later slice" not in body  # placeholder gone


def test_dashboard_settings_ui_card(monkeypatch, tmp_path):
    """Settings UI card is an Alpine form persisting prefs client-side (slice 6)."""
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    shell = client.get("/dashboard").get_data(as_text=True)
    assert 'class="settings-grid"' in shell
    # Theme + density controls bound with x-model and applied on change.
    assert 'id="set-theme"' in shell
    assert 'id="set-density"' in shell
    assert 'x-model="theme"' in shell
    assert 'x-model="density"' in shell
    assert "applyDensity()" in shell
    # Density is bootstrapped before paint (no flash), alongside theme.
    assert 'setAttribute("data-density"' in shell
    # Compact density has a stylesheet hook.
    css = client.get("/static/dashboard.css").get_data(as_text=True)
    assert '[data-density="compact"]' in css


def test_dashboard_summary_returns_expected_shape(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    output_dir = tmp_path / "output" / "sum12345"
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar("sum12345", _result(jid="sum12345"), output_dir)

    # Clear dashboard cache to ensure fresh fetch
    from dashboard_cache import clear

    clear()

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/summary?apikey={VALID_KEY}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "counts" in data
    assert "stack_health" in data
    assert "queue" in data
    assert data["counts"]["total_decisions"] >= 1


def test_dashboard_summary_counts_pending_from_derived_status(monkeypatch, tmp_path):
    """Terminal records with a historical PENDING outcome must not inflate the card."""
    _patch_paths(monkeypatch, tmp_path)
    output_base = tmp_path / "output"

    pending_dir = output_base / "pnd11111"
    pending_dir.mkdir(parents=True)
    server._write_verification_sidecar(
        "pnd11111",
        _result(jid="pnd11111", decision="ACCEPT", outcome="PENDING"),
        pending_dir,
    )

    discarded_dir = output_base / "dis22222"
    discarded_dir.mkdir(parents=True)
    discarded_path = server._write_verification_sidecar(
        "dis22222",
        _result(jid="dis22222", decision="REVIEW_REQUIRED", outcome="PENDING"),
        discarded_dir,
    )
    discarded = json.loads(discarded_path.read_text())
    discarded["lifecycle"]["state"] = "discarded"
    discarded_path.write_text(json.dumps(discarded))

    imported_dir = output_base / "imp33333"
    imported_dir.mkdir(parents=True)
    server._write_verification_sidecar(
        "imp33333",
        _result(jid="imp33333", decision="ACCEPT", outcome="MANUAL_IMPORTED"),
        imported_dir,
    )

    from dashboard_cache import clear

    clear()

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/summary?apikey={VALID_KEY}")
    assert resp.status_code == 200
    counts = resp.get_json()["counts"]
    assert counts["pending"] == 1
    assert counts["discarded"] == 1
    assert counts["imported"] == 1


def test_dashboard_summary_flags_blocking_lidarr_commands(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    output_dir = tmp_path / "output" / "cmd12345"
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar("cmd12345", _result(jid="cmd12345"), output_dir)

    import dashboard

    monkeypatch.setenv("LIDARR_API_URL", "http://lidarr/api/v1")
    monkeypatch.setattr(server, "_get_lidarr_key", lambda: "lidarr-key")
    monkeypatch.setattr(dashboard.time, "time", lambda: 7200.0)
    monkeypatch.setattr(dashboard, "_check_flac_detective", lambda: "ok")

    class FakeResponse:
        def __init__(self, payload, ok=True, status_code=200):
            self._payload = payload
            self.ok = ok
            self.status_code = status_code

        def json(self):
            return self._payload

    def fake_get(url, **kwargs):
        if url == "http://lidarr/api/v1/queue?pageSize=1":
            return FakeResponse({"totalRecords": 4})
        if url == "http://lidarr/api/v1/command":
            return FakeResponse(
                [
                    {
                        "id": 14568,
                        "name": "RescanFolders",
                        "status": "started",
                        "message": "Importing 1608 tracks",
                        "queued": "1970-01-01T00:00:00Z",
                        "started": "1970-01-01T00:10:00Z",
                    },
                    {
                        "id": 14647,
                        "name": "ManualImport",
                        "status": "queued",
                        "message": None,
                        "queued": "1970-01-01T01:00:00Z",
                        "started": None,
                    },
                ]
            )
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("requests.get", fake_get)

    from dashboard_cache import clear

    clear()

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/summary?apikey={VALID_KEY}")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["stack_health"]["lidarr"] == "blocked"
    commands = data["queue"]["lidarr_commands"]
    assert commands["status"] == "blocked"
    assert commands["active_count"] == 2
    assert commands["blocking_count"] == 2
    reasons = [item["blocking_reason"] for item in commands["commands"]]
    assert "RescanFolders has been started for 110m." in reasons
    assert "ManualImport is queued behind a started RescanFolders command." in reasons


def test_dashboard_records_returns_records(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    output_dir = tmp_path / "output" / "rec12345"
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar("rec12345", _result(jid="rec12345"), output_dir)

    from dashboard_cache import clear

    clear()

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/records?apikey={VALID_KEY}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "records" in data
    jids = [r["jid"] for r in data["records"]]
    assert "rec12345" in jids
    rec = next(r for r in data["records"] if r["jid"] == "rec12345")
    assert rec["status_reason"] == "Imported after quality checks passed."


def test_dashboard_records_filters_by_decision(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    output_base = tmp_path / "output"
    for jid, dec in [("acc111", "ACCEPT"), ("rev222", "REVIEW_REQUIRED")]:
        d = output_base / jid
        d.mkdir(parents=True)
        server._write_verification_sidecar(jid, _result(jid=jid, decision=dec), d)

    from dashboard_cache import clear

    clear()

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/records?decision=ACCEPT&apikey={VALID_KEY}")
    data = resp.get_json()
    jids = [r["jid"] for r in data["records"]]
    assert "acc111" in jids
    assert "rev222" not in jids


def test_dashboard_record_detail_404_for_unknown(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/record/nonexistent?apikey={VALID_KEY}")
    assert resp.status_code == 404


def test_dashboard_record_detail_includes_available_actions(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    output_dir = tmp_path / "output" / "det12345"
    output_dir.mkdir(parents=True)
    # REVIEW_REQUIRED → pending_review → should allow promote+discard
    server._write_verification_sidecar(
        "det12345",
        _result(jid="det12345", decision="REVIEW_REQUIRED", outcome="SKIPPED"),
        output_dir,
    )

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/record/det12345?apikey={VALID_KEY}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "promote" in data["available_actions"]
    assert "discard" in data["available_actions"]
    assert "retry_import" not in data["available_actions"]
    assert data["status_reason"] == "Review required by policy"
    assert data["sensors"] == []
    assert data["files"] == []


def test_dashboard_record_detail_includes_release_identity(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    output_dir = tmp_path / "output" / "ident12"
    output_dir.mkdir(parents=True)
    result = _result(jid="ident12", decision="BLOCK", outcome="SKIPPED")
    result.identity_decision = "WRONG_ALBUM"
    result.identity_confidence = 100.0
    result.identity_reasons = ["artist MBID mismatch"]
    result.identity_best_release_id = 30
    result.identity_current_release_id = 30
    result.sensors = [
        {
            "name": "release_identity",
            "status": "fail",
            "summary": "Observed MusicBrainz identity does not match Lidarr's target album.",
            "evidence": {
                "identity_decision": "WRONG_ALBUM",
                "identity_confidence": 100.0,
                "identity_reasons": ["artist MBID mismatch"],
                "best_release_id": 30,
                "current_release_id": 30,
                "score": 0.0,
                "track_count_delta": 0,
                "title_similarity": 1.0,
                "lidarr_rejections": ["match is not close enough: 70.1% vs 80%"],
                "file_count": 10,
                "track_titles": ["one night in paris"],
                "artist_names": ["10-cc"],
                "album_titles": ["Sheet Music"],
                "artist_mbids": ["wrong-artist"],
                "release_group_mbids": ["wrong-group"],
                "release_mbids": ["wrong-release"],
            },
        }
    ]
    server._write_verification_sidecar("ident12", result, output_dir)

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/record/ident12?apikey={VALID_KEY}")

    assert resp.status_code == 200
    identity = resp.get_json()["release_identity"]
    assert identity["decision"] == "WRONG_ALBUM"
    assert identity["confidence"] == 100.0
    assert identity["reasons"] == ["artist MBID mismatch"]
    assert identity["best_release_id"] == 30
    assert identity["current_release_id"] == 30
    assert identity["lidarr_rejections"] == ["match is not close enough: 70.1% vs 80%"]
    assert identity["observed"]["artist_names"] == ["10-cc"]
    assert identity["observed"]["release_group_mbids"] == ["wrong-group"]


def test_dashboard_record_detail_does_not_reconcile_review_required(
    monkeypatch, tmp_path
):
    _patch_paths(monkeypatch, tmp_path)
    output_dir = tmp_path / "output" / "revfast1"
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar(
        "revfast1",
        _result(jid="revfast1", decision="REVIEW_REQUIRED", outcome="PENDING"),
        output_dir,
    )

    def fail_reconcile(*args, **kwargs):
        raise AssertionError("REVIEW_REQUIRED drawer should not call Lidarr reconcile")

    monkeypatch.setattr(server, "_reconcile_pending_import", fail_reconcile)

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/record/revfast1?apikey={VALID_KEY}")

    assert resp.status_code == 200
    assert resp.get_json()["derived_status"] == "needs_review"


def test_review_required_sidecar_starts_dashboard_media_prewarm(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    output_dir = tmp_path / "output" / "warm1234"
    output_dir.mkdir(parents=True)
    (output_dir / "01.flac").write_bytes(b"fake flac")
    result = _result(jid="warm1234", decision="REVIEW_REQUIRED", outcome="PENDING")
    started = []

    class FakeThread:
        def __init__(self, *, target, args, daemon):
            started.append({"target": target, "args": args, "daemon": daemon})

        def start(self):
            pass

    with server._dashboard_media_prewarm_lock:
        server._dashboard_media_prewarm_inflight.clear()
    monkeypatch.setattr(server.threading, "Thread", FakeThread)

    server._write_verification_sidecar("warm1234", result, output_dir)

    assert started == [
        {
            "target": server._prewarm_dashboard_media_worker,
            "args": ("warm1234",),
            "daemon": True,
        }
    ]
    with server._dashboard_media_prewarm_lock:
        server._dashboard_media_prewarm_inflight.discard("warm1234")


def test_dashboard_record_detail_includes_job_timings(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    output_dir = tmp_path / "output" / "tim12345"
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar("tim12345", _result(jid="tim12345"), output_dir)
    monkeypatch.setattr(
        server,
        "_jobs",
        {
            "tim12345": {"id": "tim12345", "timings": {"flac_detective_sec": 12.345}},
        },
    )

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/record/tim12345?apikey={VALID_KEY}")
    assert resp.status_code == 200
    assert resp.get_json()["timings"]["flac_detective_sec"] == 12.345


def test_lidarr_context_requires_apikey():
    client = server.app.test_client()
    assert client.get("/dashboard/v1/lidarr-context/abc12345").status_code == 401


def test_dashboard_timings_requires_apikey():
    client = server.app.test_client()
    assert client.get("/dashboard/v1/timings").status_code == 401


def test_dashboard_timings_aggregates_jobs(monkeypatch):
    monkeypatch.setattr(server, "_verification_records", lambda: [])
    monkeypatch.setattr(
        server,
        "_jobs",
        {
            "tim1": {
                "created_at": 1779620000,
                "timings": {"flac_detective_sec": 10, "pre_import_total_sec": 100},
            },
            "tim2": {
                "created_at": 1779620010,
                "timings": {"flac_detective_sec": 20, "pre_import_total_sec": 200},
            },
            "tim3": {
                "created_at": 1779620020,
                "timings": {"flac_detective_sec": 30, "pre_import_total_sec": 300},
            },
        },
    )
    monkeypatch.setattr("dashboard.time.time", lambda: 1779620100)

    from dashboard_cache import clear

    clear()

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/timings?window=1h&apikey={VALID_KEY}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["sample_count"] == 3
    assert data["stages"]["flac_detective_sec"]["median"] == 20
    assert data["stages"]["flac_detective_sec"]["p95"] == 30
    assert data["stages"]["pre_import_total_sec"]["fastest"] == 100


def test_dashboard_timings_filters_stage(monkeypatch):
    monkeypatch.setattr(server, "_verification_records", lambda: [])
    monkeypatch.setattr(
        server,
        "_jobs",
        {
            "tim1": {
                "created_at": 1779620000,
                "timings": {"flac_detective_sec": 10, "postprocess_sec": 1},
            },
        },
    )
    monkeypatch.setattr("dashboard.time.time", lambda: 1779620100)

    from dashboard_cache import clear

    clear()

    client = server.app.test_client()
    resp = client.get(
        f"/dashboard/v1/timings?window=1h&stage=postprocess_sec&apikey={VALID_KEY}"
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert list(data["stages"].keys()) == ["postprocess_sec"]


def test_dashboard_media_requires_apikey():
    client = server.app.test_client()
    assert client.get("/dashboard/v1/audio-sample/abc12345").status_code == 401
    assert client.get("/dashboard/v1/spectrum/abc12345").status_code == 401


def test_dashboard_media_rejects_missing_audio(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/audio-sample/missing1?apikey={VALID_KEY}")

    assert resp.status_code == 404


def test_dashboard_media_artifact_generates_from_contained_audio(monkeypatch, tmp_path):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "aud12345"
    output_dir.mkdir(parents=True)
    source = output_dir / "01.flac"
    source.write_bytes(b"fake flac")
    media_dir = tmp_path / "media"

    import dashboard

    monkeypatch.setattr(dashboard, "_dashboard_media_dir", lambda: media_dir)

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(cmd[-1]).write_bytes(b"mp3")

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    monkeypatch.setattr(dashboard.subprocess, "run", fake_run)

    path, error = dashboard._media_artifact(server, "aud12345", "audio")

    assert error is None
    assert path == media_dir / "aud12345.sample.mp3"
    assert path.read_bytes() == b"mp3"


def test_record_detail_marks_media_review_available_only_for_retained_non_imported(
    monkeypatch, tmp_path
):
    output_base = _patch_paths(monkeypatch, tmp_path)

    review_dir = output_base / "rev12345"
    review_dir.mkdir(parents=True)
    (review_dir / "01.flac").write_bytes(b"fake flac")
    review = _result(jid="rev12345", decision="REVIEW_REQUIRED", outcome="PENDING")
    review.overrides = ["fake_hi_res"]
    review.verdict = "SUSPICIOUS"
    server._write_verification_sidecar("rev12345", review, review_dir)

    imported_dir = output_base / "imp12345"
    imported_dir.mkdir(parents=True)
    (imported_dir / "01.flac").write_bytes(b"fake flac")
    server._write_verification_sidecar(
        "imp12345", _result(jid="imp12345"), imported_dir
    )

    import dashboard

    assert dashboard._build_record_detail(server, "rev12345")["media"] == {
        "available": True,
        "files_present": True,
        "review_relevant": True,
        "reason": "Audio review is available for retained, non-imported files.",
    }

    imported_media = dashboard._build_record_detail(server, "imp12345")["media"]
    assert imported_media["available"] is False
    assert imported_media["files_present"] is True
    assert imported_media["review_relevant"] is False


def test_dashboard_media_endpoint_hides_imported_records(monkeypatch, tmp_path):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "impmedia"
    output_dir.mkdir(parents=True)
    (output_dir / "01.flac").write_bytes(b"fake flac")
    server._write_verification_sidecar("impmedia", _result(jid="impmedia"), output_dir)

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/audio-sample/impmedia?apikey={VALID_KEY}")

    assert resp.status_code == 404
    assert "hidden for imported records" in resp.get_json()["error"]


def test_dashboard_media_artifact_rejects_uncontained_job_path(monkeypatch, tmp_path):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_base.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "01.flac").write_bytes(b"fake flac")
    monkeypatch.setattr(
        server,
        "_jobs",
        {
            "escape1": {"id": "escape1", "output_dir": str(outside)},
        },
    )

    import dashboard

    path, error = dashboard._media_artifact(server, "escape1", "audio")

    assert path is None
    assert error == "no audio file available"
    assert output_base.exists()


def test_lidarr_context_returns_album_queue_and_history(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    output_dir = tmp_path / "output" / "ctx12345"
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar("ctx12345", _result(jid="ctx12345"), output_dir)
    monkeypatch.setattr(server, "_get_lidarr_key", lambda: "lidarr-key")
    monkeypatch.setenv("LIDARR_API_URL", "http://lidarr/api/v1")
    monkeypatch.setenv("LIDARR_WEB_URL", "http://lidarr")

    class FakeResponse:
        ok = True

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url, **kwargs):
        if url.endswith("/album/100"):
            return FakeResponse(
                {
                    "id": 100,
                    "title": "Test Album",
                    "artist": {"artistName": "Test Artist"},
                    "statistics": {"trackCount": 12, "trackFileCount": 10},
                    "monitored": True,
                    "profileId": 1,
                    "currentRelease": {"title": "Deluxe", "trackCount": 12},
                }
            )
        if url.endswith("/queue"):
            return FakeResponse(
                {
                    "records": [
                        {
                            "id": 1,
                            "albumId": 100,
                            "title": "Test Artist - Test Album",
                            "downloadId": "ctx12345",
                            "status": "completed",
                            "sizeleft": 0,
                        },
                        {"id": 2, "albumId": 999, "title": "Other"},
                    ]
                }
            )
        if url.endswith("/history"):
            return FakeResponse(
                {
                    "records": [
                        {
                            "albumId": 100,
                            "date": "2026-05-24T12:00:00Z",
                            "eventType": "downloadFolderImported",
                            "indexer": "TidalHires",
                            "downloadId": "ctx12345",
                            "sourceTitle": "Test Artist - Test Album",
                        },
                        {"albumId": 999, "eventType": "grabbed"},
                    ]
                }
            )
        raise AssertionError(url)

    import requests

    monkeypatch.setattr(requests, "get", fake_get)

    from dashboard_cache import clear

    clear()

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/lidarr-context/ctx12345?apikey={VALID_KEY}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["album"]["title"] == "Test Album"
    assert data["album"]["track_file_count"] == 10
    assert data["queue"]["in_queue"] is True
    assert len(data["queue"]["queue_entries"]) == 1
    assert data["grab_history"][0]["successful"] is True


def test_dashboard_action_rejects_unallowed(monkeypatch, tmp_path):
    """ACCEPT/MANUAL_IMPORTED record allows no actions — discard should 409."""
    _patch_paths(monkeypatch, tmp_path)
    output_dir = tmp_path / "output" / "act12345"
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar("act12345", _result(jid="act12345"), output_dir)

    client = server.app.test_client()
    resp = client.post(
        f"/dashboard/v1/action/act12345?apikey={VALID_KEY}",
        json={"action": "discard"},
    )
    assert resp.status_code == 409
    assert "allowed" in resp.get_json()


def test_dashboard_action_404_for_unknown(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    resp = client.post(
        f"/dashboard/v1/action/nonexistent?apikey={VALID_KEY}",
        json={"action": "discard"},
    )
    assert resp.status_code == 404


def test_derive_status_for_common_combinations():
    from dashboard import derive_status

    assert (
        derive_status(
            {
                "v2_verification_decision": "ACCEPT",
                "v2_import_outcome": "MANUAL_IMPORTED",
                "lifecycle": {"state": "created"},
            }
        )
        == "imported"
    )
    assert (
        derive_status(
            {
                "v2_verification_decision": "REVIEW_REQUIRED",
                "lifecycle": {"state": "pending_review"},
            }
        )
        == "needs_review"
    )
    assert (
        derive_status(
            {"v2_import_outcome": "FAILED", "lifecycle": {"state": "created"}}
        )
        == "failed"
    )
    assert derive_status({"lifecycle": {"state": "discarded"}}) == "discarded"
    assert (
        derive_status(
            {"v2_verification_decision": "BLOCK", "lifecycle": {"state": "created"}}
        )
        == "blocked"
    )
    assert (
        derive_status(
            {
                "v2_verification_decision": "BLOCK",
                "v2_import_outcome": "MANUAL_IMPORTED",
                "lifecycle": {"state": "created"},
            }
        )
        == "policy_violation"
    )


def test_status_reason_for_operator_states():
    from dashboard import status_reason

    assert (
        status_reason(
            {
                "v2_verification_decision": "REVIEW_REQUIRED",
                "v2_import_outcome": "PENDING",
                "v2_overrides": ["fake_hi_res"],
                "lifecycle": {"state": "pending_review"},
            }
        )
        == "Looks like upsampled hi-res: useful high-frequency content stops at the file's technical ceiling."
    )
    assert (
        status_reason(
            {
                "v2_verification_decision": "ACCEPT_PROVISIONAL",
                "v2_import_outcome": "FAILED",
                "reason": "manualimport and rescue failed",
                "lifecycle": {"state": "created"},
            }
        )
        == "Import failed: manualimport and rescue failed"
    )
    assert status_reason(
        {
            "v2_verification_decision": "ACCEPT_PROVISIONAL",
            "v2_import_outcome": "FAILED",
            "reason": "nothing pre-existing",
            "job_error": "manual promote import failed",
            "lifecycle": {"state": "created"},
        }
    ) == (
        "Import failed after QC passed: Lidarr ManualImport did not confirm any imported files. "
        "Open the record for Lidarr context, then retry import or discard."
    )
    assert (
        status_reason(
            {
                "v2_verification_decision": "ACCEPT_PROVISIONAL",
                "v2_import_outcome": "MANUAL_IMPORTED",
                "reason": "score=68",
                "lifecycle": {"state": "created"},
            }
        )
        == "Imported provisionally because the score was below full-accept threshold."
    )
    assert (
        status_reason(
            {
                "v2_verification_decision": "BLOCK",
                "v2_import_outcome": "SKIPPED",
                "reason": "validator unavailable",
                "lifecycle": {"state": "created"},
            }
        )
        == "Blocked by policy: validator unavailable."
    )
    assert status_reason(
        {
            "v2_verification_decision": "BLOCK",
            "v2_import_outcome": "SKIPPED",
            "reason": "codec mismatch",
            "v2_overrides": ["codec_mismatch", "no_audio_files"],
            "sensors": [
                {
                    "name": "ffprobe",
                    "evidence": {"codec_gate_skipped": 10, "flac_count": 0},
                }
            ],
            "lifecycle": {"state": "created"},
        }
    ) == (
        "Skipped before import: the release was advertised as FLAC, "
        "but the download contained 10 non-FLAC audio file(s). "
        "All were stopped by the codec gate, so no FLAC files remained for import."
    )
    assert (
        status_reason(
            {
                "v2_verification_decision": "BLOCK",
                "v2_import_outcome": "MANUAL_IMPORTED",
                "reason": "codec mismatch",
                "lifecycle": {"state": "created"},
            }
        )
        == "Policy violation: this record was imported even though V2 decided BLOCK. Keep it for audit and inspect Lidarr library/history."
    )
    assert (
        status_reason(
            {
                "v2_verification_decision": "REVIEW_REQUIRED",
                "v2_import_outcome": "PENDING",
                "lifecycle": {"state": "discarded", "actor": "user_discard"},
            }
        )
        == "Discarded by user; files were removed and the grab was blocklisted when possible."
    )


def test_record_job_timing_rounds_and_persists(monkeypatch):
    saved = []
    monkeypatch.setattr(server, "_jobs", {})
    monkeypatch.setattr(server, "_save_jobs", lambda: saved.append(True))

    server._record_job_timing("timing1", "flac_detective_sec", 1.23456)

    assert server._jobs["timing1"]["timings"]["flac_detective_sec"] == 1.235
    assert saved


def test_job_cancel_accepts_running_tidal_grab_in_f24(tmp_path):
    db_file = tmp_path / "state.db"
    state_db._initialized = False
    state_db.init(db_path=db_file)
    job_id = state_db.enqueue_job(jid="tidalrun", type="tidal_grab")
    state_db.dequeue_next_job(worker_id="worker-1")

    client = server.app.test_client()
    response = client.post(f"/dashboard/v1/jobs/{job_id}/cancel?apikey={VALID_KEY}")

    assert response.status_code == 200
    body = response.get_json()
    assert body["cancel_requested"] is True
    assert body["state"] == "cancelling"
    job = state_db.get_job(job_id)
    assert job["cancel_requested"] == 1
    assert job["state"] == "cancelling"


def test_job_cancel_409_has_operator_friendly_error(tmp_path):
    db_file = tmp_path / "state.db"
    state_db._initialized = False
    state_db.init(db_path=db_file)
    job_id = state_db.enqueue_job(jid="donecancel", type="noop")
    state_db.dequeue_next_job(worker_id="worker-1")
    state_db.mark_job_completed(job_id, result_state="ok")

    client = server.app.test_client()
    response = client.post(f"/dashboard/v1/jobs/{job_id}/cancel?apikey={VALID_KEY}")

    assert response.status_code == 409
    body = response.get_json()
    assert body["error"] == "cannot cancel terminal job"
    assert body["state"] == "completed"


def test_available_actions_per_state():
    from dashboard import available_actions

    # REVIEW_REQUIRED + pending_review → promote/discard
    assert set(
        available_actions(
            {
                "v2_verification_decision": "REVIEW_REQUIRED",
                "lifecycle": {"state": "pending_review"},
            }
        )
    ) == {"promote", "discard"}
    # FAILED + ACCEPT → retry/discard
    assert set(
        available_actions(
            {
                "v2_verification_decision": "ACCEPT",
                "v2_import_outcome": "FAILED",
                "lifecycle": {"state": "created"},
            }
        )
    ) == {"retry_import", "discard"}
    # IMPORTED → no actions
    assert (
        available_actions(
            {
                "v2_verification_decision": "ACCEPT",
                "v2_import_outcome": "MANUAL_IMPORTED",
                "lifecycle": {"state": "created"},
            }
        )
        == []
    )
    # discarded → no actions
    assert available_actions({"lifecycle": {"state": "discarded"}}) == []


def test_dashboard_cache_invalidate_on_action(monkeypatch, tmp_path):
    """POST action should invalidate summary + records cache."""
    _patch_paths(monkeypatch, tmp_path)
    output_dir = tmp_path / "output" / "inv12345"
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar(
        "inv12345",
        _result(jid="inv12345", decision="BLOCK", outcome="SKIPPED"),
        output_dir,
    )

    from dashboard_cache import clear, _cache

    clear()

    client = server.app.test_client()
    # Prime cache with summary
    client.get(f"/dashboard/v1/summary?apikey={VALID_KEY}")
    assert any(k[0] == "summary" for k in _cache)

    # Trigger action — should invalidate cache
    client.post(
        f"/dashboard/v1/action/inv12345?apikey={VALID_KEY}",
        json={"action": "discard"},
    )
    assert not any(k[0] == "summary" for k in _cache)


# ---- F1.6: DB-backed records + actions endpoint ----


def test_dashboard_records_uses_db_when_available(monkeypatch, tmp_path):
    """When DB has records, /records returns _source=db marker."""
    _patch_paths(monkeypatch, tmp_path)
    output_dir = tmp_path / "output" / "dbq12345"
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar("dbq12345", _result(jid="dbq12345"), output_dir)

    from dashboard_cache import clear

    clear()

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/records?apikey={VALID_KEY}")
    data = resp.get_json()
    rec = next(r for r in data["records"] if r["jid"] == "dbq12345")
    assert rec.get("_source") == "db"


def test_dashboard_records_db_reason_uses_sidecar_evidence(monkeypatch, tmp_path):
    """DB-backed rows should still expose non-cryptic status reasons."""
    _patch_paths(monkeypatch, tmp_path)
    output_dir = tmp_path / "output" / "dbblock1"
    output_dir.mkdir(parents=True)
    result = _result(jid="dbblock1", decision="BLOCK", outcome="SKIPPED")
    result.score = 0
    result.verdict = "UNKNOWN"
    result.overrides = ["codec_mismatch", "no_audio_files"]
    result.sensors = [
        {
            "name": "ffprobe",
            "evidence": {"codec_gate_skipped": 10, "flac_count": 0},
        }
    ]
    server._write_verification_sidecar("dbblock1", result, output_dir)

    from dashboard_cache import clear

    clear()

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/records?apikey={VALID_KEY}")
    data = resp.get_json()
    rec = next(r for r in data["records"] if r["jid"] == "dbblock1")
    assert rec.get("_source") == "db"
    assert rec["overrides"] == ["codec_mismatch", "no_audio_files"]
    assert rec["status_reason"] == (
        "Skipped before import: the release was advertised as FLAC, "
        "but the download contained 10 non-FLAC audio file(s). "
        "All were stopped by the codec gate, so no FLAC files remained for import."
    )


def test_dashboard_actions_endpoint_empty_when_no_actions(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/actions?apikey={VALID_KEY}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["actions"] == []
    assert data["returned"] == 0


def test_dashboard_actions_per_jid(monkeypatch, tmp_path):
    """Action POST should be logged + readable via /actions/<jid>."""
    _patch_paths(monkeypatch, tmp_path)
    output_dir = tmp_path / "output" / "logj1234"
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar(
        "logj1234",
        _result(jid="logj1234", decision="BLOCK", outcome="SKIPPED"),
        output_dir,
    )

    client = server.app.test_client()
    # Discard a BLOCK record (allowed action)
    client.post(
        f"/dashboard/v1/action/logj1234?apikey={VALID_KEY}",
        json={"action": "discard"},
    )

    # Verify action logged
    resp = client.get(f"/dashboard/v1/actions/logj1234?apikey={VALID_KEY}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["actions"]) == 1
    assert data["actions"][0]["action"] == "discard"
    assert data["actions"][0]["jid"] == "logj1234"


def test_dashboard_actions_requires_apikey(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    assert client.get("/dashboard/v1/actions").status_code == 401
    assert client.get("/dashboard/v1/actions/abc123").status_code == 401
