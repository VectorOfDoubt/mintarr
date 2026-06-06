"""Tests for CD-rip evidence detection/parsing (F5.3 slice 1, read-only)."""

from __future__ import annotations

import cd_rip_evidence as cre

_EAC_OK = """Exact Audio Copy V1.6 from 23. October 2020

Used drive  : ASUS DRW-24F1ST

Track  1
     Filename Artist - Album\\01 - Track.wav
     Peak level 98.9 %
     Copy CRC B8B7B8C9
     Accurately ripped (confidence 5)  [B8B7B8C9]
     Copy OK

Track  2
     Copy CRC 1A2B3C4D
     Accurately ripped (confidence 8)  [1A2B3C4D]
     Copy OK

All tracks accurately ripped.

No errors occurred
"""

_EAC_ERROR = """Exact Audio Copy V1.6 from 23. October 2020

Track  1
     Copy CRC B8B7B8C9
     Copy OK

Track  2
     Timing problem
     Copy aborted

There were errors
"""

_XLD_OK = """X Lossless Decoder version 20210101 (153.3)

AccurateRip Summary (DiscID: 00012345-...)
    Track 01 : OK (v2, confidence 12)
    Track 02 : OK (v2, confidence 30)
    ->All tracks accurately ripped.
"""

_NOT_A_RIP = """This is just some readme text.
No ripper signature here.
"""


def _write(folder, name, text, encoding="utf-8"):
    p = folder / name
    p.write_text(text, encoding=encoding)
    return p


def test_skipped_when_no_log_or_cue(tmp_path):
    (tmp_path / "01 track.flac").write_bytes(b"AUDIO")
    ev = cre.evaluate_folder(tmp_path)
    assert ev.detected is False
    assert ev.status == "skipped"


def test_cue_only_is_detected_but_warn(tmp_path):
    _write(tmp_path, "album.cue", 'FILE "01.flac" WAVE\n  TRACK 01 AUDIO\n')
    ev = cre.evaluate_folder(tmp_path)
    assert ev.detected is True
    assert ev.status == "warn"
    assert ev.has_cue is True
    assert ev.ripper is None


def test_eac_clean_log_is_pass_with_accuraterip(tmp_path):
    _write(tmp_path, "Artist - Album.log", _EAC_OK)
    _write(tmp_path, "Artist - Album.cue", "FILE x WAVE\n")
    ev = cre.evaluate_folder(tmp_path)
    assert ev.detected is True
    assert ev.status == "pass"
    assert ev.ripper == "eac"
    assert ev.ripper_version == "1.6"
    assert ev.has_cue is True
    assert ev.tracks_copy_ok == 2
    assert ev.accuraterip.present is True
    assert ev.accuraterip.accurate is True
    assert ev.accuraterip.min_confidence == 5
    # The "All tracks accurately ripped." summary must not count as a per-track
    # hit (regression: matched once exceeded total).
    assert ev.accuraterip.matched == 2
    assert ev.accuraterip.total == 2


def test_eac_log_with_errors_is_warn(tmp_path):
    _write(tmp_path, "rip.log", _EAC_ERROR)
    ev = cre.evaluate_folder(tmp_path)
    assert ev.detected is True
    assert ev.status == "warn"
    assert ev.ripper == "eac"


def test_accuraterip_matched_never_exceeds_total(tmp_path):
    # Regression for the summary-line double-count: every fixture with an
    # all-accurate summary must keep matched <= total.
    for name, text in (("eac.log", _EAC_OK), ("xld.log", _XLD_OK)):
        folder = tmp_path / name
        folder.mkdir()
        _write(folder, name, text)
        ar = cre.evaluate_folder(folder).accuraterip
        assert ar.matched <= ar.total, f"{name}: matched={ar.matched} total={ar.total}"
        assert ar.matched == 2
        assert ar.total == 2


def test_xld_ok_log_is_pass(tmp_path):
    _write(tmp_path, "rip.log", _XLD_OK)
    ev = cre.evaluate_folder(tmp_path)
    assert ev.detected is True
    assert ev.status == "pass"
    assert ev.ripper == "xld"
    assert ev.accuraterip.accurate is True
    assert ev.accuraterip.min_confidence == 12


def test_non_rip_log_is_skipped(tmp_path):
    _write(tmp_path, "readme.txt", _NOT_A_RIP)
    ev = cre.evaluate_folder(tmp_path)
    assert ev.detected is False
    assert ev.status == "skipped"


def test_utf16_log_is_parsed(tmp_path):
    _write(tmp_path, "rip.log", _EAC_OK, encoding="utf-16")
    ev = cre.evaluate_folder(tmp_path)
    assert ev.detected is True
    assert ev.status == "pass"
    assert ev.ripper == "eac"


def test_evaluate_is_read_only(tmp_path):
    _write(tmp_path, "rip.log", _EAC_OK)
    audio = tmp_path / "01 track.flac"
    audio.write_bytes(b"AUDIO")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}

    cre.evaluate_folder(tmp_path)

    after = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert before == after  # nothing added, removed, or modified
