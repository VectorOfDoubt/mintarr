# Frequently Asked Questions

> **Type:** Community
> **Version:** 0.1 — 2026-05-26
> **Status:** Stub. Grows from issues and Discussions.
> **Audience:** Anyone with a question that has been asked before.

---

## Stub

The FAQ grows over time. Right now it contains questions that have already come up in design and review.

For Q&A organised by topic, see [COMPARISON.md](COMPARISON.md) §9 for "vs other tools" questions.

---

## General

### What is Mintarr?

Mintarr is a quality control and import orchestration layer that sits between music sources (TIDAL, Soulseek, LocalFolder, future SAB / qBit / CD-rip) and Lidarr. See [VISION.md](../strategy/VISION.md).

### Is Mintarr a Lidarr replacement?

No. Mintarr is a companion to Lidarr. See [ADR-0008](../architecture/adr/0008-strategic-positioning.md) and [COMPARISON.md §2](COMPARISON.md#2-vs-lidarr-alone).

### When will Mintarr have a stable release?

When Phase 0 is complete. There is no deadline; quality of decisions, tests, and documentation comes before shipping speed. Track [ROADMAP.md](../strategy/ROADMAP.md) for status.

### Where can I get help?

GitHub Issues for bug reports and feature requests; GitHub Discussions for questions and use-case discussion (links added after public-repo cutover).

For security issues, see [SECURITY.md](https://github.com/eivindsjursen-lab/mintarr/blob/main/SECURITY.md).

---

## Installation

### Do I need to install Lidarr first?

Yes. Mintarr depends on Lidarr being functional. See [INSTALL.md](../operations/INSTALL.md).

### Can I run Mintarr without Docker?

Not officially in v1. A bare-metal install path is possible (Mintarr is a Flask app) but not documented. Docker Compose is the supported path.

### Does Mintarr work with Lidarr v4?

Not yet. See [LIDARR_INTEGRATION.md §2](../specs/LIDARR_INTEGRATION.md#2-supported-lidarr-versions).

---

## Configuration

### Can I run multiple Mintarr instances for multi-user?

Yes. See [ADR-0002 single-instance arr-pattern](../architecture/adr/0002-single-instance-arr-pattern.md). Each Mintarr container is single-admin; for multiple isolated users, run multiple containers behind separate auth.

### How do I use Mintarr with reverse-proxy SSO?

See [CONFIGURATION.md §7.2](../operations/CONFIGURATION.md#72-reverse-proxy-sso). Set `MINTARR_REMOTE_USER_TRUSTED=true` when you're sure the proxy is authenticating users.

---

## Adapters

### How do I add a new source adapter?

See [ADAPTER_TUTORIAL.md](../development/ADAPTER_TUTORIAL.md). The contract is in [ADAPTER_PROTOCOL_v1.md](../specs/ADAPTER_PROTOCOL_v1.md).

### Does my adapter need a connector manifest?

For built-in adapters, yes. For local-only adapters (just for your install), no — the adapter alone is enough. See [CONNECTOR_MANIFEST_v1.md](../specs/CONNECTOR_MANIFEST_v1.md).

### Why does my adapter need to use `ctx.run_subprocess` instead of `subprocess.run`?

So cancellation and timeout are handled uniformly. See [ADAPTER_PROTOCOL_v1.md §6](../specs/ADAPTER_PROTOCOL_v1.md#6-pipelinecontext-the-runtime-handle).

---

## Verification

### What does V2 verification do?

It runs every imported file through codec gate, integrity check, and FLAC Detective spectral analysis, then applies a policy that decides ACCEPT / ACCEPT_PROVISIONAL / REVIEW_REQUIRED / BLOCK. See [PIPELINE.md §4](../architecture/PIPELINE.md#4-phase-3-verify).

### Why was my import REVIEW_REQUIRED?

The V2 policy could not auto-decide. Sidecar evidence will explain. See the dashboard's record detail drawer.

### Why was my import BLOCKED?

Either codec gate failed (file isn't actually FLAC), `flac -t` failed (FLAC stream is corrupted), FLAC Detective verdict was FAKE_CERTAIN/SUSPICIOUS against an existing higher-quality library entry, or FLAC Detective was unreachable (fail-closed). See the sidecar.

---

## Troubleshooting

### Mintarr says "validator unavailable" on every grab

FLAC Detective is unreachable. Check `FLAC_API_URL` and that the flac-detective container is running.

### Lidarr says "Indexer unreachable" when I add Mintarr

Mintarr's Newznab endpoint must respond within Lidarr's timeout (default 10s). Check `MINTARR_API_KEY` matches what Lidarr is configured with.

### Where do I find the logs?

`docker logs mintarr` for runtime logs. Sidecars on disk for per-record decision details. `decisions.jsonl` for the append-only audit log.

---

> Last updated: 2026-05-26
