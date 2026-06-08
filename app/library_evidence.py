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

SENSOR_VERSION = "mintarr-library-evidence 2026-06-08b"
# F5.4 scan-tier split: metadata (ffprobe) and integrity (flac -t) are separate
# evidence tiers, each with its own sensor version and freshness, layered onto the
# same library_evidence row (see F5.4_SCAN_TIERS.md). Metadata tells us the
# lossless-tier axis quickly; integrity is the heavy full-file decode. Unknown
# integrity stays unknown — never read as OK.
METADATA_SENSOR_VERSION = "mintarr-library-metadata 2026-06-08"
INTEGRITY_SENSOR_VERSION = "mintarr-library-integrity 2026-06-08"
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
    integrity_ok: bool | None = None  # audio frames decode (genuine corruption ⇒ False)
    checksum_ok: bool | None = None  # FLAC MD5 verified; False ⇒ stale-MD5 (plays fine)


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


@dataclass(frozen=True)
class IntegrityMeasurement:
    """``flac -t`` integrity verdict for one trackfile (F5.4 integrity tier).

    ``integrity_ok`` = audio frames decode (genuine corruption ⇒ False);
    ``checksum_ok`` = FLAC MD5 verified (False ⇒ stale MD5, plays fine). Both None
    when not applicable/unchecked (non-FLAC, or unmeasured) — i.e. *unknown*.
    """

    status: str  # "measured" | "unmeasured"
    reason: str | None = None
    integrity_ok: bool | None = None
    checksum_ok: bool | None = None


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
    if row.get("sensor_version") != METADATA_SENSOR_VERSION:
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
        checksum_ok=probe.get("checksum_ok"),
    )


def measure_trackfile_metadata(
    lidarr_path: str,
    *,
    library_root: str | None = None,
    lidarr_root: str | None = None,
    prober=None,
) -> TrackMeasurement:
    """Metadata tier (ffprobe only): the fast lossless-tier axis. Never raises.

    Leaves ``integrity_ok`` / ``checksum_ok`` as None (unknown) — only the
    integrity tier may set them. Same resolution/containment as ``measure_trackfile``.
    """
    return measure_trackfile(
        lidarr_path,
        library_root=library_root,
        lidarr_root=lidarr_root,
        prober=prober or _metadata_prober,
    )


def measure_trackfile_integrity(
    lidarr_path: str,
    *,
    library_root: str | None = None,
    lidarr_root: str | None = None,
    prober=None,
) -> IntegrityMeasurement:
    """Integrity tier (``flac -t``): heavy full-file decode. Never raises.

    Returns only the integrity dimensions so the caller layers them onto an
    existing metadata row. ``measured`` with both None means the codec has no
    flac-level integrity check (e.g. non-FLAC) — that is *unknown*, not OK.
    """
    root = library_root if library_root is not None else configured_library_root()
    if not root:
        return IntegrityMeasurement("unmeasured", "library not mounted")
    mapped, reason = resolve_library_path(
        lidarr_path,
        library_root=root,
        lidarr_root=lidarr_root if lidarr_root is not None else _lidarr_root(),
    )
    if mapped is None:
        return IntegrityMeasurement("unmeasured", reason)
    try:
        probe = (prober or _integrity_prober)(mapped)
    except Exception:
        log.exception("[library_evidence] integrity probe failed for a library file")
        return IntegrityMeasurement("unmeasured", "integrity probe failed")
    return IntegrityMeasurement(
        status="measured",
        integrity_ok=probe.get("integrity_ok"),
        checksum_ok=probe.get("checksum_ok"),
    )


def is_integrity_row_fresh(row: dict) -> bool:
    """True iff a stored integrity verdict is current for the file on disk.

    Independent of the metadata tier: a row can have fresh metadata but stale or
    absent integrity evidence. Same path/size/mtime basis as the other tiers,
    keyed on ``INTEGRITY_SENSOR_VERSION``. Stale ⇒ integrity reads *unknown*.
    """
    root = configured_library_root()
    if not root or row.get("integrity_sensor_version") != INTEGRITY_SENSOR_VERSION:
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
    """Return the per-file Detective entry for the *exact* ``mapped`` path, else None.

    Requires a full-path match, never a basename one: two albums can both hold an
    ``01.flac``, so basename matching could cache one file's authenticity against
    another's ``trackfile_id`` (§8b.1). Detective must mount the library at the
    same path Mintarr resolved (§8b deployment requirement), so the reported path
    equals the path we sent; anything else ⇒ None ⇒ caller records *unknown*.
    """
    if not isinstance(result, dict):
        return None
    files = result.get("files")
    if not isinstance(files, list):
        return None
    target = os.path.normpath(str(mapped))
    for item in files:
        if not isinstance(item, dict):
            continue
        item_path = item.get("path")
        if isinstance(item_path, str) and os.path.normpath(item_path) == target:
            return item
    return None


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


def _run_ffprobe_fields(path: Path) -> dict:
    """ffprobe the first audio stream → codec/sample_rate/bit_depth/channels.

    Header reads only — the cheap metadata tier. Raises on ffprobe failure.
    """
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
    return {
        "codec": fields.get("codec_name", ""),
        "sample_rate": _int(fields.get("sample_rate")),
        "bit_depth": _int(fields.get("bits_per_raw_sample")),
        "channels": _int(fields.get("channels")),
    }


def _run_flac_test(path: Path) -> tuple[bool | None, bool | None]:
    """``flac -t`` (full read + decode) → (integrity_ok, checksum_ok). See §2.1."""
    result = subprocess.run(
        ["flac", "-t", "-s", str(path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return _classify_flac_test(result.returncode, result.stderr or "")


def _metadata_prober(path: Path) -> dict:
    """Metadata tier: ffprobe only. Integrity/checksum stay unknown (None)."""
    fields = _run_ffprobe_fields(path)
    return {**fields, "integrity_ok": None, "checksum_ok": None}


def _integrity_prober(path: Path) -> dict:
    """Integrity tier: ``flac -t`` decode for FLAC → integrity_ok + checksum_ok.

    Confirms the codec with a cheap ffprobe so we only ``flac -t`` real FLAC; for
    non-FLAC there is no flac-level integrity check, so both stay None (unknown).
    """
    codec = (_run_ffprobe_fields(path).get("codec") or "").lower()
    if codec != "flac":
        return {"codec": codec or None, "integrity_ok": None, "checksum_ok": None}
    integrity_ok, checksum_ok = _run_flac_test(path)
    return {"codec": codec, "integrity_ok": integrity_ok, "checksum_ok": checksum_ok}


def _default_prober(path: Path) -> dict:
    """Legacy fused probe (ffprobe + ``flac -t``) used by the back-compat cheap mode."""
    fields = _run_ffprobe_fields(path)
    integrity_ok: bool | None = None
    checksum_ok: bool | None = None
    if (fields.get("codec") or "") == "flac":
        integrity_ok, checksum_ok = _run_flac_test(path)
    return {**fields, "integrity_ok": integrity_ok, "checksum_ok": checksum_ok}


def _classify_flac_test(returncode: int, stderr: str) -> tuple[bool, bool | None]:
    """Map a ``flac -t`` result to (integrity_ok, checksum_ok) — see §2.1.

    ``flac -t`` exits non-zero for both a stale-MD5 file and a genuinely corrupt
    one. We split them: a pure *MD5 signature mismatch* (the audio decodes, only
    the stored checksum is stale) is usable audio (``integrity_ok=True``) with a
    failed checksum (``checksum_ok=False``); any other non-zero result is a hard
    decode error (``integrity_ok=False``). An unrecognized failure is treated as
    invalid — never softened. Success ⇒ both ok; an *unset* STREAMINFO MD5 also
    exits 0 but verifies nothing, so ``checksum_ok`` is None.
    """
    if returncode == 0:
        lowered = stderr.lower()
        # flac warns and still exits 0 when the STREAMINFO MD5 is unset/zero —
        # nothing was checked, so report checksum as unknown rather than verified.
        # The real message is "WARNING, cannot check MD5 signature since it was
        # unset in the STREAMINFO" (plus older "skipping MD5" phrasings).
        if (
            "unset" in lowered
            or "cannot check md5" in lowered
            or "skipping md5" in lowered
        ):
            return True, None
        return True, True
    lowered = stderr.lower()
    if "md5 signature mismatch" in lowered and not _has_decode_error(lowered):
        # Frames decoded; only the whole-stream MD5 did not match → stale checksum.
        return True, False
    # Genuine decode/frame failure, truncation, or any unrecognized error.
    return False, None


def _has_decode_error(lowered_stderr: str) -> bool:
    """True if flac -t stderr shows an actual decode/frame failure (not just MD5)."""
    markers = (
        "decoder",
        "lost sync",
        "frame crc",
        "crc mismatch",
        "unparseable",
        "error reading",
        "premature eof",
        "while decoding",
        "got error code",
    )
    return any(m in lowered_stderr for m in markers)


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
