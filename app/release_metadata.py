"""Read-only observed-release metadata extraction for F5.1."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from release_family import (
    AUDIO_SUFFIXES,
    ObservedRelease,
    normalize_track_title_for_match,
)

MUSICBRAINZ_ARTIST_KEYS = (
    "musicbrainz_artistid",
    "musicbrainz artist id",
    "musicbrainz_artist_id",
)
MUSICBRAINZ_RELEASE_KEYS = (
    "musicbrainz_albumid",
    "musicbrainz album id",
    "musicbrainz_releaseid",
    "musicbrainz release id",
)
MUSICBRAINZ_RELEASE_GROUP_KEYS = (
    "musicbrainz_releasegroupid",
    "musicbrainz release group id",
    "musicbrainz_release_group_id",
)


@dataclass(frozen=True)
class ObservedReleaseEvidence:
    observed: ObservedRelease
    files: tuple[dict[str, Any], ...]
    mutagen_available: bool
    tag_read_errors: int = 0

    def to_sensor_evidence(self) -> dict[str, Any]:
        return {
            "file_count": self.observed.file_count,
            "track_titles": sorted(self.observed.track_titles),
            "artist_names": sorted(self.observed.artist_names),
            "album_titles": sorted(self.observed.album_titles),
            "artist_mbids": sorted(mbid for mbid in self.observed.artist_mbids if mbid),
            "release_group_mbids": sorted(
                mbid for mbid in self.observed.release_group_mbids if mbid
            ),
            "release_mbids": sorted(
                mbid for mbid in self.observed.release_mbids if mbid
            ),
            "mutagen_available": self.mutagen_available,
            "tag_read_errors": self.tag_read_errors,
            "files": list(self.files),
        }


def collect_observed_release(output_dir: Path) -> ObservedReleaseEvidence:
    mutagen_file, mutagen_available = _mutagen_file_reader()
    file_evidence: list[dict[str, Any]] = []
    track_titles: set[str] = set()
    artist_names: set[str] = set()
    album_titles: set[str] = set()
    artist_mbids: set[str] = set()
    release_group_mbids: set[str] = set()
    release_mbids: set[str] = set()
    tag_read_errors = 0

    for path in sorted(_audio_files(output_dir)):
        rel = str(path.relative_to(output_dir))
        tag_data: dict[str, list[str]] = {}
        error = None
        if mutagen_file is not None:
            try:
                tag_data = _read_tags(mutagen_file, path)
            except Exception as exc:
                tag_read_errors += 1
                error = str(exc)

        raw_title = _first(tag_data, ("title",)) or path.stem
        normalized_title = normalize_track_title_for_match(raw_title)
        if normalized_title:
            track_titles.add(normalized_title)

        artist = _first(tag_data, ("artist", "albumartist", "album artist"))
        album = _first(tag_data, ("album",))
        if artist:
            artist_names.add(artist)
        if album:
            album_titles.add(album)

        artist_mbids.update(_values(tag_data, MUSICBRAINZ_ARTIST_KEYS))
        release_group_mbids.update(_values(tag_data, MUSICBRAINZ_RELEASE_GROUP_KEYS))
        release_mbids.update(_values(tag_data, MUSICBRAINZ_RELEASE_KEYS))

        file_evidence.append(
            {
                "path": rel,
                "title": raw_title,
                "normalized_title": normalized_title,
                "artist": artist,
                "album": album,
                "artist_mbids": sorted(_values(tag_data, MUSICBRAINZ_ARTIST_KEYS)),
                "release_group_mbids": sorted(
                    _values(tag_data, MUSICBRAINZ_RELEASE_GROUP_KEYS)
                ),
                "release_mbids": sorted(_values(tag_data, MUSICBRAINZ_RELEASE_KEYS)),
                "tag_source": "mutagen" if tag_data else "filename",
                "error": error,
            }
        )

    return ObservedReleaseEvidence(
        observed=ObservedRelease(
            file_count=len(file_evidence),
            track_titles=frozenset(track_titles),
            artist_names=frozenset(artist_names),
            album_titles=frozenset(album_titles),
            artist_mbids=frozenset(artist_mbids),
            release_group_mbids=frozenset(release_group_mbids),
            release_mbids=frozenset(release_mbids),
            artist_mbid=_single_or_none(artist_mbids),
            release_group_mbid=_single_or_none(release_group_mbids),
            release_mbid=_single_or_none(release_mbids),
        ),
        files=tuple(file_evidence),
        mutagen_available=mutagen_available,
        tag_read_errors=tag_read_errors,
    )


def _audio_files(output_dir: Path) -> list[Path]:
    return [
        path
        for path in output_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES
    ]


def _mutagen_file_reader():
    try:
        from mutagen import File as mutagen_file
    except Exception:
        return None, False
    return mutagen_file, True


def _read_tags(mutagen_file, path: Path) -> dict[str, list[str]]:
    audio = mutagen_file(path, easy=True)
    if audio is None or not getattr(audio, "tags", None):
        return {}
    out: dict[str, list[str]] = {}
    for key, value in audio.tags.items():
        values = value if isinstance(value, list) else [value]
        out[str(key).lower()] = [
            str(item).strip() for item in values if str(item).strip()
        ]
    return out


def _first(tags: dict[str, list[str]], keys: tuple[str, ...]) -> str | None:
    for value in sorted(_values(tags, keys)):
        return value
    return None


def _values(tags: dict[str, list[str]], keys: tuple[str, ...]) -> set[str]:
    values: set[str] = set()
    for key in keys:
        values.update(tags.get(key.lower(), []))
    return values


def _single_or_none(values: set[str]) -> str | None:
    if len(values) == 1:
        return next(iter(values))
    return None
