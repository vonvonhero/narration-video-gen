#!/usr/bin/env python3
"""Inventory or remove only model files owned by Narration Video Gen."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def _inside(path, root):
    path = os.path.abspath(os.fspath(path))
    root = os.path.abspath(os.fspath(root))
    return os.path.commonpath((path, root)) == root


def _tree_size(path):
    try:
        if path.is_symlink() or path.is_file():
            return path.lstat().st_size
        if not path.is_dir():
            return 0
    except OSError:
        return 0
    total = 0
    for directory, dirnames, filenames in os.walk(path, followlinks=False):
        base = Path(directory)
        for name in dirnames + filenames:
            item = base / name
            try:
                total += item.lstat().st_size
            except OSError:
                pass
    return total


def model_targets(root):
    root = Path(root).resolve()
    sys.path.insert(0, str(root / "src"))
    from narration_video_gen.compat import load_yaml_file

    models_root = root / "models"
    lock = load_yaml_file(root / "manifests" / "models.lock.yaml")
    targets = []
    for entry in lock.get("models", []):
        target = models_root / entry["path"]
        if not _inside(target, models_root):
            raise ValueError("model path escapes models/: %s" % entry["path"])
        targets.append(target)

    # This directory is created and owned in its entirety by tts-backend.sh.
    # Removing only the snapshot listed in tts-models.lock.yaml would leave the
    # Hugging Face blob store behind and reclaim almost no space.
    tts_root = models_root / "irodori-tts-v4.1-small"
    targets.append(tts_root)

    unique = []
    seen = set()
    for target in targets:
        lexical = os.path.abspath(os.fspath(target))
        if lexical not in seen:
            seen.add(lexical)
            unique.append(Path(lexical))
    return models_root, unique


def active_operations(root):
    """Return generation/download processes that make model deletion unsafe."""
    root = str(Path(root).resolve())
    models = str(Path(root) / "models")
    active = []
    proc = Path("/proc")
    if not proc.is_dir():
        return active
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            parts = [part.decode("utf-8", "replace") for part in
                     (entry / "cmdline").read_bytes().split(b"\0") if part]
        except OSError:
            continue
        if not parts:
            continue
        joined = " ".join(parts)
        downloading = ("download-models.sh" in joined or
                       (Path(parts[0]).name == "curl" and models in joined))
        generating = (any("narration-video-gen" in part for part in parts)
                      and "run" in parts)
        if (downloading or generating) and root in joined:
            active.append({"pid": int(entry.name), "command": joined[:300]})
    return sorted(active, key=lambda item: item["pid"])


def inventory(root):
    root = Path(root).resolve()
    models_root, targets = model_targets(root)
    rows = []
    for target in targets:
        exists = target.exists() or target.is_symlink()
        rows.append({
            "path": str(target.relative_to(root)),
            "exists": exists,
            "bytes": _tree_size(target) if exists else 0,
        })
    return {
        "root": str(root),
        "models_root": str(models_root),
        "targets": rows,
        "bytes": sum(row["bytes"] for row in rows),
        "active": active_operations(root),
    }


def remove_models(root):
    root = Path(root).resolve()
    models_root, targets = model_targets(root)
    active = active_operations(root)
    if active:
        raise RuntimeError("model download or video generation is still running")
    removed = []
    parents = set()
    for target in targets:
        if not _inside(target, models_root):
            raise ValueError("refusing target outside models/: %s" % target)
        if target.is_symlink() or target.is_file():
            size = _tree_size(target)
            target.unlink()
        elif target.is_dir():
            size = _tree_size(target)
            shutil.rmtree(target)
        else:
            continue
        removed.append({"path": str(target.relative_to(root)), "bytes": size})
        parent = target.parent
        while _inside(parent, models_root) and parent != models_root:
            parents.add(parent)
            parent = parent.parent
    for directory in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    models_root.mkdir(parents=True, exist_ok=True)
    return {"removed": removed, "bytes": sum(row["bytes"] for row in removed)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--delete", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = inventory(args.root) if args.check else remove_models(args.root)
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 3
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
