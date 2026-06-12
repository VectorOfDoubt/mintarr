"""V2 integration tests around server import flow."""

from __future__ import annotations

import os
from types import SimpleNamespace

from release_family import ObservedRelease
from release_metadata import ObservedReleaseEvidence
import server


def _response(status_code=200, payload=None, text=""):
    return SimpleNamespace(status_code=status_code, text=text, json=lambda: payload)


def _manualimport_item(jid: str):
    return {
        "path": f"/downloads/TidalHiRes/complete/{jid}/01.flac",
        "artist": {"id": 10},
        "album": {"id": 20, "currentRelease": {"id": 30}},
        "albumReleaseId": 30,
        "tracks": [{"id": 101}],
        "quality": {"quality": {"name": "FLAC 24bit"}},
        "rejections": [],
    }


def test_compute_verification_accepts_complete_authentic_album(tmp_path):
    jid = "abc12345"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    (output_dir / "01.flac").write_bytes(b"flac")

    result = server._compute_verification(
        jid,
        output_dir,
        [_manualimport_item(jid)],
        verdict="AUTHENTIC",
        detective_error=None,
        detective_result={"files": [{"is_fake_high_res": False}]},
        existing_kbps=320,
        existing_label="MP3-320",
        new_effective_kbps=3000,
        album_ids=[20],
        title="Artist - Album",
    )

    assert result.score == 100
    assert result.verification_decision == "ACCEPT"
    assert result.import_outcome == "PENDING"
    assert result.identity_decision == "SAME_RELEASE"
    assert result.identity_confidence == 85.0
    assert result.identity_reasons[0] == (
        "Lidarr ManualImport matched every observed audio file"
    )
    assert result.components == {
        "ffprobe": 25,
        "flac_t": 25,
        "detective": 35,
        "complete": 15,
    }
    assert result.overrides == []
    assert [s["name"] for s in result.sensors] == [
        "ffprobe",
        "flac_t",
        "flac_detective",
        "release_identity",
    ]
    assert result.sensors[0]["status"] == "pass"
    assert result.sensors[1]["status"] == "pass"
    assert result.sensors[2]["evidence"]["overall_verdict"] == "AUTHENTIC"
    assert result.sensors[3]["class"] == "metadata_identity"
    assert result.sensors[3]["evidence"]["track_titles"] == ["01"]
    assert result.sensors[3]["evidence"]["identity_decision"] == "SAME_RELEASE"


def test_compute_verification_reviews_fewer_tracks_than_complete_existing(tmp_path):
    jid = "under123"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    manualimport_items = []
    for idx in range(10):
        filename = f"{idx + 1:02d}.flac"
        (output_dir / filename).write_bytes(b"flac")
        item = _manualimport_item(jid)
        item["path"] = f"/downloads/TidalHiRes/complete/{jid}/{filename}"
        item["tracks"] = [{"id": 100 + idx}]
        manualimport_items.append(item)

    result = server._compute_verification(
        jid,
        output_dir,
        manualimport_items,
        verdict="AUTHENTIC",
        detective_error=None,
        detective_result={"files": [{"is_fake_high_res": False}] * 10},
        existing_kbps=192,
        existing_label="MP3-192",
        new_effective_kbps=3000,
        album_ids=[20],
        title="Artist - Album",
        existing_track_count=11,
        new_track_count=10,
        expected_track_count=11,
    )

    assert result.verification_decision == "REVIEW_REQUIRED"
    assert result.import_outcome == "PENDING"
    assert result.identity_decision == "SAME_RELEASE"
    assert "track_count_undercount" in result.overrides
    assert result.to_decisions_log()["reason"] == (
        "candidate has fewer tracks than tracked release"
    )


def test_compute_verification_blocks_wrong_album_identity_even_with_authentic_audio(
    tmp_path, mocker
):
    jid = "abc12345"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    (output_dir / "01.flac").write_bytes(b"flac")
    manualimport_item = _manualimport_item(jid)
    manualimport_item["artist"]["foreignArtistId"] = "expected-artist-mbid"
    mocker.patch.object(
        server,
        "collect_observed_release",
        return_value=ObservedReleaseEvidence(
            observed=ObservedRelease(
                file_count=1,
                track_titles=frozenset({"01"}),
                artist_mbids=frozenset({"different-artist-mbid"}),
                artist_mbid="different-artist-mbid",
            ),
            files=(
                {
                    "path": "01.flac",
                    "title": "01",
                    "normalized_title": "01",
                    "artist": None,
                    "album": None,
                    "artist_mbids": ["different-artist-mbid"],
                    "release_group_mbids": [],
                    "release_mbids": [],
                    "tag_source": "mutagen",
                    "error": None,
                },
            ),
            mutagen_available=True,
        ),
    )

    result = server._compute_verification(
        jid,
        output_dir,
        [manualimport_item],
        verdict="AUTHENTIC",
        detective_error=None,
        detective_result={"files": [{"is_fake_high_res": False}]},
        existing_kbps=320,
        existing_label="MP3-320",
        new_effective_kbps=3000,
        album_ids=[20],
        title="Artist - Album",
    )

    assert result.score == 100
    assert result.verification_decision == "BLOCK"
    assert result.identity_decision == "WRONG_ALBUM"
    assert result.to_decisions_log()["reason"] == "wrong album identity"


def test_compute_verification_routes_insufficient_identity_to_review_not_block(
    tmp_path,
):
    jid = "abc12345"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    (output_dir / "01.flac").write_bytes(b"flac")

    result = server._compute_verification(
        jid,
        output_dir,
        [],
        verdict="AUTHENTIC",
        detective_error=None,
        detective_result={"files": [{"is_fake_high_res": False}]},
        existing_kbps=0,
        existing_label="nothing",
        new_effective_kbps=3000,
        album_ids=[],
        title="Artist - Album",
    )

    assert result.score == 85
    assert result.verification_decision == "REVIEW_REQUIRED"
    assert result.identity_decision == "INSUFFICIENT_EVIDENCE"
    assert result.to_decisions_log()["reason"] == (
        "insufficient release identity evidence"
    )


def test_compute_verification_does_not_review_non_family_lidarr_rejection(tmp_path):
    jid = "abc12345"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    (output_dir / "01.flac").write_bytes(b"flac")
    manualimport_item = _manualimport_item(jid)
    manualimport_item["rejections"] = [
        {"reason": "Couldn't find similar album", "type": "permanent"}
    ]

    result = server._compute_verification(
        jid,
        output_dir,
        [manualimport_item],
        verdict="AUTHENTIC",
        detective_error=None,
        detective_result={"files": [{"is_fake_high_res": False}]},
        existing_kbps=0,
        existing_label="nothing",
        new_effective_kbps=3000,
        album_ids=[20],
        title="Artist - Album",
    )

    assert result.score == 85
    assert result.verification_decision == "ACCEPT"
    assert result.identity_decision == "SAME_RELEASE"
    assert result.identity_confidence == 75.0


def test_compute_verification_validator_error_blocks(tmp_path):
    jid = "abc12345"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    (output_dir / "01.flac").write_bytes(b"flac")

    result = server._compute_verification(
        jid,
        output_dir,
        [_manualimport_item(jid)],
        verdict=None,
        detective_error="HTTP 500",
        detective_result=None,
        existing_kbps=0,
        existing_label="nothing",
        new_effective_kbps=3000,
        album_ids=[20],
        title="Artist - Album",
    )

    assert result.score == 0
    assert result.new_kbps == 0
    assert result.verification_decision == "BLOCK"
    assert result.overrides == ["validator_error"]


def test_compute_verification_no_audio_files_is_not_validator_error(tmp_path):
    jid = "abc12345"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    (output_dir / "cover.jpg").write_bytes(b"jpg")

    result = server._compute_verification(
        jid,
        output_dir,
        [],
        verdict=None,
        detective_error='HTTP 400: {"error":"no .flac files found at path"}',
        detective_result=None,
        existing_kbps=0,
        existing_label="nothing",
        new_effective_kbps=3000,
        album_ids=[],
        title="Artist - Album",
    )

    assert result.score == 0
    assert result.new_kbps == 0
    assert result.verification_decision == "BLOCK"
    assert result.overrides == ["no_audio_files"]
    assert result.to_decisions_log()["reason"] == "no audio files downloaded"


def test_compute_verification_blocks_partial_codec_gate_download(tmp_path):
    jid = "codecpartial"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    (output_dir / "01.flac").write_bytes(b"flac")
    server._jobs[jid] = {"id": jid, "codec_gate_skipped": 6, "download_exit_code": 1}

    try:
        result = server._compute_verification(
            jid,
            output_dir,
            [_manualimport_item(jid)],
            verdict="AUTHENTIC",
            detective_error=None,
            detective_result={"files": [{"is_fake_high_res": False}]},
            existing_kbps=1411,
            existing_label="FLAC",
            new_effective_kbps=3000,
            album_ids=[20],
            title="Artist - Album",
        )
    finally:
        server._jobs.pop(jid, None)

    assert result.score == 0
    assert result.new_kbps == 0
    assert result.verification_decision == "BLOCK"
    assert result.overrides == ["codec_mismatch"]


def test_compute_verification_explains_all_files_skipped_by_codec_gate(tmp_path):
    jid = "codecallskip"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    server._jobs[jid] = {"id": jid, "codec_gate_skipped": 10}

    try:
        result = server._compute_verification(
            jid,
            output_dir,
            [],
            verdict="UNKNOWN",
            detective_error="no .flac files found at path",
            detective_result=None,
            existing_kbps=0,
            existing_label="nothing",
            new_effective_kbps=0,
            album_ids=[],
            title="AFI - Silver Bleeds",
        )
    finally:
        server._jobs.pop(jid, None)

    assert result.verification_decision == "BLOCK"
    assert result.import_outcome == "PENDING"
    assert result.overrides == ["codec_mismatch", "no_audio_files"]
    assert result.sensors[0]["summary"] == (
        "Downloaded 10 audio file(s), but all were skipped because they were not FLAC/ALAC."
    )
    assert result.sensors[1]["summary"] == (
        "No FLAC files remained after the codec gate removed non-FLAC downloads."
    )
    assert result.sensors[2]["summary"] == (
        "Spectral analysis skipped because the codec gate left no FLAC files."
    )


def test_compute_verification_fake_hi_res_requires_review(tmp_path):
    jid = "abc12345"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    (output_dir / "01.flac").write_bytes(b"flac")

    result = server._compute_verification(
        jid,
        output_dir,
        [_manualimport_item(jid)],
        verdict="AUTHENTIC",
        detective_error=None,
        detective_result={"files": [{"is_fake_high_res": True}]},
        existing_kbps=0,
        existing_label="nothing",
        new_effective_kbps=3000,
        album_ids=[20],
        title="Artist - Album",
    )

    assert result.score == 100
    assert result.verification_decision == "REVIEW_REQUIRED"
    assert result.overrides == ["fake_hi_res"]


def test_compute_verification_persists_flac_detective_file_evidence(tmp_path):
    jid = "evidence1"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    (output_dir / "01.flac").write_bytes(b"flac")

    result = server._compute_verification(
        jid,
        output_dir,
        [_manualimport_item(jid)],
        verdict="AUTHENTIC",
        detective_error=None,
        detective_result={
            "overall_verdict": "AUTHENTIC",
            "file_count": 1,
            "files": [
                {
                    "filepath": f"/output/{jid}/01.flac",
                    "verdict": "AUTHENTIC",
                    "sample_rate": 96000,
                    "bit_depth": 24,
                    "cutoff_freq": 47900.4,
                    "is_fake_high_res": True,
                    "wrapper_overrides": [
                        "override:fake_hires_cutoff_at_nyquist(47900/48000)"
                    ],
                }
            ],
        },
        existing_kbps=0,
        existing_label="nothing",
        new_effective_kbps=3000,
        album_ids=[20],
        title="Artist - Album",
    )

    assert result.files == [
        {
            "filename": "01.flac",
            "size_bytes": None,
            "sample_rate": 96000,
            "bit_depth": 24,
            "duration_sec": None,
            "estimated_kbps": None,
            "detective_verdict": "AUTHENTIC",
            "cutoff_hz": 47900,
            "nyquist_hz": 48000,
            "is_fake_high_res": True,
            "estimated_mp3_bitrate": None,
            "wrapper_overrides": ["override:fake_hires_cutoff_at_nyquist(47900/48000)"],
            "error": None,
        }
    ]
    assert result.sensors[2]["status"] == "warn"
    assert result.sensors[2]["evidence"]["fake_hi_res"] is True
    assert result.sensors[2]["evidence"]["wrapper_overrides"] == [
        "override:fake_hires_cutoff_at_nyquist(47900/48000)"
    ]


def test_v2_enabled_fake_without_existing_goes_review_required(tmp_path, mocker):
    jid = "8bb5dade"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    (output_dir / "01.flac").write_bytes(b"flac")

    api = "http://lidarr/api/v1"
    key = "lidarr-key"
    server._jobs[jid] = {
        "id": jid,
        "status": "completed",
        "title": "Test Album",
        "percent": 100,
    }

    mocker.patch.dict(
        os.environ, {"LIDARR_API_URL": api, "V2_VERIFICATION_ENABLED": "true"}
    )
    mocker.patch.object(server, "_get_lidarr_key", return_value=key)
    mocker.patch.object(server, "_save_jobs")
    log_decision = mocker.patch.object(server, "_log_decision")

    def fake_get(url, **kwargs):
        if url == f"{api}/manualimport":
            return _response(payload=[_manualimport_item(jid)])
        if url == f"{api}/trackfile?albumId=20":
            return _response(payload=[])
        if url == f"{api}/queue?pageSize=200":
            return _response(payload={"records": [{"id": 44, "downloadId": jid}]})
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, **kwargs):
        if url == "http://host.docker.internal:8889/analyze":
            return _response(
                payload={
                    "overall_verdict": "FAKE_CERTAIN",
                    "file_count": 1,
                    "files": [],
                }
            )
        raise AssertionError(f"unexpected POST {url}")

    mocker.patch("requests.get", side_effect=fake_get)
    mocker.patch("requests.post", side_effect=fake_post)
    delete_mock = mocker.patch(
        "requests.delete", return_value=_response(status_code=204)
    )

    server._trigger_lidarr_import(jid, output_dir)

    assert server._jobs[jid]["status"] == "review_required"
    assert "V2 review required" in server._jobs[jid]["warning"]
    v2_result = log_decision.call_args.kwargs["v2_result"]
    assert v2_result.verification_decision == "REVIEW_REQUIRED"
    assert v2_result.import_outcome == "PENDING"
    # Review-hold invariant: a pending review stays visible to Lidarr (lidarr_hold,
    # not hidden) and must NOT clean the Lidarr queue — clearing it is what let Lidarr
    # re-grab the album. Cleanup happens only on promote/discard/expire.
    assert server._jobs[jid].get("lidarr_hold") is True
    assert not server._jobs[jid].get("hidden_from_lidarr")
    delete_mock.assert_not_called()


def test_v2_block_skips_manualimport_for_authentic_partial_codec_mismatch(
    tmp_path, mocker
):
    jid = "7fbd72dd"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    (output_dir / "08.flac").write_bytes(b"flac")

    api = "http://lidarr/api/v1"
    key = "lidarr-key"
    server._jobs[jid] = {
        "id": jid,
        "status": "completed",
        "title": "Andrea Bocelli - Season of Champions",
        "percent": 100,
        "codec_gate_skipped": 7,
    }

    mocker.patch.dict(
        os.environ, {"LIDARR_API_URL": api, "V2_VERIFICATION_ENABLED": "true"}
    )
    mocker.patch.object(server, "OUTPUT_BASE", tmp_path)
    mocker.patch.object(server, "BLOCKED_DECISIONS_DIR", tmp_path / "blocked")
    mocker.patch.object(server, "_get_lidarr_key", return_value=key)
    mocker.patch.object(server, "_save_jobs")
    log_decision = mocker.patch.object(server, "_log_decision")

    def fake_get(url, **kwargs):
        if url == f"{api}/manualimport":
            return _response(payload=[_manualimport_item(jid)])
        if url == f"{api}/trackfile?albumId=20":
            return _response(payload=[{"id": 1}])
        if url == f"{api}/history?pageSize=50&sortKey=date&sortDirection=descending":
            return _response(
                payload={
                    "records": [{"id": 123, "downloadId": jid, "eventType": "grabbed"}]
                }
            )
        if url == f"{api}/queue?pageSize=200":
            return _response(payload={"records": [{"id": 44, "downloadId": jid}]})
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, **kwargs):
        if url == "http://host.docker.internal:8889/analyze":
            return _response(
                payload={"overall_verdict": "AUTHENTIC", "file_count": 1, "files": []}
            )
        if url == f"{api}/history/failed/123":
            return _response(status_code=200, payload={})
        raise AssertionError(f"unexpected POST {url}")

    post_mock = mocker.patch("requests.post", side_effect=fake_post)
    mocker.patch("requests.get", side_effect=fake_get)
    delete_mock = mocker.patch(
        "requests.delete", return_value=_response(status_code=204)
    )

    server._trigger_lidarr_import(jid, output_dir)

    command_posts = [
        call for call in post_mock.call_args_list if call.args[0] == f"{api}/command"
    ]
    assert command_posts == []
    assert server._jobs[jid]["status"] == "failed"
    assert server._jobs[jid]["hidden_from_lidarr"] is True
    assert server._jobs[jid]["error"] == "v2 policy block: codec mismatch"
    v2_result = log_decision.call_args.kwargs["v2_result"]
    assert v2_result.verification_decision == "BLOCK"
    assert v2_result.import_outcome == "SKIPPED"
    assert v2_result.overrides == ["codec_mismatch"]
    delete_mock.assert_called_once()
