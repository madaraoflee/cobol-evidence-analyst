"""Small local deployment settings, separate from credentials and source data."""

import json
import os
from pathlib import Path

from agent_policy import resolve_agent_policy


DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parents[1] / ".poc-data" / "agent-settings.json"
MAX_SETTINGS_BYTES = 32768


def load_agent_policy(path=None, *, environ=None):
    """Resolve optional deployment overrides without changing process settings.

    Missing default settings preserve existing behavior. An explicitly selected
    missing or invalid file must not silently expand a configured call budget.
    """
    environment = os.environ if environ is None else environ
    selected = path if path is not None else environment.get("AGENT_SETTINGS_PATH")
    location = Path(selected).expanduser() if selected is not None else DEFAULT_SETTINGS_PATH
    try:
        with location.open("rb") as handle:
            raw = handle.read(MAX_SETTINGS_BYTES + 1)
    except FileNotFoundError:
        if selected is None:
            return resolve_agent_policy()
        raise ValueError("AGENT_SETTINGS_UNREADABLE") from None
    except (OSError, ValueError):
        raise ValueError("AGENT_SETTINGS_UNREADABLE") from None
    if len(raw) > MAX_SETTINGS_BYTES:
        raise ValueError("AGENT_SETTINGS_TOO_LARGE")
    try:
        settings = json.loads(raw.decode("utf-8-sig"))
        if (not isinstance(settings, dict) or set(settings) - {"version", "agent"}
                or type(settings.get("version")) is not int or settings["version"] != 1
                or not isinstance(settings.get("agent"), dict)):
            raise ValueError()
        return resolve_agent_policy(settings["agent"])
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise ValueError("AGENT_SETTINGS_INVALID") from None
