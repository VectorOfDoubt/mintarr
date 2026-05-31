"""PipelineContext Protocol (F3.1).

Dependency-injected handle that adapters use to interact with the worker
runtime without importing from server.py. Concrete implementation lives
in adapters/runtime.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol


class PipelineContext(Protocol):
    """Runtime handle for an in-flight adapter download.

    Locked contract (F3 design v0.3 §5/§17): adapters depend only on this
    protocol — never on server.py. Do not add Lidarr-aware methods here;
    Lidarr knowledge stays in the common pipeline.
    """

    jid: str
    worker_job_id: int | None
    raw_dir: Path
    output_dir: Path

    def check_cancelled(self) -> None:
        """Raise worker.JobCancelled if cancel_requested is set."""
        ...

    def run_subprocess(
        self,
        argv: list[str],
        *,
        timeout: int,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        """Cancellable subprocess. Polls cancel; SIGTERM + 10s grace + SIGKILL."""
        ...

    def set_progress(
        self,
        *,
        stage: str,
        percent: int,
        message: str = "",
        **extra,
    ) -> None:
        """Project progress into state_db jobs + SAB-compat _jobs dict."""
        ...

    def log(self, level: str, msg: str, *args, **fields) -> None:
        """Structured logging — adapter name auto-prefixed in implementation."""
        ...
