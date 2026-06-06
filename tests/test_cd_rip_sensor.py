"""Tests for the advisory CD-rip evidence sensor wiring (F5.3 slice 2)."""

from __future__ import annotations

import server

_EAC_OK = """Exact Audio Copy V1.6 from 23. October 2020

Track  1
     Accurately ripped (confidence 5)  [B8B7B8C9]
     Copy OK
Track  2
     Accurately ripped (confidence 8)  [1A2B3C4D]
     Copy OK

All tracks accurately ripped.
No errors occurred
"""

_EAC_ERR = """Exact Audio Copy V1.6 from 23. October 2020

Track  1
     Timing problem
     Copy aborted

There were errors
"""


def test_cd_rip_sensor_is_registered():
    from sensor_registry import default_registry

    sensor = default_registry.get("cd_rip_evidence")
    assert sensor.sensor_class == "source_specific_proof"
    assert sensor.stage == "source_specific"
    assert sensor.fail_policy == "skip"


def test_cd_rip_sensor_none_for_non_rip(tmp_path):
    (tmp_path / "01 track.flac").write_bytes(b"AUDIO")
    assert server._build_cd_rip_sensor(tmp_path) is None


def test_cd_rip_sensor_pass_for_clean_eac(tmp_path):
    (tmp_path / "rip.log").write_text(_EAC_OK)
    (tmp_path / "album.cue").write_text("FILE x WAVE\n")

    sensor = server._build_cd_rip_sensor(tmp_path)

    assert sensor is not None
    assert sensor["name"] == "cd_rip_evidence"
    assert sensor["class"] == "source_specific_proof"
    assert sensor["status"] == "pass"
    assert sensor["severity"] == "info"
    assert sensor["evidence_schema_version"] == "cd-rip-evidence-v1"
    assert sensor["policy_version"]  # set from SENSOR_POLICY_VERSION
    ar = sensor["evidence"]["accuraterip"]
    assert ar["accurate"] is True
    assert ar["matched"] <= ar["total"]
    assert sensor["evidence"]["has_cue"] is True
    # secret-safety: only a basename, no path separators
    assert "/" not in (sensor["evidence"]["log_filename"] or "")


def test_cd_rip_sensor_warn_for_errors(tmp_path):
    (tmp_path / "rip.log").write_text(_EAC_ERR)

    sensor = server._build_cd_rip_sensor(tmp_path)

    assert sensor is not None
    assert sensor["status"] == "warn"
    assert sensor["severity"] == "warning"
    assert sensor["severity"] != "blocker"  # advisory: never blocks
