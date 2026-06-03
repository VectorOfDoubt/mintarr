"""Pure V2 verification policy.

No filesystem, network, time, or random dependencies belong in this module.
It is intentionally safe to unit-test without importing the Flask server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

VerificationDecision = Literal[
    "ACCEPT", "ACCEPT_PROVISIONAL", "REVIEW_REQUIRED", "BLOCK"
]
ImportOutcome = Literal["MANUAL_IMPORTED", "RESCUED", "FAILED", "SKIPPED", "PENDING"]


@dataclass
class VerificationResult:
    """Verification decision plus the separate outcome of the import attempt."""

    jid: str
    score: int
    verification_decision: VerificationDecision
    import_outcome: ImportOutcome = "PENDING"
    components: dict[str, int] = field(default_factory=dict)
    overrides: list[str] = field(default_factory=list)
    verdict: str = "UNKNOWN"
    new_kbps: int = 0
    existing_kbps: int = 0
    existing_label: str = "nothing"
    album_ids: list[int] = field(default_factory=list)
    title: str = ""
    sensors: list[dict[str, Any]] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    timestamp: float = 0.0
    timestamp_iso: str = ""

    def to_decisions_log(self) -> dict[str, Any]:
        """Return the additive decisions.jsonl record for V2."""
        return {
            "ts": self.timestamp,
            "ts_iso": self.timestamp_iso,
            "jid": self.jid,
            "decision": self._legacy_decision_label(),
            "verdict": self.verdict,
            "new_kbps": self.new_kbps,
            "existing_quality": self.existing_label,
            "existing_kbps": self.existing_kbps,
            "album_ids": self.album_ids,
            "title": self.title,
            "reason": self._legacy_reason(),
            "v2_verification_decision": self.verification_decision,
            "v2_import_outcome": self.import_outcome,
            "v2_score": self.score,
            "v2_components": self.components,
            "v2_overrides": self.overrides,
            "sensors": self.sensors,
            "files": self.files,
        }

    def _legacy_decision_label(self) -> str:
        if self.import_outcome == "RESCUED":
            return "RESCUED_BY_RESCAN"
        if self.verification_decision == "ACCEPT":
            return (
                "IMPORTED_AUTHENTIC"
                if self.verdict == "AUTHENTIC"
                else "IMPORTED_WITH_WARNING"
            )
        if self.verification_decision == "ACCEPT_PROVISIONAL":
            return "IMPORTED_DESPITE_FAKE"
        if self.verification_decision == "BLOCK":
            return "BLOCKED"
        if self.verification_decision == "REVIEW_REQUIRED":
            return "REVIEW_REQUIRED"
        return "UNKNOWN"

    def _legacy_reason(self) -> str:
        if "codec_mismatch" in self.overrides:
            return "codec mismatch"
        if "flac_t_fail" in self.overrides:
            return "flac -t failed"
        if "no_audio_files" in self.overrides:
            return "no audio files downloaded"
        if "no_flac_files" in self.overrides:
            return "no flac files found"
        if "validator_error" in self.overrides:
            return "validator unavailable"
        if self.existing_kbps == 0:
            return "nothing pre-existing"
        if self.new_kbps > self.existing_kbps * 1.2:
            return f"upgrade from {self.existing_label}"
        return f"score={self.score}"


def compute_components(
    ffprobe_ok: bool,
    flac_t_ok: bool,
    detective_verdict: str,
    complete_album: bool,
) -> dict[str, int]:
    """Return the score components. Maximum sum is 100."""
    detective_scores = {
        "AUTHENTIC": 35,
        "WARNING": 18,
        "SUSPICIOUS": 5,
        "FAKE_CERTAIN": 0,
        "FAKE": 0,
    }
    return {
        "ffprobe": 25 if ffprobe_ok else 0,
        "flac_t": 25 if flac_t_ok else 0,
        "detective": detective_scores.get(detective_verdict, 0),
        "complete": 15 if complete_album else 0,
    }


def apply_overrides(
    components: dict[str, int],
    codec_mismatch: bool,
    flac_t_failed: bool,
    validator_error: bool,
    fake_hi_res: bool,
    detective_verdict: str,
) -> tuple[int, list[str]]:
    """Return score plus hard/capping overrides."""
    overrides: list[str] = []

    if codec_mismatch:
        overrides.append("codec_mismatch")
        return 0, overrides
    if flac_t_failed:
        overrides.append("flac_t_fail")
        return 0, overrides
    if validator_error:
        overrides.append("validator_error")
        return 0, overrides

    score = sum(components.values())

    if detective_verdict == "SUSPICIOUS" and score > 60:
        score = 60
        overrides.append("suspicious_cap_60")

    if fake_hi_res:
        overrides.append("fake_hi_res")

    return score, overrides


def decide(
    score: int,
    existing_kbps: int,
    new_kbps: int,
    detective_verdict: str,
    overrides: list[str],
    *,
    existing_track_count: int = 0,
    new_track_count: int = 0,
    expected_track_count: int = 0,
) -> VerificationDecision:
    """Convert score, context, and overrides into a verification decision.

    Completeness rule (added 2026-05-23, V2.1): when an existing album in the
    library is incomplete and the new candidate would fill it, the candidate
    can be accepted even if quality drops or score is low. Reasoning: a complete
    album with lower quality often beats a partial library state, since Lidarr
    will keep searching for upgrades via Y+. fake_hi_res and FAKE_CERTAIN still
    route to REVIEW_REQUIRED so the user retains veto power.

    track-count parameters default to 0 (unknown), making the new rule a no-op
    when caller can't supply Lidarr's album-statistics. All track_count=0 means
    we fall through to pre-existing kbps-based logic.
    """
    is_complete_upgrade = (
        expected_track_count > 0
        and existing_track_count < expected_track_count
        and new_track_count >= expected_track_count
    )

    if "codec_mismatch" in overrides:
        return "BLOCK"
    if "flac_t_fail" in overrides:
        return "BLOCK"
    if "validator_error" in overrides:
        return "BLOCK"

    # fake_hi_res always routes to review regardless of completeness — user must accept the risk
    if "fake_hi_res" in overrides:
        return "REVIEW_REQUIRED"

    # FAKE_CERTAIN: completeness makes it reviewable even if existing > 0
    if detective_verdict in ("FAKE_CERTAIN", "FAKE"):
        if existing_kbps > 0 and not is_complete_upgrade:
            return "BLOCK"
        return "REVIEW_REQUIRED"

    # Lossless-beskyttelse: completeness vinner hvis eksisterende lossless er ufullstendig
    if existing_kbps >= 1400 and detective_verdict == "SUSPICIOUS":
        if is_complete_upgrade:
            return "ACCEPT_PROVISIONAL"
        return "BLOCK"

    if detective_verdict == "SUSPICIOUS":
        is_kbps_upgrade = (existing_kbps == 0) or (new_kbps > existing_kbps * 1.2)
        if is_kbps_upgrade or is_complete_upgrade:
            return "ACCEPT_PROVISIONAL"
        return "BLOCK"

    if score < 20:
        if is_complete_upgrade:
            return "ACCEPT_PROVISIONAL"
        return "BLOCK"
    if score >= 70:
        return "ACCEPT"
    return "ACCEPT_PROVISIONAL"
