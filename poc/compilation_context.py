"""Immutable compilation inputs shared by expansion, parsing and cache identity."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
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
        if not isinstance(self.source_format, str) or self.source_format not in {"auto", "free", "fixed"}:
            raise ValueError("COMPILATION_SOURCE_FORMAT_INVALID")
        for name in ("dialect", "encoding", "preprocessing_version", "frontend_version", "target_profile"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("COMPILATION_CONTEXT_INVALID")
        if self.framework_version is not None and (
                not isinstance(self.framework_version, str) or not self.framework_version.strip()):
            raise ValueError("COMPILATION_CONTEXT_INVALID")
        for name in ("copy_library_order_known", "conditions_complete"):
            if type(getattr(self, name)) is not bool:
                raise ValueError("COMPILATION_CONTEXT_INVALID")
        for name in ("debug_lines", "nested_copy_replacing"):
            if getattr(self, name) is not None and type(getattr(self, name)) is not bool:
                raise ValueError("COMPILATION_CONTEXT_INVALID")
        for name in ("copy_libraries", "resource_kinds"):
            values = getattr(self, name)
            if not isinstance(values, (list, tuple)) or any(
                    not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError("COMPILATION_CONTEXT_INVALID")
            object.__setattr__(self, name, tuple(values))
        for library in self.copy_libraries:
            path = PurePosixPath(library)
            if path.is_absolute() or PureWindowsPath(library).drive or ".." in path.parts or "\\" in library:
                raise ValueError("COMPILATION_COPY_LIBRARY_INVALID")
        conditions = self.compile_conditions
        if isinstance(conditions, Mapping):
            conditions = list(conditions.items())
        if not isinstance(conditions, (list, tuple)) or any(
                not isinstance(item, (list, tuple)) or len(item) != 2
                or not isinstance(item[0], str) or not item[0].strip()
                or type(item[1]) not in (bool, str, int) for item in conditions):
            raise ValueError("COMPILATION_CONDITION_INVALID")
        conditions = tuple((name.upper(), value) for name, value in conditions)
        names = [name for name, _ in conditions]
        if len(set(names)) != len(names):
            raise ValueError("COMPILATION_CONDITION_DUPLICATE")
        object.__setattr__(self, "compile_conditions", conditions)

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
    if value is not None and not isinstance(value, Mapping):
        raise ValueError("COMPILATION_CONTEXT_INVALID")
    values = dict(value) if value is not None else {}
    if set(values) - {item.name for item in fields(CompilationContext)}:
        raise ValueError("COMPILATION_CONTEXT_INVALID")
    if source_options:
        values = {key: source_options[key] for key in ("encoding", "source_format")
                  if key in source_options} | values
    conditions = values.get("compile_conditions", ())
    if isinstance(conditions, Mapping):
        if any(not isinstance(key, str) for key in conditions):
            raise ValueError("COMPILATION_CONDITION_INVALID")
        values["compile_conditions"] = sorted(conditions.items())
    return CompilationContext(**values)
