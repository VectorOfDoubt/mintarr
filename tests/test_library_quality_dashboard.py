"""Tests for F5.4 slice 5d library quality ranking views."""

from __future__ import annotations

import dashboard
import library_evidence
import server
import state_db

VALID_KEY = "tidalhires-test-api-key"


def _seed(trackfile_id: int, album_id: int, **overrides):
    row = {
        "trackfile_id": trackfile_id,
        "album_id": album_id,
        "path": f"/library/Artist/Album/{trackfile_id:02d}.flac",
        "status": "measured",
        "codec": "flac",
        "sample_rate": 96000,
        "bit_depth": 24,
        "lossless": True,
        "integrity_ok": True,
        "sensor_version": library_evidence.METADATA_SENSOR_VERSION,
        # default: integrity tier ran (so integrity-based buckets/ok work); the
        # metadata-only case overrides integrity_sensor_version to None.
        "integrity_sensor_version": library_evidence.INTEGRITY_SENSOR_VERSION,
    }
    row.update(overrides)
    state_db.upsert_library_evidence(row)


def _fresh(monkeypatch):
    monkeypatch.setattr(library_evidence, "is_measured_row_fresh", lambda _row: True)
    monkeypatch.setattr(library_evidence, "is_spectral_row_fresh", lambda _row: True)


def test_library_quality_requires_auth():
    client = server.app.test_client()

    assert client.get("/dashboard/v1/library-quality").status_code == 401
    assert client.get("/dashboard/v1/library-quality/partial").status_code == 401


def test_bucket_precedence_is_worst_first(monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.setattr(library_evidence, "spectral_enabled", lambda: True)
    _seed(1, 100, integrity_ok=False)
    state_db.update_library_spectral(
        {
            "trackfile_id": 1,
            "album_id": 100,
            "authentic": False,
            "spectral_status": "measured",
            "spectral_sensor_version": library_evidence.SPECTRAL_SENSOR_VERSION,
        }
    )
    _seed(2, 101)
    state_db.update_library_spectral(
        {
            "trackfile_id": 2,
            "album_id": 101,
            "authentic": False,
            "spectral_status": "measured",
            "spectral_sensor_version": library_evidence.SPECTRAL_SENSOR_VERSION,
        }
    )

    view = dashboard._build_library_quality_view()
    by_album = {album["album_id"]: album for album in view["albums"]}

    assert by_album[100]["primary_bucket"] == "invalid"
    assert by_album[101]["primary_bucket"] == "measured_fake"


def test_stale_evidence_gets_own_bucket_without_disk_stat(monkeypatch):
    def fail_live_freshness(_row):
        raise AssertionError("dashboard ranking must not stat library files")

    monkeypatch.setattr(library_evidence, "is_measured_row_fresh", fail_live_freshness)
    monkeypatch.setattr(library_evidence, "is_spectral_row_fresh", fail_live_freshness)
    _seed(3, 102, integrity_ok=True, sensor_version="older")

    view = dashboard._build_library_quality_view()

    assert view["albums"][0]["primary_bucket"] == "stale"
    assert view["albums"][0]["stale_count"] == 1


def test_all_non_error_buckets_are_classified(monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.setattr(library_evidence, "spectral_enabled", lambda: True)
    _seed(10, 110, status="unmeasured", reason="library not mounted")
    _seed(11, 111, codec="mp3", lossless=False, integrity_ok=None)  # genuinely lossy
    _seed(16, 115, bit_depth=16, sample_rate=44100)  # all lossless 16/44 → redbook
    _seed(12, 112, bit_depth=24, sample_rate=48000)
    _seed(13, 112, bit_depth=24, sample_rate=96000)
    _seed(14, 113, bit_depth=24, sample_rate=96000)
    _seed(15, 114, bit_depth=24, sample_rate=96000)
    state_db.update_library_spectral(
        {
            "trackfile_id": 15,
            "album_id": 114,
            "authentic": True,
            "spectral_status": "measured",
            "spectral_sensor_version": library_evidence.SPECTRAL_SENSOR_VERSION,
        }
    )

    view = dashboard._build_library_quality_view()
    by_album = {album["album_id"]: album["primary_bucket"] for album in view["albums"]}

    assert by_album[110] == "unmeasured"
    assert by_album[111] == "lossy"  # genuinely lossy — actionable upgrade
    assert by_album[115] == "redbook"  # 16/44 lossless — fine, not a defect
    assert by_album[112] == "mixed_tier"
    assert by_album[113] == "unknown_authenticity"
    assert by_album[114] == "ok"


def test_library_quality_json_uses_basenames_only(monkeypatch):
    _fresh(monkeypatch)
    _seed(4, 103, path="/mnt/music/Artist/Album/01 - One.flac")
    _seed(5, 103, path="H:\\Music\\Artist\\Album\\02 - Two.flac")

    resp = server.app.test_client().get(
        f"/dashboard/v1/library-quality?apikey={VALID_KEY}"
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    files = payload["albums"][0]["sample_files"]
    names = {f["filename"] for f in files}
    assert names == {"01 - One.flac", "02 - Two.flac"}
    serialized = resp.get_data(as_text=True)
    assert "/mnt/music" not in serialized
    assert "H:\\Music" not in serialized


def test_library_quality_includes_scan_progress(monkeypatch):
    _fresh(monkeypatch)
    _seed(6, 104)
    run = state_db.enqueue_library_scan(mode="cheap")
    state_db.update_library_scan_run_state(
        run["id"],
        "running",
        totals={"total_items": 10, "processed_items": 4, "measured_items": 3},
    )

    resp = server.app.test_client().get(
        f"/dashboard/v1/library-quality?apikey={VALID_KEY}"
    )

    assert resp.status_code == 200
    active = resp.get_json()["scan"]["active"]
    assert active["id"] == run["id"]
    assert active["mode"] == "cheap"
    assert active["processed_items"] == 4
    assert active["percent"] == 40.0


def test_library_quality_partial_renders(monkeypatch):
    _fresh(monkeypatch)
    _seed(7, 105, bit_depth=16, sample_rate=44100)

    resp = server.app.test_client().get(
        f"/dashboard/v1/library-quality/partial?apikey={VALID_KEY}"
    )

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Library quality" in body
    assert "cd quality (16/44)" in body.lower()  # redbook bucket chip renders
    assert "07.flac" in body


def test_library_quality_partial_has_full_console_layout(monkeypatch):
    _fresh(monkeypatch)
    _seed(17, 117, bit_depth=16, sample_rate=44100)

    resp = server.app.test_client().get(
        f"/dashboard/v1/library-quality/partial?apikey={VALID_KEY}&limit=50"
    )

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "library-quality-table" in body
    assert "library-quality-explainer" in body
    assert "Decision impact" in body
    assert "Operator action" in body
    assert "Artist / album" in body
    assert "Clear bucket" not in body


def test_library_quality_compact_partial_is_summary_only(monkeypatch):
    _fresh(monkeypatch)
    _seed(18, 118, bit_depth=16, sample_rate=44100)

    resp = server.app.test_client().get(
        f"/dashboard/v1/library-quality/partial?apikey={VALID_KEY}&compact=1"
    )

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Open Quality" in body
    assert "Use the Quality section for album-level inspection" in body
    assert "library-quality-list" not in body
    assert "library-quality-table" not in body


def test_checksum_mismatch_gets_own_bucket_not_invalid(monkeypatch):
    # The dogfood case: a stale-MD5 album (decodes, checksum failed) must land in
    # checksum_mismatch, never invalid and never ok.
    def fail_live_freshness(_row):
        raise AssertionError("dashboard ranking must not stat library files")

    monkeypatch.setattr(library_evidence, "is_measured_row_fresh", fail_live_freshness)
    monkeypatch.setattr(library_evidence, "is_spectral_row_fresh", fail_live_freshness)
    monkeypatch.setattr(library_evidence, "spectral_enabled", lambda: False)
    _seed(61, 960, integrity_ok=True, checksum_ok=0)  # stale MD5
    _seed(62, 961, integrity_ok=True, checksum_ok=1)  # clean (tier 3)

    view = dashboard._build_library_quality_view()
    by_album = {a["album_id"]: a for a in view["albums"]}

    assert by_album[960]["primary_bucket"] == "checksum_mismatch"
    assert by_album[960]["md5_mismatch_count"] == 1
    assert by_album[961]["primary_bucket"] == "ok"
    counts = {b["key"]: b["count"] for b in view["buckets"]}
    assert counts["checksum_mismatch"] == 1
    assert counts["invalid"] == 0


def test_nonstandard_flac_tags_get_cleanup_bucket_not_invalid(monkeypatch):
    # Dogfood case: ID3-contaminated FLAC trips strict flac -t but is an advisory
    # cleanup finding, not a replacement-driving decode-corrupt signal.
    monkeypatch.setattr(library_evidence, "is_measured_row_fresh", lambda _r: True)
    monkeypatch.setattr(library_evidence, "is_spectral_row_fresh", lambda _r: True)
    monkeypatch.setattr(library_evidence, "spectral_enabled", lambda: False)
    _seed(
        72,
        972,
        integrity_ok=None,
        checksum_ok=None,
        integrity_issue="nonstandard_flac_tags",
    )
    _seed(73, 973, integrity_ok=False, integrity_issue="decode_corrupt")

    view = dashboard._build_library_quality_view()
    by_album = {a["album_id"]: a for a in view["albums"]}
    counts = {b["key"]: b["count"] for b in view["buckets"]}

    assert by_album[972]["primary_bucket"] == "nonstandard_flac_tags"
    assert by_album[972]["nonstandard_flac_tags_count"] == 1
    assert by_album[972]["invalid_count"] == 0
    assert by_album[972]["integrity_known"] is False
    assert by_album[973]["primary_bucket"] == "invalid"
    assert counts["nonstandard_flac_tags"] == 1
    assert counts["invalid"] == 1


def test_checksum_mismatch_ranks_below_invalid(monkeypatch):
    # invalid (genuine corruption) outranks checksum_mismatch within an album.
    monkeypatch.setattr(library_evidence, "is_measured_row_fresh", lambda _r: True)
    monkeypatch.setattr(library_evidence, "is_spectral_row_fresh", lambda _r: True)
    _seed(71, 970, integrity_ok=False, checksum_ok=0)
    view = dashboard._build_library_quality_view(bucket="invalid")
    assert {a["album_id"] for a in view["albums"]} == {970}


def test_lossy_and_redbook_are_distinct_buckets(monkeypatch):
    # The dogfood point: a genuinely lossy file (OGG/MP3, tier 0) is an actionable
    # upgrade candidate; a 16/44 FLAC (tier 1) is fine CD quality. They must not
    # share a bucket.
    monkeypatch.setattr(library_evidence, "is_measured_row_fresh", lambda _r: True)
    monkeypatch.setattr(library_evidence, "is_spectral_row_fresh", lambda _r: True)
    monkeypatch.setattr(library_evidence, "spectral_enabled", lambda: False)
    _seed(80, 980, codec="ogg", lossless=False, integrity_ok=None, bit_depth=None)
    _seed(81, 981, bit_depth=16, sample_rate=44100)  # redbook FLAC

    view = dashboard._build_library_quality_view()
    by_album = {a["album_id"]: a["primary_bucket"] for a in view["albums"]}
    counts = {b["key"]: b["count"] for b in view["buckets"]}

    assert by_album[980] == "lossy"
    assert by_album[981] == "redbook"
    assert counts["lossy"] == 1
    assert counts["redbook"] == 1
    assert "lossy_or_low_tier" not in counts


def test_library_quality_reports_audio_tier_distribution(monkeypatch):
    monkeypatch.setattr(library_evidence, "is_measured_row_fresh", lambda _r: True)
    monkeypatch.setattr(library_evidence, "is_spectral_row_fresh", lambda _r: True)
    monkeypatch.setattr(library_evidence, "spectral_enabled", lambda: False)
    _seed(82, 982, codec="mp3", lossless=False, integrity_ok=None, bit_depth=None)
    _seed(83, 983, bit_depth=16, sample_rate=44100)
    _seed(84, 984, bit_depth=24, sample_rate=48000)
    _seed(85, 985, bit_depth=24, sample_rate=96000)
    _seed(86, 986, bit_depth=16, sample_rate=44100)
    _seed(87, 986, bit_depth=24, sample_rate=96000)

    view = dashboard._build_library_quality_view()
    tiers = {b["key"]: b["count"] for b in view["tiers"]}
    by_album = {a["album_id"]: a["tier_bucket"] for a in view["albums"]}

    assert tiers["lossy"] == 1
    assert tiers["redbook"] == 1
    assert tiers["lossless_24"] == 1
    assert tiers["hires"] == 1
    assert tiers["mixed"] == 1
    assert by_album[985] == "hires"


def test_metadata_only_album_is_integrity_unknown_not_ok(monkeypatch):
    # Guardrail §7: metadata measured (tier known) but integrity not verified must
    # NOT render as the fully-verified `ok` bucket.
    monkeypatch.setattr(library_evidence, "is_measured_row_fresh", lambda _r: True)
    monkeypatch.setattr(library_evidence, "is_spectral_row_fresh", lambda _r: True)
    monkeypatch.setattr(library_evidence, "spectral_enabled", lambda: False)
    # clean hi-res, integrity tier NOT run (no integrity_sensor_version)
    _seed(90, 990, bit_depth=24, sample_rate=96000, integrity_sensor_version=None)
    # same album but integrity verified → ok
    _seed(91, 991, bit_depth=24, sample_rate=96000)

    view = dashboard._build_library_quality_view()
    by_album = {a["album_id"]: a for a in view["albums"]}

    assert by_album[990]["primary_bucket"] == "integrity_unknown"
    assert by_album[990]["integrity_known"] is False
    assert by_album[991]["primary_bucket"] == "ok"
    assert by_album[991]["integrity_known"] is True


def test_metadata_only_does_not_read_as_invalid(monkeypatch):
    # A metadata-only row that happens to carry a stale integrity_ok must not be
    # read as invalid — integrity is gated on its own freshness.
    monkeypatch.setattr(library_evidence, "is_measured_row_fresh", lambda _r: True)
    monkeypatch.setattr(library_evidence, "is_spectral_row_fresh", lambda _r: True)
    monkeypatch.setattr(library_evidence, "spectral_enabled", lambda: False)
    _seed(92, 992, integrity_ok=False, integrity_sensor_version=None)  # stale integrity
    view = dashboard._build_library_quality_view()
    a = view["albums"][0]
    assert a["primary_bucket"] == "integrity_unknown"  # not "invalid"
    assert a["invalid_count"] == 0


def test_library_quality_rollup_counts_rows_beyond_first_10000(monkeypatch):
    # Regression for dogfood undercount: _build_library_quality_view used to call
    # list_library_evidence(limit=10000), so albums beyond that cap disappeared
    # from bucket counts. The aggregate must use all evidence rows.
    monkeypatch.setattr(library_evidence, "is_measured_row_fresh", lambda _r: True)
    monkeypatch.setattr(library_evidence, "is_spectral_row_fresh", lambda _r: True)
    monkeypatch.setattr(library_evidence, "spectral_enabled", lambda: False)
    for i in range(10001):
        _seed(
            100000 + i,
            200000 + i,
            path=f"/library/Artist/Album/{i:05d}.flac",
            bit_depth=16,
            sample_rate=44100,
        )
    _seed(
        300001,
        400001,
        path="/library/Artist/Late/01.flac",
        integrity_ok=False,
        integrity_issue="decode_corrupt",
    )

    view = dashboard._build_library_quality_view(bucket="invalid", limit=5)
    counts = {b["key"]: b["count"] for b in view["buckets"]}

    assert counts["invalid"] == 1
    assert view["filtered_albums"] == 1
    assert view["albums"][0]["album_id"] == 400001
