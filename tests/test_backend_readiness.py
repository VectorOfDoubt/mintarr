"""Readiness checks for the ADR-0014 SAB backend lane."""

from __future__ import annotations

from pathlib import Path

from backend_readiness import sab_backend_readiness


class _Response:
    def __init__(self, payload, ok=True):
        self._payload = payload
        self.ok = ok

    def json(self):
        return self._payload


def _env(root: Path, **overrides):
    data = {
        "MINTARR_SAB_BACKEND_ENABLED": "true",
        "MINTARR_SAB_BACKEND_URL": "http://sab:8090/sabnzbd",
        "MINTARR_SAB_BACKEND_API_KEY": "secret",
        "MINTARR_SAB_BACKEND_CATEGORY": "mintarr-music",
        "MINTARR_SAB_BACKEND_DOWNLOAD_ROOT": str(root),
        "MINTARR_SAB_BACKEND_PATH_MAP": "H:\\Downloads\\complete=>/sab-backend",
    }
    data.update(overrides)
    return data


def _lidarr_client(name, *, host, port, category, enabled=True, client_id=1):
    return {
        "id": client_id,
        "name": name,
        "implementation": "Sabnzbd",
        "enable": enabled,
        "fields": [
            {"name": "host", "value": host},
            {"name": "port", "value": port},
            {"name": "musicCategory", "value": category},
        ],
    }


def _indexer(name, *, download_client_id, active=True, base_url="http://prowlarr/14"):
    return {
        "name": name,
        "implementation": "Newznab",
        "protocol": "usenet",
        "enableRss": False,
        "enableAutomaticSearch": active,
        "enableInteractiveSearch": active,
        "downloadClientId": download_client_id,
        "fields": [{"name": "baseUrl", "value": base_url}],
    }


def test_disabled_lane_reports_disabled(tmp_path):
    result = sab_backend_readiness(
        env=_env(tmp_path, MINTARR_SAB_BACKEND_ENABLED="false")
    )

    assert result["status"] == "disabled"
    assert result["checks"][0]["status"] == "disabled"


def test_ready_when_backend_and_lidarr_route_are_exclusive(tmp_path):
    root = tmp_path / "complete"
    root.mkdir()

    def fake_get(url, **kwargs):
        if url.endswith("/api"):
            return _Response({"categories": ["*", "music", "mintarr-music"]})
        if url.endswith("/downloadclient"):
            return _Response(
                [
                    _lidarr_client(
                        "Mintarr backend",
                        host="host.docker.internal",
                        port=5025,
                        category="mintarr-music",
                    ),
                    _lidarr_client(
                        "Direct SAB",
                        host="sab",
                        port=8090,
                        category="movies",
                        client_id=2,
                    ),
                ]
            )
        if url.endswith("/indexer"):
            return _Response([_indexer("NZBgeek", download_client_id=1)])
        raise AssertionError(url)

    result = sab_backend_readiness(
        env=_env(root),
        lidarr_api_url="http://lidarr/api/v1",
        lidarr_api_key="lidarr-key",
        get=fake_get,
    )

    assert result["status"] == "ok"
    statuses = {c["key"]: c["status"] for c in result["checks"]}
    assert statuses["sab_categories"] == "ok"
    assert statuses["lidarr_route"] == "ok"
    assert statuses["indexer_route"] == "ok"
    assert statuses["direct_backend_conflict"] == "ok"


def test_blocks_when_lidarr_has_no_mintarr_backend_client(tmp_path):
    root = tmp_path / "complete"
    root.mkdir()

    def fake_get(url, **kwargs):
        if url.endswith("/api"):
            return _Response({"categories": ["mintarr-music"]})
        if url.endswith("/downloadclient"):
            return _Response(
                [
                    _lidarr_client(
                        "TidalHires",
                        host="host.docker.internal",
                        port=5025,
                        category="music",
                    )
                ]
            )
        if url.endswith("/indexer"):
            return _Response([_indexer("NZBgeek", download_client_id=0)])
        raise AssertionError(url)

    result = sab_backend_readiness(
        env=_env(root),
        lidarr_api_url="http://lidarr/api/v1",
        lidarr_api_key="lidarr-key",
        get=fake_get,
    )

    assert result["status"] == "blocked"
    route = next(c for c in result["checks"] if c["key"] == "lidarr_route")
    assert route["status"] == "fail"
    assert "mintarr-music" in route["detail"]


def test_blocks_direct_lidarr_to_backend_conflict(tmp_path):
    root = tmp_path / "complete"
    root.mkdir()

    def fake_get(url, **kwargs):
        if url.endswith("/api"):
            return _Response({"categories": ["mintarr-music"]})
        if url.endswith("/downloadclient"):
            return _Response(
                [
                    _lidarr_client(
                        "Mintarr backend",
                        host="host.docker.internal",
                        port=5025,
                        category="mintarr-music",
                    ),
                    _lidarr_client(
                        "Direct SAB",
                        host="sab",
                        port=8090,
                        category="mintarr-music",
                    ),
                ]
            )
        if url.endswith("/indexer"):
            return _Response([_indexer("NZBgeek", download_client_id=1)])
        raise AssertionError(url)

    result = sab_backend_readiness(
        env=_env(root),
        lidarr_api_url="http://lidarr/api/v1",
        lidarr_api_key="lidarr-key",
        get=fake_get,
    )

    assert result["status"] == "blocked"
    conflict = next(
        c for c in result["checks"] if c["key"] == "direct_backend_conflict"
    )
    assert conflict["status"] == "fail"
    assert "Direct SAB" in conflict["detail"]


def test_blocks_external_usenet_indexer_not_pinned_to_backend_client(tmp_path):
    root = tmp_path / "complete"
    root.mkdir()

    def fake_get(url, **kwargs):
        if url.endswith("/api"):
            return _Response({"categories": ["mintarr-music"]})
        if url.endswith("/downloadclient"):
            return _Response(
                [
                    _lidarr_client(
                        "Mintarr backend",
                        host="host.docker.internal",
                        port=5025,
                        category="mintarr-music",
                        client_id=77,
                    )
                ]
            )
        if url.endswith("/indexer"):
            return _Response([_indexer("NZBgeek", download_client_id=0)])
        raise AssertionError(url)

    result = sab_backend_readiness(
        env=_env(root),
        lidarr_api_url="http://lidarr/api/v1",
        lidarr_api_key="lidarr-key",
        get=fake_get,
    )

    assert result["status"] == "blocked"
    route = next(c for c in result["checks"] if c["key"] == "indexer_route")
    assert route["status"] == "fail"
    assert "NZBgeek" in route["detail"]


def test_warns_when_no_external_usenet_indexer_is_active(tmp_path):
    root = tmp_path / "complete"
    root.mkdir()

    def fake_get(url, **kwargs):
        if url.endswith("/api"):
            return _Response({"categories": ["mintarr-music"]})
        if url.endswith("/downloadclient"):
            return _Response(
                [
                    _lidarr_client(
                        "Mintarr backend",
                        host="host.docker.internal",
                        port=5025,
                        category="mintarr-music",
                        client_id=77,
                    )
                ]
            )
        if url.endswith("/indexer"):
            return _Response(
                [
                    _indexer(
                        "TidalHires",
                        download_client_id=4,
                        base_url="http://host.docker.internal:5025",
                    ),
                    _indexer("NZBgeek", download_client_id=0, active=False),
                ]
            )
        raise AssertionError(url)

    result = sab_backend_readiness(
        env=_env(root),
        lidarr_api_url="http://lidarr/api/v1",
        lidarr_api_key="lidarr-key",
        get=fake_get,
    )

    assert result["status"] == "warning"
    route = next(c for c in result["checks"] if c["key"] == "indexer_route")
    assert route["status"] == "warn"


def test_missing_backend_category_blocks(tmp_path):
    root = tmp_path / "complete"
    root.mkdir()

    def fake_get(url, **kwargs):
        if url.endswith("/api"):
            return _Response({"categories": ["music"]})
        if url.endswith("/downloadclient"):
            return _Response([])
        if url.endswith("/indexer"):
            return _Response([])
        raise AssertionError(url)

    result = sab_backend_readiness(
        env=_env(root),
        lidarr_api_url="http://lidarr/api/v1",
        lidarr_api_key="lidarr-key",
        get=fake_get,
    )

    assert result["status"] == "blocked"
    sab = next(c for c in result["checks"] if c["key"] == "sab_categories")
    assert sab["status"] == "fail"


def test_sab_exception_detail_redacts_apikey(tmp_path):
    root = tmp_path / "complete"
    root.mkdir()

    def fake_get(url, **kwargs):
        if url.endswith("/api"):
            raise RuntimeError(
                "HTTPConnectionPool(host='sab', url='/api?mode=get_cats&apikey=secret&output=json')"
            )
        raise AssertionError(url)

    result = sab_backend_readiness(
        env=_env(root),
        lidarr_api_url="",
        lidarr_api_key="",
        get=fake_get,
    )

    sab = next(c for c in result["checks"] if c["key"] == "sab_categories")
    assert "secret" not in sab["detail"]
    assert "apikey=<redacted>" in sab["detail"]


def test_lidarr_exception_detail_redacts_embedded_secret(tmp_path):
    root = tmp_path / "complete"
    root.mkdir()

    def fake_get(url, **kwargs):
        if url.endswith("/api"):
            return _Response({"categories": ["mintarr-music"]})
        if url.endswith("/downloadclient"):
            raise RuntimeError(
                "failed GET http://lidarr/api/v1/downloadclient?apikey=lidarr-secret"
            )
        raise AssertionError(url)

    result = sab_backend_readiness(
        env=_env(root),
        lidarr_api_url="http://lidarr/api/v1",
        lidarr_api_key="lidarr-key",
        get=fake_get,
    )

    route = next(c for c in result["checks"] if c["key"] == "lidarr_clients")
    assert "lidarr-secret" not in route["detail"]
    assert "apikey=<redacted>" in route["detail"]
