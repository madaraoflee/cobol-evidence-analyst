"""Read-only Windows runtime identity and neutral in-memory SQLite checks.

This module does not change any application database or journal policy.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import struct
import sys


APPROVED_PIN = {
    "version": "3.54.1",
    "url": "https://sqlite.org/2026/sqlite-dll-win-x64-3540100.zip",
    "archive_sha3_256": "c2d09c8babb9399277055c057c97a53f1ffd7c5f11d0c1f2118ffdfa39f2daa4",
    "dll_sha256": "4b59d3cded9dc1608544160f3b4b10ac974c6ac0d6ffc23064e5f4c8dbd05aea",
    "source_id": "2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76",
}
SCHEMA = "sqlite-runtime-probe/v1"
REQUIRED_OPTIONS = {"ENABLE_FTS5", "ENABLE_RTREE", "ENABLE_MATH_FUNCTIONS", "THREADSAFE=1"}
REQUIRED_CHECKS = {"json", "fts4", "fts5", "rtree", "math", "serialize", "backup", "config"}


def _require(condition, message):
    if not condition:
        raise RuntimeError(message)


def _is_windows():
    return os.name == "nt"


def _root(path):
    path = Path(path)
    _require(path.is_absolute() and path.is_dir(), "A real absolute runtime root is required")
    return path.resolve()


def _contained_file(value, root):
    _require(isinstance(value, str) and bool(value), "Runtime file path is missing")
    path = Path(value)
    _require(path.is_absolute() and ".." not in path.parts, "Runtime path must be absolute")
    _require(path.is_file() and not path.is_symlink() and stat.S_ISREG(path.stat().st_mode),
             "Runtime path must name an ordinary file")
    try:
        path.resolve().relative_to(root)
    except ValueError as error:
        raise RuntimeError("Runtime file is outside the allowed root") from error
    return path


def validate_report(value, pin, allowed_root, *, identity_kind=None):
    """Validate identity, neutral checks and actual DLL bytes for either caller."""
    _require(pin == APPROVED_PIN, "Runtime pin is not the reviewed official component")
    root = _root(allowed_root)
    _require(isinstance(value, dict), "Runtime report must be an object")
    _require(value.get("schema_version") == SCHEMA and value.get("status") == "passed",
             "Runtime probe did not pass")
    _require(value.get("runtime_platform") == "Windows" and value.get("pointer_bits") == 64,
             "Windows x64 runtime is required")
    kind = value.get("identity_kind")
    _require(kind in {"source", "compiled"} and (identity_kind is None or kind == identity_kind),
             "Runtime identity kind differs from the required caller")
    _require(value.get("version") == pin["version"] and value.get("source_id") == pin["source_id"],
             "Loaded SQLite source identity differs from the pin")
    checks = value.get("checks")
    _require(isinstance(checks, dict) and set(checks) == REQUIRED_CHECKS
             and all(item is True for item in checks.values()), "Runtime feature checks are incomplete")
    options = value.get("compile_options")
    _require(isinstance(options, list) and all(isinstance(item, str) for item in options)
             and REQUIRED_OPTIONS.issubset(options), "Required SQLite compile options are missing")
    dll = _contained_file(value.get("loaded_dll_path"), root)
    _require(dll.name.lower() == "sqlite3.dll", "Unexpected SQLite library filename")
    actual = hashlib.sha256(dll.read_bytes()).hexdigest()
    _require(actual == value.get("loaded_dll_sha256") == pin["dll_sha256"],
             "Actual SQLite DLL bytes differ from the pin")
    extension = value.get("extension_path")
    if extension is None:
        _require(kind == "compiled" and value.get("extension_origin") == "built_in",
                 "A source runtime must identify its extension file")
    else:
        _contained_file(extension, root)
        _require(value.get("extension_origin") == "file", "Unexpected extension origin")
    return value


def _loaded_dll_path():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel.GetModuleHandleW.restype = ctypes.c_void_p
    kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
    kernel.GetModuleFileNameW.restype = ctypes.c_uint
    handle = kernel.GetModuleHandleW("sqlite3.dll")
    _require(bool(handle), "SQLite DLL is not loaded")
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetModuleFileNameW(handle, buffer, len(buffer))
    _require(0 < length < len(buffer), "Unable to identify the loaded SQLite DLL")
    return Path(buffer.value)


def _identity():
    import sqlite3
    import _sqlite3

    dll = _loaded_dll_path()
    connection = sqlite3.connect(":memory:")
    try:
        source_id = connection.execute("SELECT sqlite_source_id()").fetchone()[0]
        options = sorted(row[0] for row in connection.execute("PRAGMA compile_options"))
    finally:
        connection.close()
    extension = getattr(_sqlite3, "__file__", None)
    compiled = bool(globals().get("__compiled__") or getattr(sys, "frozen", False))
    return {
        "identity_kind": "compiled" if compiled else "source",
        "runtime_platform": platform.system(), "pointer_bits": struct.calcsize("P") * 8,
        "python_version": platform.python_version(),
        "python_major_minor": list(sys.version_info[:2]),
        "executable": sys.executable, "prefix": sys.prefix, "base_prefix": sys.base_prefix,
        "extension_path": str(extension) if extension else None,
        "extension_origin": "file" if extension else "built_in",
        "loaded_dll_path": str(dll),
        "loaded_dll_sha256": hashlib.sha256(dll.read_bytes()).hexdigest(),
        "version": sqlite3.sqlite_version, "source_id": source_id,
        "compile_options": options,
    }


def _memory_feature_checks():
    import sqlite3

    checks = {}
    db = sqlite3.connect(":memory:")
    try:
        _require(db.execute("SELECT json_extract('{\"value\":7}', '$.value')").fetchone()[0] == 7,
                 "JSON feature check failed")
        checks["json"] = True
        for kind in ("fts4", "fts5"):
            db.execute("CREATE VIRTUAL TABLE " + kind + "_probe USING " + kind + "(body)")
            db.execute("INSERT INTO " + kind + "_probe VALUES ('neutral business')")
            _require(db.execute("SELECT count(*) FROM " + kind + "_probe WHERE body MATCH 'neutral'").fetchone()[0] == 1,
                     "Full-text feature check failed")
            checks[kind] = True
        db.execute("CREATE VIRTUAL TABLE bounds_probe USING rtree(id, lo, hi)")
        db.execute("INSERT INTO bounds_probe VALUES (1, 2, 3)")
        _require(db.execute("SELECT count(*) FROM bounds_probe").fetchone()[0] == 1,
                 "Spatial feature check failed")
        checks["rtree"] = True
        _require(db.execute("SELECT sqrt(81)").fetchone()[0] == 9.0, "Math feature check failed")
        checks["math"] = True
        db.execute("CREATE TABLE amount_probe(value INTEGER)")
        db.execute("INSERT INTO amount_probe VALUES (42)")
        db.commit()
        encoded = db.serialize()
        restored, backed_up = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
        try:
            restored.deserialize(encoded)
            _require(restored.execute("SELECT value FROM amount_probe").fetchone()[0] == 42,
                     "Serialization feature check failed")
            checks["serialize"] = True
            db.backup(backed_up)
            _require(backed_up.execute("SELECT value FROM amount_probe").fetchone()[0] == 42,
                     "Backup feature check failed")
            checks["backup"] = True
            db.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
            _require(db.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE) is True,
                     "Connection configuration feature check failed")
            checks["config"] = True
        finally:
            restored.close()
            backed_up.close()
    finally:
        db.close()
    return checks


def run_probe(pin, allowed_root):
    """Probe the currently running interpreter/EXE without opening business files."""
    _require(pin == APPROVED_PIN, "Runtime pin is not the reviewed official component")
    root = _root(allowed_root)
    _require(_is_windows(), "Windows runtime probe requires Windows")
    result = {**_identity(), "schema_version": SCHEMA, "status": "passed",
              "checks": _memory_feature_checks(), "allowed_root": str(root)}
    return validate_report(result, pin, root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pin-manifest", required=True, type=Path)
    parser.add_argument("--allowed-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        pin = json.loads(args.pin_manifest.read_text(encoding="utf-8"))
        print(json.dumps(run_probe(pin, args.allowed_root), ensure_ascii=True))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted"}), file=sys.stderr)
        return 130
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__,
                          "error": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
