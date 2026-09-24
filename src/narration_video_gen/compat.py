"""YAML loading that works with or without PyYAML installed."""

from __future__ import annotations

from pathlib import Path

try:  # pragma: no cover - depends on the host environment
    import yaml as _pyyaml
except ImportError:  # pragma: no cover
    _pyyaml = None

from . import yamlmin

USING_PYYAML = _pyyaml is not None


def load_yaml(text):
    """Parse a YAML string using PyYAML when available, else the bundled subset."""
    if _pyyaml is not None:
        return _pyyaml.safe_load(text)
    return yamlmin.safe_load(text)


def load_yaml_file(path):
    """Parse a YAML file, raising a message that names the file on failure."""
    path = Path(path)
    try:
        return load_yaml(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - re-raised with the filename attached
        raise ValueError("%s: %s" % (path, exc)) from exc
