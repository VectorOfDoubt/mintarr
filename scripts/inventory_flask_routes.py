#!/usr/bin/env python3
"""Emit Flask route inventory JSON for CUTOVER_MANIFEST.md §4.

The script supports both the pre-cutover private layout (`tidalhires/app`) and
the target public layout (`app`). It sets safe test defaults before importing
the server so route inventory does not start the worker or require live keys.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _add_app_path() -> None:
    root = Path.cwd()
    candidates = [root, root / "app", root / "tidalhires" / "app"]
    for candidate in candidates:
        if (candidate / "server.py").exists():
            sys.path.insert(0, str(candidate))
            return
    raise SystemExit("could not find app/server.py or tidalhires/app/server.py")


def main() -> int:
    os.environ.setdefault("MINTARR_API_KEY", "mintarr-route-inventory-key")
    os.environ.setdefault("MINTARR_DISABLE_WORKER", "1")
    os.environ.setdefault("DOWNLOAD_BASE", "/tmp/mintarr-routes/downloads")
    os.environ.setdefault("OUTPUT_BASE", "/tmp/mintarr-routes/output")
    os.environ.setdefault("TIDAL_DL_NG_CONFIG", "/tmp/mintarr-routes/tidal-config")
    os.environ.setdefault("MINTARR_STATE_DB", "/tmp/mintarr-routes/state.db")

    _add_app_path()
    import server  # type: ignore

    routes = []
    for rule in sorted(
        server.app.url_map.iter_rules(), key=lambda r: (str(r.rule), r.endpoint)
    ):
        methods = sorted(m for m in rule.methods if m not in {"HEAD", "OPTIONS"})
        routes.append(
            {"rule": str(rule.rule), "endpoint": rule.endpoint, "methods": methods}
        )
    print(json.dumps(routes, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
