"""Read-only readiness checks for the ADR-0014 SAB backend lane."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import requests

from download_backend import is_generic_category, redact


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _check(key: str, label: str, status: str, detail: str = "") -> dict:
    return {"key": key, "label": label, "status": status, "detail": detail}


def _safe_error_detail(exc: Exception) -> str:
    return redact(str(exc))[:160]


def _sab_api_base(url: str) -> str:
    return url.rstrip("/") + "/api"


def _host_port(url: str) -> tuple[str, int | None]:
    parsed = urlparse(url if "://" in url else f"http://{url}")
    return (parsed.hostname or "", parsed.port)


def _download_client_field(client: dict, name: str):
    for field in client.get("fields") or []:
        if isinstance(field, dict) and field.get("name") == name:
            return field.get("value")
    return None


def _client_category(client: dict) -> str:
    return str(
        _download_client_field(client, "musicCategory")
        or _download_client_field(client, "category")
        or ""
    ).strip()


def _client_points_to_mintarr(client: dict) -> bool:
    host = str(_download_client_field(client, "host") or "").strip().lower()
    try:
        port = int(_download_client_field(client, "port") or 0)
    except (TypeError, ValueError):
        port = 0
    return port == 5025 and host in {"host.docker.internal", "127.0.0.1", "localhost"}


def _client_points_to_backend(client: dict, backend_url: str) -> bool:
    backend_host, backend_port = _host_port(backend_url)
    host = str(_download_client_field(client, "host") or "").strip().lower()
    try:
        port = int(_download_client_field(client, "port") or 0)
    except (TypeError, ValueError):
        port = 0
    return bool(backend_host) and host == backend_host.lower() and port == backend_port


def _sab_categories(url: str, api_key: str, *, get=requests.get) -> list[str] | None:
    response = get(
        _sab_api_base(url),
        params={"mode": "get_cats", "apikey": api_key, "output": "json"},
        timeout=5,
    )
    if not response.ok:
        return None
    payload = response.json()
    categories = payload.get("categories")
    if isinstance(categories, list):
        return [str(c) for c in categories]
    return None


def _lidarr_download_clients(
    api_url: str, api_key: str, *, get=requests.get
) -> list[dict] | None:
    response = get(
        api_url.rstrip("/") + "/downloadclient",
        headers={"X-Api-Key": api_key},
        timeout=5,
    )
    if not response.ok:
        return None
    payload = response.json()
    return payload if isinstance(payload, list) else None


def _lidarr_indexers(
    api_url: str, api_key: str, *, get=requests.get
) -> list[dict] | None:
    response = get(
        api_url.rstrip("/") + "/indexer",
        headers={"X-Api-Key": api_key},
        timeout=5,
    )
    if not response.ok:
        return None
    payload = response.json()
    return payload if isinstance(payload, list) else None


def _indexer_field(indexer: dict, name: str):
    for field in indexer.get("fields") or []:
        if isinstance(field, dict) and field.get("name") == name:
            return field.get("value")
    return None


def _indexer_points_to_mintarr(indexer: dict) -> bool:
    base_url = str(_indexer_field(indexer, "baseUrl") or "")
    host, port = _host_port(base_url)
    return port == 5025 and host.lower() in {
        "host.docker.internal",
        "127.0.0.1",
        "localhost",
    }


def _indexer_active(indexer: dict) -> bool:
    return bool(
        indexer.get("enableRss")
        or indexer.get("enableAutomaticSearch")
        or indexer.get("enableInteractiveSearch")
    )


def sab_backend_readiness(
    *,
    env: dict[str, str] | None = None,
    lidarr_api_url: str = "",
    lidarr_api_key: str = "",
    get=requests.get,
) -> dict:
    """Return read-only readiness for the managed SAB backend lane.

    The result is intentionally operator-facing and secret-free. It never edits
    SAB/Lidarr configuration; it only reports whether the lane is usable and
    exclusive enough to route real grabs through Mintarr.
    """

    env_map = env if env is not None else os.environ
    enabled = _truthy(env_map.get("MINTARR_SAB_BACKEND_ENABLED"))
    url = (env_map.get("MINTARR_SAB_BACKEND_URL") or "").strip()
    api_key = (env_map.get("MINTARR_SAB_BACKEND_API_KEY") or "").strip()
    category = (env_map.get("MINTARR_SAB_BACKEND_CATEGORY") or "").strip()
    root = (env_map.get("MINTARR_SAB_BACKEND_DOWNLOAD_ROOT") or "").strip()
    path_map = (env_map.get("MINTARR_SAB_BACKEND_PATH_MAP") or "").strip()
    checks: list[dict] = []

    checks.append(
        _check(
            "enabled",
            "Backend lane enabled",
            "ok" if enabled else "disabled",
            "MINTARR_SAB_BACKEND_ENABLED=true" if enabled else "lane is disabled",
        )
    )
    if not enabled:
        return {
            "status": "disabled",
            "category": category,
            "checks": checks,
            "summary": "SAB backend lane is disabled.",
        }

    category_status = (
        "fail" if not category else "warn" if is_generic_category(category) else "ok"
    )
    checks.append(
        _check(
            "category",
            "Dedicated category",
            category_status,
            category or "missing",
        )
    )
    checks.append(
        _check(
            "backend_url", "SAB backend URL", "ok" if url else "fail", url or "missing"
        )
    )
    checks.append(
        _check(
            "backend_api_key",
            "SAB backend API key",
            "ok" if api_key else "fail",
            "configured" if api_key else "missing",
        )
    )
    root_status = "ok" if root and Path(root).is_dir() else "fail"
    checks.append(
        _check(
            "download_root", "Mounted completed root", root_status, root or "missing"
        )
    )
    checks.append(
        _check(
            "path_map",
            "Remote path map",
            "ok" if path_map else "warn",
            "configured" if path_map else "not configured",
        )
    )

    if url and api_key:
        try:
            categories = _sab_categories(url, api_key, get=get)
            if categories is None:
                checks.append(_check("sab_categories", "SAB category visible", "fail"))
            elif category in categories:
                checks.append(
                    _check("sab_categories", "SAB category visible", "ok", category)
                )
            else:
                checks.append(
                    _check(
                        "sab_categories",
                        "SAB category visible",
                        "fail",
                        f"{category or 'missing'} not advertised by backend",
                    )
                )
        except Exception as exc:
            checks.append(
                _check(
                    "sab_categories",
                    "SAB category visible",
                    "fail",
                    _safe_error_detail(exc),
                )
            )

    if lidarr_api_url and lidarr_api_key and category:
        try:
            clients = _lidarr_download_clients(lidarr_api_url, lidarr_api_key, get=get)
            if clients is None:
                checks.append(_check("lidarr_clients", "Lidarr client route", "fail"))
            else:
                enabled_sab = [
                    c
                    for c in clients
                    if c.get("enable") and c.get("implementation") == "Sabnzbd"
                ]
                mintarr_matches = [
                    c
                    for c in enabled_sab
                    if _client_category(c) == category and _client_points_to_mintarr(c)
                ]
                direct_conflicts = [
                    c
                    for c in enabled_sab
                    if _client_category(c) == category
                    and _client_points_to_backend(c, url)
                ]
                if mintarr_matches:
                    mintarr_client_ids = {
                        int(c.get("id") or 0) for c in mintarr_matches
                    }
                    checks.append(
                        _check(
                            "lidarr_route",
                            "Lidarr routes category to Mintarr",
                            "ok",
                            ", ".join(
                                str(c.get("name") or c.get("id"))
                                for c in mintarr_matches
                            ),
                        )
                    )
                else:
                    mintarr_client_ids = set()
                    checks.append(
                        _check(
                            "lidarr_route",
                            "Lidarr routes category to Mintarr",
                            "fail",
                            f"no enabled SAB client for category {category}",
                        )
                    )
                indexers = _lidarr_indexers(lidarr_api_url, lidarr_api_key, get=get)
                if indexers is None:
                    checks.append(
                        _check(
                            "indexer_route",
                            "External usenet indexers pinned",
                            "fail",
                            "could not read Lidarr indexers",
                        )
                    )
                else:
                    external_usenet = [
                        i
                        for i in indexers
                        if i.get("implementation") == "Newznab"
                        and i.get("protocol") == "usenet"
                        and _indexer_active(i)
                        and not _indexer_points_to_mintarr(i)
                    ]
                    unpinned = [
                        i
                        for i in external_usenet
                        if int(i.get("downloadClientId") or 0) not in mintarr_client_ids
                    ]
                    if unpinned:
                        checks.append(
                            _check(
                                "indexer_route",
                                "External usenet indexers pinned",
                                "fail",
                                ", ".join(
                                    str(i.get("name") or i.get("id")) for i in unpinned
                                ),
                            )
                        )
                    elif external_usenet:
                        checks.append(
                            _check(
                                "indexer_route",
                                "External usenet indexers pinned",
                                "ok",
                                ", ".join(
                                    str(i.get("name") or i.get("id"))
                                    for i in external_usenet
                                ),
                            )
                        )
                    else:
                        checks.append(
                            _check(
                                "indexer_route",
                                "External usenet indexers pinned",
                                "warn",
                                "no active external usenet indexer is routed to this lane",
                            )
                        )
                if direct_conflicts:
                    checks.append(
                        _check(
                            "direct_backend_conflict",
                            "No direct Lidarr→SAB bypass",
                            "fail",
                            ", ".join(
                                str(c.get("name") or c.get("id"))
                                for c in direct_conflicts
                            ),
                        )
                    )
                else:
                    checks.append(
                        _check(
                            "direct_backend_conflict",
                            "No direct Lidarr→SAB bypass",
                            "ok",
                            "no enabled direct client for this category",
                        )
                    )
        except Exception as exc:
            checks.append(
                _check(
                    "lidarr_clients",
                    "Lidarr client route",
                    "fail",
                    _safe_error_detail(exc),
                )
            )
    else:
        checks.append(
            _check(
                "lidarr_route",
                "Lidarr routes category to Mintarr",
                "warn",
                "Lidarr API unavailable for machine verification",
            )
        )

    if any(c["status"] == "fail" for c in checks):
        status = "blocked"
    elif any(c["status"] == "warn" for c in checks):
        status = "warning"
    else:
        status = "ok"
    return {
        "status": status,
        "category": category,
        "checks": checks,
        "summary": {
            "ok": "SAB backend lane is ready.",
            "warning": "SAB backend lane is usable with warnings.",
            "blocked": "SAB backend lane is not ready for live grabs.",
        }[status],
    }
