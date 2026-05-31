#!/usr/bin/env python3
"""Validate relative Markdown links for CUTOVER_MANIFEST.md §5.

Usage:
    python scripts/check_markdown_links.py docs README.md CONTRIBUTING.md

External URLs, mailto links and in-page anchors are ignored. Relative file
links must resolve from the Markdown file that contains them.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

INLINE_LINK_RE = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")
REF_LINK_RE = re.compile(r"^\s*\[[^\]]+]:\s+(\S+)", re.MULTILINE)
SKIP_PREFIXES = (
    "http://",
    "https://",
    "mailto:",
    "tel:",
    "#",
    "app://",
)


def _iter_markdown_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.rglob("*.md")))
        elif path.suffix.lower() == ".md":
            files.append(path)
    return files


def _normalise_target(raw: str) -> str:
    target = raw.strip().split()[0].strip("<>")
    return target.split("#", 1)[0]


def _link_targets(text: str) -> list[str]:
    targets = [_normalise_target(m.group(1)) for m in INLINE_LINK_RE.finditer(text)]
    targets.extend(_normalise_target(m.group(1)) for m in REF_LINK_RE.finditer(text))
    return [t for t in targets if t and not t.startswith(SKIP_PREFIXES)]


def check_file(path: Path) -> list[str]:
    errors: list[str] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for target in _link_targets(text):
        if "://" in target:
            continue
        resolved = (path.parent / target).resolve()
        if not resolved.exists():
            errors.append(f"{path}: broken link -> {target}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()

    errors: list[str] = []
    for path in _iter_markdown_files(args.paths):
        errors.extend(check_file(path))

    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
