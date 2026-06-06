"""CD-rip evidence detection and parsing (F5.3 slice 1).

Pure and read-only: given a completed-folder path, detect whether it looks like
a CD rip and parse the on-disk rip artifacts (EAC/XLD/whipper log + cue) into a
structured evidence object. No audio is read, nothing is mutated, no network is
used, and nothing here influences import policy — slice 2 maps this to a
SensorResult and slice 4 wires scoring. See the F5.3 design doc.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Logs are small; cap reads so a mislabeled huge .log can't blow up memory.
_MAX_LOG_BYTES = 5 * 1024 * 1024

# Ripper fingerprints, checked against the log head (case-insensitive).
_RIPPERS: tuple[tuple[str, str], ...] = (
    ("Exact Audio Copy", "eac"),
    ("X Lossless Decoder", "xld"),
    ("whipper", "whipper"),
    ("morituri", "whipper"),
    ("cyanrip", "cyanrip"),
)

# Positive per-track markers across rippers.
_COPY_OK_RE = re.compile(r"\bCopy OK\b", re.IGNORECASE)
_XLD_TRACK_OK_RE = re.compile(r"Track\s+\d+\s*:\s*OK\b", re.IGNORECASE)
# Per-track AccurateRip entries always carry a confidence token; the summary
# line "All tracks accurately ripped." does not, so it is matched separately
# (as a boolean) and never counted as a track — keeping matched <= total.
_AR_CONFIDENCE_RE = re.compile(r"confidence\s+(\d+)", re.IGNORECASE)
_ALL_ACCURATE_RE = re.compile(r"all tracks accurately ripped", re.IGNORECASE)
_VERSION_RE = re.compile(r"V?(\d+\.\d+(?:\.\d+)?(?:\s*beta\s*\d+)?)", re.IGNORECASE)

# Negative markers that downgrade a log to warn.
_ERROR_MARKERS: tuple[str, ...] = (
    "copy aborted",
    "there were errors",
    "timing problem",
    "missing samples",
    "rip not accurate",
    "no match",
    "track not present in database",
    " : ng",
    "accurately ripped (confidence 0)",
)


@dataclass(frozen=True)
class AccurateRipResult:
    present: bool = False
    accurate: bool = False
    min_confidence: int | None = None
    matched: int = 0
    total: int = 0


@dataclass(frozen=True)
class CdRipEvidence:
    """Parsed, read-only CD-rip evidence for a completed folder."""

    detected: bool
    status: str  # "pass" | "warn" | "skipped"
    summary: str
    ripper: str | None = None
    ripper_version: str | None = None
    log_filename: str | None = None
    has_cue: bool = False
    tracks_copy_ok: int = 0
    accuraterip: AccurateRipResult = field(default_factory=AccurateRipResult)


def evaluate_folder(folder: Path | str) -> CdRipEvidence:
    """Detect and parse CD-rip evidence in a completed folder (read-only)."""
    root = Path(folder)
    log_path = _find_rip_log(root)
    has_cue = _has_cue(root)

    if log_path is None:
        if has_cue:
            return CdRipEvidence(
                detected=True,
                status="warn",
                summary="cue sheet present but no verifiable rip log",
                has_cue=True,
            )
        return CdRipEvidence(
            detected=False, status="skipped", summary="no rip log or cue sheet"
        )

    text = _read_text(log_path)
    return _parse_log(text, log_filename=log_path.name, has_cue=has_cue)


def _find_rip_log(root: Path) -> Path | None:
    """Return the first file that looks like a ripper log, else None."""
    if not root.is_dir():
        return None
    candidates = sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in (".log", ".txt")
    )
    for path in candidates:
        head = _read_text(path, limit=4096)
        if head and _ripper_from_head(head) is not None:
            return path
    return None


def _has_cue(root: Path) -> bool:
    if not root.is_dir():
        return False
    return any(p.suffix.lower() == ".cue" for p in root.rglob("*") if p.is_file())


def _ripper_from_head(head: str) -> str | None:
    lowered = head.lower()
    for needle, name in _RIPPERS:
        if needle.lower() in lowered:
            return name
    return None


def _read_text(path: Path, *, limit: int = _MAX_LOG_BYTES) -> str:
    """Read a log tolerantly across the encodings rippers commonly emit."""
    try:
        raw = path.read_bytes()[:limit]
    except OSError:
        return ""
    for encoding in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_log(text: str, *, log_filename: str, has_cue: bool) -> CdRipEvidence:
    ripper = _ripper_from_head(text)
    ripper_version = _ripper_version(text, ripper)

    copy_ok = len(_COPY_OK_RE.findall(text)) + len(_XLD_TRACK_OK_RE.findall(text))
    ar = _parse_accuraterip(text)
    error_detected = any(marker in text.lower() for marker in _ERROR_MARKERS)

    if error_detected or (ar.present and not ar.accurate):
        status = "warn"
        summary = "rip log present with errors or AccurateRip mismatch"
    elif ar.accurate or copy_ok > 0:
        status = "pass"
        summary = _pass_summary(ar, copy_ok)
    else:
        status = "warn"
        summary = "rip log present but no positive copy/AccurateRip evidence"

    return CdRipEvidence(
        detected=True,
        status=status,
        summary=summary,
        ripper=ripper,
        ripper_version=ripper_version,
        log_filename=log_filename,
        has_cue=has_cue,
        tracks_copy_ok=copy_ok,
        accuraterip=ar,
    )


def _ripper_version(text: str, ripper: str | None) -> str | None:
    if ripper is None:
        return None
    # Look near the ripper name in the first lines for a version token.
    head = "\n".join(text.splitlines()[:5])
    match = _VERSION_RE.search(head)
    return match.group(1).strip() if match else None


def _parse_accuraterip(text: str) -> AccurateRipResult:
    # One confidence token per AccurateRip track entry. `total` is the number of
    # such entries; `matched` is those with confidence > 0 (a confidence of 0 is
    # a no-match). `all_accurate` is the disc-level summary line, kept as a flag
    # only — never added to a count — so matched <= total always holds.
    confidences = [int(m) for m in _AR_CONFIDENCE_RE.findall(text)]
    all_accurate = bool(_ALL_ACCURATE_RE.search(text))
    present = bool(confidences) or all_accurate
    if not present:
        return AccurateRipResult()

    total = len(confidences)
    matched = sum(1 for c in confidences if c > 0)
    non_zero = [c for c in confidences if c > 0]
    has_zero_conf = matched < total
    accurate = (all_accurate or matched > 0) and not has_zero_conf
    return AccurateRipResult(
        present=True,
        accurate=accurate,
        min_confidence=min(non_zero) if non_zero else None,
        matched=matched,
        total=total,
    )


def _pass_summary(ar: AccurateRipResult, copy_ok: int) -> str:
    if ar.accurate:
        if ar.min_confidence is not None:
            return f"AccurateRip-verified rip (min confidence {ar.min_confidence})"
        return "AccurateRip-verified rip"
    return f"rip log present, {copy_ok} track(s) copied OK"
