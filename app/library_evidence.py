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
# The spectral (FLAC Detective) tier is a *separate* sensor with its own version
# and freshness, layered onto the same library_evidence row (F5.4 §8b). Bumping
# this re-measures only the spectral verdict, not the cheap ffprobe tier.
SPECTRAL_SENSOR_VERSION = "mintarr-library-spectral 2026-06-08"
_LOSSLESS_CODECS = {"flac", "alac"}
# Detective per-file verdicts that mean "not a genuine lossless file".
_FAKE_VERDICTS = {"FAKE", "FAKE_CERTAIN", "SUSPICIOUS"}


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


@dataclass(frozen=True)
class SpectralMeasurement:
    """FLAC Detective authenticity verdict for one existing library trackfile.

    ``authentic`` is deliberately tri-state (§8b): True (genuine), False (a
    measured fake), or None (not spectrally measured — disabled, Detective
    unreachable, or no per-file result could be matched back to this exact file).
    Unknown must never be read as authentic.
    """

    status: str  # "measured" | "unmeasured"
    reason: str | None = None
    authentic: bool | None = None
    verdict: str | None = None


def configured_library_root() -> str | None:
    return os.environ.get("MINTARR_LIBRARY_ROOT") or None


def spectral_enabled() -> bool:
    """True only when the operator opted into the heavier spectral tier (§8b)."""
    return os.environ.get("MINTARR_LIBRARY_SPECTRAL", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _lidarr_root() -> str:
    return os.environ.get("MINTARR_LIBRARY_LIDARR_ROOT", "")


def stat_for_freshness(
    lidarr_path: str,
    *,
    library_root: str | None = None,
    lidarr_root: str | None = None,
) -> tuple[str | None, int | None, float | None]:
    """Cheap (resolved_path, size, mtime) for the staleness check — no probing.

    Returns ``(None, None, None)`` when the file can't be resolved. Combined with
    the stored ``sensor_version``, this is the locked freshness basis: re-measure
    unless path, size, mtime *and* sensor version all still match.
    """
    root = library_root if library_root is not None else configured_library_root()
    if not root:
        return None, None, None
    mapped, _reason = resolve_library_path(
        lidarr_path,
        library_root=root,
        lidarr_root=lidarr_root if lidarr_root is not None else _lidarr_root(),
    )
    if mapped is None:
        return None, None, None
    try:
        st = mapped.stat()
    except OSError:
        return None, None, None
    return str(mapped), st.st_size, st.st_mtime


def is_measured_row_fresh(row: dict) -> bool:
    """True iff a stored measurement is safe to use for a decision *right now*.

    Guards against stale historical evidence becoming decision-active (F5.4 3b):
    the library must be mounted now, the stored ``sensor_version`` must be
    current, and the file on disk must still match the stored size + mtime inside
    the mount (re-stat, no probing). Anything else ⇒ not fresh ⇒ caller falls
    back to the Lidarr label.
    """
    root = configured_library_root()
    if not root or row.get("status") != "measured":
        return False
    if row.get("sensor_version") != SENSOR_VERSION:
        return False
    path = row.get("path")
    if not path:
        return False
    candidate = Path(path)
    try:
        if not _is_relative_to(candidate.resolve(), Path(root).resolve()):
            return False
        if candidate.is_symlink() or not candidate.is_file():
            return False
        st = candidate.stat()
    except OSError:
        return False
    return st.st_size == row.get("size") and st.st_mtime == row.get("mtime")


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


def is_spectral_row_fresh(row: dict) -> bool:
    """True iff a stored spectral verdict is current for the file on disk.

    Independent of the cheap-tier freshness (``is_measured_row_fresh``): a row can
    have fresh ffprobe evidence but stale/absent spectral evidence. Same basis as
    §3.4 but keyed on the *spectral* sensor version — re-measure unless the file's
    path/size/mtime still match and ``spectral_sensor_version`` is current.
    """
    root = configured_library_root()
    if not root or row.get("spectral_status") != "measured":
        return False
    if row.get("spectral_sensor_version") != SPECTRAL_SENSOR_VERSION:
        return False
    path = row.get("path")
    if not path:
        return False
    candidate = Path(path)
    try:
        if not _is_relative_to(candidate.resolve(), Path(root).resolve()):
            return False
        if candidate.is_symlink() or not candidate.is_file():
            return False
        st = candidate.stat()
    except OSError:
        return False
    return st.st_size == row.get("size") and st.st_mtime == row.get("mtime")


def measure_trackfile_spectral(
    lidarr_path: str,
    *,
    library_root: str | None = None,
    lidarr_root: str | None = None,
    client=None,
) -> SpectralMeasurement:
    """Spectrally measure one existing trackfile via FLAC Detective. Never raises.

    Per §8b.1 the analysis unit is this *single trackfile* at its exact resolved
    path — never a directory. The Detective response is matched back to that exact
    file; an unmatched or empty result is discarded as ``unmeasured`` (unknown),
    not cached as authentic.
    """
    if not spectral_enabled():
        return SpectralMeasurement("unmeasured", "spectral disabled")
    root = library_root if library_root is not None else configured_library_root()
    if not root:
        return SpectralMeasurement("unmeasured", "library not mounted")
    mapped, reason = resolve_library_path(
        lidarr_path,
        library_root=root,
        lidarr_root=lidarr_root if lidarr_root is not None else _lidarr_root(),
    )
    if mapped is None:
        return SpectralMeasurement("unmeasured", reason)

    try:
        result = (client or _default_spectral_client)(mapped)
    except Exception:
        log.exception("[library_evidence] spectral probe failed for a library file")
        return SpectralMeasurement("unmeasured", "detective unreachable")

    entry = _match_detective_file(result, mapped)
    if entry is None:
        # No per-file result for THIS exact file — never assume authentic (§8b.1).
        return SpectralMeasurement("unmeasured", "no detective result for file")
    verdict = (
        entry.get("verdict") or result.get("overall_verdict") or ""
    ).upper() or None
    fake = bool(entry.get("is_fake_high_res")) or (verdict in _FAKE_VERDICTS)
    return SpectralMeasurement(
        status="measured",
        authentic=not fake,
        verdict=verdict,
    )


def _match_detective_file(result: dict | None, mapped: Path) -> dict | None:
    """Return the per-file Detective entry for ``mapped`` exactly, else None.

    Match by basename so a differing container mount view still resolves, but
    refuse to guess: if no entry's basename equals the requested file, return None
    so the caller records *unknown* rather than another file's authenticity.
    """
    if not isinstance(result, dict):
        return None
    files = result.get("files")
    if not isinstance(files, list):
        return None
    target = mapped.name
    for item in files:
        if not isinstance(item, dict):
            continue
        item_path = item.get("path") or ""
        if _basename(str(item_path)) == target:
            return item
    return None


def _basename(path: str) -> str:
    """Last path segment, splitting on both separators (paths may be foreign)."""
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _default_spectral_client(path: Path) -> dict:
    """POST the single file path to the FLAC Detective service and return JSON.

    Detective must mount the library read-only at the same path Mintarr resolved
    (§8b deployment requirement), so the resolved path is sent as-is.
    """
    import requests  # local import: keeps the module importable without the dep

    url = os.environ.get("FLAC_API_URL", "http://host.docker.internal:8889/analyze")
    resp = requests.post(url, json={"path": str(path)}, timeout=900)
    if resp.status_code != 200:
        raise RuntimeError(f"detective HTTP {resp.status_code}")
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("detective returned non-object")
    return data


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
