"""F4.1 static connector registry tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import server

VALID_KEY = "tidalhires-test-api-key"


def _manifest(
    connector_id: str,
    *,
    kind=None,
    required: bool = False,
    default_enabled: bool = True,
    install_profile: str | None = None,
    docker_service: str | None = None,
    required_env: tuple[str, ...] = (),
    optional_env: tuple[str, ...] = (),
):
    from connectors import ConnectorKind, ConnectorManifest, MANIFEST_API_VERSION

    return ConnectorManifest(
        id=connector_id,
        display_name=connector_id.replace("_", " ").title(),
        kind=kind or ConnectorKind.VERIFIER,
        api_version=MANIFEST_API_VERSION,
        adapter_class=None,
        default_enabled=default_enabled,
        required=required,
        install_profile=install_profile,
        docker_service=docker_service,
        required_env=required_env,
        optional_env=optional_env,
        capabilities=(),
        docs_url=f"connectors/{connector_id}/",
        min_supported_version=None,
    )


class _DummyConnector:
    def __init__(
        self,
        connector_id: str,
        *,
        kind=None,
        required: bool = False,
        installed: bool = True,
        enabled: bool = True,
    ) -> None:
        from connectors import ConnectorKind

        self.manifest = _manifest(
            connector_id,
            kind=kind or ConnectorKind.VERIFIER,
            required=required,
            default_enabled=enabled,
        )
        self._installed = installed
        self._enabled = enabled

    def is_installed(self) -> bool:
        return self._installed

    def is_enabled(self) -> bool:
        return self._enabled

    def health(self):
        from connectors import health_checked_now

        if not self._enabled:
            return health_checked_now("disabled")
        if not self._installed:
            return health_checked_now("missing", "not installed")
        return health_checked_now("ok")

    def detected_version(self) -> str | None:
        return "1.2.3"


def test_manifest_serialisation_round_trip():
    from connectors import ConnectorKind, manifest_from_dict, manifest_to_dict

    manifest = _manifest("round_trip", kind=ConnectorKind.OUTPUT)
    data = manifest_to_dict(manifest)
    assert manifest_to_dict(manifest_from_dict(data)) == data


def test_manifest_validation_rejects_invalid_required_default():
    from connectors import ConnectorKind, ConnectorManifest, MANIFEST_API_VERSION

    with pytest.raises(ValueError, match="required=True implies"):
        ConnectorManifest(
            id="bad",
            display_name="Bad",
            kind=ConnectorKind.VERIFIER,
            api_version=MANIFEST_API_VERSION,
            adapter_class=None,
            default_enabled=False,
            required=True,
            install_profile=None,
            docker_service=None,
            required_env=(),
            optional_env=(),
            capabilities=(),
            docs_url="connectors/bad/",
            min_supported_version=None,
        )


def test_manifest_validation_rejects_duplicate_ids():
    import connectors

    connectors.reset_registry()
    connectors.register(_DummyConnector("dup"))
    with pytest.raises(ValueError, match="already registered"):
        connectors.register(_DummyConnector("dup"))


def test_required_connectors_returns_subset():
    import connectors

    connectors.reset_registry()
    connectors.register(_DummyConnector("required", required=True))
    connectors.register(_DummyConnector("optional", required=False))
    assert [
        connector.manifest.id for connector in connectors.required_connectors()
    ] == ["required"]


def test_get_connectors_endpoint_requires_api_key():
    client = server.app.test_client()
    assert client.get("/dashboard/v1/connectors").status_code == 401


def test_get_connectors_endpoint_returns_all(monkeypatch):
    import connectors

    connectors.reset_registry()
    connectors.register(_DummyConnector("alpha"))
    connectors.register(_DummyConnector("beta"))

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/connectors?apikey={VALID_KEY}")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["api_version"] == "1.0.0"
    assert [item["id"] for item in data["connectors"]] == ["alpha", "beta"]
    assert data["connectors"][0]["manifest"]["id"] == "alpha"
    assert data["connectors"][0]["runtime"]["health"] == "ok"


def test_connector_payload_overlays_persisted_config():
    import connectors
    import state_db

    connectors.reset_registry()
    connectors.register(_DummyConnector("alpha", enabled=True))
    state_db.set_connector_config("alpha", enabled=True, mode="dry_run", actor="test")

    payload = connectors.registry_payload()
    runtime = payload["connectors"][0]["runtime"]
    assert runtime["enabled"] is True
    assert runtime["mode"] == "dry_run"
    assert runtime["config"]["actor"] == "test"


def test_connector_payload_includes_secret_safe_install_guidance():
    import connectors
    from connectors import ConnectorKind

    connectors.reset_registry()

    class _MissingSoulseek(_DummyConnector):
        def __init__(self):
            self.manifest = _manifest(
                "soulseek",
                kind=ConnectorKind.SOURCE,
                default_enabled=False,
                install_profile="soulseek",
                docker_service="slskd",
                required_env=("SOULSEEK_ENABLED", "SOULSEEK_DOWNLOAD_ROOT"),
                optional_env=("SLSKD_API_KEY",),
            )
            self._installed = False
            self._enabled = False

    connectors.register(_MissingSoulseek())

    item = connectors.registry_payload()["connectors"][0]
    guidance = item["install_guidance"]

    assert guidance["show"] is True
    assert guidance["reason"] == "Dependency is missing or not reachable."
    assert guidance["install_profile"] == "soulseek"
    assert guidance["docker_service"] == "slskd"
    assert guidance["required_env"] == [
        "SOULSEEK_ENABLED",
        "SOULSEEK_DOWNLOAD_ROOT",
    ]
    assert guidance["optional_env"] == ["SLSKD_API_KEY"]
    assert any("compose profile" in action for action in guidance["actions"])
    assert any("service is running" in action for action in guidance["actions"])
    assert "secret" not in str(guidance).lower()


def test_connector_config_endpoint_requires_api_key():
    client = server.app.test_client()
    resp = client.post(
        "/dashboard/v1/connectors/tidal/config", json={"mode": "dry_run"}
    )
    assert resp.status_code == 401


def test_connector_config_dry_run_does_not_persist():
    import connectors
    import state_db

    connectors.reset_registry()
    connectors.register(_DummyConnector("alpha", enabled=True))

    client = server.app.test_client()
    resp = client.post(
        f"/dashboard/v1/connectors/alpha/config?apikey={VALID_KEY}",
        json={"mode": "dry_run", "dry_run": True},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["valid"] is True
    assert data["dry_run"] is True
    assert data["config"]["mode"] == "dry_run"
    assert state_db.get_connector_config("alpha") is None


def test_connector_config_persists_mode_change():
    import connectors
    import state_db

    connectors.reset_registry()
    connectors.register(_DummyConnector("alpha", enabled=True))

    client = server.app.test_client()
    resp = client.post(
        f"/dashboard/v1/connectors/alpha/config?apikey={VALID_KEY}",
        json={"mode": "dry_run"},
    )

    assert resp.status_code == 200
    assert resp.get_json()["config"]["mode"] == "dry_run"
    assert state_db.get_connector_config("alpha")["mode"] == "dry_run"


def test_connector_config_rejects_required_disable():
    import connectors

    connectors.reset_registry()
    connectors.register(_DummyConnector("ffprobe", required=True, enabled=True))

    client = server.app.test_client()
    resp = client.post(
        f"/dashboard/v1/connectors/ffprobe/config?apikey={VALID_KEY}",
        json={"mode": "disabled"},
    )

    assert resp.status_code == 409
    assert "required connectors must stay in import mode" in resp.get_json()["errors"]


def test_connector_config_rejects_disabling_only_output_for_import_source():
    import connectors
    from connectors import ConnectorKind

    connectors.reset_registry()
    connectors.register(
        _DummyConnector(
            "source", kind=ConnectorKind.SOURCE, installed=True, enabled=True
        )
    )
    connectors.register(
        _DummyConnector(
            "ffprobe", kind=ConnectorKind.VERIFIER, required=True, installed=True
        )
    )
    connectors.register(
        _DummyConnector(
            "output", kind=ConnectorKind.OUTPUT, installed=True, enabled=True
        )
    )

    client = server.app.test_client()
    resp = client.post(
        f"/dashboard/v1/connectors/output/config?apikey={VALID_KEY}",
        json={"mode": "disabled"},
    )

    assert resp.status_code == 409
    assert resp.get_json()["errors"] == [
        "source connectors in import mode require at least one installed output connector"
    ]


def test_tidal_connector_installed_iff_token_present(tmp_path):
    from adapters.tidal import TidalAdapter
    from connectors import (
        AdapterBackedConnector,
        ConnectorKind,
        ConnectorManifest,
        MANIFEST_API_VERSION,
    )

    config_dir = tmp_path / "tidal"
    config_dir.mkdir()
    adapter = TidalAdapter(config_dir=str(config_dir))

    import adapters

    adapters.reset_registry()
    adapters.register(adapter)

    connector = AdapterBackedConnector(
        manifest=ConnectorManifest(
            id="tidal",
            display_name="TIDAL",
            kind=ConnectorKind.SOURCE,
            api_version=MANIFEST_API_VERSION,
            adapter_class="adapters.tidal:TidalAdapter",
            default_enabled=True,
            required=False,
            install_profile=None,
            docker_service=None,
            required_env=("TIDAL_DL_NG_CONFIG",),
            optional_env=(),
            capabilities=("hires_audio",),
            docs_url="connectors/tidal/",
            min_supported_version=None,
        ),
        adapter_name="tidal",
    )

    assert connector.is_installed() is False
    (config_dir / "token.json").write_text('{"access_token": "x"}')
    assert connector.is_installed() is True


def test_ffprobe_connector_detects_installed_via_path_lookup(monkeypatch):
    from connectors import BinaryConnector

    connector = BinaryConnector(
        manifest=_manifest("ffprobe"),
        binary="ffprobe",
        version_args=("-version",),
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/ffprobe" if name == "ffprobe" else None
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: SimpleNamespace(
            stdout="ffprobe version 6.1 Copyright", stderr="", returncode=0
        ),
    )

    assert connector.is_installed() is True
    assert connector.health().status == "ok"
    assert connector.detected_version() == "ffprobe version 6.1 Copyright"


def test_flac_detective_connector_health_via_http_probe(monkeypatch):
    from connectors import FlacDetectiveConnector

    monkeypatch.setenv("FLAC_API_URL", "http://detective:8889/analyze")

    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(ok=True, status_code=200)

    monkeypatch.setattr("requests.get", fake_get)

    connector = FlacDetectiveConnector()
    assert connector.health().status == "ok"
    assert calls == ["http://detective:8889/health"]


def test_lidarr_manual_import_connector_health_via_api_probe(monkeypatch):
    from connectors import LidarrConnector

    monkeypatch.setenv("LIDARR_API_URL", "http://lidarr/api/v1")
    monkeypatch.setenv("LIDARR_API_KEY", "lidarr-key")

    def fake_get(url, **kwargs):
        assert kwargs["headers"]["X-Api-Key"] == "lidarr-key"
        if url == "http://lidarr/api/v1/queue":
            return SimpleNamespace(ok=True, status_code=200)
        if url == "http://lidarr/api/v1/system/status":
            return SimpleNamespace(
                ok=True, status_code=200, json=lambda: {"version": "2.14.1"}
            )
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("requests.get", fake_get)

    connector = LidarrConnector(_manifest("lidarr_manual_import"))
    assert connector.is_installed() is True
    assert connector.health().status == "ok"
    assert connector.detected_version() == "2.14.1"


def test_required_connector_missing_logs_warning_but_does_not_fail_boot(caplog):
    import connectors

    connectors.reset_registry()
    connectors.register(
        _DummyConnector("required_missing", required=True, installed=False)
    )

    missing = connectors.check_required_connectors_installed()

    assert missing == ["required_missing"]
    assert "required connector missing: required_missing" in caplog.text


def test_no_source_in_import_mode_without_required_verifiers():
    import connectors
    from connectors import ConnectorKind

    connectors.reset_registry()
    connectors.register(
        _DummyConnector(
            "source", kind=ConnectorKind.SOURCE, installed=True, enabled=True
        )
    )
    connectors.register(
        _DummyConnector(
            "ffprobe", kind=ConnectorKind.VERIFIER, required=True, installed=False
        )
    )
    connectors.register(
        _DummyConnector(
            "output", kind=ConnectorKind.OUTPUT, installed=True, enabled=True
        )
    )

    violations = connectors.import_mode_invariant_violations()

    assert violations == [
        "source connectors in import mode require installed verifier: ffprobe"
    ]


def test_at_least_one_output_connector_must_be_installed():
    import connectors
    from connectors import ConnectorKind

    connectors.reset_registry()
    connectors.register(
        _DummyConnector(
            "source", kind=ConnectorKind.SOURCE, installed=True, enabled=True
        )
    )
    connectors.register(
        _DummyConnector(
            "ffprobe", kind=ConnectorKind.VERIFIER, required=True, installed=True
        )
    )
    connectors.register(
        _DummyConnector(
            "output", kind=ConnectorKind.OUTPUT, installed=False, enabled=True
        )
    )

    violations = connectors.import_mode_invariant_violations()

    assert violations == [
        "source connectors in import mode require at least one installed output connector"
    ]
