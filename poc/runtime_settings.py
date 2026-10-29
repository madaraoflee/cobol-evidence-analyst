"""Small local deployment settings, separate from credentials and source data."""

import json
import os
from pathlib import Path

from agent_policy import resolve_agent_policy
from app_paths import state_root


DEFAULT_SETTINGS_PATH = state_root() / "agent-settings.json"
MAX_SETTINGS_BYTES = 32768


def _unique_settings_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("AGENT_SETTINGS_INVALID")
        result[key] = value
    return result


def load_agent_policy(path=None, *, environ=None):
    """Resolve optional deployment overrides without changing process settings.

    Missing default settings preserve existing behavior. An explicitly selected
    missing or invalid file must not silently expand a configured call budget.
    Missing/null request and investigation limits use source-bound progress;
    explicit numbers retain their deployment limits. Missing/null time limits do not
    create a hard deadline. Any explicit numeric deadline, including a saved 180,
    remains an override: its provenance cannot be inferred safely from its value.
    """
    environment = os.environ if environ is None else environ
    selected = path if path is not None else environment.get("AGENT_SETTINGS_PATH")
    try:
        location = Path(selected).expanduser() if selected is not None else DEFAULT_SETTINGS_PATH
        with location.open("rb") as handle:
            raw = handle.read(MAX_SETTINGS_BYTES + 1)
    except FileNotFoundError:
        if selected is None:
            return resolve_agent_policy()
        raise ValueError("AGENT_SETTINGS_UNREADABLE") from None
    except (OSError, ValueError, TypeError, RuntimeError):
        raise ValueError("AGENT_SETTINGS_UNREADABLE") from None
    if len(raw) > MAX_SETTINGS_BYTES:
        raise ValueError("AGENT_SETTINGS_TOO_LARGE")
    try:
        settings = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_settings_object)
        if (not isinstance(settings, dict) or set(settings) - {"version", "agent"}
                or type(settings.get("version")) is not int or settings["version"] != 1
                or not isinstance(settings.get("agent"), dict)):
            raise ValueError()
        return resolve_agent_policy(settings["agent"])
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise ValueError("AGENT_SETTINGS_INVALID") from None
