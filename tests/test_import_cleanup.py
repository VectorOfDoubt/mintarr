"""Regression tests for Lidarr queue cleanup after successful import."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import ANY

import server
from verification import VerificationResult


def _response(status_code=200, payload=None, text=""):
    return SimpleNamespace(status_code=status_code, text=text, json=lambda: payload)


def _seed_job(jid: str, title: str = "Test Album"):
    server._jobs[jid] = {
        "id": jid,
        "status": "completed",
        "title": title,
        "percent": 100,
    }


def _assert_queue_cleanup(delete_mock, api: str, key: str, qid: int):
    delete_mock.assert_called_once_with(
        f"{api}/queue/{qid}",
        params={"removeFromClient": "false", "blocklist": "false"},
        headers={"X-Api-Key": key},
        timeout=10,
    )


def _release_family_items(jid: str, count: int = 4) -> list[dict]:
    return [
        {
            "path": f"/downloads/TidalHiRes/complete/{jid}/{idx:02d} - Track {idx}.flac",
            "artist": {"id": 10},
            "album": {"id": 20, "currentRelease": {"id": 30}},
            "albumReleaseId": 30,
            "tracks": [{"id": 100 + idx, "title": f"Track {idx}"}],
            "quality": {"quality": {"name": "FLAC 24bit"}},
            "rejections": [
                {
                    "reason": "Album match is not close enough: 70.1 % vs 80 %",
                    "type": "permanent",
                }
            ],
        }
        for idx in range(1, count + 1)
    ]


def test_rescue_place_and_rescan_can_be_disabled(monkeypatch, mocker):
    monkeypatch.setenv("TIDALHIRES_RESCUE_RESCAN_ENABLED", "false")
    get_mock = mocker.patch("requests.get")
    post_mock = mocker.patch("requests.post")

    ok = server._rescue_place_and_rescan(
        "abc12345",
        [
            {
                "albumId": 20,
                "artistId": 10,
                "path": "/downloads/TidalHiRes/complete/abc12345/01.flac",
            }
        ],
        "http://lidarr/api/v1",
        "lidarr-key",
    )

    assert ok is False
    get_mock.assert_not_called()
    post_mock.assert_not_called()


def test_download_job_stays_processing_until_lidarr_import(tmp_path, mocker):
    jid = "4478eaef8089"
    download_base = tmp_path / "downloads"
    output_base = tmp_path / "output"
    work_dir = download_base / jid
    album_dir = work_dir / "Albums" / "Artist - Album"

    mocker.patch.object(server, "DOWNLOAD_BASE", download_base)
    mocker.patch.object(server, "OUTPUT_BASE", output_base)
    mocker.patch.object(server, "_save_jobs")
    observed = {}

    def fake_run(args, **kwargs):
        if args[:2] == ["tidal-dl-ng", "dl"]:
            album_dir.mkdir(parents=True)
            (album_dir / "01.flac").write_bytes(b"flac")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    def fake_trigger(
        import_jid,
        output_dir,
        worker_job_id=None,
        *,
        source_type="tidal",
        target_album_id=None,
    ):
        observed.update(server._jobs[import_jid])
        observed["trigger_output_dir"] = str(output_dir)
        observed["worker_job_id"] = worker_job_id
        observed["trigger_source_type"] = source_type
        observed["trigger_target_album_id"] = target_album_id

    mocker.patch.object(server.subprocess, "run", side_effect=fake_run)
    mocker.patch.object(server, "_trigger_lidarr_import", side_effect=fake_trigger)
    server._jobs[jid] = {"id": jid, "status": "queued", "album_id": 123, "percent": 0}

    server._run_download_job(jid, 123)

    assert observed["status"] == "processing"
    assert "completed_at" not in observed
    assert observed["output_dir"] == str(output_base / jid)
    assert observed["trigger_output_dir"] == str(output_base / jid)
    server._jobs.pop(jid, None)


def test_manualimport_success_does_not_delete_lidarr_queue(tmp_path, mocker):
    jid = "592b388d"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    for idx in range(2):
        (output_dir / f"{idx + 1:02d}.flac").write_bytes(b"flac")

    api = "http://lidarr/api/v1"
    key = "lidarr-key"
    _seed_job(jid)
    manualimport_items = [
        {
            "path": f"/downloads/TidalHiRes/complete/{jid}/01.flac",
            "artist": {"id": 10},
            "album": {"id": 20, "currentRelease": {"id": 30}},
            "albumReleaseId": 30,
            "tracks": [{"id": 101}],
            "quality": {"quality": {"name": "FLAC 24bit"}},
            "rejections": [],
        },
        {
            "path": f"/downloads/TidalHiRes/complete/{jid}/02.flac",
            "artist": {"id": 10},
            "album": {"id": 20, "currentRelease": {"id": 30}},
            "albumReleaseId": 30,
            "tracks": [{"id": 102}],
            "quality": {"quality": {"name": "FLAC 24bit"}},
            "rejections": [],
        },
    ]

    mocker.patch.dict(os.environ, {"LIDARR_API_URL": api})
    mocker.patch.object(server, "_get_lidarr_key", return_value=key)
    mocker.patch.object(server, "_log_decision")
    mocker.patch.object(server, "_save_jobs")
    mocker.patch.object(server.time, "sleep")

    trackfile_calls = {"count": 0}

    def fake_get(url, **kwargs):
        if url == f"{api}/manualimport":
            return _response(payload=manualimport_items)
        if url == f"{api}/trackfile?albumId=20":
            trackfile_calls["count"] += 1
            if trackfile_calls["count"] == 1:
                return _response(payload=[])
            return _response(payload=[{"id": 1}, {"id": 2}])
        if url == f"{api}/queue?pageSize=200":
            return _response(payload={"records": [{"id": 99, "downloadId": jid}]})
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, **kwargs):
        if url == "http://host.docker.internal:8889/analyze":
            return _response(payload={"overall_verdict": "AUTHENTIC", "file_count": 2})
        if url == f"{api}/command":
            return _response(status_code=201, payload={"id": 123})
        raise AssertionError(f"unexpected POST {url}")

    get_mock = mocker.patch("requests.get", side_effect=fake_get)
    post_mock = mocker.patch("requests.post", side_effect=fake_post)
    delete_mock = mocker.patch(
        "requests.delete", return_value=_response(status_code=204)
    )

    server._trigger_lidarr_import(jid, output_dir)

    delete_mock.assert_not_called()
    assert get_mock.call_count >= 5
    assert post_mock.call_count == 2
    assert server._jobs[jid]["status"] == "completed"
    assert server._jobs[jid]["hidden_from_lidarr"] is True


def test_wait_for_manualimport_progress_requires_every_submitted_file(mocker):
    files = [
        {"path": "/downloads/job/01.flac"},
        {"path": "/downloads/job/02.flac"},
        {"path": "/downloads/job/03.flac"},
    ]
    observed_required = []

    def fake_count(jid, api, key, count_files, pre_counts, required_count, **kwargs):
        observed_required.append(required_count)
        return 2

    mocker.patch.object(server, "_count_manualimport_progress", side_effect=fake_count)
    sleep_mock = mocker.patch.object(server.time, "sleep")

    imported_count, required_count = server._wait_for_manualimport_progress(
        "jid-1",
        "http://lidarr/api/v1",
        "lidarr-key",
        files,
        {20: 0},
        timeout_s=2,
        interval_s=1,
    )

    assert required_count == 3
    assert imported_count == 2
    assert observed_required == [3, 3, 3]
    assert sleep_mock.call_count == 2


def test_wait_for_manualimport_progress_returns_when_all_files_accounted(mocker):
    files = [
        {"path": "/downloads/job/01.flac"},
        {"path": "/downloads/job/02.flac"},
        {"path": "/downloads/job/03.flac"},
    ]
    mocker.patch.object(server, "_count_manualimport_progress", side_effect=[2, 3])
    sleep_mock = mocker.patch.object(server.time, "sleep")

    imported_count, required_count = server._wait_for_manualimport_progress(
        "jid-1",
        "http://lidarr/api/v1",
        "lidarr-key",
        files,
        {20: 0},
        timeout_s=2,
        interval_s=1,
    )

    assert (imported_count, required_count) == (3, 3)
    sleep_mock.assert_called_once_with(1)


def test_soulseek_album_title_guard_handles_year_suffix_and_scene_mismatch():
    assert server._soulseek_album_title_compatible(
        "Artist - Album (2024) [Soulseek] [FLAC]",
        "Album",
    )
    assert not server._soulseek_album_title_compatible(
        "The_Pussycat_Dolls-PCD_Forever_(Deluxe_Edition)-16BIT-WEB-FLAC-2026-ENRiCH",
        "PCD",
    )


def test_infer_lidarr_target_album_id_from_queue(mocker):
    api = "http://lidarr/api/v1"
    key = "lidarr-key"
    jid = "queue-target"

    def fake_get(url, **kwargs):
        if url == f"{api}/queue?pageSize=200":
            return _response(
                payload={"records": [{"downloadId": jid, "albumId": 9829}]}
            )
        raise AssertionError(f"unexpected GET {url}")

    mocker.patch("requests.get", side_effect=fake_get)

    assert server._infer_lidarr_target_album_id(jid, api, key) == 9829


def test_infer_lidarr_target_album_id_from_grab_history(mocker):
    api = "http://lidarr/api/v1"
    key = "lidarr-key"
    jid = "history-target"

    def fake_get(url, **kwargs):
        if url == f"{api}/queue?pageSize=200":
            return _response(payload={"records": []})
        if url == f"{api}/history?pageSize=50&sortKey=date&sortDirection=descending":
            return _response(
                payload={
                    "records": [
                        {
                            "downloadId": jid,
                            "eventType": "downloadIgnored",
                            "albumId": 20,
                        },
                        {"downloadId": jid, "eventType": "grabbed", "albumId": 9829},
                    ]
                }
            )
        raise AssertionError(f"unexpected GET {url}")

    mocker.patch("requests.get", side_effect=fake_get)

    assert server._infer_lidarr_target_album_id(jid, api, key) == 9829


def test_soulseek_manualimport_album_title_mismatch_blocks_wrong_lidarr_album(
    tmp_path, mocker
):
    jid = "a9ead0f97861"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    for idx in range(2):
        (output_dir / f"{idx + 1:02d}.flac").write_bytes(b"flac")

    api = "http://lidarr/api/v1"
    key = "lidarr-key"
    _seed_job(
        jid,
        title="The_Pussycat_Dolls-PCD_Forever_(Deluxe_Edition)-16BIT-WEB-FLAC-2026-ENRiCH",
    )
    manualimport_items = [
        {
            "path": f"/downloads/TidalHiRes/complete/{jid}/01.flac",
            "artist": {"id": 10},
            "album": {"id": 20, "title": "PCD", "currentRelease": {"id": 30}},
            "albumReleaseId": 30,
            "tracks": [{"id": 101}],
            "quality": {"quality": {"name": "FLAC 24bit"}},
            "rejections": [],
        },
        {
            "path": f"/downloads/TidalHiRes/complete/{jid}/02.flac",
            "artist": {"id": 10},
            "album": {"id": 20, "title": "PCD", "currentRelease": {"id": 30}},
            "albumReleaseId": 30,
            "tracks": [{"id": 102}],
            "quality": {"quality": {"name": "FLAC 24bit"}},
            "rejections": [],
        },
    ]

    mocker.patch.dict(os.environ, {"LIDARR_API_URL": api})
    mocker.patch.object(server, "_get_lidarr_key", return_value=key)
    mocker.patch.object(server, "_save_jobs")
    log_decision = mocker.patch.object(server, "_log_decision")
    mocker.patch.object(server.time, "sleep")

    def fake_get(url, **kwargs):
        if url == f"{api}/manualimport":
            return _response(payload=manualimport_items)
        if url == f"{api}/trackfile?albumId=20":
            return _response(payload=[])
        if url == f"{api}/album/20":
            return _response(
                payload={"statistics": {"trackCount": 12, "trackFileCount": 0}}
            )
        if url == f"{api}/queue?pageSize=200":
            return _response(payload={"records": [{"id": 77, "downloadId": jid}]})
        if url == f"{api}/history?pageSize=50&sortKey=date&sortDirection=descending":
            return _response(
                payload={
                    "records": [
                        {"eventType": "grabbed", "downloadId": jid, "albumId": 9829},
                    ]
                }
            )
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, **kwargs):
        if url == "http://host.docker.internal:8889/analyze":
            return _response(payload={"overall_verdict": "AUTHENTIC", "file_count": 2})
        if url == f"{api}/command":
            raise AssertionError("Soulseek guard must stop before Lidarr ManualImport")
        raise AssertionError(f"unexpected POST {url}")

    mocker.patch("requests.get", side_effect=fake_get)
    post_mock = mocker.patch("requests.post", side_effect=fake_post)
    delete_mock = mocker.patch(
        "requests.delete", return_value=_response(status_code=204)
    )

    server._trigger_lidarr_import(jid, output_dir, source_type="soulseek")

    _assert_queue_cleanup(delete_mock, api, key, 77)
    assert post_mock.call_count == 1
    assert server._jobs[jid]["status"] == "failed"
    assert server._jobs[jid]["target_album_id"] == 9829
    assert "manualimport target album mismatch" in server._jobs[jid]["error"]
    assert "expected Lidarr albumId 9829" in server._jobs[jid]["error"]
    failed_record = log_decision.call_args_list[-1].kwargs["v2_result"]
    assert failed_record.import_outcome == "FAILED"
    assert failed_record.verification_decision == "ACCEPT"


def test_soulseek_manualimport_album_title_match_allows_import(tmp_path, mocker):
    jid = "45f4f5d5d1fd"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    for idx in range(2):
        (output_dir / f"{idx + 1:02d}.flac").write_bytes(b"flac")

    api = "http://lidarr/api/v1"
    key = "lidarr-key"
    _seed_job(
        jid,
        title="The_Pussycat_Dolls-PCD_Forever_(Deluxe_Edition)-16BIT-WEB-FLAC-2026-ENRiCH",
    )
    manualimport_items = [
        {
            "path": f"/downloads/TidalHiRes/complete/{jid}/01.flac",
            "artist": {"id": 10},
            "album": {
                "id": 9829,
                "title": "PCD Forever (Deluxe Edition)",
                "currentRelease": {"id": 30},
            },
            "albumReleaseId": 30,
            "tracks": [{"id": 101}],
            "quality": {"quality": {"name": "FLAC 24bit"}},
            "rejections": [],
        },
        {
            "path": f"/downloads/TidalHiRes/complete/{jid}/02.flac",
            "artist": {"id": 10},
            "album": {
                "id": 9829,
                "title": "PCD Forever (Deluxe Edition)",
                "currentRelease": {"id": 30},
            },
            "albumReleaseId": 30,
            "tracks": [{"id": 102}],
            "quality": {"quality": {"name": "FLAC 24bit"}},
            "rejections": [],
        },
    ]

    mocker.patch.dict(os.environ, {"LIDARR_API_URL": api})
    mocker.patch.object(server, "_get_lidarr_key", return_value=key)
    mocker.patch.object(server, "_log_decision")
    mocker.patch.object(server, "_save_jobs")
    mocker.patch.object(server.time, "sleep")

    trackfile_calls = {"count": 0}

    def fake_get(url, **kwargs):
        if url == f"{api}/manualimport":
            return _response(payload=manualimport_items)
        if url == f"{api}/trackfile?albumId=9829":
            trackfile_calls["count"] += 1
            if trackfile_calls["count"] == 1:
                return _response(payload=[])
            return _response(payload=[{"id": 1}, {"id": 2}])
        if url == f"{api}/album/9829":
            return _response(
                payload={"statistics": {"trackCount": 25, "trackFileCount": 0}}
            )
        if url == f"{api}/queue?pageSize=200":
            return _response(payload={"records": [{"id": 78, "downloadId": jid}]})
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, **kwargs):
        if url == "http://host.docker.internal:8889/analyze":
            return _response(payload={"overall_verdict": "AUTHENTIC", "file_count": 2})
        if url == f"{api}/command":
            return _response(status_code=201, payload={"id": 123})
        raise AssertionError(f"unexpected POST {url}")

    mocker.patch("requests.get", side_effect=fake_get)
    post_mock = mocker.patch("requests.post", side_effect=fake_post)
    delete_mock = mocker.patch(
        "requests.delete", return_value=_response(status_code=204)
    )

    server._trigger_lidarr_import(jid, output_dir, source_type="soulseek")

    delete_mock.assert_not_called()
    assert post_mock.call_count == 2
    assert server._jobs[jid]["status"] == "completed"


def test_manualimport_replace_success_uses_history_without_rescue(tmp_path, mocker):
    jid = "f0026bd878e9"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    for idx in range(2):
        (output_dir / f"{idx + 1:02d}.flac").write_bytes(b"flac")

    api = "http://lidarr/api/v1"
    key = "lidarr-key"
    _seed_job(jid)
    manualimport_items = [
        {
            "path": f"/downloads/TidalHiRes/complete/{jid}/01.flac",
            "artist": {"id": 10},
            "album": {"id": 20, "currentRelease": {"id": 30}},
            "albumReleaseId": 30,
            "tracks": [{"id": 101}],
            "quality": {"quality": {"name": "FLAC 24bit"}},
            "rejections": [],
        },
        {
            "path": f"/downloads/TidalHiRes/complete/{jid}/02.flac",
            "artist": {"id": 10},
            "album": {"id": 20, "currentRelease": {"id": 30}},
            "albumReleaseId": 30,
            "tracks": [{"id": 102}],
            "quality": {"quality": {"name": "FLAC 24bit"}},
            "rejections": [],
        },
    ]

    mocker.patch.dict(os.environ, {"LIDARR_API_URL": api})
    mocker.patch.object(server, "_get_lidarr_key", return_value=key)
    mocker.patch.object(server, "_log_decision")
    mocker.patch.object(server, "_save_jobs")
    mocker.patch.object(server.time, "sleep")
    rescue_mock = mocker.patch.object(server, "_rescue_place_and_rescan")

    def fake_get(url, **kwargs):
        if url == f"{api}/manualimport":
            return _response(payload=manualimport_items)
        if url == f"{api}/trackfile?albumId=20":
            return _response(payload=[{"id": 1}, {"id": 2}])
        if url == f"{api}/history?pageSize=100&sortKey=date&sortDirection=descending":
            return _response(
                payload={
                    "records": [
                        {"downloadId": jid, "eventType": "trackFileImported"},
                        {"downloadId": jid, "eventType": "trackFileImported"},
                    ]
                }
            )
        if url == f"{api}/queue?pageSize=200":
            return _response(payload={"records": [{"id": 44, "downloadId": jid}]})
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, **kwargs):
        if url == "http://host.docker.internal:8889/analyze":
            return _response(payload={"overall_verdict": "AUTHENTIC", "file_count": 2})
        if url == f"{api}/command":
            return _response(status_code=201, payload={"id": 123})
        raise AssertionError(f"unexpected POST {url}")

    mocker.patch("requests.get", side_effect=fake_get)
    mocker.patch("requests.post", side_effect=fake_post)
    delete_mock = mocker.patch(
        "requests.delete", return_value=_response(status_code=204)
    )

    server._trigger_lidarr_import(jid, output_dir)

    rescue_mock.assert_not_called()
    delete_mock.assert_not_called()
    assert server._jobs[jid]["status"] == "completed"
    assert server._jobs[jid]["hidden_from_lidarr"] is True


def test_manualimport_moved_files_counts_as_success_without_history(tmp_path, mocker):
    jid = "c335e496eee4"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    for idx in range(2):
        (output_dir / f"{idx + 1:02d}.flac").write_bytes(b"flac")

    api = "http://lidarr/api/v1"
    key = "lidarr-key"
    _seed_job(jid)
    manualimport_items = [
        {
            "path": f"/downloads/TidalHiRes/complete/{jid}/01.flac",
            "artist": {"id": 10},
            "album": {"id": 20, "currentRelease": {"id": 30}},
            "albumReleaseId": 30,
            "tracks": [{"id": 101}],
            "quality": {"quality": {"name": "FLAC 24bit"}},
            "rejections": [],
        },
        {
            "path": f"/downloads/TidalHiRes/complete/{jid}/02.flac",
            "artist": {"id": 10},
            "album": {"id": 20, "currentRelease": {"id": 30}},
            "albumReleaseId": 30,
            "tracks": [{"id": 102}],
            "quality": {"quality": {"name": "FLAC 24bit"}},
            "rejections": [],
        },
    ]

    mocker.patch.dict(os.environ, {"LIDARR_API_URL": api})
    mocker.patch.object(server, "_get_lidarr_key", return_value=key)
    mocker.patch.object(server, "_log_decision")
    mocker.patch.object(server, "_save_jobs")
    mocker.patch.object(server.time, "sleep")
    rescue_mock = mocker.patch.object(server, "_rescue_place_and_rescan")

    trackfile_calls = {"count": 0}

    def fake_get(url, **kwargs):
        if url == f"{api}/manualimport":
            return _response(payload=manualimport_items)
        if url == f"{api}/trackfile?albumId=20":
            trackfile_calls["count"] += 1
            if trackfile_calls["count"] == 1:
                return _response(payload=[])
            return _response(payload=[{"id": 1}, {"id": 2}])
        if url == f"{api}/history?pageSize=100&sortKey=date&sortDirection=descending":
            return _response(payload={"records": []})
        if url == f"{api}/queue?pageSize=200":
            return _response(payload={"records": [{"id": 45, "downloadId": jid}]})
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, **kwargs):
        if url == "http://host.docker.internal:8889/analyze":
            return _response(payload={"overall_verdict": "AUTHENTIC", "file_count": 2})
        if url == f"{api}/command":
            return _response(status_code=201, payload={"id": 123, "status": "failed"})
        raise AssertionError(f"unexpected POST {url}")

    mocker.patch("requests.get", side_effect=fake_get)
    mocker.patch("requests.post", side_effect=fake_post)
    delete_mock = mocker.patch(
        "requests.delete", return_value=_response(status_code=204)
    )

    server._trigger_lidarr_import(jid, output_dir)

    rescue_mock.assert_not_called()
    delete_mock.assert_not_called()
    assert server._jobs[jid]["status"] == "completed"
    assert server._jobs[jid]["hidden_from_lidarr"] is True


def test_all_rejected_manualimport_marks_failed_and_cleans_queue(tmp_path, mocker):
    jid = "71f4fc24259a"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    (output_dir / "01.flac").write_bytes(b"flac")

    api = "http://lidarr/api/v1"
    key = "lidarr-key"
    _seed_job(jid)
    rejected_items = [
        {
            "path": f"/downloads/TidalHiRes/complete/{jid}/01.flac",
            "artist": {"id": 10},
            "album": {"id": 20, "currentRelease": {"id": 30}},
            "tracks": [{"id": 101}],
            "quality": {"quality": {"name": "FLAC 24bit"}},
            "rejections": [
                {"reason": "Couldn't find similar album", "type": "permanent"}
            ],
        },
    ]

    mocker.patch.dict(os.environ, {"LIDARR_API_URL": api})
    mocker.patch.object(server, "_get_lidarr_key", return_value=key)
    save_mock = mocker.patch.object(server, "_save_jobs")
    log_decision = mocker.patch.object(server, "_log_decision")

    def fake_get(url, **kwargs):
        if url == f"{api}/manualimport":
            return _response(payload=rejected_items)
        if url == f"{api}/trackfile?albumId=20":
            return _response(payload=[])
        if url == f"{api}/queue?pageSize=200":
            return _response(payload={"records": [{"id": 77, "downloadId": jid}]})
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, **kwargs):
        if url == "http://host.docker.internal:8889/analyze":
            return _response(payload={"overall_verdict": "AUTHENTIC", "file_count": 1})
        raise AssertionError(f"unexpected POST {url}")

    mocker.patch("requests.get", side_effect=fake_get)
    mocker.patch("requests.post", side_effect=fake_post)
    delete_mock = mocker.patch(
        "requests.delete", return_value=_response(status_code=204)
    )

    server._trigger_lidarr_import(jid, output_dir)

    _assert_queue_cleanup(delete_mock, api, key, 77)
    assert server._jobs[jid]["status"] == "failed"
    assert server._jobs[jid]["error"] == "no importable files after verification"
    assert server._jobs[jid]["hidden_from_lidarr"] is True
    save_mock.assert_called()
    log_decision.assert_any_call(
        jid,
        v2_result=ANY,
        decision="IMPORT_FAILED",
        reason="no importable files after verification",
        verdict="AUTHENTIC",
        new_kbps=3000,
        existing_quality="nothing",
        existing_kbps=0,
        album_ids=[20],
        title="Test Album",
    )
    failed_record = log_decision.call_args_list[-1].kwargs["v2_result"]
    assert failed_record.import_outcome == "FAILED"
    assert failed_record.verification_decision == "ACCEPT"


def test_release_family_rejections_are_force_imported_after_verification(
    tmp_path, mocker
):
    jid = "93b7fc24259a"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    for idx in range(2):
        (
            output_dir / f"{idx + 1:02d} - Track {idx + 1} (2026 Remaster).flac"
        ).write_bytes(b"flac")

    api = "http://lidarr/api/v1"
    key = "lidarr-key"
    _seed_job(jid)
    manualimport_items = [
        {
            "path": f"/downloads/TidalHiRes/complete/{jid}/01 - Track 1 (2026 Remaster).flac",
            "artist": {"id": 10},
            "album": {"id": 20, "currentRelease": {"id": 30}},
            "albumReleaseId": 30,
            "tracks": [{"id": 101, "title": "Track 1"}],
            "quality": {"quality": {"name": "FLAC 24bit"}},
            "rejections": [
                {
                    "reason": "Album match is not close enough: 56.3 % vs 80 %",
                    "type": "permanent",
                },
                {"reason": "Has unmatched tracks", "type": "permanent"},
            ],
        },
        {
            "path": f"/downloads/TidalHiRes/complete/{jid}/02 - Track 2 (2026 Remaster).flac",
            "artist": {"id": 10},
            "album": {"id": 20, "currentRelease": {"id": 30}},
            "albumReleaseId": 30,
            "tracks": [{"id": 102, "title": "Track 2"}],
            "quality": {"quality": {"name": "FLAC 24bit"}},
            "rejections": [
                {
                    "reason": "Album match is not close enough: 56.3 % vs 80 %",
                    "type": "permanent",
                },
                {"reason": "Has unmatched tracks", "type": "permanent"},
            ],
        },
    ]

    mocker.patch.dict(os.environ, {"LIDARR_API_URL": api})
    mocker.patch.object(server, "_get_lidarr_key", return_value=key)
    mocker.patch.object(server, "_log_decision")
    mocker.patch.object(server, "_save_jobs")
    mocker.patch.object(server.time, "sleep")

    trackfile_calls = {"count": 0}

    def fake_get(url, **kwargs):
        if url == f"{api}/manualimport":
            return _response(payload=manualimport_items)
        if url == f"{api}/trackfile?albumId=20":
            trackfile_calls["count"] += 1
            if trackfile_calls["count"] == 1:
                return _response(payload=[])
            return _response(payload=[{"id": 1}, {"id": 2}])
        if url == f"{api}/queue?pageSize=200":
            return _response(payload={"records": [{"id": 55, "downloadId": jid}]})
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, **kwargs):
        if url == "http://host.docker.internal:8889/analyze":
            return _response(payload={"overall_verdict": "AUTHENTIC", "file_count": 2})
        if url == f"{api}/command":
            files = kwargs["json"]["files"]
            assert len(files) == 2
            assert files[0]["trackIds"] == [101]
            assert files[1]["trackIds"] == [102]
            return _response(status_code=201, payload={"id": 123})
        raise AssertionError(f"unexpected POST {url}")

    mocker.patch("requests.get", side_effect=fake_get)
    post_mock = mocker.patch("requests.post", side_effect=fake_post)
    delete_mock = mocker.patch(
        "requests.delete", return_value=_response(status_code=204)
    )

    server._trigger_lidarr_import(jid, output_dir)

    delete_mock.assert_not_called()
    assert post_mock.call_count == 2
    assert server._jobs[jid]["status"] == "completed"
    assert server._jobs[jid]["hidden_from_lidarr"] is True


def test_release_switch_default_disabled_does_not_put_album(
    tmp_path, mocker, monkeypatch
):
    jid = "swdisabled1"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    for idx in range(1, 5):
        (output_dir / f"{idx:02d} - Track {idx}.flac").write_bytes(b"flac")

    api = "http://lidarr/api/v1"
    key = "lidarr-key"
    _seed_job(jid)
    manualimport_items = _release_family_items(jid, count=4)
    monkeypatch.delenv("MINTARR_RELEASE_SWITCH_STRATEGY", raising=False)
    mocker.patch.dict(os.environ, {"LIDARR_API_URL": api})
    mocker.patch.object(server, "_get_lidarr_key", return_value=key)
    mocker.patch.object(server, "_log_decision")
    mocker.patch.object(server, "_save_jobs")

    trackfile_calls = {"count": 0}

    def fake_get(url, **kwargs):
        if url == f"{api}/manualimport":
            return _response(payload=manualimport_items)
        if url == f"{api}/trackfile?albumId=20":
            trackfile_calls["count"] += 1
            if trackfile_calls["count"] == 1:
                return _response(payload=[])
            return _response(payload=[{"id": idx} for idx in range(4)])
        if url == f"{api}/album/20":
            return _response(payload={"id": 20, "statistics": {"trackCount": 4}})
        if url == f"{api}/queue?pageSize=200":
            return _response(payload={"records": [{"id": 55, "downloadId": jid}]})
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, **kwargs):
        if url == "http://host.docker.internal:8889/analyze":
            return _response(payload={"overall_verdict": "AUTHENTIC", "file_count": 4})
        if url == f"{api}/command":
            return _response(status_code=201, payload={"id": 123})
        raise AssertionError(f"unexpected POST {url}")

    mocker.patch("requests.get", side_effect=fake_get)
    mocker.patch("requests.post", side_effect=fake_post)
    put_mock = mocker.patch("requests.put")
    delete_mock = mocker.patch(
        "requests.delete", return_value=_response(status_code=204)
    )

    server._trigger_lidarr_import(jid, output_dir)

    put_mock.assert_not_called()
    delete_mock.assert_not_called()
    assert server._jobs[jid]["status"] == "completed"


def test_release_switch_disallowed_for_audio_block_and_wrong_album():
    blocked = VerificationResult(
        jid="switchblk",
        score=0,
        verification_decision="BLOCK",
        overrides=["codec_mismatch"],
    )
    wrong_album = VerificationResult(
        jid="switchwrong",
        score=100,
        verification_decision="ACCEPT",
        identity_decision="WRONG_ALBUM",
    )

    assert server._release_switch_allowed_for_result(blocked) is False
    assert server._release_switch_allowed_for_result(wrong_album) is False


def test_release_switch_auto_restores_previous_release_on_import_failure(
    tmp_path, mocker, monkeypatch
):
    jid = "swautorestore"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    for idx in range(1, 5):
        (output_dir / f"{idx:02d} - Track {idx}.flac").write_bytes(b"flac")

    api = "http://lidarr/api/v1"
    key = "lidarr-key"
    _seed_job(jid)
    audit_log = tmp_path / "release_switch_audit.jsonl"
    manualimport_calls = {"count": 0}
    album_payload = {
        "id": 20,
        "currentRelease": {"id": 30},
        "albumReleaseId": 30,
        "statistics": {"trackCount": 4, "trackFileCount": 0},
        "releases": [
            {"id": 30, "trackCount": 2, "monitored": True},
            {"id": 40, "trackCount": 4, "monitored": False},
        ],
    }

    monkeypatch.setenv("MINTARR_RELEASE_SWITCH_STRATEGY", "auto_high_confidence")
    mocker.patch.dict(os.environ, {"LIDARR_API_URL": api})
    mocker.patch.object(server, "RELEASE_SWITCH_AUDIT_LOG", audit_log)
    mocker.patch.object(server, "_get_lidarr_key", return_value=key)
    mocker.patch.object(server, "_log_decision")
    mocker.patch.object(server, "_save_jobs")
    mocker.patch.object(server.time, "sleep")

    def fake_get(url, **kwargs):
        if url == f"{api}/manualimport":
            manualimport_calls["count"] += 1
            if manualimport_calls["count"] == 1:
                return _response(payload=_release_family_items(jid, count=4))
            return _response(payload=[])
        if url == f"{api}/trackfile?albumId=20":
            return _response(payload=[])
        if url == f"{api}/album/20":
            return _response(payload=json.loads(json.dumps(album_payload)))
        if url == f"{api}/track?albumReleaseId=30":
            return _response(
                payload=[
                    {"id": 101, "title": "Intro"},
                    {"id": 102, "title": "Outro"},
                ]
            )
        if url == f"{api}/track?albumReleaseId=40":
            return _response(
                payload=[
                    {"id": 101, "title": "Track 1"},
                    {"id": 102, "title": "Track 2"},
                    {"id": 103, "title": "Track 3"},
                    {"id": 104, "title": "Track 4"},
                ]
            )
        if url == f"{api}/queue?pageSize=200":
            return _response(payload={"records": [{"id": 77, "downloadId": jid}]})
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, **kwargs):
        if url == "http://host.docker.internal:8889/analyze":
            return _response(payload={"overall_verdict": "AUTHENTIC", "file_count": 4})
        raise AssertionError(f"unexpected POST {url}")

    mocker.patch("requests.get", side_effect=fake_get)
    mocker.patch("requests.post", side_effect=fake_post)
    put_mock = mocker.patch("requests.put", return_value=_response(status_code=200))
    delete_mock = mocker.patch(
        "requests.delete", return_value=_response(status_code=204)
    )

    server._trigger_lidarr_import(jid, output_dir)

    assert put_mock.call_count == 2
    switched_album = put_mock.call_args_list[0].kwargs["json"]
    restored_album = put_mock.call_args_list[1].kwargs["json"]
    assert switched_album["albumReleaseId"] == 40
    assert restored_album["albumReleaseId"] == 30
    events = [json.loads(line) for line in audit_log.read_text().splitlines()]
    assert [event["event"] for event in events] == ["attempt", "applied", "restored"]
    assert events[-1]["trigger"] == "manualimport_zero_imported"
    _assert_queue_cleanup(delete_mock, api, key, 77)


def test_validator_fail_closed_cleans_lidarr_queue(tmp_path, mocker):
    jid = "91f4fc24259a"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    (output_dir / "01.flac").write_bytes(b"flac")

    api = "http://lidarr/api/v1"
    key = "lidarr-key"
    _seed_job(jid)
    manualimport_items = [
        {
            "path": f"/downloads/TidalHiRes/complete/{jid}/01.flac",
            "artist": {"id": 10},
            "album": {"id": 20, "currentRelease": {"id": 30}},
            "tracks": [{"id": 101}],
            "quality": {"quality": {"name": "FLAC 24bit"}},
            "rejections": [],
        },
    ]

    mocker.patch.dict(os.environ, {"LIDARR_API_URL": api})
    mocker.patch.object(server, "_get_lidarr_key", return_value=key)
    mocker.patch.object(server, "_save_jobs")
    mocker.patch.object(server, "_log_decision")

    def fake_get(url, **kwargs):
        if url == f"{api}/manualimport":
            return _response(payload=manualimport_items)
        if url == f"{api}/trackfile?albumId=20":
            return _response(payload=[])
        if url == f"{api}/history?pageSize=50&sortKey=date&sortDirection=descending":
            return _response(payload={"records": []})
        if url == f"{api}/queue?pageSize=200":
            return _response(payload={"records": [{"id": 66, "downloadId": jid}]})
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, **kwargs):
        if url == "http://host.docker.internal:8889/analyze":
            return _response(status_code=500, text="validator down")
        raise AssertionError(f"unexpected POST {url}")

    mocker.patch("requests.get", side_effect=fake_get)
    mocker.patch("requests.post", side_effect=fake_post)
    delete_mock = mocker.patch(
        "requests.delete", return_value=_response(status_code=204)
    )

    server._trigger_lidarr_import(jid, output_dir)

    _assert_queue_cleanup(delete_mock, api, key, 66)
    assert server._jobs[jid]["status"] == "failed"
    assert "validator unavailable" in server._jobs[jid]["error"]
    assert server._jobs[jid]["hidden_from_lidarr"] is True


def test_rescue_failed_marks_failed_and_cleans_queue(tmp_path, mocker):
    jid = "8bb5dade010b"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    (output_dir / "01.flac").write_bytes(b"flac")

    api = "http://lidarr/api/v1"
    key = "lidarr-key"
    _seed_job(jid)
    manualimport_items = [
        {
            "path": f"/downloads/TidalHiRes/complete/{jid}/01.flac",
            "artist": {"id": 10},
            "album": {"id": 20, "currentRelease": {"id": 30}},
            "albumReleaseId": 30,
            "tracks": [{"id": 101}],
            "quality": {"quality": {"name": "FLAC 24bit"}},
            "rejections": [],
        },
    ]

    mocker.patch.dict(os.environ, {"LIDARR_API_URL": api})
    mocker.patch.object(server, "_get_lidarr_key", return_value=key)
    mocker.patch.object(server, "_save_jobs")
    log_decision = mocker.patch.object(server, "_log_decision")
    mocker.patch.object(server, "_rescue_place_and_rescan", return_value=False)
    mocker.patch.object(server.time, "sleep")

    trackfile_calls = {"count": 0}

    def fake_get(url, **kwargs):
        if url == f"{api}/manualimport":
            return _response(payload=manualimport_items)
        if url == f"{api}/trackfile?albumId=20":
            trackfile_calls["count"] += 1
            return _response(payload=[])
        if url == f"{api}/queue?pageSize=200":
            return _response(payload={"records": [{"id": 88, "downloadId": jid}]})
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, **kwargs):
        if url == "http://host.docker.internal:8889/analyze":
            return _response(payload={"overall_verdict": "AUTHENTIC", "file_count": 1})
        if url == f"{api}/command":
            return _response(status_code=201, payload={"id": 123})
        raise AssertionError(f"unexpected POST {url}")

    mocker.patch("requests.get", side_effect=fake_get)
    mocker.patch("requests.post", side_effect=fake_post)
    delete_mock = mocker.patch(
        "requests.delete", return_value=_response(status_code=204)
    )

    server._trigger_lidarr_import(jid, output_dir)

    _assert_queue_cleanup(delete_mock, api, key, 88)
    assert server._jobs[jid]["status"] == "failed"
    assert server._jobs[jid]["error"] == "manualimport and rescue failed"
    assert trackfile_calls["count"] >= 3
    log_decision.assert_any_call(
        jid,
        v2_result=ANY,
        decision="IMPORT_FAILED",
        reason="manualimport and rescue failed",
        verdict="AUTHENTIC",
        new_kbps=3000,
        existing_quality="nothing",
        existing_kbps=0,
        album_ids=[20],
        title="Test Album",
    )
    failed_record = log_decision.call_args_list[-1].kwargs["v2_result"]
    assert failed_record.import_outcome == "FAILED"
    assert failed_record.verification_decision == "ACCEPT"


def test_pending_lidarr_manualimport_does_not_rescue_or_fail(tmp_path, mocker):
    jid = "90707e7b2a9c"
    output_dir = tmp_path / jid
    output_dir.mkdir()
    (output_dir / "01.flac").write_bytes(b"flac")

    api = "http://lidarr/api/v1"
    key = "lidarr-key"
    _seed_job(jid)
    manualimport_items = [
        {
            "path": f"/downloads/TidalHiRes/complete/{jid}/01.flac",
            "artist": {"id": 10},
            "album": {"id": 20, "currentRelease": {"id": 30}},
            "albumReleaseId": 30,
            "tracks": [{"id": 101}],
            "quality": {"quality": {"name": "FLAC 24bit"}},
            "rejections": [],
        },
    ]

    mocker.patch.dict(os.environ, {"LIDARR_API_URL": api})
    mocker.patch.object(server, "_get_lidarr_key", return_value=key)
    mocker.patch.object(server, "_save_jobs")
    log_decision = mocker.patch.object(server, "_log_decision")
    rescue_mock = mocker.patch.object(server, "_rescue_place_and_rescan")
    mocker.patch.object(server.time, "sleep")

    def fake_get(url, **kwargs):
        if url == f"{api}/manualimport":
            return _response(payload=manualimport_items)
        if url == f"{api}/trackfile?albumId=20":
            return _response(payload=[])
        if url == f"{api}/history?pageSize=100&sortKey=date&sortDirection=descending":
            return _response(payload={"records": []})
        if url == f"{api}/command/123":
            return _response(payload={"id": 123, "status": "queued"})
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, **kwargs):
        if url == "http://host.docker.internal:8889/analyze":
            return _response(payload={"overall_verdict": "AUTHENTIC", "file_count": 1})
        if url == f"{api}/command":
            return _response(status_code=201, payload={"id": 123, "status": "queued"})
        raise AssertionError(f"unexpected POST {url}")

    mocker.patch("requests.get", side_effect=fake_get)
    mocker.patch("requests.post", side_effect=fake_post)

    server._trigger_lidarr_import(jid, output_dir)

    rescue_mock.assert_not_called()
    assert server._jobs[jid]["status"] == "completed"
    assert server._jobs[jid]["warning"] == "Lidarr ManualImport pending"
    pending_record = log_decision.call_args_list[-1].kwargs["v2_result"]
    assert pending_record.import_outcome == "PENDING"
