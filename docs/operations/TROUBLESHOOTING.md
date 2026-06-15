# Troubleshooting

> **Type:** Operations / support
> **Version:** 0.1 - 2026-06-03
> **Status:** Draft skeleton. Grows from issues, dogfood runs, and operator reports.
> **Audience:** Operators diagnosing Mintarr, Lidarr, source adapters, and verifier failures.

---

## 1. How to debug safely

Start with the narrowest symptom and avoid deleting state until you know which
component is stuck.

Useful first checks:

```bash
docker logs mintarr --tail 100
curl -H "X-Api-Key: $MINTARR_API_KEY" \
    http://127.0.0.1:5025/dashboard/v1/summary
```

Then check Lidarr's queue and health page. A Lidarr library rescan can look like
a stuck import, but it is not the same thing as an active Mintarr grab.

Do not paste real API keys, TIDAL tokens, slskd API keys, full `downloadUrl`
values, or OAuth token JSON into public issues. Redact them first.

## 2. Mintarr will not start

### API key too short

Symptom:

```text
MINTARR_API_KEY or TIDALHIRES_API_KEY must be set and at least 16 characters
```

Fix:

```bash
openssl rand -base64 32
```

Set the result as `MINTARR_API_KEY`, restart Mintarr, and update Lidarr's
Mintarr indexer/download-client API key to match.

### Missing mount or wrong path

If Mintarr starts but cannot import, verify the expected container paths:

| Path | Purpose |
|---|---|
| `/config` | state DB, sidecars, decisions log, connector config |
| `/downloads` | SAB-style work/download root |
| `/output` | complete folder exposed to Lidarr |
| `/lidarr-config` | read-only Lidarr config mount for API-key discovery |

See [CONFIGURATION.md](CONFIGURATION.md) for the full environment and mount
catalogue.

## 3. Lidarr cannot connect to Mintarr

### Indexer test fails

Check:

- Lidarr can reach Mintarr at the configured URL.
- The API key in Lidarr matches `MINTARR_API_KEY`.
- Mintarr's `/api?t=caps` endpoint responds.

Example:

```bash
curl "http://127.0.0.1:5025/api?t=caps&apikey=$MINTARR_API_KEY"
```

### Download client test fails

Mintarr exposes a SAB-compatible endpoint. In Lidarr, configure the download
client as SABnzbd and point it at Mintarr's host/port with the same API key.

If the indexer test passes but the download-client test fails, check that Lidarr
is using the same route Mintarr exposes: `/sabnzbd/api`.

## 4. A grab is stuck

Use `/dashboard/v1/summary` first. It shows:

- Mintarr active job count
- Lidarr queue count
- Lidarr blocking commands
- connector health

If Mintarr has active jobs, inspect `/dashboard/v1/jobs`. The job `progress`
field tells you whether it is waiting on a source adapter, copying files,
validating, or waiting for Lidarr ManualImport.

If Mintarr has no active jobs but Lidarr still has queue rows, the issue is in
Lidarr's tracked-download state. Do not remove rows blindly if an import is
still running.

### Lidarr `RescanFolders` blocks searches or imports

Symptom:

- Mintarr has no active import problem, but Lidarr release searches return
  errors or time out.
- `/dashboard/v1/summary` reports a started `RescanFolders` command.
- Lidarr shows messages such as `Importing N tracks` or `Identifying album
  N/M`.
- `ManualImport` or `ProcessMonitoredDownloads` commands are queued behind the
  rescan.

Important Lidarr behaviour:

- `DELETE /api/v1/command/<id>` can cancel queued commands.
- It cannot cancel a command that is already `started`; Lidarr returns
  `409 Conflict`.

This is not a Mintarr queue bug. Lidarr's command queue only removes queued
commands through that API. A running `RescanFolders` has no public cooperative
cancel endpoint.

Safe operator options:

1. If the command is queued, cancel it with Lidarr's API or UI.
2. If it is already started, either wait for it to finish or do a controlled
   Lidarr restart.
3. After a restart, verify `/api/v1/command` has no active `RescanFolders`
   before retrying release search or import dogfood.

Do not edit Lidarr's SQLite command tables directly while Lidarr is running.
Restarting Lidarr is safer than manually mutating command state.

## 5. Soulseek issues

### Soulseek download stays at N-1 files

This usually means slskd has a partial download that stopped before completion.
Mintarr waits until the selected files exist under `SOULSEEK_DOWNLOAD_ROOT`.

Check the slskd incomplete/complete folders. If one file has not changed for a
long time, cancel the Mintarr job from the dashboard or API, then remove the
dead Lidarr queue row only after the Mintarr job is terminal.

### Soulseek release imports to the wrong album

Mintarr has a target-album guard for Soulseek ManualImport. If Lidarr resolves
the files to a different album, Mintarr marks the job failed and cleans the
queue instead of importing to the wrong album.

Known trigger: deluxe editions, remasters, and similarly named albums where
Lidarr's ManualImport resolution picks another release family.

## 6. Verification issues

### Validator unavailable

Mintarr fails closed when required validators are unavailable in import mode.
Check:

- `FLAC_API_URL`
- that the FLAC Detective service is running
- network reachability from the Mintarr container

### Codec mismatch

If a source advertises FLAC but downloads AAC/M4A or other non-FLAC audio, the
codec gate blocks before import. This is expected safety behaviour.

For TIDAL, ensure PKCE OAuth is enabled unless you are deliberately debugging
client behaviour. Non-PKCE sessions can return AAC/HIGH instead of FLAC/LOSSLESS.

### REVIEW_REQUIRED

`REVIEW_REQUIRED` means Mintarr found evidence it should not auto-import but also
should not hard-block without operator review. Use the dashboard record detail
view and the `verification.json` sidecar for the reason.

## 7. Where evidence lives

| Evidence | Location |
|---|---|
| Runtime logs | `docker logs mintarr` |
| Latest dashboard state | `/dashboard/v1/summary`, `/dashboard/v1/records` |
| Per-record sidecar | `/output/<jid>/verification.json` |
| Blocked sidecars | `/config/blocked_decisions/*.json` |
| Discarded sidecars | `/config/discarded/*.json` |
| Expired review sidecars | `/config/expired_review/*.json` |
| Audit log | `/config/decisions.jsonl` |
| State index | `/config/mintarr_state.db` or legacy alias path |

If sidecars and state DB disagree, sidecars are the source of truth. Rebuild the
state index with `app/backfill_state.py` after backing up `/config`.

## 8. Reporting a bug

Include:

- Mintarr version or commit
- Docker image tag
- Lidarr version
- source adapter (`tidal`, `local`, `soulseek`, etc.)
- redacted logs
- relevant `verification.json` sidecar, with secrets removed
- what you expected and what happened

Do not include API keys, OAuth tokens, slskd keys, or full URLs containing keys.

---

> Last updated: 2026-06-03
