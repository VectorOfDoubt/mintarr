"""SourceAdapter Protocol + value objects (F3.1).

Each adapter owns only download_raw(). Normalization, verification, and
import are handled by the common pipeline (pipeline.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .context import PipelineContext


@dataclass(frozen=True)
class ReleaseCandidate:
    """A search hit surfaced via newznab.

    GUID contract: globally unique = f"{source_type}:{source_id}".
    Title contract: must be parseable by Lidarr's quality detector. Source
    tag is appended as suffix '[<Source>]' only after regression-testing
    that the parser still detects FLAC/24bit correctly. F3.1 does not
    surface multi-adapter results; this dataclass is the locked contract
    that F3.3 will rely on.
    """

    source_type: str
    source_id: str
    title: str
    artist: str
    album: str
    year: int | None
    quality_tag: str
    size_bytes: int
    download_url: str
    priority: int = 50
    extra: dict = field(default_factory=dict)

    @property
    def guid(self) -> str:
        return f"{self.source_type}:{self.source_id}"


@dataclass(frozen=True)
class RawDownload:
    """Outcome of adapter.download_raw() — before normalize/verify/import."""

    files_dir: Path
    file_count: int
    total_bytes: int


@runtime_checkable
class SourceAdapter(Protocol):
    """Contract for source adapters. Adapters own only download_raw().

    Implementations must:
      - not import from server.py or pipeline.py
      - emit progress via ctx.set_progress()
      - poll ctx.check_cancelled() at natural checkpoints in long loops
      - use ctx.run_subprocess(...) for child processes (handles cancel)
    """

    name: str
    source_type: str

    def is_enabled(self) -> bool:
        """Per-adapter toggle. Default-disabled adapters can ship dormant."""
        ...

    def search(
        self,
        query: str,
        artist: str = "",
        album: str = "",
        year: int | None = None,
    ) -> list[ReleaseCandidate]:
        """Newznab search. Should return within ~5s."""
        ...

    def download_raw(
        self,
        candidate_id: str,
        ctx: "PipelineContext",
    ) -> RawDownload:
        """Fetch raw files into ctx.raw_dir. May raise JobCancelled."""
        ...

    def cleanup(self, jid: str, ctx: "PipelineContext") -> None:
        """Adapter-specific teardown on cancel/failure. Default no-op."""
        ...
