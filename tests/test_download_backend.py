"""ADR-0014 slice 2 — download-backend abstraction + SAB client tests.

Focus per the design: category containment, path containment, and secret
redaction, plus submit/status/cancel against a mocked SAB.
"""

from __future__ import annotations

import logging

import pytest

from download_backend import (
    BackendState,
    DownloadBackend,
    SabBackendClient,
    contained_path,
    ensure_category,
    is_generic_category,
    redact,
    redact_params,
)


class _Resp:
    def __init__(self, payload, *, fail=False):
        self._payload = payload
        self._fail = fail

    def raise_for_status(self):
        if self._fail:
            raise RuntimeError("http error")

    def json(self):
        return self._payload


def _recorder(payload_for):
    """Build a fake `requests.get` that records calls and returns canned JSON.

    ``payload_for`` maps the SAB ``mode`` to a payload (or callable).
    """
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append({"url": url, "params": params, "timeout": timeout})
        mode = (params or {}).get("mode")
        payload = payload_for.get(mode, {})
        return _Resp(payload(params) if callable(payload) else payload)

    fake_get.calls = calls
    return fake_get


# ---- redaction -----------------------------------------------------------


def test_redact_masks_apikey_and_password():
    assert redact("http://h/api?mode=queue&apikey=SECRET123&x=1") == (
        "http://h/api?mode=queue&apikey=<redacted>&x=1"
    )
    assert "PW" not in redact("login?username=u&password=PW")


def test_redact_params_masks_secret_keys():
    out = redact_params({"mode": "addurl", "apikey": "SECRET", "cat": "mintarr-music"})
    assert out == {"mode": "addurl", "apikey": "<redacted>", "cat": "mintarr-music"}


# ---- category containment ------------------------------------------------


def test_ensure_category_fails_closed_on_empty():
    with pytest.raises(ValueError):
        ensure_category("")
    with pytest.raises(ValueError):
        ensure_category("   ")
    with pytest.raises(ValueError):
        ensure_category(None)


def test_ensure_category_strips():
    assert ensure_category("  mintarr-music ") == "mintarr-music"


def test_is_generic_category():
    assert is_generic_category("music")
    assert is_generic_category("Downloads")
    assert not is_generic_category("mintarr-music")


# ---- path containment ----------------------------------------------------


def test_contained_path_accepts_inside_root(tmp_path):
    inside = tmp_path / "mintarr-music" / "Album"
    inside.mkdir(parents=True)
    assert contained_path(tmp_path, inside) == inside.resolve()


def test_contained_path_rejects_outside_and_traversal(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    assert contained_path(root, outside) is None
    assert contained_path(root, root / ".." / "other") is None
    assert contained_path(root, root) is None  # the root itself is not a job dir


# ---- SAB client: config / enablement -------------------------------------


def _client(payload_for=None, **kw):
    return SabBackendClient(
        url="http://sab",
        api_key="KEY",
        category="mintarr-music",
        download_root=kw.pop("download_root", "/backend/complete"),
        request=_recorder(payload_for or {}),
        **kw,
    )


def test_is_enabled_requires_url_key_category():
    assert _client().is_enabled()
    assert not SabBackendClient(url="", api_key="K", category="c").is_enabled()
    assert not SabBackendClient(url="u", api_key="", category="c").is_enabled()
    assert not SabBackendClient(url="u", api_key="K", category="").is_enabled()


def test_sab_client_satisfies_backend_protocol():
    assert isinstance(_client(), DownloadBackend)


# ---- submit --------------------------------------------------------------


def test_submit_adds_url_into_category():
    client = _client({"addurl": {"status": True, "nzo_ids": ["NZO-1"]}})
    job = client.submit(url="http://nzb/x")
    assert job.backend_job_id == "NZO-1"
    assert job.category == "mintarr-music"
    call = client._request.calls[0]["params"]
    assert call["mode"] == "addurl"
    assert call["cat"] == "mintarr-music"
    assert call["name"] == "http://nzb/x"


def test_submit_raises_without_nzo_id():
    client = _client({"addurl": {"status": True, "nzo_ids": []}})
    with pytest.raises(RuntimeError):
        client.submit(url="http://nzb/x")


def test_submit_fails_closed_without_category():
    client = SabBackendClient(
        url="http://sab", api_key="KEY", category="", request=_recorder({})
    )
    with pytest.raises(ValueError):
        client.submit(url="http://nzb/x")


def test_submit_url_secret_redacted_in_logs(caplog):
    # The release/download URL passed as name= can itself embed an indexer
    # apikey; it must not leak into the debug log.
    client = _client({"addurl": {"status": True, "nzo_ids": ["NZO-1"]}})
    leaky = "http://indexer/api?t=get&id=1&apikey=INDEXER_SECRET"
    with caplog.at_level(logging.DEBUG, logger="tidalhires.download_backend"):
        client.submit(url=leaky)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "INDEXER_SECRET" not in text
    assert "<redacted>" in text


# ---- status --------------------------------------------------------------


def test_status_downloading_from_queue():
    client = _client(
        {
            "queue": {
                "queue": {
                    "slots": [
                        {"nzo_id": "NZO-1", "status": "Downloading", "percentage": "42"}
                    ]
                }
            }
        }
    )
    st = client.status("NZO-1")
    assert st.state is BackendState.DOWNLOADING
    assert st.progress == 42.0
    assert st.completed_path is None


def test_status_failed_from_queue():
    client = _client(
        {"queue": {"queue": {"slots": [{"nzo_id": "NZO-1", "status": "Failed"}]}}}
    )
    assert client.status("NZO-1").state is BackendState.FAILED


def test_status_completed_only_when_path_contained(tmp_path):
    done = tmp_path / "Album"
    done.mkdir()
    client = _client(
        {
            "queue": {"queue": {"slots": []}},
            "history": {
                "history": {
                    "slots": [
                        {"nzo_id": "NZO-1", "status": "Completed", "storage": str(done)}
                    ]
                }
            },
        },
        download_root=str(tmp_path),
    )
    st = client.status("NZO-1")
    assert st.state is BackendState.COMPLETED
    assert st.completed_path == str(done.resolve())


def test_status_completed_drops_uncontained_path(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    client = _client(
        {
            "queue": {"queue": {"slots": []}},
            "history": {
                "history": {
                    "slots": [
                        {
                            "nzo_id": "NZO-1",
                            "status": "Completed",
                            "storage": str(outside),
                        }
                    ]
                }
            },
        },
        download_root=str(root),
    )
    st = client.status("NZO-1")
    assert st.state is BackendState.COMPLETED
    assert st.completed_path is None  # outside root → never import from it


def test_status_unknown_when_absent():
    client = _client(
        {"queue": {"queue": {"slots": []}}, "history": {"history": {"slots": []}}}
    )
    assert client.status("NZO-9").state is BackendState.UNKNOWN


# ---- cancel --------------------------------------------------------------


def test_cancel_deletes_from_queue_and_history():
    client = _client({"queue": {"status": True}, "history": {"status": True}})
    assert client.cancel("NZO-1", delete_files=True) is True
    modes = [c["params"]["mode"] for c in client._request.calls]
    assert modes == ["queue", "history"]
    assert client._request.calls[0]["params"]["name"] == "delete"
    assert client._request.calls[0]["params"]["del_files"] == 1


# ---- secret never logged -------------------------------------------------


def test_apikey_never_appears_in_logs(caplog):
    client = _client({"queue": {"queue": {"slots": []}}, "history": {"history": {}}})
    with caplog.at_level(logging.DEBUG, logger="tidalhires.download_backend"):
        client.status("NZO-1")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "KEY" not in text
    assert "<redacted>" in text
