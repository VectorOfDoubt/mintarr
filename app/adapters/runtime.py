"""Concrete PipelineContext implementation (F3.1).

Bridges the adapter Protocol to the existing server.py helpers via lazy
imports so adapters do not need to know server.py exists.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("tidalhires.adapters.runtime")


class RuntimePipelineContext:
    """Real context handed to adapters during execute_source_grab.

    Lazy-imports server.py to avoid circular imports at module load — the
    adapter package is loaded by server.py during boot, so server must be
    fully initialized before any context method runs.
    """

    def __init__(
        self,
        *,
        jid: str,
        worker_job_id: int | None,
        raw_dir: Path,
        output_dir: Path,
        adapter_name: str,
    ) -> None:
        self.jid = jid
        self.worker_job_id = worker_job_id
        self.raw_dir = raw_dir
        self.output_dir = output_dir
        self._adapter_name = adapter_name
        self._log = logging.getLogger(f"tidalhires.adapter.{adapter_name}")

    def check_cancelled(self) -> None:
        import server

        server._raise_if_job_cancelled(self.worker_job_id, self.jid, self.raw_dir)

    def run_subprocess(
        self,
        argv: list[str],
        *,
        timeout: int,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        import server

        return server._run_cancellable_subprocess(
            argv,
            worker_job_id=self.worker_job_id,
            jid=self.jid,
            work_dir=self.raw_dir,
            timeout=timeout,
            text=text,
        )

    def set_progress(
        self,
        *,
        stage: str,
        percent: int,
        message: str = "",
        **extra,
    ) -> None:
        import server

        server._set_worker_progress(
            self.worker_job_id,
            self.jid,
            stage,
            percent,
            message,
            adapter=self._adapter_name,
            **extra,
        )

    def log(self, level: str, msg: str, *args, **fields) -> None:
        log_fn = getattr(self._log, level, self._log.info)
        if fields:
            log_fn("[%s] " + msg + " %s", self.jid, *args, fields)
        else:
            log_fn("[%s] " + msg, self.jid, *args)
