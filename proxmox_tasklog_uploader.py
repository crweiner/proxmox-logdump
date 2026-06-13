#!/usr/bin/env python3

"""Upload failed Proxmox VE and PBS task logs to an HTTP relay."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


CONFIG = {
    "pbs": {
        "list_cmd": [
            "proxmox-backup-manager",
            "task",
            "list",
            "--all",
            "--limit",
            "200",
            "--output-format",
            "json",
        ],
        "log_cmd": ["proxmox-backup-manager", "task", "log"],
        "state_name": "pbs-failed-tasklogs.json",
    },
    "pve": {
        "list_cmd": [
            "pvenode",
            "task",
            "list",
            "--source",
            "all",
            "--limit",
            "200",
            "--output-format",
            "json",
        ],
        "log_cmd": ["pvenode", "task", "log"],
        "state_name": "pve-failed-tasklogs.json",
    },
}

FAILURE_MARKERS = (
    "task error:",
    "error:",
    " failed - ",
    " failed:",
    "sync failed with some errors",
    "cleanup error",
    "handler failed:",
    "create_locked_backup_group failed",
    "input/output error",
    "read-only file system",
    "exit code 255",
)

SKIP_WORKER_TYPES = {
    "diskinit",
    "logrotate",
    "termproxy",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload failed Proxmox task logs to a webhook relay."
    )
    parser.add_argument("--source", choices=sorted(CONFIG), required=True)
    parser.add_argument(
        "--upload-url",
        help="Full relay URL, for example http://relay:1880/ingest/tasklog/pve",
    )
    parser.add_argument(
        "--relay-base-url",
        default=os.environ.get("PROXMOX_LOGDUMP_RELAY_BASE_URL"),
        help="Relay base URL; the script appends /ingest/tasklog/<source>.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("PROXMOX_LOGDUMP_TOKEN"),
        help="Shared relay token. Defaults to PROXMOX_LOGDUMP_TOKEN.",
    )
    parser.add_argument(
        "--state-file",
        help="Path to the local state file tracking uploaded task UPIDs.",
    )
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("PROXMOX_LOGDUMP_STATE_DIR", "/var/lib/proxmox-logdump"),
        help="Directory used for the default state file path.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=int(os.environ.get("PROXMOX_LOGDUMP_LIMIT", "200")),
        help="Maximum number of recent tasks to inspect per run.",
    )
    parser.add_argument(
        "--backfill-seen",
        action="store_true",
        help="Mark non-failed finished tasks as seen during the first run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the uploads that would be sent without POSTing them.",
    )
    parser.add_argument(
        "--command-timeout",
        type=int,
        default=int(os.environ.get("PROXMOX_LOGDUMP_COMMAND_TIMEOUT", "45")),
        help="Timeout in seconds for the Proxmox CLI commands.",
    )
    return parser.parse_args()


def run_json(cmd: list[str], timeout: int) -> Any:
    result = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    payload = json.loads(result.stdout)
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def run_text(cmd: list[str], timeout: int) -> str:
    result = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout


def build_upload_url(args: argparse.Namespace, source: str) -> str:
    if args.upload_url:
        return args.upload_url

    specific_env = os.environ.get(f"PROXMOX_LOGDUMP_{source.upper()}_UPLOAD_URL")
    if specific_env:
        return specific_env

    generic_env = os.environ.get("PROXMOX_LOGDUMP_UPLOAD_URL")
    if generic_env:
        return generic_env

    if args.relay_base_url:
        return args.relay_base_url.rstrip("/") + f"/ingest/tasklog/{source}"

    raise SystemExit(
        "missing relay URL: set --upload-url, --relay-base-url, "
        "PROXMOX_LOGDUMP_UPLOAD_URL, or the source-specific upload URL env var"
    )


def default_state_file(source: str, state_dir: str) -> str:
    return str(Path(state_dir) / CONFIG[source]["state_name"])


def state_load(path: str) -> dict[str, list[str]]:
    file_path = Path(path)
    if not file_path.exists():
        return {"seen_upids": []}
    with file_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        return {"seen_upids": []}
    seen = data.get("seen_upids", [])
    if not isinstance(seen, list):
        seen = []
    return {"seen_upids": [str(item) for item in seen]}


def state_save(path: str, state: dict[str, list[str]]) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = file_path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp_path.replace(file_path)


def task_upid(task: dict[str, Any]) -> str | None:
    for key in ("upid", "id"):
        value = task.get(key)
        if value:
            return str(value)
    return None


def is_running(task: dict[str, Any]) -> bool:
    status = str(task.get("status") or task.get("exitstatus") or "").strip().lower()
    if status in {"running", "active"}:
        return True
    return not (task.get("endtime") or task.get("end_time")) and bool(status)


def looks_successful(status: str) -> bool:
    normalized = str(status or "").strip().lower()
    return normalized in {"ok", "success", "stopped"}


def looks_failed(status: str) -> bool:
    normalized = str(status or "").strip().lower()
    if not normalized:
        return False
    return (
        "error" in normalized
        or "fail" in normalized
        or normalized in {"unknown", "warning", "warn"}
    )


def log_indicates_failure(log_text: str) -> bool:
    lowered = log_text.lower()
    return any(marker in lowered for marker in FAILURE_MARKERS)


def upload_log(url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Proxmox-Token": token,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    args = parse_args()
    upload_url = build_upload_url(args, args.source)
    state_file = args.state_file or default_state_file(args.source, args.state_dir)

    if not args.token:
        print("missing token: set --token or PROXMOX_LOGDUMP_TOKEN", file=sys.stderr)
        return 2

    cfg = CONFIG[args.source]
    list_cmd = list(cfg["list_cmd"])
    if "--limit" in list_cmd:
        idx = list_cmd.index("--limit")
        list_cmd[idx + 1] = str(args.limit)

    try:
        tasks = run_json(list_cmd, timeout=args.command_timeout)
    except subprocess.TimeoutExpired:
        print("task list command timed out", file=sys.stderr)
        return 1
    if not isinstance(tasks, list):
        print("task list command did not return a list", file=sys.stderr)
        return 1

    state = state_load(state_file)
    seen = set(state["seen_upids"])
    node_name = socket.gethostname().split(".")[0]
    uploaded: list[dict[str, Any]] = []
    changed = False

    for task in tasks:
        if not isinstance(task, dict):
            continue

        upid = task_upid(task)
        if not upid or upid in seen:
            continue

        if is_running(task):
            continue

        worker_type = str(task.get("type") or task.get("worker_type") or "").strip().lower()
        if worker_type in SKIP_WORKER_TYPES:
            if args.backfill_seen:
                seen.add(upid)
                changed = True
            continue

        status = str(task.get("status") or task.get("exitstatus") or "")
        if looks_successful(status):
            if args.backfill_seen:
                seen.add(upid)
                changed = True
            continue

        try:
            log_text = run_text(cfg["log_cmd"] + [upid], timeout=args.command_timeout)
        except subprocess.TimeoutExpired:
            print(f"task log timed out for {upid}", file=sys.stderr)
            continue
        if not looks_failed(status) and not log_indicates_failure(log_text):
            if args.backfill_seen:
                seen.add(upid)
                changed = True
            continue

        payload = {
            "source": args.source,
            "node": task.get("node") or node_name,
            "task_type": task.get("type") or task.get("worker_type") or "task",
            "status": status or "error",
            "upid": upid,
            "started_at": task.get("starttime") or task.get("start_time"),
            "ended_at": task.get("endtime") or task.get("end_time"),
            "task": task,
            "log_text": log_text,
        }

        if args.dry_run:
            uploaded.append(
                {
                    "upid": upid,
                    "status": status or "error",
                    "upload_url": upload_url,
                    "dry_run": True,
                }
            )
            continue

        try:
            result = upload_log(upload_url, args.token, payload)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            print(f"upload failed for {upid}: HTTP {exc.code} {detail}", file=sys.stderr)
            return 1
        except Exception as exc:  # pragma: no cover
            print(f"upload failed for {upid}: {exc}", file=sys.stderr)
            return 1

        seen.add(upid)
        changed = True
        uploaded.append(
            {
                "upid": upid,
                "status": status or "error",
                "path": result.get("path"),
                "commit_url": result.get("commit_url"),
            }
        )

    if changed:
        state_save(state_file, {"seen_upids": sorted(seen)})

    print(json.dumps({"uploaded": uploaded, "seen_count": len(seen)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
