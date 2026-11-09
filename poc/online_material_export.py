"""Export an existing sparse index into immutable, portable query objects.

The source database and source tree are read-only inputs. This exporter does
not establish repository-wide completeness or run an analysis/model request.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

from business_index import PARSER_VERSION
from compilation_context import resolve_compilation_context
from online_material_format import (
    SCHEMA_VERSION, INLINE_POSTING_BYTES, INLINE_POSTING_PATHS,
    MAX_OBJECT_BYTES, MAX_SOURCE_BYTES, MaterialError, bucket,
    canonical_json, digest, object_reference, reference_key, relative_path,
    seal_manifest, tokens, valid_commit, valid_hash,
)
from source_reading import _safe_file, _verified_lines
from structural_index import SCHEMA_VERSION as INDEX_SCHEMA_VERSION


PAGE_ITEMS = 256
_PAGE_BYTES = min(MAX_OBJECT_BYTES, 1024 * 1024)
_OPTIONS = {"encoding", "source_format", "extensions", "include_extensionless", "compilation_context"}
_RELATION_METADATA = {
    "boundary", "source_index_kind", "semantic_binding_performed", "compilation_status",
    "compilation_guards", "diagnostics", "support_basis", "range_end",
}


def _cancel(check):
    if check is not None:
        check()


def _json(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError, RecursionError):
        raise MaterialError("MATERIAL_INVALID_INDEX") from None


def _portable_options(value):
    if not isinstance(value, dict) or set(value) != _OPTIONS:
        raise MaterialError("MATERIAL_INVALID_OPTIONS")
    if (not isinstance(value["encoding"], str) or not value["encoding"]
            or value["source_format"] not in {"auto", "free", "fixed"}
            or type(value["include_extensionless"]) is not bool
            or not isinstance(value["extensions"], list)
            or any(not isinstance(item, str) or not item.startswith(".")
                   or "/" in item or "\\" in item for item in value["extensions"])):
        raise MaterialError("MATERIAL_INVALID_OPTIONS")
    try:
        compilation = resolve_compilation_context(value["compilation_context"], source_options=value).to_dict()
    except (ValueError, TypeError):
        raise MaterialError("MATERIAL_INVALID_OPTIONS") from None
    if compilation != value["compilation_context"]:
        raise MaterialError("MATERIAL_INVALID_OPTIONS")

    def check(item):
        if isinstance(item, str):
            if (item.startswith(("/", "\\", "file:")) or re.match(r"^[A-Za-z]:", item)
                    or any(ord(char) < 32 for char in item)):
                raise MaterialError("MATERIAL_NONPORTABLE_OPTIONS")
        elif isinstance(item, dict):
            for key, child in item.items():
                check(key)
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)

    check(value)
    return value


def _metadata(db, root):
    metadata = dict(db.execute("SELECT key,value FROM metadata"))
    if (metadata.get("index_kind") != "business_sparse"
            or metadata.get("parser_version") != PARSER_VERSION
            or metadata.get("schema_version") != INDEX_SCHEMA_VERSION):
        raise MaterialError("MATERIAL_UNSUPPORTED_INDEX")
    if metadata.get("source_root_hash") != digest(str(root).encode("utf-8")):
        raise MaterialError("MATERIAL_SOURCE_ROOT_MISMATCH")
    options = _portable_options(_json(metadata.get("source_options")))
    snapshot = metadata.get("snapshot_id", "")
    if not snapshot.startswith("sha256:"):
        raise MaterialError("MATERIAL_INVALID_INDEX")
    valid_hash(snapshot[7:])
    return metadata, {"index_kind": metadata["index_kind"], "parser_version": metadata["parser_version"],
                      "schema_version": metadata["schema_version"], "source_options": options}


def _write_object(destination, value):
    data = canonical_json(value)
    reference = object_reference(data)
    path = destination / reference["path"]
    try:
        with path.open("xb") as stream:
            stream.write(data)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != data:
            raise MaterialError("MATERIAL_OBJECT_CONFLICT") from None
    return reference


def _post(spool, kind, key, path):
    spool.execute("INSERT OR IGNORE INTO postings VALUES (?,?,?,?)", (kind, bucket(key), key, path))


def _add_terms(spool, text, path):
    spool.executemany("INSERT OR IGNORE INTO postings VALUES ('terms',?,?,?)",
                      ((bucket(term), term, path) for term in tokens(text)))


def _fact_rows(db, path, sha, line_count):
    def verify(value, first="start_line", last="end_line"):
        if (type(value.get(first)) is not int or type(value.get(last)) is not int
                or not 1 <= value[first] <= value[last] <= line_count):
            raise MaterialError("MATERIAL_INVALID_FACT_LOCATION")
        if "_source_sha256" in value and value.pop("_source_sha256") != sha:
            raise MaterialError("MATERIAL_FACT_SOURCE_MISMATCH")
        if "_source_path" in value and value.pop("_source_path") != path:
            raise MaterialError("MATERIAL_FACT_SOURCE_MISMATCH")
        return value

    symbols = db.execute("""SELECT s.symbol_id,s.symbol_type,s.name,s.program_name,s.qualified_name,
        s.definition_unit_id,s.evidence_id,u.start_line,u.end_line,u.parse_status,
        e.source_sha256 AS _source_sha256,e.relative_path AS _source_path
        FROM symbols s LEFT JOIN code_units u ON u.unit_id=s.definition_unit_id
        LEFT JOIN evidence_spans e ON e.evidence_id=s.evidence_id
        WHERE s.relative_path=? ORDER BY s.symbol_id""", (path,))
    for row in symbols:
        yield "symbols", verify(dict(row))
    relations = db.execute("""SELECT r.relation_id,r.from_entity_id,r.relation_type,r.target_name,
        r.target_scope,r.target_entity_id,r.status,r.evidence_id,r.metadata_json,
        e.start_line,e.end_line,u.program_name,
        e.source_sha256 AS _source_sha256,e.relative_path AS _source_path
        FROM relations r LEFT JOIN evidence_spans e ON e.evidence_id=r.evidence_id
        LEFT JOIN code_units u ON u.unit_id=r.from_entity_id
        WHERE r.relative_path=? ORDER BY r.relation_id""", (path,))
    for row in relations:
        value = verify(dict(row))
        metadata = _json(value.pop("metadata_json"))
        if not isinstance(metadata, dict):
            raise MaterialError("MATERIAL_INVALID_INDEX")
        value["metadata"] = {key: metadata[key] for key in sorted(_RELATION_METADATA & metadata.keys())}
        yield "relations", value
    rules = db.execute("""SELECT rule_id,program_name,paragraph_name,rule_kind,first_line,last_line,
        occurrence_count,reads_json,writes_json,condition_json FROM business_rules
        WHERE relative_path=? ORDER BY rule_id""", (path,))
    for row in rules:
        value = verify(dict(row), "first_line", "last_line")
        for stored, public in (("reads_json", "reads"), ("writes_json", "writes"), ("condition_json", "condition")):
            value[public] = _json(value.pop(stored))
        yield "rules", value


def _export_facts(db, spool, destination, path, sha, line_count, check):
    references = []
    base = {"kind": "facts", "relative_path": path, "source_sha256": sha}
    page = {**base, "symbols": [], "relations": [], "rules": []}
    count, size = 0, len(canonical_json(page))
    for kind, value in _fact_rows(db, path, sha, line_count):
        _cancel(check)
        item_size = len(canonical_json(value)) + 1
        if item_size + len(canonical_json(base)) + 64 > _PAGE_BYTES:
            raise MaterialError("MATERIAL_FACT_SIZE_LIMIT")
        if count and (count >= PAGE_ITEMS or size + item_size > _PAGE_BYTES):
            references.append(_write_object(destination, page))
            page = {**base, "symbols": [], "relations": [], "rules": []}
            count, size = 0, len(canonical_json(page))
        page[kind].append(value)
        count += 1
        size += item_size
        if kind == "symbols":
            for key in ("name", "program_name", "qualified_name"):
                if value.get(key):
                    _add_terms(spool, value[key], path)
        elif kind == "relations" and value["relation_type"] in {"CALLS", "INCLUDES_COPY"} and value["target_name"]:
            _post(spool, "references", reference_key(value["relation_type"], value["target_name"]), path)
    if count:
        references.append(_write_object(destination, page))
    return references


def _export_files(db, spool, root, destination, check):
    total, source_bytes = 0, 0
    snapshot = hashlib.sha256()
    for raw in db.execute("SELECT * FROM source_files ORDER BY relative_path COLLATE material_casefold"):
        _cancel(check)
        row = dict(raw)
        path, sha = relative_path(row["relative_path"]), valid_hash(row["sha256"])
        if type(row["line_count"]) is not int or row["line_count"] < 0:
            raise MaterialError("MATERIAL_INVALID_INDEX")
        physical = _safe_file(root, path)
        size = physical.stat().st_size
        if size > MAX_SOURCE_BYTES:
            raise MaterialError("MATERIAL_SOURCE_SIZE_LIMIT")
        _add_terms(spool, path, path)
        # Exhausting this iterator verifies bytes, encoding, line count and
        # file identity. The high limit retains long physical lines in the
        # bounded single-file budget instead of silently dropping their terms.
        for text, truncated in _verified_lines(root, row, check, MAX_SOURCE_BYTES):
            _cancel(check)
            if truncated:
                raise MaterialError("MATERIAL_SOURCE_SIZE_LIMIT")
            _add_terms(spool, text, path)
        record = {"relative_path": path, "sha256": sha, "size": size, "encoding": row["encoding"],
                  "source_format": row["format_hint"], "line_count": row["line_count"],
                  "facts": _export_facts(db, spool, destination, path, sha, row["line_count"], check)}
        spool.execute("INSERT INTO files VALUES (?,?,?)", (bucket(path), path, canonical_json(record)))
        snapshot.update(path.encode("utf-8") + b"\0" + sha.encode("ascii") + b"\n")
        source_bytes += size
        total += 1
    if not total:
        raise MaterialError("MATERIAL_EMPTY_INDEX")
    return total, source_bytes, "sha256:" + snapshot.hexdigest()


def _routing_bucket(destination, kind, rows, check):
    entries, size = {}, len(canonical_json({"kind": kind, "entries": {}}))
    for key, value in rows:
        _cancel(check)
        size += len(canonical_json(key)) + len(canonical_json(value)) + 2
        if size > MAX_OBJECT_BYTES:
            raise MaterialError("MATERIAL_BUCKET_SIZE_LIMIT")
        entries[key] = value
    return _write_object(destination, {"kind": kind, "entries": entries})


def _posting_entry(spool, destination, kind, key, check):
    paths, pages, count = [], [], 0
    for (path,) in spool.execute("SELECT path FROM postings WHERE kind=? AND key=? ORDER BY path", (kind, key)):
        _cancel(check)
        paths.append(path)
        count += 1
        if len(paths) == PAGE_ITEMS:
            pages.append(_write_object(destination, {"kind": "postings", "key": key, "paths": paths}))
            paths = []
    if not pages and count <= INLINE_POSTING_PATHS and len(canonical_json(paths)) <= INLINE_POSTING_BYTES:
        return {"count": count, "paths": paths}
    if paths:
        pages.append(_write_object(destination, {"kind": "postings", "key": key, "paths": paths}))
    return {"count": count, "pages": pages}


def _export_routes(spool, destination, check):
    routes = {"files": {}, "terms": {}, "references": {}}
    for (prefix,) in spool.execute("SELECT DISTINCT prefix FROM files ORDER BY prefix"):
        rows = ((path, _json(record)) for path, record in
                spool.execute("SELECT path,record FROM files WHERE prefix=? ORDER BY path", (prefix,)))
        routes["files"][prefix] = _routing_bucket(destination, "files", rows, check)
    for kind in ("terms", "references"):
        for (prefix,) in spool.execute("SELECT DISTINCT prefix FROM postings WHERE kind=? ORDER BY prefix", (kind,)):
            def entries():
                for (key,) in spool.execute("SELECT DISTINCT key FROM postings WHERE kind=? AND prefix=? ORDER BY key", (kind, prefix)):
                    yield key, _posting_entry(spool, destination, kind, key, check)
            routes[kind][prefix] = _routing_bucket(destination, kind, entries(), check)
    return routes


def _report_coverage(report):
    if report is None:
        return {}, [], []
    if not isinstance(report, dict) or not isinstance(report.get("scope"), dict):
        raise MaterialError("MATERIAL_INVALID_BUILD_REPORT")
    scope = report["scope"]
    scalar_keys = {"kind", "mode", "index_kind", "file_count", "selected_file_count", "catalog_file_count",
                   "complete_dependency_closure", "input_coverage_complete", "truncated",
                   "runtime_paths_verified", "full_statement_analysis_performed"}
    portable = {}
    for key in sorted(scalar_keys & scope.keys()):
        value = scope[key]
        if not isinstance(value, (str, int, bool)) or (isinstance(value, str) and ("/" in value or "\\" in value or ":" in value)):
            raise MaterialError("MATERIAL_INVALID_BUILD_REPORT")
        portable[key] = value

    def records(values, keys):
        if not isinstance(values, list):
            raise MaterialError("MATERIAL_INVALID_BUILD_REPORT")
        result = []
        for value in values:
            if not isinstance(value, dict):
                raise MaterialError("MATERIAL_INVALID_BUILD_REPORT")
            record = {key: value[key] for key in keys & value.keys()}
            if "relative_path" in record:
                relative_path(record["relative_path"])
            if any(not isinstance(item, (str, int, bool)) for item in record.values()):
                raise MaterialError("MATERIAL_INVALID_BUILD_REPORT")
            result.append(record)
        return sorted(result, key=canonical_json)

    skips = records(report.get("input_skips", []), {"relative_path", "reason_code"})
    boundaries = records(scope.get("boundaries", []),
                         {"relative_path", "relation_type", "target_name", "status", "count"})
    return portable, skips, boundaries


def _coverage(db, metadata, count, size, build_report):
    boundaries = [
        {"code": "REPOSITORY_COMPLETENESS_NOT_VERIFIED"},
        {"code": "SPARSE_SEMANTICS_ONLY"},
        {"code": "RULE_OCCURRENCE_DETAILS_NOT_EXPORTED"},
    ]
    file_boundaries = _json(metadata.get("business_file_boundaries", "{}"))
    if not isinstance(file_boundaries, dict):
        raise MaterialError("MATERIAL_INVALID_INDEX")
    counts = {}
    for values in file_boundaries.values():
        if not isinstance(values, dict):
            raise MaterialError("MATERIAL_INVALID_INDEX")
        for code, number in values.items():
            if not isinstance(code, str) or type(number) is not int or number < 0:
                raise MaterialError("MATERIAL_INVALID_INDEX")
            counts[code] = counts.get(code, 0) + number
    boundaries.extend({"code": code, "count": number} for code, number in sorted(counts.items()))
    for relation, status, number in db.execute("""SELECT relation_type,status,COUNT(*) FROM relations
            WHERE relation_type IN ('CALLS','CALL_TARGET_FROM','INCLUDES_COPY')
            AND (status != 'confirmed' OR relation_type='CALL_TARGET_FROM')
            GROUP BY relation_type,status ORDER BY relation_type,status"""):
        boundaries.append({"code": "UNRESOLVED_DEPENDENCY", "relation_type": relation,
                           "status": status, "count": number})
    build_scope, skips, reported_boundaries = _report_coverage(build_report)
    boundaries.extend({"code": "SOURCE_SCOPE_BOUNDARY", **value} for value in reported_boundaries)
    return {"indexed_files": count, "source_bytes": size, "scope": "indexed_snapshot_only",
            "boundaries": boundaries, "build_scope": build_scope, "input_skips": skips}


def export_material(database: Path, source: Path, destination: Path, *, repository_id: str,
                    commit: str, subdirectory="", check_cancel=None, build_report=None,
                    commit_verification="caller_declared") -> dict:
    """Export one read transaction; publish the manifest only after verification.

    The caller supplies the repository/commit binding. This offline function
    verifies the index against local bytes, not that those bytes came from the
    claimed remote commit. The build/publishing layer must establish that link.
    """
    database, source, destination = Path(database), Path(source), Path(destination)
    valid_commit(commit)
    relative_path(subdirectory, allow_empty=True)
    if commit_verification not in {"caller_declared", "git_checked"}:
        raise MaterialError("MATERIAL_INVALID_COMMIT_VERIFICATION")
    _report_coverage(build_report)
    # Validate caller identity before creating any output.
    seal_manifest({"schema_version": SCHEMA_VERSION,
        "repository": {"identity": repository_id, "commit": commit, "subdirectory": subdirectory},
        "analysis": {}, "coverage": {}, "routes": {"files": {}, "terms": {}, "references": {}}})
    _cancel(check_cancel)
    if database.is_symlink() or source.is_symlink():
        raise MaterialError("MATERIAL_INVALID_PATH")
    root, database = source.resolve(strict=True), database.resolve(strict=True)
    if not root.is_dir() or not database.is_file():
        raise MaterialError("MATERIAL_INVALID_PATH")
    if destination.exists() or destination.is_symlink():
        raise MaterialError("MATERIAL_DESTINATION_EXISTS")
    if destination.resolve().is_relative_to(root):
        raise MaterialError("MATERIAL_DESTINATION_INSIDE_SOURCE")
    destination.mkdir()
    created = True
    interrupted = []
    try:
        (destination / "objects").mkdir()
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.create_collation("material_casefold", lambda a, b: (a.casefold() > b.casefold()) - (a.casefold() < b.casefold()))
            if check_cancel:
                def sql_progress():
                    try:
                        check_cancel()
                        return 0
                    except BaseException as error:
                        interrupted.append(error)
                        return 1
                db.set_progress_handler(sql_progress, 1000)
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            metadata, analysis = _metadata(db, root)
            analysis["commit_verification"] = commit_verification
            with tempfile.TemporaryDirectory(prefix=".spool-", dir=destination) as temporary:
                with closing(sqlite3.connect(str(Path(temporary) / "postings.sqlite"))) as spool:
                    spool.executescript("""PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
                        PRAGMA cache_size=-8192; PRAGMA temp_store=FILE;
                        CREATE TABLE postings(kind TEXT,prefix TEXT,key TEXT,path TEXT,
                            PRIMARY KEY(kind,key,path)) WITHOUT ROWID;
                        CREATE INDEX buckets ON postings(kind,prefix,key);
                        CREATE TABLE files(prefix TEXT,path TEXT PRIMARY KEY,record BLOB);
                        CREATE INDEX file_buckets ON files(prefix,path);""")
                    count, size, snapshot = _export_files(db, spool, root, destination, check_cancel)
                    if snapshot != metadata["snapshot_id"]:
                        raise MaterialError("MATERIAL_SNAPSHOT_MISMATCH")
                    routes = _export_routes(spool, destination, check_cancel)
                    coverage = _coverage(db, metadata, count, size, build_report)
            manifest = seal_manifest({"schema_version": SCHEMA_VERSION,
                "repository": {"identity": repository_id, "commit": commit, "subdirectory": subdirectory},
                "analysis": analysis, "coverage": coverage, "routes": routes})
            _cancel(check_cancel)
            with (destination / "material.json").open("xb") as stream:
                stream.write(canonical_json(manifest))
        created = False
        return manifest
    except sqlite3.Error:
        if interrupted:
            raise interrupted[0]
        raise MaterialError("MATERIAL_INVALID_INDEX") from None
    except ValueError as error:
        if isinstance(error, MaterialError):
            raise
        if str(error) in {"SOURCE_HASH_MISMATCH", "SOURCE_LINE_COUNT_MISMATCH", "SOURCE_ENCODING_INVALID", "SOURCE_PATH_INVALID"}:
            raise MaterialError("MATERIAL_" + str(error)) from None
        raise
    finally:
        if created:
            shutil.rmtree(destination)
