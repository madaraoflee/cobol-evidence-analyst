"""Plan a local index upgrade without moving profiles or changing source inputs."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from analyze_source import _paths
from business_index import PARSER_VERSION
from compilation_context import resolve_compilation_context
from repo_inventory import parse_extensions, validate_source_options
from report_view import DIRECT_REPORT_BYTES, VIEW_REPORT_BYTES, report_sha256
from repository_discovery import SEARCH_VERSION
from structural_index import SCHEMA_VERSION


def _decision(status, reason, **values):
    return {"status": status, "reason_code": reason, **values}


_PARSER_UPGRADES = frozenset({("business-sparse-v1.7", "business-sparse-v1.9"),
                              ("business-sparse-v1.8", "business-sparse-v1.9")})
_FRONTEND_UPGRADES = frozenset({("cobol-facts-v5", "cobol-facts-v6")})
_INDEX_METADATA_QUERY = ("SELECT key,value FROM metadata WHERE key IN "
    "('source_root_hash','index_kind','schema_version','snapshot_id','parser_version','source_options')")
_SEARCH_METADATA_QUERY = "SELECT key,value FROM repo_metadata WHERE key IN ('ready','snapshot_id','version')"


def _same_compilation_inputs(recorded, current):
    if not isinstance(recorded, dict) or recorded.keys() != current.keys():
        return False
    for key, value in recorded.items():
        if value == current[key]:
            continue
        if (key != "frontend_version" or not isinstance(value, str)
                or (value, current[key]) not in _FRONTEND_UPGRADES):
            return False
    return True


def _full_repository_scope(output, source, snapshot):
    path = output / "diagnosis.json"
    if not path.is_file() or path.is_symlink():
        return False
    if path.stat().st_size <= DIRECT_REPORT_BYTES:
        report = json.loads(path.read_text(encoding="utf-8"))
    else:
        view_path = output / "diagnosis-view.json"
        if (view_path.is_symlink() or not view_path.is_file()
                or view_path.stat().st_size > VIEW_REPORT_BYTES):
            return False
        report = json.loads(view_path.read_text(encoding="utf-8"))
        projection = report.get("display_projection", {}) if isinstance(report, dict) else {}
        if (not isinstance(projection, dict) or projection.get("source_size") != path.stat().st_size
                or projection.get("source_sha256") != report_sha256(path)):
            return False
    if not isinstance(report, dict) or report.get("source_root") != str(source):
        return False
    if report.get("index_upgrade_scope_unverified") is True:
        return False
    build, scope = report.get("build_report"), report.get("scope")
    if not isinstance(build, dict) or not isinstance(scope, dict):
        return False
    build_scope = build.get("scope")
    return (isinstance(build_scope, dict) and build.get("snapshot_id") == snapshot
            and report.get("snapshot_id", snapshot) == snapshot
            and all(name in report and report[name] is None for name in ("entry_requested", "selected_entry"))
            and "selected_entry" in build and build["selected_entry"] is None
            and scope.get("kind") == build_scope.get("kind") == "full_directory"
            and scope.get("mode") in {"repository_index", "repository_question"}
            and build_scope.get("mode") in {"repository_index", "business_sparse_directory"})


def plan_workspace_upgrade(saved, *, check_integrity=True):
    """Return ordinary offline-start options only for a known version change.

    Current indexes take the metadata-only path. A proposed upgrade additionally
    checks database integrity and reproduces the saved compilation inputs.
    Missing, damaged or differently configured workspaces require explicit
    recovery; this function never writes any file or calls a model. A caller may
    skip a repeated integrity scan only when revalidating the same attempt key
    from an earlier checked plan within the same startup.
    """
    if not isinstance(saved, dict):
        return _decision("manual", "WORKSPACE_NOT_SAVED")
    if saved.get("restore_blocked") is True:
        return _decision("manual", "SOURCE_UPDATE_RECOVERY_REQUIRED")
    try:
        raw_source, raw_output = Path(saved["source"]), Path(saved["output"])
        if (not raw_source.is_absolute() or not raw_output.is_absolute()
                or raw_source.is_symlink() or raw_output.is_symlink()):
            return _decision("manual", "WORKSPACE_PATH_INVALID")
        source, output = _paths(raw_source, raw_output)
        if not source.is_dir():
            return _decision("manual", "SOURCE_DIRECTORY_UNAVAILABLE")
        pending = output / "source-update-pending.json"
        if pending.exists() or pending.is_symlink():
            return _decision("manual", "SOURCE_UPDATE_RECOVERY_REQUIRED")
        database = output / "structural-index.sqlite"
        if not database.is_file():
            return _decision("manual", "SOURCE_INDEX_UNAVAILABLE")
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
            connection.execute("BEGIN")
            metadata = dict(connection.execute(_INDEX_METADATA_QUERY))
            if metadata.get("source_root_hash") != hashlib.sha256(str(source).encode()).hexdigest():
                return _decision("manual", "SOURCE_ROOT_MISMATCH")
            schema_supported = metadata.get("schema_version") == SCHEMA_VERSION or (
                (metadata.get("schema_version"), SCHEMA_VERSION) == ("0.3", "0.4")
                and (metadata.get("parser_version"), PARSER_VERSION) in _PARSER_UPGRADES)
            if metadata.get("index_kind") != "business_sparse" or not schema_supported:
                return _decision("manual", "SOURCE_INDEX_KIND_UNSUPPORTED")
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", metadata.get("snapshot_id", "")):
                return _decision("manual", "SOURCE_INDEX_INVALID")
            search = dict(connection.execute(_SEARCH_METADATA_QUERY))
            if search.get("ready") != "1" or search.get("snapshot_id") != metadata["snapshot_id"]:
                return _decision("manual", "SOURCE_SEARCH_RECOVERY_REQUIRED")
            parser_changed = metadata.get("parser_version") != PARSER_VERSION
            search_changed = search.get("version") != SEARCH_VERSION
            if not parser_changed and not search_changed:
                return _decision("current", "SOURCE_INDEX_CURRENT")
            if ((parser_changed and (metadata.get("parser_version"), PARSER_VERSION) not in _PARSER_UPGRADES)
                    or search_changed):
                return _decision("manual", "SOURCE_INDEX_VERSION_UNSUPPORTED")
            options = json.loads(metadata["source_options"])
        if not isinstance(options, dict):
            return _decision("manual", "SOURCE_OPTIONS_INVALID")
        if set(options) - {"encoding", "source_format", "extensions", "include_extensionless", "compilation_context"}:
            return _decision("manual", "SOURCE_OPTIONS_NOT_REPRODUCIBLE")
        encoding, source_format = options["encoding"], options["source_format"]
        if not isinstance(encoding, str) or not isinstance(source_format, str):
            return _decision("manual", "SOURCE_OPTIONS_INVALID")
        validate_source_options(encoding, source_format)
        extensions = options["extensions"]
        if (not isinstance(extensions, list) or not extensions or
                not all(isinstance(item, str) and item.startswith(".") and "," not in item for item in extensions)):
            return _decision("manual", "SOURCE_OPTIONS_INVALID")
        extension_text = ",".join(sorted(extensions))
        if len(extension_text) > 256 or sorted(parse_extensions(extension_text)) != sorted(extensions):
            return _decision("manual", "SOURCE_OPTIONS_INVALID")
        # The web intake currently always includes extensionless members.
        if options.get("include_extensionless") is not True:
            return _decision("manual", "SOURCE_OPTIONS_NOT_REPRODUCIBLE")
        preferences = saved.get("source_preferences")
        if preferences is not None:
            if not isinstance(preferences, dict):
                return _decision("manual", "SOURCE_OPTIONS_CHANGED")
            if (Path(preferences.get("source", "")).resolve() != source
                    or Path(preferences.get("output", "")).resolve() != output
                    or preferences.get("encoding", "auto") != encoding
                    or preferences.get("source_format", "auto") != source_format):
                return _decision("manual", "SOURCE_OPTIONS_CHANGED")
            selected_extensions = preferences.get("extensions")
            if selected_extensions is None:
                selected_extensions = sorted(parse_extensions(None))
            if sorted(selected_extensions) != sorted(extensions):
                return _decision("manual", "SOURCE_OPTIONS_CHANGED")
        preferences = preferences or {}
        context_path = preferences.get("compilation_context_path")
        if context_path:
            context_file = Path(context_path)
            if (not context_file.is_absolute() or context_file.is_symlink()
                    or not context_file.is_file() or context_file.stat().st_size > 128000):
                return _decision("manual", "COMPILATION_CONTEXT_UNAVAILABLE")
            context = resolve_compilation_context(json.loads(context_file.read_text(encoding="utf-8")),
                                                  source_options=options).to_dict()
        else:
            context = resolve_compilation_context(source_options=options).to_dict()
        # Internal parser generations advance during an application upgrade.
        # User compilation inputs stay exact; no condition or COPY order is guessed.
        if "compilation_context" in options and not _same_compilation_inputs(options["compilation_context"], context):
            return _decision("manual", "COMPILATION_CONTEXT_CHANGED")
        if "compilation_context" not in options and context_path:
            return _decision("manual", "COMPILATION_CONTEXT_CHANGED")
        request = {"source": str(source), "output": str(output), "encoding": encoding,
                   "source_format": source_format, "extensions": extension_text,
                   "question": None, "allow_network": False, "verify_content": True,
                   "index_mode": "full", "reading_strategy": "retrieval",
                   "answer_detail": "brief" if saved.get("answer_detail") == "brief" else "detailed"}
        if context_path:
            request["compilation_context_path"] = str(context_file)
        reference = saved.get("framework_reference_path")
        if isinstance(reference, str) and reference:
            request["framework_reference_path"] = reference
        attempt_identity = {"source_root_hash": metadata["source_root_hash"],
                            "snapshot_id": metadata["snapshot_id"], "source_options": options,
                            "parser_version": metadata["parser_version"], "search_version": search["version"],
                            "schema_version": metadata["schema_version"], "target_schema_version": SCHEMA_VERSION,
                            "target_parser_version": PARSER_VERSION, "target_search_version": SEARCH_VERSION,
                            "target_compilation_context": context}
        attempt_key = hashlib.sha256(json.dumps(attempt_identity, sort_keys=True,
                                               separators=(",", ":")).encode()).hexdigest()
        if saved.get("index_upgrade_attempt") == attempt_key:
            return _decision("manual", "INDEX_UPGRADE_PREVIOUS_ATTEMPT")
        if not _full_repository_scope(output, source, metadata["snapshot_id"]):
            return _decision("manual", "SOURCE_SCOPE_NOT_REPRODUCIBLE")
        # A failed attempt must not rescan a large index on every restart.
        # Check integrity only for a new eligible upgrade, then bind that check
        # to the same metadata used to reconstruct its source settings.
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
            connection.execute("BEGIN")
            if (dict(connection.execute(_INDEX_METADATA_QUERY)) != metadata
                    or dict(connection.execute(_SEARCH_METADATA_QUERY)) != search):
                return _decision("manual", "SOURCE_INDEX_CHANGED_DURING_UPGRADE_CHECK")
            if check_integrity:
                if connection.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
                    return _decision("manual", "SOURCE_INDEX_INVALID")
                if connection.execute("SELECT 1 FROM source_files LIMIT 1").fetchone() is None:
                    return _decision("manual", "SOURCE_INDEX_EMPTY")
        return _decision("ready", "SOURCE_INDEX_VERSION_UPGRADE", options=request,
                         parser_version=metadata["parser_version"], search_version=search["version"],
                         attempt_key=attempt_key)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return _decision("manual", "SOURCE_INDEX_INVALID")
