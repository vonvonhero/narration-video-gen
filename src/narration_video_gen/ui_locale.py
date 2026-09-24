"""Resolve the human-interface language without changing the host locale."""

from __future__ import annotations

import os
from pathlib import Path


SUPPORTED_LANGUAGES = ("ja", "en")


def config_root():
    override = os.environ.get("NVG_CONFIG_HOME")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "narration-video-gen"
    return Path.home() / ".config" / "narration-video-gen"


def _normalise(value):
    value = str(value or "").strip().lower()
    if value.startswith(("ja", "japanese")):
        return "ja"
    if value.startswith(("en", "english")):
        return "en"
    return None


def language():
    """Return ja or en, preferring app settings over the process locale."""
    explicit = _normalise(os.environ.get("NVG_UI_LANGUAGE"))
    if explicit:
        return explicit

    try:
        saved = _normalise((config_root() / "ui-language").read_text(
            encoding="utf-8"))
    except (OSError, UnicodeError):
        saved = None
    if saved:
        return saved

    locale_value = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") \
        or os.environ.get("LANG") or "C"
    return "ja" if _normalise(locale_value) == "ja" else "en"


def is_japanese():
    return language() == "ja"

