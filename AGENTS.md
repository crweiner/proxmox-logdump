# AGENTS.md

This file explains how an AI agent or engineer should reason about the `proxmox-logdump` project.

## Purpose

`proxmox-logdump` captures failed Proxmox VE and Proxmox Backup Server task logs and archives them into Git through a small HTTP relay.

The system has two separate data paths:

1. Notification archive path:
   Proxmox webhook -> relay -> `events/.../*.json`
2. Full task-log archive path:
   host collector -> relay -> `task-logs/.../*.log`

Do not confuse them. The collector does not replace the notification webhook, and the webhook does not replace the collector.

## Repository Expectations

The relay writes files into these trees:

```text
events/
task-logs/
```

Expected file patterns:

```text
events/pve/YYYY/MM/DD/<stamp>_<host>_<kind>_<severity>_<nonce>.json
events/pbs/YYYY/MM/DD/<stamp>_<host>_<kind>_<severity>_<nonce>.json
task-logs/pve/YYYY/MM/DD/<stamp>_<node>_<task_type>_<status>_<upid>.log
task-logs/pbs/YYYY/MM/DD/<stamp>_<node>_<task_type>_<status>_<upid>.log
```

## Runtime Model

The reference relay uses the Forgejo or Gitea contents API, not a local Git clone.

That means:

- one file write usually becomes one Git commit
- commit bursts are normal during failure bursts or first-run backfill
- write serialization is required to avoid branch ref-lock conflicts

If you see many commits, inspect the commit messages before assuming something is wrong. Commit messages beginning with `Archive Proxmox ...` are expected runtime commits.

## Relay Contract

Expected inbound HTTP endpoints:

```text
POST /webhook/proxmox/pve
POST /webhook/proxmox/pbs
POST /ingest/tasklog/pve
POST /ingest/tasklog/pbs
```

Expected authentication:

- shared token in `X-Proxmox-Token`

Expected relay behavior:

1. validate token
2. validate payload shape
3. derive destination repo path
4. write file through Forgejo or Gitea contents API
5. return created path and commit URL

## Starter Flow Artifact

The file `node-red-flow.example.json` is the canonical reusable Node-RED starter flow for this project.

Expectations:

- it is sanitized
- it is environment-driven
- it is meant to be importable without hardcoded lab IPs or tokens

If relay behavior changes, update the starter flow together with the README and this file.

## Proxmox Configuration Expectations

Recommended webhook target names:

- `node-red-pve`
- `node-red-pbs`

Recommended matcher name:

- `node-red-errors`

Recommended severities:

- `Error`
- `Unknown`

If an agent changes target names or matcher names, it should update documentation in the README and this file in the same change.

## Collector Behavior

The Python collector:

- reads recent tasks with Proxmox CLI tools
- skips running tasks
- skips already-seen UPIDs
- fetches task logs for failed or suspicious tasks
- uploads each qualifying task log
- records seen UPIDs in a local state file

Important first-run behavior:

- `--backfill-seen` marks successful finished tasks as seen
- it does not suppress failed-task uploads
- older failed tasks still inside the recent task window will be uploaded

## Safety Rules For Agents

When modifying this project or a live deployment:

- do not change Proxmox jobs, VM config, CT config, storage config, or backup config as part of relay-only work
- prefer dry-runs before enabling timers
- validate both PVE and PBS paths independently
- treat Node-RED auth and secret handling as a separate security change unless explicitly requested
- do not silently rotate tokens or rename targets without documenting the change

## Validation Checklist

After changes, verify:

1. notification endpoints accept valid requests and reject invalid tokens
2. task-log ingest endpoints accept valid requests and reject invalid tokens
3. repo paths are still correct
4. Forgejo writes are serialized
5. one successful request returns a commit URL
6. the host collector dry-run completes on both PVE and PBS
7. the systemd timer starts cleanly on both PVE and PBS
8. the starter Node-RED flow still matches the documented endpoints and env vars

## Documentation Rule

If runtime behavior changes, update:

- `README.md`
- `AGENTS.md`

in the same edit set.
