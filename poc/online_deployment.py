"""Prepare and inspect source-only producer tools without deploying services."""

from __future__ import annotations

import argparse
import ast
from contextlib import closing
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

# A plain source CLI invocation must not add unlisted cache files to its bundle.
sys.dont_write_bytecode = True

from online_material_format import MaterialError, canonical_json, decode_json, digest, valid_hash


SCHEMA = "online-producer-sources/v1"
MANIFEST = "producer-manifest.json"
ENTRIES = ("online_deployment.py", "online_preflight.py", "online_publish.py")
MAX_FILES = 256
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024
_NAME = re.compile(r"[a-z][a-z0-9_]*\.py\Z")
_LIMIT = 1_000_000


def _safe_path(path):
    value = Path(os.path.abspath(Path(path).expanduser()))
    if any(item.is_symlink() for item in (value, *value.parents)):
        raise MaterialError("DEPLOYMENT_SYMLINK_REJECTED")
    return value


def _read(path, maximum):
    if path.is_symlink() or not path.is_file():
        raise MaterialError("DEPLOYMENT_SOURCE_MISSING")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise MaterialError("DEPLOYMENT_SIZE_LIMIT")
    return data


def _closure(directory):
    """Follow imports at every AST depth; do not execute or infer dynamic loads."""
    pending, files = list(ENTRIES), {}
    total = 0
    while pending:
        name = pending.pop()
        if name in files:
            continue
        if len(files) >= MAX_FILES:
            raise MaterialError("DEPLOYMENT_FILE_LIMIT")
        data = _read(directory / name, MAX_FILE_BYTES)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise MaterialError("DEPLOYMENT_SIZE_LIMIT")
        try:
            tree = ast.parse(data, filename=name)
        except (SyntaxError, ValueError, RecursionError):
            raise MaterialError("DEPLOYMENT_INVALID_PYTHON") from None
        files[name] = data
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    modules.add(node.module.split(".")[0])
                else:
                    modules.update(alias.name for alias in node.names)
        for module in sorted(modules):
            candidate = module + ".py"
            if _NAME.fullmatch(candidate) and (directory / candidate).exists():
                pending.append(candidate)
            elif module not in sys.stdlib_module_names and module not in sys.builtin_module_names:
                raise MaterialError("DEPLOYMENT_DEPENDENCY_MISSING")
    return files


def _manifest(files):
    return {"schema_version": SCHEMA, "entries": list(ENTRIES),
            "scope": "static_python_import_closure", "dynamic_imports_inferred": False,
            "files": {name: {"sha256": digest(data), "size": len(data)}
                      for name, data in sorted(files.items())}}


def stage(output, *, source_directory=None):
    source = _safe_path(source_directory or Path(__file__).parent)
    destination = _safe_path(output)
    if destination.exists():
        raise MaterialError("DEPLOYMENT_DESTINATION_EXISTS")
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise MaterialError("DEPLOYMENT_DESTINATION_OVERLAPS_SOURCE")
    if not destination.parent.is_dir():
        raise MaterialError("DEPLOYMENT_PARENT_MISSING")
    files = _closure(source)
    manifest = _manifest(files)
    destination.mkdir(mode=0o700)
    try:
        for name, data in {**files, MANIFEST: canonical_json(manifest)}.items():
            with (destination / name).open("xb") as stream:
                stream.write(data)
            (destination / name).chmod(0o644)
        verify(destination)
    except BaseException:
        shutil.rmtree(destination)
        raise
    return {"status": "sources_staged", "files": len(files),
            "source_bytes": sum(map(len, files.values())), "manifest_sha256": digest(canonical_json(manifest)),
            "scope": manifest["scope"], "dynamic_imports_inferred": False}


def verify(directory):
    root = _safe_path(directory)
    if not root.is_dir():
        raise MaterialError("DEPLOYMENT_DIRECTORY_MISSING")
    raw = _read(root / MANIFEST, MAX_MANIFEST_BYTES)
    manifest = decode_json(raw, maximum=MAX_MANIFEST_BYTES)
    if (not isinstance(manifest, dict)
            or set(manifest) != {"schema_version", "entries", "scope", "dynamic_imports_inferred", "files"}
            or manifest["schema_version"] != SCHEMA or manifest["entries"] != list(ENTRIES)
            or manifest["scope"] != "static_python_import_closure"
            or manifest["dynamic_imports_inferred"] is not False
            or not isinstance(manifest["files"], dict) or not 1 <= len(manifest["files"]) <= MAX_FILES):
        raise MaterialError("DEPLOYMENT_INVALID_MANIFEST")
    records = manifest["files"]
    for name, record in records.items():
        if (not isinstance(name, str) or not _NAME.fullmatch(name)
                or not isinstance(record, dict) or set(record) != {"sha256", "size"}
                or type(record["size"]) is not int or not 0 < record["size"] <= MAX_FILE_BYTES):
            raise MaterialError("DEPLOYMENT_INVALID_MANIFEST")
        valid_hash(record["sha256"])
    if sum(item["size"] for item in records.values()) > MAX_TOTAL_BYTES:
        raise MaterialError("DEPLOYMENT_SIZE_LIMIT")
    entries = list(root.iterdir())
    if any(path.is_symlink() for path in entries):
        raise MaterialError("DEPLOYMENT_SYMLINK_REJECTED")
    if {path.name for path in entries} != set(records) | {MANIFEST}:
        raise MaterialError("DEPLOYMENT_FILE_SET_MISMATCH")
    for name, record in records.items():
        data = _read(root / name, MAX_FILE_BYTES)
        if len(data) != record["size"] or digest(data) != record["sha256"]:
            raise MaterialError("DEPLOYMENT_SOURCE_HASH_MISMATCH")
    files = _closure(root)
    if set(files) != set(records) or canonical_json(_manifest(files)) != raw:
        raise MaterialError("DEPLOYMENT_CLOSURE_MISMATCH")
    return {"status": "sources_verified", "files": len(files),
            "source_bytes": sum(map(len, files.values())), "manifest_sha256": digest(raw),
            "scope": manifest["scope"], "dynamic_imports_inferred": False}


def _storage(directory, minimum_free_bytes):
    path = _safe_path(directory)
    if not path.is_dir():
        raise MaterialError("DEPLOYMENT_DIRECTORY_MISSING")
    with tempfile.TemporaryFile(dir=path) as stream:
        stream.write(b"producer-runtime-check\n")
        stream.flush()
        os.fsync(stream.fileno())
    free = shutil.disk_usage(path).free
    return {"status": "passed" if free >= minimum_free_bytes else "failed",
            "free_bytes": free, "minimum_free_bytes": minimum_free_bytes,
            **({"error": "DEPLOYMENT_FREE_SPACE_BELOW_FLOOR"} if free < minimum_free_bytes else {})}


def runtime(temporary_directory, output_directory, minimum_free_bytes):
    if type(minimum_free_bytes) is not int or not 0 <= minimum_free_bytes <= 2**63 - 1:
        raise MaterialError("DEPLOYMENT_INVALID_RESOURCE_FLOOR")
    checks = {"python": {"status": "passed" if sys.version_info >= (3, 10) else "failed"}}
    try:
        result = subprocess.run(["git", "--version"], capture_output=True, timeout=10, check=False)
        checks["git"] = {"status": "passed" if result.returncode == 0 else "failed"}
    except (OSError, subprocess.TimeoutExpired):
        checks["git"] = {"status": "failed", "error": "DEPLOYMENT_GIT_UNAVAILABLE"}
    try:
        with closing(sqlite3.connect(":memory:")) as database:
            database.execute("CREATE VIRTUAL TABLE runtime_fts USING fts5(content)")
            database.execute("INSERT INTO runtime_fts VALUES ('probe')")
            if database.execute("SELECT COUNT(*) FROM runtime_fts WHERE runtime_fts MATCH 'probe'").fetchone()[0] != 1:
                raise sqlite3.DatabaseError()
        checks["sqlite_fts5"] = {"status": "passed"}
    except sqlite3.Error:
        checks["sqlite_fts5"] = {"status": "failed", "error": "DEPLOYMENT_FTS5_UNAVAILABLE"}
    for key, directory in (("temporary_storage", temporary_directory), ("output_storage", output_directory)):
        try:
            checks[key] = _storage(directory, minimum_free_bytes)
        except (MaterialError, OSError) as error:
            checks[key] = {"status": "failed", "error": getattr(error, "code", "DEPLOYMENT_STORAGE_UNAVAILABLE")}
    return {"status": "runtime_checks_passed" if all(item["status"] == "passed" for item in checks.values()) else "failed",
            "checks": checks, "resource_floor_is_capacity_guarantee": False}


def inventory(source, commit, subdirectory="", *, include_extensionless=False):
    from online_analysis import _git, _verify_checkout
    from repo_inventory import DEFAULT_EXTENSIONS, iter_source_files

    root = _safe_path(source)
    _verify_checkout(root, commit, subdirectory)
    if _git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", "."):
        raise MaterialError("MATERIAL_UNTRACKED_SOURCE")
    summary = {}
    selected = []
    for path in iter_source_files(root, DEFAULT_EXTENSIONS, include_extensionless, scan_summary=summary):
        if len(selected) >= _LIMIT:
            raise MaterialError("DEPLOYMENT_INVENTORY_LIMIT")
        selected.append(path.relative_to(root).as_posix())
    _verify_checkout(root, commit, subdirectory, selected)
    # Git invoked from the scope root returns paths relative to that scope.
    tracked = _git(root, "ls-files", "-z", "--", ".").split(b"\0")
    count = sum(bool(path) for path in tracked)
    if count > _LIMIT:
        raise MaterialError("DEPLOYMENT_INVENTORY_LIMIT")
    excluded = summary["excluded_extensions"]
    return {"status": "inventory_checked", "coverage_scope": "discovery_selection_only",
            "commit": commit, "git_tracked_files": count, "selected_files": len(selected),
            "selected_tracked_files": len(selected), "tracked_not_selected": count - len(selected),
            "excluded_extensionless_file_count": summary["excluded_extensionless_file_count"],
            "excluded_extensions": dict(sorted(excluded.items())[:128]),
            "excluded_extension_kinds_truncated": len(excluded) > 128,
            "ignored_directories_and_symlinks": "not_traversed", "source_body_in_report": False,
            "source_content_parsed": False, "git_worktree_content_check": True,
            "source_semantics_verified": False, "sync_completeness": "not_verified"}


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    make = commands.add_parser("stage")
    make.add_argument("--output", required=True)
    check = commands.add_parser("verify")
    check.add_argument("--directory", required=True)
    environment = commands.add_parser("runtime")
    environment.add_argument("--temporary-directory", required=True)
    environment.add_argument("--output-directory", required=True)
    environment.add_argument("--minimum-free-bytes", required=True, type=int)
    scope = commands.add_parser("inventory")
    scope.add_argument("--source", required=True)
    scope.add_argument("--commit", required=True)
    scope.add_argument("--subdirectory", default="")
    scope.add_argument("--include-extensionless", action="store_true")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "stage":
            result = stage(args.output)
        elif args.command == "verify":
            result = verify(args.directory)
        elif args.command == "runtime":
            result = runtime(args.temporary_directory, args.output_directory, args.minimum_free_bytes)
        else:
            result = inventory(args.source, args.commit, args.subdirectory,
                               include_extensionless=args.include_extensionless)
    except (MaterialError, OSError, ValueError, RecursionError) as error:
        result = {"status": "failed", "error": getattr(error, "code", "DEPLOYMENT_OPERATION_FAILED")}
    print(canonical_json(result).decode("utf-8"))
    return 2 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
