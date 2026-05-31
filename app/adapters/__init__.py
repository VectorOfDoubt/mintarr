"""Source-adapter registry.

F3.1 introduces the SourceAdapter Protocol (base.py) and PipelineContext
(context.py). The common pipeline (pipeline.py) is source-agnostic and
delegates source-specific work to adapters via download_raw().

Adapters never import from server.py. The runtime injects a
PipelineContext that exposes check_cancelled / run_subprocess /
set_progress / log without coupling adapters to server internals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import SourceAdapter


_adapters: dict[str, "SourceAdapter"] = {}


def register(adapter: "SourceAdapter") -> None:
    if adapter.name in _adapters:
        raise ValueError(f"Adapter '{adapter.name}' already registered")
    _adapters[adapter.name] = adapter


def get_adapter(name: str) -> "SourceAdapter | None":
    return _adapters.get(name)


def enabled_adapters() -> list["SourceAdapter"]:
    return [a for a in _adapters.values() if a.is_enabled()]


def all_adapters() -> list["SourceAdapter"]:
    return list(_adapters.values())


def reset_registry() -> None:
    """Test-only helper to clear adapter registrations between tests."""
    _adapters.clear()
