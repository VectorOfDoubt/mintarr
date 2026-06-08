"""Tests for read-only library evidence measurement + storage (F5.4 slice 1)."""

from __future__ import annotations

import os

import library_evidence as le
import state_db


def _fake_prober(**probe):
    def _p(path):
        return {
            "codec": probe.get("codec", "flac"),
            "sample_rate": probe.get("sample_rate", 44100),
            "bit_depth": probe.get("bit_depth", 16),
            "channels": probe.get("channels", 2),
            "integrity_ok": probe.get("integrity_ok", True),
        }

    return _p


def _lib(tmp_path):
    root = tmp_path / "library"
    (root / "Artist" / "Album").mkdir(parents=True)
    f = root / "Artist" / "Album" / "01.flac"
    f.write_bytes(b"FLACDATA")
    return str(root)


# ---- path mapping + containment ----


def test_resolve_maps_lidarr_prefix(tmp_path):
    root = _lib(tmp_path)
    mapped, reason = le.resolve_library_path(
        "/music/Artist/Album/01.flac", library_root=root, lidarr_root="/music"
    )
    assert reason is None
    assert mapped == (tmp_path / "library" / "Artist" / "Album" / "01.flac")


def test_resolve_rejects_path_outside_lidarr_root(tmp_path):
    root = _lib(tmp_path)
    mapped, reason = le.resolve_library_path(
        "/other/Artist/Album/01.flac", library_root=root, lidarr_root="/music"
    )
    assert mapped is None
    assert "outside" in reason


def test_resolve_rejects_traversal(tmp_path):
    root = _lib(tmp_path)
    mapped, reason = le.resolve_library_path(
        "/music/../../etc/passwd", library_root=root, lidarr_root="/music"
    )
    assert mapped is None
    assert reason  # escaped root or not found


def test_resolve_rejects_symlink(tmp_path):
    root = _lib(tmp_path)
    outside = tmp_path / "outside.flac"
    outside.write_bytes(b"x")
    link = tmp_path / "library" / "Artist" / "Album" / "linked.flac"
    os.symlink(outside, link)
    mapped, reason = le.resolve_library_path(
        "/music/Artist/Album/linked.flac", library_root=root, lidarr_root="/music"
    )
    assert mapped is None
    assert "symlink" in reason or "escapes" in reason


def test_resolve_requires_lidarr_root(tmp_path):
    # Empty lidarr root must NOT map (wrong measurement is worse than none).
    root = _lib(tmp_path)
    mapped, reason = le.resolve_library_path(
        "/music/Artist/Album/01.flac", library_root=root, lidarr_root=""
    )
    assert mapped is None
    assert "lidarr root not configured" in reason


def test_resolve_missing_file(tmp_path):
    root = _lib(tmp_path)
    mapped, reason = le.resolve_library_path(
        "/music/Artist/Album/missing.flac", library_root=root, lidarr_root="/music"
    )
    assert mapped is None
    assert "not found" in reason


# ---- measurement ----


def test_measure_unmounted_is_unmeasured(monkeypatch):
    monkeypatch.delenv("MINTARR_LIBRARY_ROOT", raising=False)
    m = le.measure_trackfile("/music/x.flac")
    assert m.status == "unmeasured"
    assert "not mounted" in m.reason


def test_measure_flac_records_quality_vector(tmp_path):
    root = _lib(tmp_path)
    m = le.measure_trackfile(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="/music",
        prober=_fake_prober(codec="flac", sample_rate=96000, bit_depth=24),
    )
    assert m.status == "measured"
    assert m.codec == "flac"
    assert m.sample_rate == 96000
    assert m.bit_depth == 24
    assert m.lossless is True
    assert m.integrity_ok is True


def test_measure_lossy_is_not_lossless(tmp_path):
    root = _lib(tmp_path)
    m = le.measure_trackfile(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="/music",
        prober=_fake_prober(codec="mp3", integrity_ok=None),
    )
    assert m.status == "measured"
    assert m.lossless is False


def test_measure_no_audio_stream_is_unmeasured(tmp_path):
    # ffprobe returning no codec must be unmeasured, not measured-with-empty-vector.
    root = _lib(tmp_path)

    def _no_audio(path):
        return {"codec": "", "sample_rate": None, "bit_depth": None, "channels": None}

    m = le.measure_trackfile(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="/music",
        prober=_no_audio,
    )
    assert m.status == "unmeasured"
    assert "no audio stream" in m.reason


def test_measure_requires_lidarr_root(tmp_path):
    root = _lib(tmp_path)
    m = le.measure_trackfile(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="",
        prober=_fake_prober(),
    )
    assert m.status == "unmeasured"
    assert "lidarr root not configured" in m.reason


def test_measure_prober_failure_is_unmeasured(tmp_path):
    root = _lib(tmp_path)

    def _boom(path):
        raise RuntimeError("ffprobe blew up")

    m = le.measure_trackfile(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="/music",
        prober=_boom,
    )
    assert m.status == "unmeasured"
    assert "probe failed" in m.reason


# ---- storage ----


def test_library_evidence_storage_round_trip():
    state_db.upsert_library_evidence(
        {
            "trackfile_id": 42,
            "album_id": 7,
            "path": "/lib/a.flac",
            "size": 123,
            "mtime": 1.0,
            "status": "measured",
            "codec": "flac",
            "sample_rate": 96000,
            "bit_depth": 24,
            "channels": 2,
            "lossless": True,
            "integrity_ok": True,
            "sensor_version": le.SENSOR_VERSION,
            "evidence": {"x": 1},
        }
    )
    row = state_db.get_library_evidence(42)
    assert row["album_id"] == 7
    assert row["lossless"] == 1
    assert row["codec"] == "flac"
    assert state_db.get_album_library_evidence(7)[0]["trackfile_id"] == 42


def test_library_evidence_upsert_replaces():
    state_db.upsert_library_evidence(
        {"trackfile_id": 99, "album_id": 1, "status": "measured", "bit_depth": 16}
    )
    state_db.upsert_library_evidence(
        {"trackfile_id": 99, "album_id": 1, "status": "measured", "bit_depth": 24}
    )
    assert state_db.get_library_evidence(99)["bit_depth"] == 24


# ---- is_measured_row_fresh (F5.4 slice 3b decision-time guard) ----


def _fresh_row(tmp_path, monkeypatch):
    monkeypatch.setenv("MINTARR_LIBRARY_ROOT", str(tmp_path))
    f = tmp_path / "track.flac"
    f.write_bytes(b"DATA")
    st = f.stat()
    return {
        "status": "measured",
        "sensor_version": le.SENSOR_VERSION,
        "path": str(f),
        "size": st.st_size,
        "mtime": st.st_mtime,
    }


def test_fresh_row_is_fresh(tmp_path, monkeypatch):
    assert le.is_measured_row_fresh(_fresh_row(tmp_path, monkeypatch)) is True


def test_unmounted_is_not_fresh(tmp_path, monkeypatch):
    row = _fresh_row(tmp_path, monkeypatch)
    monkeypatch.delenv("MINTARR_LIBRARY_ROOT", raising=False)
    assert le.is_measured_row_fresh(row) is False


def test_old_sensor_version_is_not_fresh(tmp_path, monkeypatch):
    row = _fresh_row(tmp_path, monkeypatch)
    row["sensor_version"] = "mintarr-library-evidence OLD"
    assert le.is_measured_row_fresh(row) is False


def test_changed_file_is_not_fresh(tmp_path, monkeypatch):
    row = _fresh_row(tmp_path, monkeypatch)
    (tmp_path / "track.flac").write_bytes(b"DATA-CHANGED-bigger")  # size/mtime differ
    assert le.is_measured_row_fresh(row) is False


def test_unmeasured_row_is_not_fresh(tmp_path, monkeypatch):
    row = _fresh_row(tmp_path, monkeypatch)
    row["status"] = "unmeasured"
    assert le.is_measured_row_fresh(row) is False


# ---- F5.4 slice 4a: spectral (FLAC Detective) authenticity ----


def _echo_client(**entry):
    """A Detective client that reports the *exact* path it was asked to analyse.

    Mirrors the §8b deployment contract (Detective mounts the library at the same
    path Mintarr resolved), so the per-file result carries that exact path.
    """

    def _c(path):
        item = {"path": str(path)}
        item.update(entry)
        return {"overall_verdict": "AUTHENTIC", "files": [item]}

    return _c


def _client(files):
    """A Detective client returning a fixed (possibly wrong-path) payload."""

    def _c(path):
        return {"overall_verdict": "AUTHENTIC", "files": files}

    return _c


def test_spectral_disabled_is_unmeasured(tmp_path, monkeypatch):
    monkeypatch.delenv("MINTARR_LIBRARY_SPECTRAL", raising=False)
    root = _lib(tmp_path)
    m = le.measure_trackfile_spectral(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="/music",
        client=_echo_client(is_fake_high_res=False),
    )
    assert m.status == "unmeasured"
    assert m.authentic is None
    assert m.reason == "spectral disabled"


def test_spectral_genuine(tmp_path, monkeypatch):
    monkeypatch.setenv("MINTARR_LIBRARY_SPECTRAL", "true")
    root = _lib(tmp_path)
    m = le.measure_trackfile_spectral(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="/music",
        client=_echo_client(is_fake_high_res=False, verdict="AUTHENTIC"),
    )
    assert m.status == "measured"
    assert m.authentic is True


def test_spectral_fake_by_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("MINTARR_LIBRARY_SPECTRAL", "1")
    root = _lib(tmp_path)
    m = le.measure_trackfile_spectral(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="/music",
        client=_echo_client(is_fake_high_res=True),
    )
    assert m.status == "measured"
    assert m.authentic is False


def test_spectral_fake_by_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv("MINTARR_LIBRARY_SPECTRAL", "yes")
    root = _lib(tmp_path)
    m = le.measure_trackfile_spectral(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="/music",
        client=_echo_client(verdict="SUSPICIOUS"),
    )
    assert m.authentic is False
    assert m.verdict == "SUSPICIOUS"


def test_spectral_unmatched_file_is_unknown(tmp_path, monkeypatch):
    # §8b.1: a result for a DIFFERENT file must never be cached as this file's
    # authenticity — abstain to unknown.
    monkeypatch.setenv("MINTARR_LIBRARY_SPECTRAL", "on")
    root = _lib(tmp_path)
    m = le.measure_trackfile_spectral(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="/music",
        client=_client([{"path": "/x/99-other.flac", "is_fake_high_res": True}]),
    )
    assert m.status == "unmeasured"
    assert m.authentic is None
    assert m.reason == "no detective result for file"


def test_spectral_same_basename_other_album_is_unknown(tmp_path, monkeypatch):
    # Regression (#117 blocker): a same-basename file in a *different* album must
    # never match — exact path only, never basename. Otherwise one album's "01.flac"
    # could cache its authenticity against another's trackfile_id.
    monkeypatch.setenv("MINTARR_LIBRARY_SPECTRAL", "on")
    root = _lib(tmp_path)
    m = le.measure_trackfile_spectral(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="/music",
        client=_client(
            [{"path": f"{root}/Artist/Other Album/01.flac", "verdict": "FAKE"}]
        ),
    )
    assert m.status == "unmeasured"
    assert m.authentic is None
    assert m.reason == "no detective result for file"


def test_spectral_unreachable_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("MINTARR_LIBRARY_SPECTRAL", "true")
    root = _lib(tmp_path)

    def _boom(path):
        raise RuntimeError("detective down")

    m = le.measure_trackfile_spectral(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="/music",
        client=_boom,
    )
    assert m.status == "unmeasured"
    assert m.authentic is None
    assert m.reason == "detective unreachable"


def test_is_spectral_row_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("MINTARR_LIBRARY_ROOT", _lib(tmp_path))
    f = os.path.join(os.environ["MINTARR_LIBRARY_ROOT"], "Artist", "Album", "01.flac")
    st = os.stat(f)
    fresh = {
        "spectral_status": "measured",
        "spectral_sensor_version": le.SPECTRAL_SENSOR_VERSION,
        "path": f,
        "size": st.st_size,
        "mtime": st.st_mtime,
    }
    assert le.is_spectral_row_fresh(fresh) is True
    assert (
        le.is_spectral_row_fresh({**fresh, "spectral_sensor_version": "old"}) is False
    )
    assert le.is_spectral_row_fresh({**fresh, "spectral_status": "unmeasured"}) is False
    assert le.is_spectral_row_fresh({**fresh, "size": st.st_size + 1}) is False


# ---- F5.4 integrity split: flac -t classification (md5 mismatch vs decode error) ----


def test_classify_flac_clean_pass():
    assert le._classify_flac_test(0, "") == (True, True)


def test_classify_flac_unset_md5_is_unknown_checksum():
    # The real flac wording when STREAMINFO MD5 is unset (rc 0, nothing verified).
    real = "WARNING, cannot check MD5 signature since it was unset in the STREAMINFO"
    assert le._classify_flac_test(0, real) == (True, None)
    # older/alternate phrasings stay covered too
    assert le._classify_flac_test(0, "skipping MD5 check") == (True, None)


def test_classify_flac_md5_mismatch_is_valid_but_checksum_failed():
    # The dogfood case: decodes fine, only the stored MD5 is stale.
    ok, checksum = le._classify_flac_test(
        1, "track.flac: ERROR, MD5 signature mismatch"
    )
    assert ok is True
    assert checksum is False


def test_classify_flac_decode_error_is_invalid():
    ok, checksum = le._classify_flac_test(
        1, "track.flac: ERROR while decoding data\nlost sync"
    )
    assert ok is False
    assert checksum is None


def test_classify_flac_md5_mismatch_with_decode_error_is_invalid():
    # If real frame errors are present too, the conservative result is invalid.
    ok, checksum = le._classify_flac_test(
        1, "MD5 signature mismatch\nERROR: lost sync while decoding"
    )
    assert ok is False


def test_classify_flac_unrecognized_failure_is_invalid():
    # Unknown non-zero failure is never softened.
    ok, checksum = le._classify_flac_test(2, "some unexpected failure")
    assert ok is False


def test_measure_trackfile_passes_checksum_through(tmp_path):
    root = _lib(tmp_path)

    def _prober(path):
        return {
            "codec": "flac",
            "sample_rate": 44100,
            "bit_depth": 16,
            "channels": 2,
            "integrity_ok": True,
            "checksum_ok": False,
        }

    m = le.measure_trackfile(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="/music",
        prober=_prober,
    )
    assert m.status == "measured"
    assert m.integrity_ok is True
    assert m.checksum_ok is False


def test_library_evidence_checksum_round_trip():
    state_db.upsert_library_evidence(
        {
            "trackfile_id": 555,
            "album_id": 3,
            "status": "measured",
            "integrity_ok": True,
            "checksum_ok": False,
            "sensor_version": le.SENSOR_VERSION,
        }
    )
    row = state_db.get_library_evidence(555)
    assert row["integrity_ok"] == 1
    assert row["checksum_ok"] == 0


# ---- F5.4 scan tiers (slice 1a): metadata vs integrity measurement ----


def test_metadata_measure_leaves_integrity_unknown(tmp_path):
    root = _lib(tmp_path)

    def _meta(path):
        # mirrors _metadata_prober: ffprobe fields only, no integrity
        return {
            "codec": "flac",
            "sample_rate": 96000,
            "bit_depth": 24,
            "channels": 2,
            "integrity_ok": None,
            "checksum_ok": None,
        }

    m = le.measure_trackfile_metadata(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="/music",
        prober=_meta,
    )
    assert m.status == "measured"
    assert m.codec == "flac"
    assert m.bit_depth == 24
    assert m.lossless is True
    assert m.integrity_ok is None  # unknown — never OK from metadata alone
    assert m.checksum_ok is None


def test_integrity_measure_returns_integrity_dims(tmp_path):
    root = _lib(tmp_path)

    def _integ(path):
        return {"codec": "flac", "integrity_ok": True, "checksum_ok": False}

    m = le.measure_trackfile_integrity(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="/music",
        prober=_integ,
    )
    assert m.status == "measured"
    assert m.integrity_ok is True
    assert m.checksum_ok is False


def test_integrity_measure_unmounted_is_unmeasured(monkeypatch):
    monkeypatch.delenv("MINTARR_LIBRARY_ROOT", raising=False)
    m = le.measure_trackfile_integrity("/music/x.flac")
    assert m.status == "unmeasured"
    assert "not mounted" in m.reason


def test_integrity_measure_probe_failure_is_unmeasured(tmp_path):
    root = _lib(tmp_path)

    def _boom(path):
        raise RuntimeError("flac blew up")

    m = le.measure_trackfile_integrity(
        "/music/Artist/Album/01.flac",
        library_root=root,
        lidarr_root="/music",
        prober=_boom,
    )
    assert m.status == "unmeasured"
    assert "integrity probe failed" in m.reason


def test_metadata_prober_runs_no_flac_test(monkeypatch, tmp_path):
    # The whole point: metadata tier must not invoke flac -t (no full-file read).
    root = _lib(tmp_path)
    monkeypatch.setattr(
        le, "_run_ffprobe_fields", lambda p: {"codec": "flac", "sample_rate": 44100}
    )
    monkeypatch.setattr(
        le, "_run_flac_test", lambda p: (_ for _ in ()).throw(AssertionError("flac -t"))
    )
    probe = le._metadata_prober(root)  # any Path; ffprobe is patched
    assert probe["integrity_ok"] is None
    assert probe["checksum_ok"] is None


def test_integrity_prober_skips_flac_test_for_non_flac(monkeypatch):
    monkeypatch.setattr(le, "_run_ffprobe_fields", lambda p: {"codec": "mp3"})
    monkeypatch.setattr(
        le, "_run_flac_test", lambda p: (_ for _ in ()).throw(AssertionError("flac -t"))
    )
    probe = le._integrity_prober(le.Path("/x.mp3"))
    assert probe["integrity_ok"] is None  # non-FLAC ⇒ unknown, not OK


def test_is_integrity_row_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("MINTARR_LIBRARY_ROOT", _lib(tmp_path))
    f = os.path.join(os.environ["MINTARR_LIBRARY_ROOT"], "Artist", "Album", "01.flac")
    st = os.stat(f)
    fresh = {
        "integrity_sensor_version": le.INTEGRITY_SENSOR_VERSION,
        "path": f,
        "size": st.st_size,
        "mtime": st.st_mtime,
    }
    assert le.is_integrity_row_fresh(fresh) is True
    assert (
        le.is_integrity_row_fresh({**fresh, "integrity_sensor_version": "old"}) is False
    )
    assert le.is_integrity_row_fresh({**fresh, "size": st.st_size + 1}) is False


# ---- F5.4 scan tiers (slice 1b): integrity-tier storage ----


def test_integrity_sensor_column_exists_on_fresh_db():
    assert state_db._ensure_initialized()
    with state_db._connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(library_evidence)")}
    assert "integrity_sensor_version" in cols


def test_upsert_library_evidence_round_trips_integrity_sensor():
    state_db.upsert_library_evidence(
        {
            "trackfile_id": 600,
            "album_id": 4,
            "status": "measured",
            "codec": "flac",
            "integrity_ok": True,
            "checksum_ok": True,
            "sensor_version": le.METADATA_SENSOR_VERSION,
            "integrity_sensor_version": le.INTEGRITY_SENSOR_VERSION,
        }
    )
    row = state_db.get_library_evidence(600)
    assert row["integrity_sensor_version"] == le.INTEGRITY_SENSOR_VERSION


def test_update_library_integrity_layers_without_clobbering_metadata():
    # metadata tier writes the row (integrity unknown)…
    state_db.upsert_library_evidence(
        {
            "trackfile_id": 601,
            "album_id": 5,
            "status": "measured",
            "codec": "flac",
            "sample_rate": 96000,
            "bit_depth": 24,
            "lossless": True,
            "sensor_version": le.METADATA_SENSOR_VERSION,
        }
    )
    # …then the integrity tier layers its verdict on top.
    state_db.update_library_integrity(
        {
            "trackfile_id": 601,
            "album_id": 5,
            "integrity_ok": True,
            "checksum_ok": False,
            "integrity_sensor_version": le.INTEGRITY_SENSOR_VERSION,
        }
    )
    row = state_db.get_library_evidence(601)
    # integrity dims set…
    assert row["integrity_ok"] == 1
    assert row["checksum_ok"] == 0
    assert row["integrity_sensor_version"] == le.INTEGRITY_SENSOR_VERSION
    # …metadata preserved (not clobbered).
    assert row["bit_depth"] == 24
    assert row["sample_rate"] == 96000
    assert row["sensor_version"] == le.METADATA_SENSOR_VERSION


def test_update_library_integrity_creates_stub_when_metadata_absent():
    state_db.update_library_integrity(
        {
            "trackfile_id": 602,
            "album_id": 6,
            "integrity_ok": True,
            "checksum_ok": True,
            "integrity_sensor_version": le.INTEGRITY_SENSOR_VERSION,
        }
    )
    row = state_db.get_library_evidence(602)
    assert row["integrity_ok"] == 1
    assert row["integrity_sensor_version"] == le.INTEGRITY_SENSOR_VERSION
    assert row["bit_depth"] is None  # metadata not yet measured


def test_metadata_upsert_after_integrity_preserves_integrity():
    # The #134 blocker: integrity tier runs first, then the metadata tier writes
    # its (integrity-less) row — the integrity verdict must survive.
    state_db.update_library_integrity(
        {
            "trackfile_id": 610,
            "album_id": 7,
            "integrity_ok": True,
            "checksum_ok": False,
            "integrity_sensor_version": le.INTEGRITY_SENSOR_VERSION,
        }
    )
    state_db.upsert_library_metadata(
        {
            "trackfile_id": 610,
            "album_id": 7,
            "status": "measured",
            "codec": "flac",
            "sample_rate": 44100,
            "bit_depth": 16,
            "lossless": True,
            "sensor_version": le.METADATA_SENSOR_VERSION,
        }
    )
    row = state_db.get_library_evidence(610)
    # metadata written…
    assert row["bit_depth"] == 16
    assert row["sensor_version"] == le.METADATA_SENSOR_VERSION
    # …integrity preserved, not clobbered to NULL.
    assert row["integrity_ok"] == 1
    assert row["checksum_ok"] == 0
    assert row["integrity_sensor_version"] == le.INTEGRITY_SENSOR_VERSION


def test_metadata_upsert_leaves_integrity_unknown_on_new_row():
    state_db.upsert_library_metadata(
        {
            "trackfile_id": 611,
            "album_id": 7,
            "status": "measured",
            "codec": "flac",
            "bit_depth": 24,
            "sample_rate": 96000,
            "lossless": True,
            "sensor_version": le.METADATA_SENSOR_VERSION,
        }
    )
    row = state_db.get_library_evidence(611)
    assert row["bit_depth"] == 24
    assert row["integrity_ok"] is None  # unknown until integrity tier runs
    assert row["integrity_sensor_version"] is None
