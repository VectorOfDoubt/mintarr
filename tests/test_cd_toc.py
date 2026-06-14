"""Tests for CD TOC reconstruction (F5.3B slice 1, pure/read-only)."""

from __future__ import annotations

import cd_toc

F = cd_toc.SAMPLES_PER_FRAME  # 588 samples/frame


def test_msf_to_frames():
    assert cd_toc.msf_to_frames(0, 2, 0) == 150  # standard 2s lead-in
    assert cd_toc.msf_to_frames(1, 0, 0) == 60 * 75
    assert cd_toc.msf_to_frames(0, 0, 37) == 37


def test_build_toc_from_track_lengths():
    toc = cd_toc.build_toc_from_track_lengths([100, 200, 50])
    assert toc is not None
    assert toc.track_offsets_frames == (0, 100, 300)
    assert toc.leadout_frames == 350
    assert toc.track_count == 3


def test_build_toc_rejects_empty_or_nonpositive():
    assert cd_toc.build_toc_from_track_lengths([]) is None
    assert cd_toc.build_toc_from_track_lengths([100, 0, 50]) is None


def test_parse_cue_offsets():
    cue = (
        'FILE "image.flac" WAVE\n'
        "  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n"
        "  TRACK 02 AUDIO\n    INDEX 00 02:00:00\n    INDEX 01 02:01:00\n"
        "  TRACK 03 AUDIO\n    INDEX 01 05:00:37\n"
    )
    offsets = cd_toc.parse_cue_offsets(cue)
    assert offsets == [0, cd_toc.msf_to_frames(2, 1, 0), cd_toc.msf_to_frames(5, 0, 37)]


def test_parse_cue_rejects_duplicate_index01_in_track():
    cue = "  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n    INDEX 01 00:10:00\n"
    assert cd_toc.parse_cue_offsets(cue) == []


def test_parse_cue_rejects_track_missing_index01():
    # Codex repro: track 2 has only INDEX 00, so a global scan would mis-attribute
    # track 3's offset to track 2. Must reject, not return [0, 22500].
    cue = (
        "  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n"
        "  TRACK 02 AUDIO\n    INDEX 00 02:00:00\n"
        "  TRACK 03 AUDIO\n    INDEX 01 05:00:00\n"
    )
    assert cd_toc.parse_cue_offsets(cue) == []


def test_parse_cue_rejects_index_before_first_track():
    assert cd_toc.parse_cue_offsets("    INDEX 01 00:00:00\n  TRACK 01 AUDIO\n") == []


def test_parse_cue_rejects_non_audio_track():
    cue = (
        "  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n"
        "  TRACK 02 AUDIO\n    INDEX 01 05:00:00\n"
    )
    assert cd_toc.parse_cue_offsets(cue) == []


def test_parse_cue_rejects_multi_file():
    cue = (
        'FILE "01.flac" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n'
        'FILE "02.flac" WAVE\n  TRACK 02 AUDIO\n    INDEX 01 00:00:00\n'
    )
    assert cd_toc.parse_cue_offsets(cue) == []


def test_reconstruct_single_image_rejects_track_missing_index01(tmp_path):
    (tmp_path / "image.flac").write_bytes(b"x")
    (tmp_path / "image.cue").write_text(
        'FILE "image.flac" WAVE\n'
        "  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n"
        "  TRACK 02 AUDIO\n    INDEX 00 02:00:00\n"
        "  TRACK 03 AUDIO\n    INDEX 01 05:00:00\n"
    )
    assert cd_toc.reconstruct_toc(tmp_path, flac_samples=lambda p: 30000 * F) is None


def test_reconstruct_per_track_from_sample_counts(tmp_path):
    for i in range(1, 4):
        (tmp_path / f"{i:02d} track.flac").write_bytes(b"x")
    lengths = {
        "01 track.flac": 100 * F,
        "02 track.flac": 200 * F,
        "03 track.flac": 50 * F,
    }
    toc = cd_toc.reconstruct_toc(tmp_path, flac_samples=lambda p: lengths[p.name])
    assert toc is not None
    assert toc.track_offsets_frames == (0, 100, 300)
    assert toc.leadout_frames == 350


def test_reconstruct_per_track_accepts_mixed_case_flac_suffix(tmp_path):
    (tmp_path / "01 track.Flac").write_bytes(b"x")
    (tmp_path / "02 track.flac").write_bytes(b"x")
    lengths = {"01 track.Flac": 100 * F, "02 track.flac": 200 * F}
    toc = cd_toc.reconstruct_toc(tmp_path, flac_samples=lambda p: lengths[p.name])
    assert toc is not None
    assert toc.track_offsets_frames == (0, 100)
    assert toc.leadout_frames == 300


def test_reconstruct_rejects_non_frame_aligned(tmp_path):
    (tmp_path / "01 track.flac").write_bytes(b"x")
    (tmp_path / "02 track.flac").write_bytes(b"x")
    # second track is not a multiple of 588 samples → not a frame-accurate rip
    samples = {"01 track.flac": 100 * F, "02 track.flac": 200 * F + 1}
    assert (
        cd_toc.reconstruct_toc(tmp_path, flac_samples=lambda p: samples[p.name]) is None
    )


def test_reconstruct_single_image_uses_cue(tmp_path):
    (tmp_path / "image.flac").write_bytes(b"x")
    (tmp_path / "image.cue").write_text(
        'FILE "image.flac" WAVE\n'
        "  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n"
        "  TRACK 02 AUDIO\n    INDEX 01 00:02:00\n"
    )
    toc = cd_toc.reconstruct_toc(tmp_path, flac_samples=lambda p: 500 * F)
    assert toc is not None
    assert toc.track_offsets_frames == (0, 150)
    assert toc.leadout_frames == 500
    assert toc.track_count == 2


def test_reconstruct_single_image_without_cue_is_none(tmp_path):
    (tmp_path / "image.flac").write_bytes(b"x")
    assert cd_toc.reconstruct_toc(tmp_path, flac_samples=lambda p: 500 * F) is None


def test_reconstruct_no_flac_is_none(tmp_path):
    (tmp_path / "notes.txt").write_text("hi")
    assert cd_toc.reconstruct_toc(tmp_path) is None


def test_reconstruct_missing_samples_is_none(tmp_path):
    (tmp_path / "01 track.flac").write_bytes(b"x")
    (tmp_path / "02 track.flac").write_bytes(b"x")
    assert cd_toc.reconstruct_toc(tmp_path, flac_samples=lambda p: None) is None
