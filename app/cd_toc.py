"""CD TOC reconstruction (F5.3B slice 1).

Pure and read-only: rebuild a CD table-of-contents (track frame offsets +
lead-out) from a completed rip — either per-track FLAC sample counts or a
single-image FLAC + cue sheet. This is the input a later slice turns into
AccurateRip/CTDB disc IDs; **no disc-ID math, no network, no policy here**.

A CD frame (sector) is 588 samples at 44.1 kHz. Real CD tracks are frame
aligned, so a track whose sample count is not a multiple of 588 means the
material is not a frame-accurate CD rip — we return None (lane skipped) rather
than fabricate a TOC.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

SAMPLES_PER_FRAME = 588  # 44100 / 75

_CUE_FILE_RE = re.compile(r"^\s*FILE\b", re.IGNORECASE | re.MULTILINE)
_CUE_TRACK_RE = re.compile(r"^\s*TRACK\s+\d+\s+(\S+)", re.IGNORECASE)
_CUE_INDEX_RE = re.compile(r"^\s*INDEX\s+(\d+)\s+(\d+):(\d+):(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class CdToc:
    """A reconstructed CD table of contents, in CD frames (sectors)."""

    track_offsets_frames: tuple[int, ...]  # 0-based LBA of each track start
    leadout_frames: int  # total program length in frames
    track_count: int


def msf_to_frames(minutes: int, seconds: int, frames: int) -> int:
    """Convert an MM:SS:FF cue timestamp to absolute CD frames."""
    return (minutes * 60 + seconds) * 75 + frames


def build_toc_from_track_lengths(lengths_frames: Sequence[int]) -> CdToc | None:
    """Build a TOC from per-track lengths (frames). None if empty/non-positive."""
    if not lengths_frames or any(n <= 0 for n in lengths_frames):
        return None
    offsets: list[int] = []
    running = 0
    for length in lengths_frames:
        offsets.append(running)
        running += length
    return CdToc(tuple(offsets), running, len(offsets))


def parse_cue_offsets(cue_text: str) -> list[int]:
    """Return each AUDIO track's INDEX 01 offset in frames, in cue order.

    Strict because these offsets seed disc-ID computation later: parse per
    ``TRACK`` block and require **exactly one** INDEX 01 per track. Returns an
    empty list (caller treats as "no usable TOC") for any irregularity —
    a track missing INDEX 01, an INDEX 01 before the first TRACK, a duplicate
    INDEX 01, a non-AUDIO track, or a multi-FILE cue (this slice only handles a
    single-image FLAC + cue). Returning [] rather than a shorter list prevents
    one track's offset being silently attributed to another.
    """
    if len(_CUE_FILE_RE.findall(cue_text)) > 1:
        return []  # multi-FILE (per-track) cue is out of scope for this slice

    offsets: list[int] = []
    in_track = False
    have_index01 = False
    for line in cue_text.splitlines():
        track_match = _CUE_TRACK_RE.match(line)
        if track_match:
            if in_track and not have_index01:
                return []  # previous track had no INDEX 01
            if track_match.group(1).upper() != "AUDIO":
                return []  # data / mixed-mode track is out of scope
            in_track = True
            have_index01 = False
            continue
        index_match = _CUE_INDEX_RE.match(line)
        if index_match:
            if not in_track:
                return []  # INDEX before the first TRACK
            if int(index_match.group(1)) != 1:
                continue  # INDEX 00 (pregap) and others are not track starts
            if have_index01:
                return []  # duplicate INDEX 01 in one track
            mm, ss, ff = (int(g) for g in index_match.groups()[1:])
            offsets.append(msf_to_frames(mm, ss, ff))
            have_index01 = True
    if in_track and not have_index01:
        return []  # final track had no INDEX 01
    return offsets


def _samples_to_frames(samples: int) -> int | None:
    """CD-frame count for a sample count, or None if not frame aligned."""
    if samples <= 0 or samples % SAMPLES_PER_FRAME != 0:
        return None
    return samples // SAMPLES_PER_FRAME


def _default_flac_samples(path: Path) -> int | None:
    try:
        from mutagen.flac import FLAC

        info = FLAC(str(path)).info
        return int(info.total_samples)
    except Exception:
        return None


def reconstruct_toc(
    folder: Path | str,
    *,
    flac_samples: Callable[[Path], int | None] = _default_flac_samples,
) -> CdToc | None:
    """Reconstruct a CD TOC from a completed rip folder (read-only).

    Returns None (lane skipped) for anything that is not a clean, frame-aligned
    single-disc rip: no FLACs, non-frame-aligned audio, or an unusable layout.
    The ``flac_samples`` reader is injectable for testing.
    """
    root = Path(folder)
    if not root.is_dir():
        return None
    flacs = sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".flac"
    )
    if not flacs:
        return None

    if len(flacs) == 1:
        return _toc_from_single_image(flacs[0], root, flac_samples)
    return _toc_from_per_track(flacs, flac_samples)


def _toc_from_per_track(
    flacs: Sequence[Path], flac_samples: Callable[[Path], int | None]
) -> CdToc | None:
    lengths: list[int] = []
    for flac in flacs:
        samples = flac_samples(flac)
        if samples is None:
            return None
        frames = _samples_to_frames(samples)
        if frames is None:
            return None
        lengths.append(frames)
    return build_toc_from_track_lengths(lengths)


def _toc_from_single_image(
    image: Path, root: Path, flac_samples: Callable[[Path], int | None]
) -> CdToc | None:
    cues = sorted(p for p in root.rglob("*.cue") if p.is_file())
    if not cues:
        return None  # single image without a cue: no per-track TOC available
    samples = flac_samples(image)
    if samples is None:
        return None
    leadout = _samples_to_frames(samples)
    if leadout is None:
        return None
    offsets = parse_cue_offsets(_read_text(cues[0]))
    if (
        not offsets
        or offsets[0] != 0
        or any(b <= a for a, b in zip(offsets, offsets[1:]))
    ):
        return None  # missing/garbled/non-monotonic offsets
    if offsets[-1] >= leadout:
        return None
    return CdToc(tuple(offsets), leadout, len(offsets))


def _read_text(path: Path) -> str:
    try:
        raw = path.read_bytes()[: 1024 * 1024]
    except OSError:
        return ""
    for encoding in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")
