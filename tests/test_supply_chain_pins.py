from __future__ import annotations

import importlib.util
import os
import pathlib

ROOT = pathlib.Path(
    os.environ.get("MINTARR_REPO_ROOT", pathlib.Path(__file__).resolve().parent.parent)
)
SPEC = importlib.util.spec_from_file_location(
    "check_supply_chain_pins", ROOT / "scripts" / "check_supply_chain_pins.py"
)
assert SPEC and SPEC.loader
pins = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pins)


def _fixture(
    tmp_path: pathlib.Path, *, action_ref: str, base: str, deploy: str
) -> pathlib.Path:
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(f"steps:\n  - uses: actions/checkout@{action_ref}\n")
    (tmp_path / "Dockerfile").write_text(f"FROM {base}\n")
    for rel in pins.DEPLOYMENT_DOCS:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(deploy)
    return tmp_path


def test_repository_supply_chain_references_are_pinned():
    assert pins.violations(ROOT) == []


def test_mutable_references_are_rejected(tmp_path):
    root = _fixture(
        tmp_path,
        action_ref="v4",
        base="python:3.12-slim",
        deploy="image: ghcr.io/eivindsjursen-lab/mintarr:latest\n",
    )
    errors = pins.violations(root)
    assert any("full commit SHA" in error for error in errors)
    assert any("base image" in error for error in errors)
    assert any("deployment image" in error for error in errors)


def test_immutable_references_are_accepted(tmp_path):
    root = _fixture(
        tmp_path,
        action_ref="a" * 40,
        base=f"python:3.12-slim@sha256:{'b' * 64}",
        deploy=f"image: example.invalid/mintarr@sha256:{'c' * 64}\n",
    )
    assert pins.violations(root) == []


def test_local_action_is_allowed_but_unpinned_docker_action_is_rejected(tmp_path):
    root = _fixture(
        tmp_path,
        action_ref="a" * 40,
        base=f"python:3.12-slim@sha256:{'b' * 64}",
        deploy=f"image: example.invalid/mintarr@sha256:{'c' * 64}\n",
    )
    workflow = root / ".github" / "workflows" / "local.yaml"
    workflow.write_text(
        "steps:\n" "  - uses: ./local-action\n" "  - uses: docker://alpine:3.22\n"
    )
    errors = pins.violations(root)
    assert len(errors) == 1
    assert "Docker action" in errors[0]
