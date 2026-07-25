#!/usr/bin/env python3
"""Fail when mutable CI actions or container references re-enter the repository."""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
USES = re.compile(r"\buses:\s*([^\s#]+)")
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
IMAGE_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}(?:\s|$)")
DEPLOYMENT_IMAGE = re.compile(r"ghcr\.io/eivindsjursen-lab/mintarr([^\s]*)")
DEPLOYMENT_DOCS = (
    "README.md",
    "docker-compose.example.yml",
    "docs/operations/INSTALL.md",
)


def violations(root: pathlib.Path = ROOT) -> list[str]:
    errors: list[str] = []

    workflows = root / ".github" / "workflows"
    for path in sorted((*workflows.glob("*.yml"), *workflows.glob("*.yaml"))):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = USES.search(line)
            if not match:
                continue
            action = match.group(1)
            if action.startswith("./"):
                continue
            if action.startswith("docker://"):
                if not re.search(r"@sha256:[0-9a-f]{64}$", action):
                    errors.append(
                        f"{path.relative_to(root)}:{number}: Docker action is not pinned "
                        f"to a sha256 digest: {action}"
                    )
                continue
            name, separator, ref = action.rpartition("@")
            if not separator or not name or not COMMIT_SHA.fullmatch(ref):
                errors.append(
                    f"{path.relative_to(root)}:{number}: action is not pinned to a "
                    f"full commit SHA: {action}"
                )

    dockerfile = root / "Dockerfile"
    for number, line in enumerate(
        dockerfile.read_text(encoding="utf-8").splitlines(), 1
    ):
        if line.startswith("FROM ") and not IMAGE_DIGEST.search(line):
            errors.append(
                f"Dockerfile:{number}: external base image is not pinned to sha256 digest"
            )

    for rel in DEPLOYMENT_DOCS:
        text = (root / rel).read_text(encoding="utf-8")
        for match in DEPLOYMENT_IMAGE.finditer(text):
            suffix = match.group(1)
            if not (
                re.fullmatch(r"@sha256:[0-9a-f]{64}", suffix)
                or suffix == "@sha256:<release-manifest-digest>"
            ):
                errors.append(f"{rel}: Mintarr deployment image is not digest-pinned")

    return errors


def main() -> int:
    errors = violations()
    if errors:
        print("\n".join(f"ERROR: {error}" for error in errors))
        return 1
    print("supply-chain references are immutably pinned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
