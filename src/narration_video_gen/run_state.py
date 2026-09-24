"""Persistent, human-readable state for tracked generation runs."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path


def validate_run_id(run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
        raise ValueError("run ID must start with a letter or digit and contain only letters, digits, dots, hyphens or underscores (up to 128 characters)")
    return run_id


def state_path(root, run_id):
    return Path(root) / "outputs" / validate_run_id(run_id) / "run-state.json"


def log_path(root, run_id):
    return Path(root) / "outputs" / validate_run_id(run_id) / "run.log"


def write(root, run_id, **changes):
    path = state_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load(root, run_id) or {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "started_epoch": time.time(),
    }
    current.update(changes)
    current["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    handle, temporary = tempfile.mkstemp(prefix=".run-state-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(current, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return current


def load(root, run_id):
    path = state_path(root, run_id)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(state, dict) or state.get("run_id") != run_id:
        raise ValueError("invalid run state: %s" % path)
    return state


def latest(root, *, running_only=False):
    states = []
    for path in (Path(root) / "outputs").glob("*/run-state.json"):
        try:
            state = load(root, path.parent.name)
            modified_at = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        if state is None:
            continue
        if running_only and state.get("status") not in ("starting", "running"):
            continue
        states.append((modified_at, state))
    return max(states, default=(None, None), key=lambda item: item[0])[1]
