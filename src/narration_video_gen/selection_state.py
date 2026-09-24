"""Per-user, per-boot selection state for the human CLI workflow.

Profiles remain explicit in plans and run records, but a person should not have
to copy an internal profile id between every command. The state lives under a
private directory in /tmp, is scoped to this checkout, and expires at reboot.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path


def _boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return None


def _paths(root):
    uid = os.getuid()
    directory = Path("/tmp") / ("narration-video-gen-%d" % uid)
    checkout = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:16]
    return directory, directory / ("selection-%s.json" % checkout)


def state_path(root):
    """Return the state path for diagnostics and tests; do not create it."""
    return _paths(root)[1]


def _private_directory(root, create=False):
    directory, _ = _paths(root)
    if create:
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
    try:
        info = directory.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return None
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        return None
    return directory


def save(root, profile, model_family, resolution, *, recipe_options=None,
         plan_configured=False):
    """Atomically save a successful selection, refusing unsafe temp paths."""
    directory = _private_directory(root, create=True)
    if directory is None:
        raise OSError("temporary selection directory is not private and owned by this user")
    _, path = _paths(root)
    temporary = directory / (".%s.%d.tmp" % (path.name, os.getpid()))
    payload = {
        "schema_version": 1,
        "root": str(Path(root).resolve()),
        "boot_id": _boot_id(),
        "saved_at": int(time.time()),
        "profile": profile,
        "model_family": model_family,
        "resolution": resolution,
        "recipe_options": list(recipe_options or []),
        "plan_configured": bool(plan_configured),
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(temporary), flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return path


def load(root):
    """Load a valid selection for this checkout and boot, otherwise return None."""
    if _private_directory(root) is None:
        return None
    _, path = _paths(root)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            return None
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            return None
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(path), flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            current = os.fstat(stream.fileno())
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                return None
            payload = json.load(stream)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None
    if payload.get("root") != str(Path(root).resolve()):
        return None
    if payload.get("boot_id") != _boot_id():
        return None
    if not all(payload.get(key) for key in ("profile", "model_family", "resolution")):
        return None
    return payload


def clear(root):
    """Remove only this checkout's valid, user-owned regular state file."""
    if _private_directory(root) is None:
        return
    _, path = _paths(root)
    try:
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) \
                and info.st_uid == os.getuid():
            path.unlink()
    except OSError:
        pass
