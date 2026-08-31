#!/usr/bin/env python3
"""
fables-sync.py — push local agent sessions to a Fables cloud.

Discovers every session store on this machine with the same providers.py
used by the reading room, and uploads new or changed transcripts to a
remote fables-cloud instance. Run it on every machine whose sessions you
want reachable from anywhere.

Usage:
    python3 fables-sync.py --url https://fables.example.com --token <device-token>
    python3 fables-sync.py --url ... --token ... --watch 300     # daemon loop
    python3 fables-sync.py --url ... --token ... --once          # single pass

State (uploaded ids + fingerprints) is kept in ~/.local/share/fables/
sync-state.json so re-runs only transfer what changed. Sessions deleted
locally are pruned from the cloud.

Privacy: transcripts contain code and possibly secrets. Only sync to a
cloud you control.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from providers import discover, load_target  # noqa: E402

MAX_TRANSCRIPT_BYTES = 8_000_000   # safety cap; --max-bytes overrides
BATCH_SIZE = 50


def state_path(home: Path) -> Path:
    directory = home / ".local" / "share" / "fables"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "sync-state.json"


def state_key(url: str, machine: str) -> str:
    """Per-cloud state key, so switching clouds never skips uploads."""
    return f"{url.rstrip('/')}::{machine}"


def load_state(path: Path) -> dict[str, dict[str, list[float]]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, indent=1), encoding="utf-8")


def discover_sessions(home: Path, max_bytes: int = MAX_TRANSCRIPT_BYTES) -> list[dict]:
    """Return session rows with transcripts."""
    sessions, targets, _statuses = discover(home)
    rows: list[dict] = []
    for entry in sessions:
        target = targets.get(entry["id"])
        if target is None:
            continue
        try:
            raw = load_target(target)
        except (OSError, ValueError):
            continue
        if len(raw.encode("utf-8", "replace")) > max_bytes:
            print(f"  skip (oversized): {entry.get('source')} "
                  f"{entry.get('title', '')[:40]}")
            continue
        rows.append({
            "local_id": entry["id"],
            "source": entry.get("source", ""),
            "title": entry.get("title", ""),
            "cwd": entry.get("cwd") or entry.get("project") or "",
            "project": entry.get("project", ""),
            "mtime": float(entry.get("mtime") or 0),
            "size": int(entry.get("size") or 0),
            "native_id": str(entry.get("native_id") or ""),
            "transcript": raw,
        })
    return rows


def changed(rows: list[dict], state: dict[str, dict[str, list[float]]],
            machine: str) -> tuple[list[dict], list[str]]:
    known = state.get(machine, {})
    to_upload: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        seen.add(row["local_id"])
        fingerprint = [row["mtime"], row["size"]]
        if known.get(row["local_id"]) != fingerprint:
            to_upload.append(row)
    to_prune = [local_id for local_id in known if local_id not in seen]
    return to_upload, to_prune


def upload_batch(url: str, token: str, machine: str, sessions: list[dict],
                 prune: list[str]) -> dict:
    body = json.dumps({"machine": machine, "sessions": sessions,
                       "prune": prune}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + "/api/upload", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read())


def sync_once(url: str, token: str, machine: str, home: Path,
              quiet: bool = False, max_bytes: int = MAX_TRANSCRIPT_BYTES) -> int:
    state = load_state(state_path(home))
    key = state_key(url, machine)
    rows = discover_sessions(home, max_bytes)
    to_upload, to_prune = changed(rows, state, key)
    uploaded = 0
    if to_upload:
        if not quiet:
            print(f"  uploading {len(to_upload)} session(s)…")
        for start in range(0, len(to_upload), BATCH_SIZE):
            batch = to_upload[start:start + BATCH_SIZE]
            result = upload_batch(url, token, machine, batch, [])
            uploaded += int(result.get("uploaded") or 0)
    if to_prune:
        if not quiet:
            print(f"  pruning {len(to_prune)} deleted session(s)…")
        upload_batch(url, token, machine, [], to_prune)
    if uploaded or to_prune or not quiet:
        print(f"  {uploaded} uploaded, {len(to_prune)} pruned, "
              f"{len(rows)} total on this machine")
    known = state.setdefault(key, {})
    for row in rows:
        known[row["local_id"]] = [row["mtime"], row["size"]]
    for local_id in to_prune:
        known.pop(local_id, None)
    save_state(state_path(home), state)
    return uploaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fables cloud sync client")
    parser.add_argument("--url", required=True, help="cloud base URL")
    parser.add_argument("--token", required=True, help="device token")
    parser.add_argument("--machine", default=None,
                        help="machine name (default: hostname)")
    parser.add_argument("--home", default=None,
                        help="session home (default: $HOME)")
    parser.add_argument("--once", action="store_true", help="single pass")
    parser.add_argument("--max-bytes", type=int, default=MAX_TRANSCRIPT_BYTES,
                        help="skip transcripts larger than this many bytes")
    parser.add_argument("--watch", type=int, default=0,
                        help="loop every N seconds (default: single pass)")
    args = parser.parse_args(argv)
    import socket
    machine = args.machine or socket.gethostname()
    home = Path(args.home or __import__("os").environ.get("HOME", "")).expanduser()
    print(f"fables-sync → {args.url} (machine: {machine})")
    if args.once or args.watch <= 0:
        sync_once(args.url, args.token, machine, home, max_bytes=args.max_bytes)
        return 0
    try:
        while True:
            sync_once(args.url, args.token, machine, home, max_bytes=args.max_bytes)
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
