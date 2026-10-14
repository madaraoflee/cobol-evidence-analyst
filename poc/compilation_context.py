"""Immutable compilation inputs shared by expansion, parsing and cache identity."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import PurePosixPath
from statement_facts import FRONTEND_VERSION


@dataclass(frozen=True)
class CompilationContext:
    dialect: str = "unknown"
    encoding: str = "auto"
    source_format: str = "auto"
    copy_libraries: tuple[str, ...] = ()
    copy_library_order_known: bool = False
    compile_conditions: tuple[tuple[str, bool | str | int], ...] = ()
    conditions_complete: bool = False
    preprocessing_version: str = "bounded-copy-v2"
    frontend_version: str = FRONTEND_VERSION
    framework_version: str | None = None
    target_profile: str = "generic-cobol"
    resource_kinds: tuple[str, ...] = ("cobol-file", "sql")
    debug_lines: bool | None = None
    nested_copy_replacing: bool | None = None

    def __post_init__(self):
        if self.source_format not in {"auto", "free", "fixed"}:
            raise ValueError("COMPILATION_SOURCE_FORMAT_INVALID")
        for library in self.copy_libraries:
            path = PurePosixPath(library)
            if path.is_absolute() or ".." in path.parts or "\\" in library:
                raise ValueError("COMPILATION_COPY_LIBRARY_INVALID")
        names = [name.upper() for name, _ in self.compile_conditions]
        if len(set(names)) != len(names):
            raise ValueError("COMPILATION_CONDITION_DUPLICATE")

    def to_dict(self):
        return json.loads(json.dumps(asdict(self)))

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True,
            ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

    def condition(self, name):
        values = {key.upper(): value for key, value in self.compile_conditions}
        return values.get(name.upper(), False if self.conditions_complete else None)


def resolve_compilation_context(value=None, *, source_options=None):
    if isinstance(value, CompilationContext):
        return value
    values = dict(value or {})
    if source_options:
        values = {key: source_options[key] for key in ("encoding", "source_format")
                  if key in source_options} | values
    conditions = values.get("compile_conditions", ())
    if isinstance(conditions, dict):
        conditions = sorted(conditions.items())
    values["compile_conditions"] = tuple((str(key).upper(), setting) for key, setting in conditions)
    for key in ("copy_libraries", "resource_kinds"):
        if key in values:
            values[key] = tuple(values[key])
    return CompilationContext(**values)
