#!/usr/bin/env python3
"""Validate deployed v2 sidecars for CUTOVER_MANIFEST.md §4.

This is intentionally a small hand-coded schema check, not a jsonschema-based
validator. It verifies the fields that `SIDECAR_FORMAT_v2.md` promises external
readers can rely on.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REQUIRED_TOP_LEVEL = {
    "jid": str,
    "title": str,
    "ts": (int, float),
    "ts_iso": str,
    "verdict": str,
    "v2_verification_decision": str,
    "v2_import_outcome": (str, type(None)),
    "v2_score": int,
    "v2_components": dict,
    "v2_overrides": list,
    "reason": str,
    "sensors": list,
    "files": list,
    "lifecycle": dict,
}
VALID_SENSOR_STATUS = {"pass", "fail", "warn", "error", "skipped"}
VALID_SENSOR_SEVERITY = {"none", "info", "warning", "error", "blocker"}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _type_name(expected: object) -> str:
    if isinstance(expected, tuple):
        return " | ".join(t.__name__ for t in expected)
    return expected.__name__  # type: ignore[union-attr]


def validate(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        data = _load(path)
    except Exception as exc:
        return [f"{path}: invalid JSON: {exc}"]

    for field, expected in REQUIRED_TOP_LEVEL.items():
        if field not in data:
            errors.append(f"{path}: missing {field}")
        elif not isinstance(data[field], expected):
            errors.append(f"{path}: {field} must be {_type_name(expected)}")

    for index, sensor in enumerate(data.get("sensors") or []):
        prefix = f"{path}: sensors[{index}]"
        if not isinstance(sensor, dict):
            errors.append(f"{prefix} must be object")
            continue
        for field in ("name", "class", "status", "severity", "confidence", "duration_ms", "evidence"):
            if field not in sensor:
                errors.append(f"{prefix} missing {field}")
        if sensor.get("status") not in VALID_SENSOR_STATUS:
            errors.append(f"{prefix}.status invalid: {sensor.get('status')!r}")
        if sensor.get("severity") not in VALID_SENSOR_SEVERITY:
            errors.append(f"{prefix}.severity invalid: {sensor.get('severity')!r}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="sidecar files or directories")
    args = parser.parse_args()

    files: list[Path] = []
    for path in args.paths:
        if path.is_dir():
            files.extend(sorted(path.rglob("verification.json")))
            files.extend(sorted(p for p in path.rglob("*.json") if p.name != "verification.json"))
        else:
            files.append(path)

    errors: list[str] = []
    for path in files:
        errors.extend(validate(path))

    for error in errors:
        print(error, file=sys.stderr)
    print(f"checked {len(files)} sidecar file(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
