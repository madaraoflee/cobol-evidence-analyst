"""Keep bundled resources separate from a desktop user's writable profile."""

from __future__ import annotations

import os
from pathlib import Path
import sys


RESOURCE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = RESOURCE_ROOT.parent
DATA_DIR_ENV = "WORKBENCH_DATA_DIR"


def configured_data_dir() -> Path | None:
    value = os.environ.get(DATA_DIR_ENV)
    return Path(value).expanduser().resolve() if value else None


def config_root() -> Path:
    return configured_data_dir() or PROJECT_ROOT


def state_root() -> Path:
    return configured_data_dir() or PROJECT_ROOT / ".poc-data"


def default_desktop_data_dir(*, platform=None, environ=None, home=None) -> Path:
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    if platform == "win32":
        root = Path(environ["LOCALAPPDATA"]) if environ.get("LOCALAPPDATA") else home / "AppData" / "Local"
    elif platform == "darwin":
        root = home / "Library" / "Application Support"
    else:
        root = Path(environ["XDG_DATA_HOME"]) if environ.get("XDG_DATA_HOME") else home / ".local" / "share"
    return root / "COBOLWorkbench"
