"""F1.5: backfill SQLite state index from existing sidecars + decisions.jsonl.

One-time (or repeatable) script to populate state_db from on-disk state.
Sidecars remain source-of-truth; this only mirrors current state into the
queryable index.

Sources scanned (in order; later overrides earlier per jid):
1. /output/<jid>/verification.json — live sidecars (most current state)
2. /config/blocked_decisions/<jid>.json — archived BLOCK-with-deleted-output
3. /config/discarded/<jid>.json — user-discarded
4. /config/expired_review/<jid>.json — auto-expired REVIEW_REQUIRED

Run via:
    docker exec tidalhires python /app/backfill_state.py
    docker exec tidalhires python /app/backfill_state.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import state_db


log = logging.getLogger("tidalhires.backfill")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _scan_dir_for_sidecars(directory: Path) -> list[Path]:
    """Find all *.json files in a directory tree that look like verification sidecars."""
    if not directory.exists():
        return []
    if directory.is_file():
        return [directory] if directory.suffix == ".json" else []
    # Live sidecars: /output/<jid>/verification.json
    candidates = list(directory.glob("*/verification.json"))
    # Archived sidecars: /config/.../<jid>.json (flat dir)
    candidates.extend(directory.glob("*.json"))
    return candidates


def _read_sidecar(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict) and data.get("jid"):
            return data
    except Exception:
        log.warning("failed to read %s", path, exc_info=True)
    return None


def _derive_status_for_backfill(sidecar: dict) -> str:
    """Local copy of dashboard.derive_status to avoid Flask import dependency
    when running script standalone via docker exec."""
    lifecycle = sidecar.get("lifecycle") or {}
    state = lifecycle.get("state", "")
    decision = sidecar.get("v2_verification_decision", "")
    outcome = sidecar.get("v2_import_outcome", "")
    # Policy violation MUST be checked before outcome-based ones because BLOCK+imported is an audit alarm
    if decision == "BLOCK" and outcome in ("MANUAL_IMPORTED", "RESCUED"):
        return "policy_violation"
    if state == "discarded":
        return "discarded"
    if state == "expired":
        return "expired"
    if state == "promoted":
        return "promoted"
    if decision == "REVIEW_REQUIRED" and state == "pending_review":
        return "needs_review"
    if outcome == "FAILED":
        return "failed"
    if outcome == "PENDING":
        return "pending"
    if outcome in ("MANUAL_IMPORTED", "RESCUED"):
        return "imported"
    if decision == "BLOCK":
        return "blocked"
    return "unknown"


def backfill(
    *,
    output_base: Path,
    blocked_dir: Path,
    discarded_dir: Path,
    expired_dir: Path,
    dry_run: bool = False,
) -> dict:
    """Scan all sidecar locations and upsert into state_db.

    Returns counts dict. Sources are scanned in priority order so live sidecars
    overwrite older archived versions for the same jid.
    """
    state_db.init()

    sources = [
        ("output", output_base),
        ("blocked", blocked_dir),
        ("discarded", discarded_dir),
        ("expired", expired_dir),
    ]

    # Collect per-jid: later source wins (live sidecar > archived)
    # But we want the OPPOSITE: live sidecar overrides archived, so we
    # scan in priority order and the first wins.
    seen: set[str] = set()
    counts = {
        "output": 0,
        "blocked": 0,
        "discarded": 0,
        "expired": 0,
        "skipped": 0,
        "errors": 0,
    }

    for source_name, directory in sources:
        sidecars = _scan_dir_for_sidecars(directory)
        log.info(
            "scanning %s (%s): %d candidate files",
            source_name,
            directory,
            len(sidecars),
        )
        for path in sidecars:
            sidecar = _read_sidecar(path)
            if sidecar is None:
                counts["errors"] += 1
                continue
            jid = sidecar.get("jid")
            if jid in seen:
                counts["skipped"] += 1
                continue
            seen.add(jid)

            derived_status = _derive_status_for_backfill(sidecar)
            if dry_run:
                log.info(
                    "DRY-RUN would upsert %s (status=%s) from %s",
                    jid,
                    derived_status,
                    source_name,
                )
            else:
                state_db.upsert_from_sidecar(sidecar, derived_status=derived_status)
            counts[source_name] += 1

    total = sum(counts[k] for k in ("output", "blocked", "discarded", "expired"))
    counts["total"] = total
    log.info("backfill complete: %s", counts)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Scan + report, do not write"
    )
    parser.add_argument(
        "--output-base", default="/output", help="Live sidecar root (default: /output)"
    )
    parser.add_argument("--blocked-dir", default="/config/blocked_decisions")
    parser.add_argument("--discarded-dir", default="/config/discarded")
    parser.add_argument("--expired-dir", default="/config/expired_review")
    args = parser.parse_args()

    counts = backfill(
        output_base=Path(args.output_base),
        blocked_dir=Path(args.blocked_dir),
        discarded_dir=Path(args.discarded_dir),
        expired_dir=Path(args.expired_dir),
        dry_run=args.dry_run,
    )

    print(f"Backfill {'(dry-run) ' if args.dry_run else ''}complete:")
    for k in (
        "output",
        "blocked",
        "discarded",
        "expired",
        "skipped",
        "errors",
        "total",
    ):
        print(f"  {k}: {counts.get(k, 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
