"""Read-only measurement of existing Lidarr library files (F5.4 slice 1).

Maps a Lidarr trackfile path into Mintarr's read-only library mount, validates
containment, and measures the file with ffprobe (+ ``flac -t``) into a quality
vector. No decisions change here — slice 3 wires measured evidence into the
import decision. Nothing is ever written, moved, or deleted in the library.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("tidalhires.library_evidence")

SENSOR_VERSION = "mintarr-library-evidence 2026-06-07"
_LOSSLESS_CODECS = {"flac", "alac"}


@dataclass(frozen=True)
class TrackMeasurement:
    """Measured quality vector for one existing library trackfile."""

    status: str  # "measured" | "unmeasured"
    reason: str | None = None
    codec: str | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None
    channels: int | None = None
    lossless: bool | None = None
    integrity_ok: bool | None = None


def configured_library_root() -> str | None:
    return os.environ.get("MINTARR_LIBRARY_ROOT") or None


def _lidarr_root() -> str:
    return os.environ.get("MINTARR_LIBRARY_LIDARR_ROOT", "")


def resolve_library_path(
    lidarr_path: str, *, library_root: str, lidarr_root: str
) -> tuple[Path | None, str | None]:
    """Map a Lidarr path into the mount and validate it. Returns (path, reason).

    ``path`` is None (with a reason) when the file cannot be safely measured:
    unmapped prefix, traversal/symlink escape, symlinked file, or missing file.
    """
    if not lidarr_path:
        return None, "empty path"
    prefix = lidarr_root.rstrip("/")
    if not prefix:
        # Required: without a configured Lidarr root we cannot map safely. For a
        # quality register, measuring the wrong file is worse than not measuring.
        return None, "lidarr root not configured"
    if not (lidarr_path == prefix or lidarr_path.startswith(prefix + "/")):
        return None, "path outside configured lidarr root"
    rel = lidarr_path[len(prefix) :].lstrip("/")
    if not rel:
        return None, "empty relative path"

    root = Path(library_root)
    mapped = root / rel
    try:
        resolved = mapped.resolve()
        root_resolved = root.resolve()
    except OSError:
        return None, "path resolve failed"
    if not _is_relative_to(resolved, root_resolved):
        return None, "path escapes library root"
    if mapped.is_symlink():
        return None, "symlinked file rejected"
    if not mapped.is_file():
        return None, "file not found in mount"
    return mapped, None


def measure_trackfile(
    lidarr_path: str,
    *,
    library_root: str | None = None,
    lidarr_root: str | None = None,
    prober=None,
) -> TrackMeasurement:
    """Measure one existing trackfile (read-only). Never raises."""
    root = library_root if library_root is not None else configured_library_root()
    if not root:
        return TrackMeasurement("unmeasured", "library not mounted")
    mapped, reason = resolve_library_path(
        lidarr_path,
        library_root=root,
        lidarr_root=lidarr_root if lidarr_root is not None else _lidarr_root(),
    )
    if mapped is None:
        return TrackMeasurement("unmeasured", reason)

    try:
        probe = (prober or _default_prober)(mapped)
    except Exception:
        log.exception("[library_evidence] probe failed for a library file")
        return TrackMeasurement("unmeasured", "probe failed")

    codec = (probe.get("codec") or "").lower() or None
    if codec is None:
        # ffprobe returned no audio codec — empty evidence is not "measured".
        return TrackMeasurement("unmeasured", "no audio stream")
    return TrackMeasurement(
        status="measured",
        codec=codec,
        sample_rate=probe.get("sample_rate"),
        bit_depth=probe.get("bit_depth"),
        channels=probe.get("channels"),
        lossless=(codec in _LOSSLESS_CODECS) if codec else None,
        integrity_ok=probe.get("integrity_ok"),
    )


def _default_prober(path: Path) -> dict:
    """Probe a file with ffprobe; verify FLAC integrity with ``flac -t``."""
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,bits_per_raw_sample,channels",
            "-of",
            "default=nw=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if probe.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {(probe.stderr or '').strip()[:200]}")
    fields: dict[str, str] = {}
    for line in probe.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip()

    codec = fields.get("codec_name", "")
    integrity_ok: bool | None = None
    if codec == "flac":
        result = subprocess.run(
            ["flac", "-t", "-s", str(path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        integrity_ok = result.returncode == 0
    return {
        "codec": codec,
        "sample_rate": _int(fields.get("sample_rate")),
        "bit_depth": _int(fields.get("bits_per_raw_sample")),
        "channels": _int(fields.get("channels")),
        "integrity_ok": integrity_ok,
    }


def _int(value: str | None) -> int | None:
    if value is None or value in ("", "N/A"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
