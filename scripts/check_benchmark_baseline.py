"""Compare a pytest-benchmark JSON result against a stored baseline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _find_benchmark(result: dict[str, Any], name: str) -> dict[str, Any]:
    benchmarks = result.get("benchmarks")
    if not isinstance(benchmarks, list):
        raise ValueError("benchmark result is missing a benchmarks list")
    for item in benchmarks:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    available = sorted(
        str(item.get("name")) for item in benchmarks if isinstance(item, dict)
    )
    raise ValueError(f"benchmark {name!r} not found; available: {available}")


def compare_baseline(baseline_path: Path, result_path: Path) -> int:
    baseline = _load_json(baseline_path)
    result = _load_json(result_path)

    name = str(baseline["benchmark"])
    metric = str(baseline.get("metric", "mean"))
    baseline_seconds = float(baseline["baseline_seconds"])
    max_regression_ratio = float(baseline["max_regression_ratio"])
    if baseline_seconds <= 0:
        raise ValueError("baseline_seconds must be positive")
    if max_regression_ratio < 0:
        raise ValueError("max_regression_ratio must be non-negative")

    benchmark = _find_benchmark(result, name)
    stats = benchmark.get("stats")
    if not isinstance(stats, dict) or metric not in stats:
        raise ValueError(f"benchmark {name!r} is missing stats.{metric}")

    current_seconds = float(stats[metric])
    max_allowed = baseline_seconds * (1.0 + max_regression_ratio)
    delta_ratio = (current_seconds - baseline_seconds) / baseline_seconds

    print(
        f"{name}: {metric}={current_seconds:.6f}s, "
        f"baseline={baseline_seconds:.6f}s, "
        f"max_allowed={max_allowed:.6f}s, "
        f"delta={delta_ratio:+.1%}"
    )
    if current_seconds > max_allowed:
        print(
            "Benchmark regression exceeds configured threshold",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("result", type=Path)
    args = parser.parse_args()
    try:
        return compare_baseline(args.baseline, args.result)
    except Exception as exc:
        print(f"benchmark baseline check failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
