from __future__ import annotations

from types import SimpleNamespace

import release_metadata
from release_metadata import collect_observed_release


def test_collect_observed_release_uses_filename_fallback(tmp_path, monkeypatch):
    output_dir = tmp_path / "jid"
    output_dir.mkdir()
    (output_dir / "01 - Celice (2026 Remaster).flac").write_bytes(b"not-real-flac")

    monkeypatch.setattr(release_metadata, "_mutagen_file_reader", lambda: (None, False))

    evidence = collect_observed_release(output_dir)

    assert evidence.mutagen_available is False
    assert evidence.tag_read_errors == 0
    assert evidence.observed.file_count == 1
    assert evidence.observed.track_titles == frozenset({"celice"})
    assert evidence.files[0]["tag_source"] == "filename"
    assert evidence.files[0]["normalized_title"] == "celice"


def test_collect_observed_release_reads_tags_and_mbids(tmp_path, monkeypatch):
    output_dir = tmp_path / "jid"
    output_dir.mkdir()
    audio_path = output_dir / "01.flac"
    audio_path.write_bytes(b"not-real-flac")

    def fake_mutagen_file(path, easy=True):
        assert path == audio_path
        assert easy is True
        return SimpleNamespace(
            tags={
                "title": ["Celice"],
                "artist": ["a-ha"],
                "album": ["Analogue"],
                "musicbrainz_artistid": ["artist-mbid"],
                "musicbrainz_albumid": ["release-mbid"],
                "musicbrainz_releasegroupid": ["release-group-mbid"],
            }
        )

    monkeypatch.setattr(
        release_metadata, "_mutagen_file_reader", lambda: (fake_mutagen_file, True)
    )

    evidence = collect_observed_release(output_dir)

    assert evidence.mutagen_available is True
    assert evidence.tag_read_errors == 0
    assert evidence.observed.track_titles == frozenset({"celice"})
    assert evidence.observed.artist_names == frozenset({"a-ha"})
    assert evidence.observed.album_titles == frozenset({"Analogue"})
    assert evidence.observed.artist_mbid == "artist-mbid"
    assert evidence.observed.release_group_mbid == "release-group-mbid"
    assert evidence.observed.release_mbid == "release-mbid"
    assert evidence.files[0]["tag_source"] == "mutagen"


def test_collect_observed_release_falls_back_when_tag_reader_errors(
    tmp_path, monkeypatch
):
    output_dir = tmp_path / "jid"
    output_dir.mkdir()
    (output_dir / "02 - Cosy Prisons.flac").write_bytes(b"not-real-flac")

    def broken_reader(path, easy=True):
        raise RuntimeError("bad tags")

    monkeypatch.setattr(
        release_metadata, "_mutagen_file_reader", lambda: (broken_reader, True)
    )

    evidence = collect_observed_release(output_dir)

    assert evidence.mutagen_available is True
    assert evidence.tag_read_errors == 1
    assert evidence.observed.track_titles == frozenset({"cosy prisons"})
    assert evidence.files[0]["tag_source"] == "filename"
    assert evidence.files[0]["error"] == "bad tags"
