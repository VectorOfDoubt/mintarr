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
    QbitBackendClient,
    SabBackendClient,
    apply_path_map,
    contained_path,
    ensure_category,
    is_generic_category,
    magnet_btih,
    map_qbit_state,
    parse_path_map,
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


# ---- remote path mapping -------------------------------------------------


def test_parse_path_map():
    pairs = parse_path_map(r"H:\Nedlasting\sabnzbd\complete=>/sab-backend-complete")
    assert pairs == [("H:/Nedlasting/sabnzbd/complete", "/sab-backend-complete")]
    # multiple, trailing slashes stripped, blanks/malformed skipped
    assert parse_path_map("a=>/x/ ; bad ; ;c\\d=>/y") == [
        ("a", "/x"),
        ("c/d", "/y"),
    ]
    assert parse_path_map("") == []
    assert parse_path_map(None) == []


def test_apply_path_map_translates_windows_to_container():
    pairs = parse_path_map(r"H:\Nedlasting\sabnzbd\complete=>/sab-backend-complete")
    assert (
        apply_path_map(r"H:\Nedlasting\sabnzbd\complete\mintarr-music\Album", pairs)
        == "/sab-backend-complete/mintarr-music/Album"
    )
    # case-insensitive (Windows paths are)
    assert (
        apply_path_map(r"h:\nedlasting\sabnzbd\complete\X", pairs)
        == "/sab-backend-complete/X"
    )


def test_apply_path_map_passthrough_without_match_or_pairs():
    pairs = parse_path_map("H:/a=>/b")
    assert apply_path_map("/already/container/path", pairs) == "/already/container/path"
    assert apply_path_map("/x", []) == "/x"  # no pairs → unchanged (containerized)


def test_sab_status_completed_uses_path_map(tmp_path):
    # SAB (Windows) reports a backslash path; the container sees it under root.
    album = tmp_path / "mintarr-music" / "Album"
    album.mkdir(parents=True)
    win = r"H:\Nedlasting\sabnzbd\complete\mintarr-music\Album"
    client = SabBackendClient(
        url="http://sab",
        api_key="KEY",
        category="mintarr-music",
        download_root=str(tmp_path),
        path_map=f"H:\\Nedlasting\\sabnzbd\\complete=>{tmp_path}",
        request=_recorder(
            {
                "queue": {"queue": {"slots": []}},
                "history": {
                    "history": {
                        "slots": [
                            {"nzo_id": "NZO-1", "status": "Completed", "storage": win}
                        ]
                    }
                },
            }
        ),
    )
    st = client.status("NZO-1")
    assert st.state is BackendState.COMPLETED
    assert st.completed_path == str(album.resolve())


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


# ============================================================================
# qBittorrent backend client
# ============================================================================


class _QbitResp:
    def __init__(self, payload=None, *, headers=None, text="Ok.", fail=False):
        self._payload = payload
        self.headers = headers or {}
        self.text = text
        self._fail = fail

    def raise_for_status(self):
        if self._fail:
            raise RuntimeError("http error")

    def json(self):
        return self._payload


_QBIT_BASE = "http://qbit"


def _qbit_recorder(routes):
    """Fake qBit request: routes maps 'METHOD /path' -> _QbitResp or fn(calls)."""
    calls = []

    def fake(method, url, headers=None, timeout=None, params=None, data=None):
        path = url.replace(_QBIT_BASE, "", 1)
        calls.append(
            {
                "method": method,
                "path": path,
                "params": params,
                "data": data,
                "headers": headers,
            }
        )
        route = routes.get(f"{method} {path}", _QbitResp("Ok."))
        return route(calls) if callable(route) else route

    fake.calls = calls
    return fake


def _qbit(routes=None, **kw):
    return QbitBackendClient(
        url=_QBIT_BASE,
        category=kw.pop("category", "mintarr-music"),
        download_root=kw.pop("download_root", "/backend/complete"),
        request=_qbit_recorder(routes or {}),
        **kw,
    )


# ---- pure helpers --------------------------------------------------------


def test_magnet_btih():
    assert magnet_btih("magnet:?xt=urn:btih:ABCDEF0123&dn=x") == "abcdef0123"
    assert magnet_btih("http://nzb/x") is None
    assert magnet_btih("magnet:?dn=no-hash") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("downloading", BackendState.DOWNLOADING),
        ("stalledDL", BackendState.DOWNLOADING),
        ("uploading", BackendState.COMPLETED),
        ("pausedUP", BackendState.COMPLETED),
        ("error", BackendState.FAILED),
        ("missingFiles", BackendState.FAILED),
        ("weird", BackendState.UNKNOWN),
    ],
)
def test_map_qbit_state(raw, expected):
    assert map_qbit_state(raw) is expected


# ---- config / protocol ---------------------------------------------------


def test_qbit_is_enabled_requires_url_and_category():
    assert _qbit().is_enabled()
    assert not QbitBackendClient(url="", category="c").is_enabled()
    assert not QbitBackendClient(url="u", category="").is_enabled()


def test_qbit_satisfies_backend_protocol():
    assert isinstance(_qbit(), DownloadBackend)


# ---- auth ----------------------------------------------------------------


def test_qbit_logs_in_and_sends_sid_cookie():
    routes = {
        "POST /api/v2/auth/login": _QbitResp(
            "Ok.", headers={"Set-Cookie": "SID=abc123; HttpOnly; path=/"}
        ),
        "GET /api/v2/torrents/info": _QbitResp([]),
    }
    client = _qbit(routes, username="u", password="p")
    client.status("HASH")
    calls = client._request.calls
    assert calls[0]["path"] == "/api/v2/auth/login"
    # the subsequent info call carries the SID cookie
    assert calls[1]["headers"]["Cookie"] == "SID=abc123"


def test_qbit_login_fails_closed_without_sid():
    # Credentials supplied but qBit returns no Set-Cookie/SID → reject, do not
    # proceed unauthenticated.
    routes = {"POST /api/v2/auth/login": _QbitResp("Fails.", headers={})}
    client = _qbit(routes, username="u", password="wrong")
    with pytest.raises(RuntimeError):
        client.status("h1")


# ---- submit --------------------------------------------------------------


def test_qbit_submit_magnet_resolves_btih_without_diff():
    client = _qbit()
    job = client.submit(url="magnet:?xt=urn:btih:DEADBEEF01&dn=x")
    assert job.backend_job_id == "deadbeef01"
    assert job.category == "mintarr-music"
    # magnet path needs no info listing — only the add call
    paths = [c["path"] for c in client._request.calls]
    assert paths == ["/api/v2/torrents/add"]
    add = client._request.calls[0]["data"]
    assert add["category"] == "mintarr-music"
    assert add["urls"] == "magnet:?xt=urn:btih:DEADBEEF01&dn=x"


def test_qbit_submit_url_resolves_hash_by_category_diff():
    listings = [[], [{"hash": "NEWHASH"}]]
    routes = {
        "GET /api/v2/torrents/info": lambda calls: _QbitResp(listings.pop(0)),
        "POST /api/v2/torrents/add": _QbitResp("Ok."),
    }
    client = _qbit(routes)
    job = client.submit(url="http://tracker/file.torrent")
    assert job.backend_job_id == "newhash"


def test_qbit_submit_ambiguous_diff_raises():
    listings = [[], [{"hash": "A"}, {"hash": "B"}]]
    routes = {
        "GET /api/v2/torrents/info": lambda calls: _QbitResp(listings.pop(0)),
    }
    client = _qbit(routes)
    with pytest.raises(RuntimeError):
        client.submit(url="http://tracker/file.torrent")


def test_qbit_submit_fails_closed_without_category():
    client = QbitBackendClient(url=_QBIT_BASE, category="", request=_qbit_recorder({}))
    with pytest.raises(ValueError):
        client.submit(url="magnet:?xt=urn:btih:AB")


def test_qbit_submit_raises_when_add_rejected():
    # qBit answers /add with HTTP 200 + "Fails." — must not mint a job from the
    # magnet btih when the torrent was rejected.
    routes = {"POST /api/v2/torrents/add": _QbitResp("Fails.", text="Fails.")}
    client = _qbit(routes)
    with pytest.raises(RuntimeError):
        client.submit(url="magnet:?xt=urn:btih:DEADBEEF01")


# ---- status --------------------------------------------------------------


def test_qbit_status_downloading_scales_progress():
    routes = {
        "GET /api/v2/torrents/info": _QbitResp(
            [{"hash": "h1", "state": "downloading", "progress": 0.5}]
        )
    }
    st = _qbit(routes).status("H1")
    assert st.state is BackendState.DOWNLOADING
    assert st.progress == 50.0


def test_qbit_status_completed_only_when_contained(tmp_path):
    done = tmp_path / "Album"
    done.mkdir()
    routes = {
        "GET /api/v2/torrents/info": _QbitResp(
            [
                {
                    "hash": "h1",
                    "state": "uploading",
                    "progress": 1.0,
                    "content_path": str(done),
                }
            ]
        )
    }
    st = _qbit(routes, download_root=str(tmp_path)).status("h1")
    assert st.state is BackendState.COMPLETED
    assert st.completed_path == str(done.resolve())


def test_qbit_status_completed_drops_uncontained_path(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    routes = {
        "GET /api/v2/torrents/info": _QbitResp(
            [
                {
                    "hash": "h1",
                    "state": "stalledUP",
                    "progress": 1.0,
                    "content_path": str(outside),
                }
            ]
        )
    }
    st = _qbit(routes, download_root=str(root)).status("h1")
    assert st.state is BackendState.COMPLETED
    assert st.completed_path is None


def test_qbit_status_not_found_is_unknown():
    routes = {"GET /api/v2/torrents/info": _QbitResp([])}
    assert _qbit(routes).status("h9").state is BackendState.UNKNOWN


# ---- cancel (seeding-safe) -----------------------------------------------


def test_qbit_cancel_default_pauses_not_no_op():
    routes = {"POST /api/v2/torrents/pause": _QbitResp("Ok.")}
    client = _qbit(routes)
    assert client.cancel("h1") is True
    # default cancel must PROPAGATE to the backend (pause), never a silent no-op,
    # and must not delete the torrent or its data.
    call = client._request.calls[0]
    assert call["path"] == "/api/v2/torrents/pause"
    assert call["data"]["hashes"] == "h1"
    assert all(c["path"] != "/api/v2/torrents/delete" for c in client._request.calls)


def test_qbit_cancel_delete_files_opt_in():
    routes = {"POST /api/v2/torrents/delete": _QbitResp("Ok.")}
    client = _qbit(routes)
    assert client.cancel("h1", delete_files=True) is True
    call = client._request.calls[0]
    assert call["path"] == "/api/v2/torrents/delete"
    assert call["data"]["deleteFiles"] == "true"
    assert call["data"]["hashes"] == "h1"


def test_qbit_cancel_remove_policy_keeps_files():
    routes = {"POST /api/v2/torrents/delete": _QbitResp("Ok.")}
    client = _qbit(routes, cleanup_policy="remove")
    assert client.cancel("h1") is True
    assert client._request.calls[0]["data"]["deleteFiles"] == "false"


# ---- secret never logged -------------------------------------------------


def test_qbit_password_never_logged(caplog):
    routes = {
        "POST /api/v2/auth/login": _QbitResp("Ok.", headers={"Set-Cookie": "SID=z"}),
        "GET /api/v2/torrents/info": _QbitResp([]),
    }
    client = _qbit(routes, username="u", password="SUPERSECRET")
    with caplog.at_level(logging.DEBUG, logger="tidalhires.download_backend"):
        client.status("h1")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "SUPERSECRET" not in text
