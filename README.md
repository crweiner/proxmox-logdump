# proxmox-logdump

Archive failed Proxmox VE and Proxmox Backup Server task logs into Git through a small HTTP relay such as Node-RED.

This project has two parts:

1. A lightweight Python collector that runs on a Proxmox host.
2. An HTTP relay that accepts full task logs and writes them into Git.

The collector is read-only with respect to Proxmox itself. It lists recent tasks, fetches task logs for failed jobs, keeps a local state file of already-uploaded UPIDs, and POSTs matching logs to the relay.

To avoid noisy or unhelpful uploads on current Proxmox builds, the collector ignores known non-archive worker types such as `diskinit`, `logrotate`, and `termproxy`.

## What It Captures

- Proxmox VE host task logs from `pvenode task list` and `pvenode task log`
- Proxmox Backup Server task logs from `proxmox-backup-manager task list` and `proxmox-backup-manager task log`
- Failed `vzdump` jobs that include both VM and LXC backup failures
- Failed PBS sync, verify, prune, and related job logs

Important detail for Proxmox VE: when you run a scheduled `vzdump --all` backup job, Proxmox usually records that as one task log. If that one task contains failures for multiple VMs and LXCs, this collector uploads that full task log once. It does not split a single `vzdump` log into one file per guest.

## Why Webhooks Alone Are Not Enough

Proxmox webhook notifications are good for alert metadata, but they do not reliably include the full task transcript. This collector is the missing step that turns a failure event into the actual raw log text you want to preserve.

## Architecture

```text
Proxmox VE / PBS host
  -> collector script
  -> HTTP relay
  -> Git commit
  -> Forgejo / Gitea / GitHub repository
```

In the reference setup used here, the HTTP relay is Node-RED and the Git write is done through the Forgejo contents API. There is no local Git clone in the relay. Each successful file-create request becomes one Git commit.

## Included Artifacts

This project now includes:

- `proxmox_tasklog_uploader.py`
- `proxmox-tasklog-upload@.service`
- `proxmox-tasklog-upload@.timer`
- `proxmox-logdump.env.example`
- `node-red-flow.example.json`
- `AGENTS.md`

If you want to reproduce the relay quickly, start with `node-red-flow.example.json`.

## Repository Layout

The relay currently writes two top-level trees:

```text
events/
task-logs/
```

Notification webhook payloads are stored as JSON:

```text
events/pve/YYYY/MM/DD/<stamp>_<host>_<kind>_<severity>_<nonce>.json
events/pbs/YYYY/MM/DD/<stamp>_<host>_<kind>_<severity>_<nonce>.json
```

Examples:

```text
events/pve/2026/05/12/20260512T020000Z_pve-200_vzdump_error_8fce02.json
events/pbs/2026/05/12/20260512T020100Z_pbs-56_syncjob_error_bc1fdb.json
```

Full task logs are stored as raw `.log` files:

```text
task-logs/pve/YYYY/MM/DD/<stamp>_<node>_<task_type>_<status>_<upid>.log
task-logs/pbs/YYYY/MM/DD/<stamp>_<node>_<task_type>_<status>_<upid>.log
```

Examples:

```text
task-logs/pve/2026/05/11/20260511T210004Z_proxmox_vzdump_ERROR_UPID:proxmox:....log
task-logs/pbs/2026/05/12/20260512T014817Z_pbs-56_syncjob_error_UPID:proxmox-backup-server:....log
```

The notification archive and the task-log archive are intentionally separate:

- `events/` preserves the alert metadata that Proxmox emitted.
- `task-logs/` preserves the full raw task transcript collected from the host.

## Commit Behavior

The current relay does not batch writes.

- One inbound notification file create => one Forgejo commit
- One inbound task-log file create => one Forgejo commit
- The relay serializes writes at `1 request / second` to avoid Forgejo ref-lock races

This means a burst of failures will appear in Forgejo as a burst of commits. That is expected with the current design.

On first live collector run, older failed tasks inside the current task-list scan window are also uploaded. That can produce many commits all at once even if those failures happened hours earlier.

Only commits whose messages start with one of these prefixes are part of normal runtime behavior:

- `Archive Proxmox PVE notification`
- `Archive Proxmox PBS notification`
- `Archive Proxmox PVE task log`
- `Archive Proxmox PBS task log`

One-off setup and validation commits can also exist during installation or testing. Those are not part of steady-state operation.

## Node-RED Relay Design

The reference Node-RED flow exposes four HTTP endpoints:

```text
POST /webhook/proxmox/pve
POST /webhook/proxmox/pbs
POST /ingest/tasklog/pve
POST /ingest/tasklog/pbs
```

Behavior:

1. Validate the shared `X-Proxmox-Token` header.
2. Normalize the payload.
3. Build the destination repo path.
4. Base64-encode the file content.
5. Call the Forgejo contents API:

```text
POST /api/v1/repos/<owner>/<repo>/contents/<path>
```

6. Return the created path and commit URL to the caller.

Important implementation detail: the flow serializes every Forgejo write through one shared delay node so concurrent notifications do not collide on `refs/heads/main`.

### Included starter flow

The file `node-red-flow.example.json` is a sanitized Node-RED import you can use as a starting point.

It exposes the four expected HTTP endpoints and writes directly to the Forgejo or Gitea contents API.

The starter flow expects these Node-RED environment variables:

| Variable | Required | Purpose |
| --- | --- | --- |
| `PROXMOX_SHARED_TOKEN` | Yes | Shared token validated from `X-Proxmox-Token` |
| `FORGEJO_BASE_URL` | Yes | Forgejo or Gitea base URL, for example `http://forgejo:3000` |
| `FORGEJO_OWNER` | Yes | Repo owner or org |
| `FORGEJO_REPO` | Yes | Repo name |
| `FORGEJO_API_TOKEN` | Yes | API token allowed to create repo contents |
| `FORGEJO_BRANCH` | No | Branch name, defaults to `main` |

### Node-RED import steps

1. Import `node-red-flow.example.json`.
2. Set the required Node-RED environment variables.
3. Deploy the flow.
4. Send a test `POST` to `/webhook/proxmox/pve`.
5. Send a test `POST` to `/ingest/tasklog/pve`.
6. Verify that Forgejo receives one commit per created file.

### Node-RED smoke tests

Notification test:

```bash
curl -X POST http://node-red-host:1880/webhook/proxmox/pve \
  -H 'Content-Type: application/json' \
  -H 'X-Proxmox-Token: replace-with-your-token' \
  -d '{
    "source": "pve",
    "title": "Test notification",
    "message": "This is a notification test",
    "severity": "error",
    "timestamp": 1778547604,
    "fields": {
      "hostname": "pve01",
      "type": "vzdump"
    }
  }'
```

Task-log test:

```bash
curl -X POST http://node-red-host:1880/ingest/tasklog/pve \
  -H 'Content-Type: application/json' \
  -H 'X-Proxmox-Token: replace-with-your-token' \
  -d '{
    "source": "pve",
    "node": "pve01",
    "task_type": "vzdump",
    "status": "ERROR",
    "upid": "UPID:pve01:00012345:00000000:00000000:vzdump:100:root@pam:",
    "started_at": 1778547604,
    "ended_at": 1778547623,
    "task": {
      "upid": "UPID:pve01:00012345:00000000:00000000:vzdump:100:root@pam:"
    },
    "log_text": "INFO: starting\\nERROR: backup failed\\n"
  }'
```

## Proxmox Notification Setup

The collector only handles full task logs. Notification webhooks are a separate Proxmox-side configuration and should also be documented because they explain why files appear under `events/`.

### Recommended target names

- PVE target: `node-red-pve`
- PBS target: `node-red-pbs`

### Recommended matcher name

- `node-red-errors`

### Webhook target configuration

For Proxmox VE:

- Method: `POST`
- URL: `http://<relay-host>:1880/webhook/proxmox/pve`
- Header: `X-Proxmox-Token: <shared-token>`
- Content-Type: `application/json`

Body template:

```json
{
  "source": "pve",
  "title": "{{ escape title }}",
  "message": "{{ escape message }}",
  "severity": "{{ severity }}",
  "timestamp": {{ timestamp }},
  "fields": {{ json fields }}
}
```

For Proxmox Backup Server:

- Method: `POST`
- URL: `http://<relay-host>:1880/webhook/proxmox/pbs`
- Header: `X-Proxmox-Token: <shared-token>`
- Content-Type: `application/json`

Body template:

```json
{
  "source": "pbs",
  "title": "{{ escape title }}",
  "message": "{{ escape message }}",
  "severity": "{{ severity }}",
  "timestamp": {{ timestamp }},
  "fields": {{ json fields }}
}
```

If a CLI-created webhook target behaves oddly around custom headers on your Proxmox release, re-save the target once in the Proxmox Notifications UI. The UI path can be more reliable for header editing on some releases.

### Matcher recommendation

Match the webhook targets on at least:

- `Error`
- `Unknown`

That keeps the archive focused on actionable failures while still catching tasks that do not cleanly classify themselves as success or error.

### Minimal Proxmox UI checklist

For each Proxmox system:

1. Create the webhook target.
2. Set the URL for the correct endpoint.
3. Set `X-Proxmox-Token` to the shared token.
4. Use the JSON body template shown above.
5. Create a matcher that points `Error` and `Unknown` severities to that target.
6. Run the built-in test action for the target.
7. Verify a new file appears under `events/` in the repo.

## Requirements

- Proxmox VE or Proxmox Backup Server
- Python 3
- A relay endpoint that accepts:
  - `POST /ingest/tasklog/pve`
  - `POST /ingest/tasklog/pbs`
- A shared token passed in `X-Proxmox-Token`

## Relay Payload Format

The collector sends JSON like this:

```json
{
  "source": "pve",
  "node": "pve01",
  "task_type": "vzdump",
  "status": "ERROR",
  "upid": "UPID:pve01:00012345:...",
  "started_at": 1778547604,
  "ended_at": 1778547623,
  "task": {
    "upid": "UPID:pve01:00012345:..."
  },
  "log_text": "full task log here"
}
```

## Step-by-Step Setup

### 1. Build the relay

Use any small relay that can:

- authenticate an incoming request using `X-Proxmox-Token`
- accept JSON
- write `log_text` into a file in Git
- commit and push that file

Node-RED works well for this.

The quickest path is:

1. import `node-red-flow.example.json`
2. set the six environment variables listed above
3. deploy
4. test with the sample `curl` requests

Recommended file layout in Git:

```text
task-logs/pve/YYYY/MM/DD/<timestamp>_<node>_<task_type>_<status>_<upid>.log
task-logs/pbs/YYYY/MM/DD/<timestamp>_<node>_<task_type>_<status>_<upid>.log
```

### 2. Copy the collector onto the Proxmox host

Install the files:

- `proxmox_tasklog_uploader.py`
- `proxmox-tasklog-upload@.service`
- `proxmox-tasklog-upload@.timer`

### 3. Create the environment file

Create `/etc/default/proxmox-logdump`:

```bash
PROXMOX_LOGDUMP_TOKEN=replace-with-your-shared-token
PROXMOX_LOGDUMP_RELAY_BASE_URL=http://relay-host-or-ip:1880
```

Recommended optional tuning:

```bash
PROXMOX_LOGDUMP_LIMIT=50
PROXMOX_LOGDUMP_COMMAND_TIMEOUT=45
```

For PBS environments with larger or slower task logs, a higher timeout is reasonable:

```bash
PROXMOX_LOGDUMP_COMMAND_TIMEOUT=180
```

### 4. Install on Proxmox VE

```bash
install -d -m 0755 /usr/local/lib/proxmox-logdump /var/lib/proxmox-logdump
install -m 0755 proxmox_tasklog_uploader.py /usr/local/lib/proxmox-logdump/proxmox_tasklog_uploader.py
install -m 0644 proxmox-tasklog-upload@.service /etc/systemd/system/proxmox-tasklog-upload@.service
install -m 0644 proxmox-tasklog-upload@.timer /etc/systemd/system/proxmox-tasklog-upload@.timer
install -m 0600 proxmox-logdump.env.example /etc/default/proxmox-logdump
systemctl daemon-reload
systemctl enable --now proxmox-tasklog-upload@pve.timer
```

### 5. Install on Proxmox Backup Server

```bash
install -d -m 0755 /usr/local/lib/proxmox-logdump /var/lib/proxmox-logdump
install -m 0755 proxmox_tasklog_uploader.py /usr/local/lib/proxmox-logdump/proxmox_tasklog_uploader.py
install -m 0644 proxmox-tasklog-upload@.service /etc/systemd/system/proxmox-tasklog-upload@.service
install -m 0644 proxmox-tasklog-upload@.timer /etc/systemd/system/proxmox-tasklog-upload@.timer
install -m 0600 proxmox-logdump.env.example /etc/default/proxmox-logdump
systemctl daemon-reload
systemctl enable --now proxmox-tasklog-upload@pbs.timer
```

### 6. Dry-run before enabling automation

On Proxmox VE:

```bash
/usr/local/lib/proxmox-logdump/proxmox_tasklog_uploader.py --source pve --dry-run --backfill-seen
```

On Proxmox Backup Server:

```bash
/usr/local/lib/proxmox-logdump/proxmox_tasklog_uploader.py --source pbs --dry-run --backfill-seen
```

`--backfill-seen` only marks successful or non-failed finished tasks as seen. It does not suppress real failed-task uploads.

### 7. Run one live collection pass

On the first live run, the collector uploads any failed tasks it finds inside the current scan window. If you want to avoid importing older failures, lower `PROXMOX_LOGDUMP_LIMIT`, reduce the timer frequency gap before the first enable, or remove the old failures from the task history window before the first live run.

On Proxmox VE:

```bash
/usr/local/lib/proxmox-logdump/proxmox_tasklog_uploader.py --source pve --backfill-seen
```

On Proxmox Backup Server:

```bash
/usr/local/lib/proxmox-logdump/proxmox_tasklog_uploader.py --source pbs --backfill-seen
```

### 8. Enable the timers

On Proxmox VE:

```bash
systemctl enable --now proxmox-tasklog-upload@pve.timer
systemctl status --no-pager proxmox-tasklog-upload@pve.timer
```

On Proxmox Backup Server:

```bash
systemctl enable --now proxmox-tasklog-upload@pbs.timer
systemctl status --no-pager proxmox-tasklog-upload@pbs.timer
```

### 9. Verify end to end

A clean end-to-end validation looks like this:

1. Proxmox target test creates a JSON file under `events/...`
2. Manual collector run creates a `.log` file under `task-logs/...`
3. Forgejo shows one commit per archived file
4. Subsequent timer runs do not re-upload the same UPIDs

## Environment Variables

| Variable | Required | Description |
| --- | --- | --- |
| `PROXMOX_LOGDUMP_TOKEN` | Yes | Shared relay token |
| `PROXMOX_LOGDUMP_RELAY_BASE_URL` | Usually | Base relay URL; the script appends `/ingest/tasklog/<source>` |
| `PROXMOX_LOGDUMP_UPLOAD_URL` | Optional | Full upload URL override |
| `PROXMOX_LOGDUMP_PVE_UPLOAD_URL` | Optional | Source-specific PVE upload URL override |
| `PROXMOX_LOGDUMP_PBS_UPLOAD_URL` | Optional | Source-specific PBS upload URL override |
| `PROXMOX_LOGDUMP_STATE_DIR` | No | State file directory |
| `PROXMOX_LOGDUMP_LIMIT` | No | Number of recent tasks to inspect |
| `PROXMOX_LOGDUMP_COMMAND_TIMEOUT` | No | Timeout in seconds for task list and task log commands |

## Safety Notes

- The collector does not modify VM or CT configuration.
- The collector does not restart Proxmox services.
- The collector does not prune, verify, sync, or delete backups.
- The collector reads task metadata and task logs, then writes a local state file and sends an HTTP POST.

## Expected Commit Bursts

If you browse the repo commit log and see many `Archive Proxmox ...` commits clustered together, the common reasons are:

1. Multiple failing tasks occurred close together.
2. A timer run found several older failed tasks that had not been uploaded yet.
3. Both the notification webhook and the full task-log collector archived related failure data.

That pattern is expected with the current one-file-per-commit relay.

## Node-RED Security

If you use Node-RED as the relay, secure it before exposing it beyond a trusted LAN:

- enable `adminAuth` for the editor and admin API
- enable `httpNodeAuth` or validate your own shared token in the flow
- prefer HTTPS if the relay is not isolated to a private network

Node-RED’s official security guidance:

- [Securing Node-RED](https://nodered.org/docs/user-guide/runtime/securing-node-red)

## License

MIT
