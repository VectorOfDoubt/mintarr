"""Regression tests for Mintarr public env-var aliases during cutover."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


APP_DIR = Path("/app") if Path("/app/server.py").exists() else Path(__file__).resolve().parents[1] / "app"


def _run_probe(code: str, env_updates: dict[str, str | None]) -> str:
    env = os.environ.copy()
    for key in (
        "MINTARR_API_KEY",
        "TIDALHIRES_API_KEY",
        "MINTARR_STATE_DB",
        "TIDALHIRES_STATE_DB",
        "MINTARR_RESCUE_RESCAN_ENABLED",
        "TIDALHIRES_RESCUE_RESCAN_ENABLED",
        "MINTARR_DISABLE_WORKER",
        "TIDALHIRES_DISABLE_WORKER",
    ):
        env.pop(key, None)
    env.update(
        {
            "PYTHONPATH": str(APP_DIR),
            "DOWNLOAD_BASE": "/tmp/mintarr-alias-test/downloads",
            "OUTPUT_BASE": "/tmp/mintarr-alias-test/output",
            "TIDAL_DL_NG_CONFIG": "/tmp/mintarr-alias-test/tidal-config",
        }
    )
    for key, value in env_updates.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value

    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_mintarr_api_key_alias_takes_precedence():
    out = _run_probe(
        "import server; print(server.API_KEY)",
        {
            "MINTARR_API_KEY": "mintarr-public-api-key",
            "TIDALHIRES_API_KEY": "tidalhires-legacy-api-key",
            "MINTARR_DISABLE_WORKER": "1",
        },
    )
    assert out == "mintarr-public-api-key"


def test_legacy_tidalhires_api_key_still_works():
    out = _run_probe(
        "import server; print(server.API_KEY)",
        {
            "TIDALHIRES_API_KEY": "tidalhires-legacy-api-key",
            "MINTARR_DISABLE_WORKER": "1",
        },
    )
    assert out == "tidalhires-legacy-api-key"


def test_mintarr_state_db_alias_takes_precedence():
    out = _run_probe(
        "import state_db; print(state_db._DEFAULT_DB_PATH)",
        {
            "MINTARR_STATE_DB": "/tmp/mintarr-state.db",
            "TIDALHIRES_STATE_DB": "/tmp/tidalhires-state.db",
        },
    )
    assert out == "/tmp/mintarr-state.db"


def test_legacy_tidalhires_state_db_still_works():
    out = _run_probe(
        "import state_db; print(state_db._DEFAULT_DB_PATH)",
        {"TIDALHIRES_STATE_DB": "/tmp/tidalhires-state.db"},
    )
    assert out == "/tmp/tidalhires-state.db"


def test_mintarr_rescue_rescan_alias_takes_precedence():
    out = _run_probe(
        "import server; print(server._rescue_rescan_enabled())",
        {
            "MINTARR_API_KEY": "mintarr-public-api-key",
            "MINTARR_RESCUE_RESCAN_ENABLED": "false",
            "TIDALHIRES_RESCUE_RESCAN_ENABLED": "true",
            "MINTARR_DISABLE_WORKER": "1",
        },
    )
    assert out == "False"


def test_mintarr_disable_worker_alias_prevents_worker_start():
    out = _run_probe(
        "import server, worker; print(worker.is_worker_alive())",
        {
            "MINTARR_API_KEY": "mintarr-public-api-key",
            "MINTARR_DISABLE_WORKER": "1",
        },
    )
    assert out == "False"
