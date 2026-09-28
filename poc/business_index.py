"""Streaming source facts for business reading, without a full statement graph.

The schema matches the structural index. Omitted semantic facts are deliberately
not inferred; the model reads verified original pages for business rules.
"""
from __future__ import annotations

import codecs
from collections import Counter, deque
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from datetime import datetime, timezone

from repo_inventory import DEFAULT_EXTENSIONS, SOURCE_FORMAT_RE, iter_source_files, validate_source_options, _looks_binary
from source_catalog import _dependency_targets, _entry_matches
from statement_facts import sentence_terminated, sql_host_access
from structural_index import (
    SCHEMA_VERSION, PROGRAM_ID_RE, SECTION_RE, PARAGRAPH_RE, PARAGRAPH_EXCLUSIONS,
    DATA_ITEM_RE, PERFORM_TARGET_RE, PERFORM_NON_TARGETS, _statement_kind,
    _extract_data_access, _unique_identifiers,
    _stable_id, _content_hash, _connect, _ensure_schema, _delete_file_facts,
    _resolve_relations, _database_counts,
)

PARSER_VERSION = "business-sparse-v1.2"
CHUNK_BYTES = 1024 * 1024
MAX_PHYSICAL_LINE_CHARS = 65536
MAX_FACT_CHARS = 131072
MAX_FACT_LINES = 64
COPY_EXTENSIONS = {".cpy", ".copy", ".cpb", ".inc"}
_LINE_END = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")
_BUSINESS_RULE_KINDS = frozenset({
    "ADD", "COMPUTE", "DIVIDE", "EVALUATE", "IF", "INITIALIZE",
    "MOVE", "MULTIPLY", "READ", "REWRITE", "SET", "SUBTRACT",
    "WHEN", "WRITE", "DELETE", "START", "UNSTRING", "STRING",
})


def _ensure_business_rules(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS business_rules (
            rule_id TEXT PRIMARY KEY,
            relative_path TEXT NOT NULL,
            program_name TEXT,
            paragraph_name TEXT,
            rule_kind TEXT NOT NULL,
            first_line INTEGER NOT NULL,
            last_line INTEGER NOT NULL,
            occurrence_count INTEGER NOT NULL,
            normalized_text TEXT NOT NULL,
            reads_json TEXT NOT NULL,
            writes_json TEXT NOT NULL,
            condition_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_business_rules_path
            ON business_rules(relative_path,program_name,first_line);
        CREATE TABLE IF NOT EXISTS business_rule_fields (
            rule_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            field_role TEXT NOT NULL,
            PRIMARY KEY(rule_id,field_name,field_role)
        );
        CREATE INDEX IF NOT EXISTS idx_business_rule_fields_name
            ON business_rule_fields(field_name,field_role);
    """)


def _delete_business_rules(connection, relative):
    connection.execute("DELETE FROM business_rule_fields WHERE rule_id IN "
                       "(SELECT rule_id FROM business_rules WHERE relative_path=?)", (relative,))
    connection.execute("DELETE FROM business_rules WHERE relative_path=?", (relative,))


def _safe_path(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative)
    if not relative or parts.is_absolute() or "\\" in relative or any(
        part in {"", ".", ".."} or ":" in part for part in relative.split("/")
    ):
        raise ValueError("SOURCE_PATH_INVALID")
    candidate = root
    for part in parts.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError("SOURCE_PATH_INVALID")
    if not candidate.resolve(strict=True).is_relative_to(root) or not candidate.is_file():
        raise ValueError("SOURCE_PATH_INVALID")
    return candidate


def _identity(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _verify_file(path, root, encoding, emit):
    """Hash the complete file and validate encodings with fixed-size buffers."""
    before = path.stat()
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if _identity(before) != _identity(opened) or not stat.S_ISREG(opened.st_mode):
            raise ValueError("SOURCE_CHANGED_DURING_READ")
        prefix = handle.read(8192)
        if encoding == "auto":
            candidates = ["utf-8", "cp950", "big5", "cp1252", "latin-1"]
            for bom, name in ((b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
                              (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"), (b"\xef\xbb\xbf", "utf-8-sig")):
                if prefix.startswith(bom):
                    candidates = [name]
                    break
            else:
                if _looks_binary(prefix):
                    raise ValueError("SOURCE_ENCODING_INVALID")
        else:
            candidates = [encoding]
        decoders = {name: codecs.getincrementaldecoder(name)(errors="strict") for name in candidates}
        digest, completed = hashlib.sha256(), 0
        chunk = prefix
        while chunk:
            digest.update(chunk)
            completed += len(chunk)
            for name in list(decoders):
                try:
                    decoders[name].decode(chunk)
                except UnicodeError:
                    del decoders[name]
            emit(completed)
            chunk = handle.read(CHUNK_BYTES)
        for name in list(decoders):
            try:
                decoders[name].decode(b"", final=True)
            except UnicodeError:
                del decoders[name]
        after_open = os.fstat(handle.fileno())
    after = _safe_path(root, path.relative_to(root).as_posix()).stat()
    if _identity(before) != _identity(after_open) or _identity(before) != _identity(after):
        raise ValueError("SOURCE_CHANGED_DURING_READ")
    selected = next((name for name in candidates if name in decoders), None)
    if selected is None:
        raise ValueError("SOURCE_ENCODING_INVALID")
    return {"sha256": digest.hexdigest(), "encoding": selected, "stat": before,
            "used_fallback_encoding": encoding == "auto" and selected == "latin-1"}


def _physical_lines(path, encoding, expected_stat=None):
    """Yield every physical line; a huge line is counted but not held as a fact."""
    with path.open("r", encoding=encoding, errors="strict", newline="") as handle:
        if expected_stat is not None and _identity(os.fstat(handle.fileno())) != _identity(expected_stat):
            raise ValueError("SOURCE_CHANGED_DURING_READ")
        pending, oversized, first, line_number = "", False, True, 0
        while True:
            chunk = handle.read(65536)
            if first:
                chunk, first = chunk.lstrip("\ufeff"), False
            if not chunk:
                if pending or oversized:
                    line_number += 1
                    yield line_number, None if oversized else pending.removesuffix("\r")
                if expected_stat is not None and _identity(os.fstat(handle.fileno())) != _identity(expected_stat):
                    raise ValueError("SOURCE_CHANGED_DURING_READ")
                return
            text = pending + chunk
            start = 0
            for match in _LINE_END.finditer(text):
                # CRLF split at the chunk boundary is one physical line.
                if match.group() == "\r" and match.end() == len(text):
                    break
                line = text[start:match.start()]
                line_number += 1
                yield line_number, None if oversized or len(line) > MAX_PHYSICAL_LINE_CHARS else line
                oversized = False
                start = match.end()
            pending = text[start:]
            if len(pending) > MAX_PHYSICAL_LINE_CHARS:
                oversized, pending = True, pending[-1:] if pending.endswith("\r") else ""


def _clean(raw, active_format):
    expanded = raw.expandtabs(8)
    directive = SOURCE_FORMAT_RE.search(expanded)
    if directive:
        return "", directive.group(1).lower(), False
    fixed = active_format == "fixed" or (active_format == "auto" and len(expanded) >= 7 and (
        expanded[:6].isdigit() or (not expanded[:6].strip() and expanded[6] in " */-Dd")))
    if fixed:
        if len(expanded) >= 7 and expanded[6] in "*/":
            return "", active_format, False
        continuation = len(expanded) >= 7 and expanded[6] == "-"
        code = expanded[7:72]
    else:
        continuation, code = False, expanded
        if code.lstrip().startswith("*"):
            return "", active_format, continuation
    if "*>" in code:
        quote, index = None, 0
        while index < len(code):
            char = code[index]
            if quote:
                if char == quote:
                    if index + 1 < len(code) and code[index + 1] == quote:
                        index += 2
                        continue
                    quote = None
            elif char in "'\"":
                quote = char
            elif code[index:index + 2] == "*>":
                code = code[:index]
                break
            index += 1
    return code.strip(), active_format, continuation


class _Facts:
    def __init__(self, connection, relative, metadata, source_format, emit, copybook_hint=False):
        self.connection, self.relative, self.metadata = connection, relative, metadata
        self.source_format, self.emit = source_format, emit
        self.program, self.program_id, self.section_id, self.paragraph_id = None, None, None, None
        self.section_name, self.paragraph_name = None, None
        self.division, self.active_format = None, source_format
        self.pending = None
        self.rule_batch = {}
        self.rule_partition = _content_hash(relative)[:12]
        self.line_count = 0
        self.boundaries = Counter()
        self.copybook = copybook_hint or Path(relative).suffix.casefold() in COPY_EXTENSIONS
        self.sql = False

    def _close(self, kind, end):
        key = {"Program": "program_id", "Section": "section_id", "Paragraph": "paragraph_id"}[kind]
        identity = getattr(self, key)
        if identity:
            self.connection.execute("UPDATE code_units SET end_line=MAX(start_line,?) WHERE unit_id=?", (end, identity))
            setattr(self, key, None)
            if kind == "Paragraph":
                self.paragraph_name = None

    def _business_rule(self, kind, code, start, end):
        rule_kind = "EXEC_SQL" if kind == "SqlRule" else _statement_kind(code)
        reads, writes, _ = _extract_data_access(code)
        if kind == "SqlRule":
            reads, writes, _ = sql_host_access(code)
        conditions = _unique_identifiers(code) if rule_kind in {"IF", "EVALUATE", "WHEN"} else []
        key = _stable_id("rule", self.relative, self.program, self.paragraph_name or self.section_name,
                         rule_kind, code)
        # Keep one file's primary-key writes together. Fully random rule IDs
        # scatter every insertion across a growing repository index and cause
        # heavy page-cache churn for millions of rules. The original digest
        # still supplies identity; existing cached rule IDs remain readable.
        key = "rule_" + self.rule_partition + "_" + key[5:]
        row = self.rule_batch.get(key)
        if row:
            row[6] = max(row[6], end)
            row[7] += 1
        else:
            self.rule_batch[key] = [key, self.relative, self.program, self.paragraph_name or self.section_name,
                                    rule_kind, start, end, 1, code,
                                    json.dumps(reads), json.dumps(writes), json.dumps(conditions)]
        if len(self.rule_batch) >= 2048:
            self._flush_rules()

    def _flush_rules(self):
        if not self.rule_batch:
            return
        rows = list(self.rule_batch.values())
        self.connection.executemany("""
            INSERT INTO business_rules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(rule_id) DO UPDATE SET
                first_line=MIN(first_line,excluded.first_line),
                last_line=MAX(last_line,excluded.last_line),
                occurrence_count=occurrence_count+excluded.occurrence_count
        """, rows)
        fields = []
        for row in rows:
            for role, names in (("read", json.loads(row[9])), ("write", json.loads(row[10])),
                                ("condition", json.loads(row[11]))):
                fields.extend((row[0], name, role) for name in names)
        self.connection.executemany("INSERT OR IGNORE INTO business_rule_fields VALUES (?,?,?)", fields)
        self.rule_batch.clear()

    def _unit(self, kind, name, start, end, text, raw, parse_status="complete", symbol=None):
        evidence = _stable_id("ev", self.relative, self.metadata["sha256"], start, end)
        self.connection.execute("INSERT OR IGNORE INTO evidence_spans VALUES (?,?,?,?,?,?)",
                                (evidence, self.relative, start, end, self.metadata["sha256"], raw))
        identity = _stable_id("unit", self.relative, kind, name, start)
        parent = self.paragraph_id or self.section_id or self.program_id
        self.connection.execute("INSERT OR IGNORE INTO code_units VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                (identity, self.relative, kind, name, self.program, parent, start, end,
                                 text, _content_hash(text), evidence, parse_status))
        self.connection.execute("INSERT INTO code_units_fts(unit_id,name,program_name,normalized_text) VALUES (?,?,?,?)",
                                (identity, name, self.program, text))
        if symbol:
            self.connection.execute("INSERT OR IGNORE INTO symbols VALUES (?,?,?,?,?,?,?,?)",
                                    (_stable_id("sym", self.relative, symbol, self.program, name, start), self.relative,
                                     symbol, name, self.program, f"{self.program or ''}::{name}", identity, evidence))
        return identity, evidence

    def _fact(self, pending):
        kind, code_lines, raw_lines, start, end, complete = pending
        code, raw = " ".join(code_lines), "\n".join(raw_lines)
        upper = code.upper()
        if kind == "Program":
            match = PROGRAM_ID_RE.match(code)
            if not match:
                self.boundaries["program_header_unrecognized"] += 1
                return
            self._close("Paragraph", start - 1)
            self._close("Section", start - 1)
            self._close("Program", start - 1)
            self.program = match.group(1).upper()
            self.copybook = False
            self.program_id, _ = self._unit("Program", self.program, start, end, code, raw, symbol="Program")
        elif kind == "ProcedureSignature":
            self._close("Paragraph", start - 1)
            self._close("Section", start - 1)
            self.section_name, self.division = None, "PROCEDURE"
            self._unit(kind, "PROCEDURE", start, end, code, raw, "complete" if complete else "partial")
        elif kind == "DataItem":
            match = DATA_ITEM_RE.match(code)
            if match:
                self._unit(kind, match.group(2).upper(), start, end, code, raw,
                           "complete" if complete else "partial", "ConditionName" if match.group(1) == "88" else "Field")
        elif kind == "Dependency":
            unit, evidence = self._unit("Statement", "DEPENDENCY", start, end, code, raw,
                                        "complete" if complete else "partial")
            targets, dynamic = _dependency_targets(code)
            dependencies = targets + [("CALL_TARGET_FROM", name) for name in dynamic]
            # Literal text cannot create a PERFORM relation.
            unquoted = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", " ", upper)
            for match in PERFORM_TARGET_RE.finditer(unquoted):
                if match.group(1) not in PERFORM_NON_TARGETS:
                    dependencies.append(("PERFORMS_THRU" if match.group(2) else "PERFORMS", match.group(1)))
            for relation, target in dict.fromkeys(dependencies):
                metadata = {"source_index_kind": "business_sparse", "semantic_binding_performed": False}
                if relation == "INCLUDES_COPY" and re.search(r"\b(REPLACING|OF|IN)\b", unquoted):
                    metadata["boundary"] = "copy_replacing_or_library_not_expanded"
                    self.boundaries[metadata["boundary"]] += 1
                if relation == "CALL_TARGET_FROM":
                    metadata["boundary"] = "runtime_target_requires_value_flow"
                    self.boundaries[metadata["boundary"]] += 1
                if not complete:
                    metadata["boundary"] = "dependency_statement_incomplete"
                self.connection.execute("INSERT OR IGNORE INTO relations VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (_stable_id("rel", self.relative, unit, relation, target.upper()), self.relative, unit,
                     relation, target.upper(), self.program if relation.startswith("PERFORM") or relation == "CALL_TARGET_FROM" else None,
                     None, "unresolved", evidence, json.dumps(metadata)))
        elif kind in {"BusinessRule", "SqlRule"}:
            self._business_rule(kind, upper, start, end)
        if not complete:
            self.boundaries["bounded_fact_fragment"] += 1

    def _flush(self, complete=False):
        if self.pending:
            self.pending[-1] = complete
            self._fact(self.pending)
            self.pending = None

    def consume(self, line_number, raw):
        self.line_count = line_number
        if line_number % 4096 == 0:
            self.emit(line_number)
        if raw is None:
            self._flush()
            self.boundaries["source_line_exceeds_fact_budget"] += 1
            return
        code, next_format, continuation = _clean(raw, self.active_format)
        if self.source_format == "auto":
            self.active_format = next_format
        if self.pending and self.pending[0] == "SqlRule":
            if code:
                self.pending[1].append(code)
                self.pending[2].append(raw)
                self.pending[4] = line_number
            if "END-EXEC" in code.upper():
                self._flush(complete=True)
            elif len(self.pending[2]) >= MAX_FACT_LINES or sum(map(len, self.pending[2])) > MAX_FACT_CHARS:
                self._flush()
                self.sql = True
                self.boundaries["embedded_statement_exceeds_fact_budget"] += 1
            return
        if self.pending:
            kind = self.pending[0]
            parameter_tail = bool(PARAGRAPH_RE.match(code) and (
                kind == "ProcedureSignature" or (kind == "Dependency" and
                re.search(r"\bCALL\b.*\bUSING\b", " ".join(self.pending[1]), re.I))))
            starts_new = bool(code and not continuation and (
                PROGRAM_ID_RE.match(code) or SECTION_RE.match(code) or re.match(r"^(?:IDENTIFICATION|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION\b", code, re.I)
                or DATA_ITEM_RE.match(code) or _statement_kind(code.upper()) != "OTHER"
                or re.match(r"^(?:ELSE|END-IF|WHEN|END-EVALUATE|GOBACK)\b", code, re.I)
                or (PARAGRAPH_RE.match(code) and self.division == "PROCEDURE" and not parameter_tail)))
            if kind == "Program":
                starts_new = not bool(re.fullmatch(r"['\"]?[A-Z0-9_$#@-]+['\"]?\.?", code, re.I)) if code else False
            if starts_new or len(self.pending[2]) >= MAX_FACT_LINES or sum(map(len, self.pending[2])) + len(raw) > MAX_FACT_CHARS:
                self._flush(complete=starts_new)
            else:
                self.pending[1].append(code)
                self.pending[2].append(raw)
                self.pending[4] = line_number
                if code and sentence_terminated(code):
                    self._flush(complete=True)
                return
        if not code:
            return
        upper = code.upper()
        if self.program_id is None and self.division is None and (
            DATA_ITEM_RE.match(code) or upper.startswith("DATA DIVISION")
        ):
            self.copybook = True
        if self.copybook and self.program_id is None:
            self.program = Path(self.relative).stem.upper()
            self.program_id, _ = self._unit("Copybook", self.program, line_number, line_number, code, raw, symbol="Copybook")
        if self.sql:
            self.sql = "END-EXEC" not in upper
            return
        if upper.startswith("EXEC SQL"):
            self.pending = ["SqlRule", [code], [raw], line_number, line_number, False]
            if "END-EXEC" in upper:
                self._flush(complete=True)
            return
        if re.match(r"^END\s+PROGRAM\b", upper):
            self._close("Paragraph", line_number)
            self._close("Section", line_number)
            self._close("Program", line_number)
            self.program, self.section_name = None, None
            return
        division = re.match(r"^(IDENTIFICATION|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION\b", upper)
        kind = None
        if re.match(r"^PROGRAM-ID\s*\.", upper):
            kind = "Program"
        elif division:
            self.division = division.group(1)
            if self.division == "IDENTIFICATION" and self.program_id:
                self._close("Paragraph", line_number - 1)
                self._close("Section", line_number - 1)
                self._close("Program", line_number - 1)
                self.program, self.section_name = None, None
            if self.division == "PROCEDURE":
                kind = "ProcedureSignature"
            else:
                return
        elif match := SECTION_RE.match(code):
            self._close("Paragraph", line_number - 1)
            self._close("Section", line_number - 1)
            self.section_name = match.group(1).upper()
            self.section_id, _ = self._unit("Section", self.section_name, line_number, line_number, code, raw, symbol="Section")
            return
        elif (self.division == "PROCEDURE" or self.copybook) and (match := PARAGRAPH_RE.match(code)) and match.group(1).upper() not in PARAGRAPH_EXCLUSIONS:
            self._close("Paragraph", line_number - 1)
            self.paragraph_name = match.group(1).upper()
            self.paragraph_id, _ = self._unit("Paragraph", match.group(1).upper(), line_number, line_number, code, raw, symbol="Paragraph")
            return
        elif (self.copybook or self.section_name == "LINKAGE") and DATA_ITEM_RE.match(code):
            kind = "DataItem"
        elif any(word in upper for word in ("CALL", "COPY", "PERFORM")):
            targets, dynamic = _dependency_targets(code)
            unquoted = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", " ", upper)
            if targets or dynamic or PERFORM_TARGET_RE.search(unquoted) or re.search(r"\b(?:CALL|COPY)\s*$", unquoted):
                kind = "Dependency"
        if kind is None and (self.division == "PROCEDURE" or self.copybook) and _statement_kind(upper) in _BUSINESS_RULE_KINDS:
            kind = "BusinessRule"
        if kind:
            self.pending = [kind, [code], [raw], line_number, line_number, False]
            if sentence_terminated(code) and not (kind == "Program" and not PROGRAM_ID_RE.match(code)):
                self._flush(complete=True)

    def finish(self):
        self._flush()
        self._flush_rules()
        self._close("Paragraph", self.line_count)
        self._close("Section", self.line_count)
        self._close("Program", self.line_count)


def _resolve_copies(connection):
    aliases = {}
    for row in connection.execute("SELECT s.symbol_id,s.relative_path,s.name FROM symbols s WHERE symbol_type='Copybook'"):
        for value in {row["name"], Path(row["relative_path"]).name.upper(), row["relative_path"].upper()}:
            aliases.setdefault(value, set()).add(row["symbol_id"])
    for row in connection.execute("SELECT relation_id,target_name,metadata_json FROM relations WHERE relation_type='INCLUDES_COPY'").fetchall():
        choices = aliases.get(row["target_name"], set())
        metadata = json.loads(row["metadata_json"])
        # A library-qualified COPY is not resolved by an unqualified filename.
        if metadata.get("boundary") == "copy_replacing_or_library_not_expanded":
            connection.execute("UPDATE relations SET status='candidate',target_entity_id=NULL WHERE relation_id=?", (row["relation_id"],))
            continue
        status = "confirmed" if len(choices) == 1 else "candidate" if choices else "unresolved"
        connection.execute("UPDATE relations SET status=?,target_entity_id=? WHERE relation_id=?",
                           (status, next(iter(choices)) if len(choices) == 1 else None, row["relation_id"]))


def build_business_index(source_root: Path, database_path: Path, *, extensions=DEFAULT_EXTENSIONS,
                         include_extensionless=False, encoding="auto", source_format="auto", quiet=False,
                         include_paths=None, progress=None, check_cancel=None, verify_content=False,
                         catalog=None, entry_program=None, **_unused):
    """Index sparse facts, optionally following all unique static dependencies.

    File count, source bytes, and call depth do not impose a hidden scope cutoff.
    Memory for source text is bounded by decoder chunks and one fact fragment;
    source paths and sparse symbol maps scale with the project.
    """
    validate_source_options(encoding, source_format)
    raw_root = Path(source_root).expanduser()
    if raw_root.is_symlink():
        raise ValueError("SOURCE_PATH_INVALID")
    root = raw_root.resolve(strict=True)
    database = Path(database_path).expanduser()
    if any(Path(str(database) + suffix).is_symlink() for suffix in ("", "-wal", "-shm")):
        raise ValueError("SNAPSHOT_PATH_INVALID")
    if not root.is_dir():
        raise ValueError("SOURCE_PATH_INVALID")
    def emit(phase, **extra):
        if check_cancel:
            check_cancel()
        if progress:
            progress({"phase": phase, "unit": "files", **extra})
    selected_entry = None
    closure = catalog is not None and entry_program is not None
    if closure:
        if Path(catalog["source_root"]).resolve() != root:
            raise ValueError("SOURCE_ROOT_MISMATCH")
        matches = _entry_matches(catalog["programs"], entry_program)
        if len(matches) != 1:
            raise ValueError("ENTRY_AMBIGUOUS" if matches else "ENTRY_NOT_FOUND")
        selected_entry = matches[0]
        known = {row["relative_path"] for row in catalog["file_entries"]}
        initial = [selected_entry["relative_path"]]
    else:
        known = set(include_paths) if include_paths is not None else {
            path.relative_to(root).as_posix() for path in iter_source_files(root, extensions, include_extensionless)}
        initial = sorted(known, key=str.casefold)
    if not known:
        raise ValueError("SOURCE_INDEX_EMPTY")
    for relative in known:
        _safe_path(root, relative)
    program_paths, copy_paths, copy_hints = {}, {}, set()
    if closure:
        for item in catalog["programs"]:
            program_paths.setdefault(item["program_name"].casefold(), set()).add(item["relative_path"])
        for relative in known:
            for alias in {Path(relative).name, Path(relative).stem, relative}:
                copy_paths.setdefault(alias.casefold(), set()).add(relative)
    connection = _connect(database.resolve())
    try:
        # Keep B-tree pages hot while inserting large rule/field indexes.
        # SQLite's small default cache otherwise repeatedly reads and evicts
        # random index pages as a large repository is ingested.
        connection.execute("PRAGMA cache_size = -65536")
        _ensure_schema(connection)
        _ensure_business_rules(connection)
        prior = dict(connection.execute("SELECT key,value FROM metadata"))
        prior_boundaries = json.loads(prior.get("business_file_boundaries", "{}"))
        prior_stats = json.loads(prior.get("file_stats", "{}"))
        previous = {row["relative_path"]: dict(row) for row in connection.execute("SELECT * FROM source_files")}
        options = {"encoding": encoding, "source_format": source_format, "extensions": sorted(extensions),
                   "include_extensionless": include_extensionless}
        options_json = json.dumps(options, sort_keys=True)
        rebuild = (prior.get("parser_version") != PARSER_VERSION or prior.get("source_options") != options_json
                   or prior.get("source_root_hash") != _content_hash(str(root)))
        updated = skipped = total_bytes = metadata_cached = content_verified = 0
        file_stats, file_boundaries, boundaries, scan_details, distributions = {}, {}, [], [], {key: Counter() for key in ("encodings", "format_hints", "artifact_kinds")}
        selected, queued = [], set(initial)
        queue = deque((relative, 0) for relative in initial)
        with connection:
            # Empty derived tables prevent strict facts from surviving a mode switch.
            for table in ("call_bindings", "call_binding_boundaries", "copy_expansions", "copy_scope_boundaries"):
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    connection.execute(f"DELETE FROM {table}")
            while queue:
                relative, depth = queue.popleft()
                path = _safe_path(root, relative)
                emit("reading", completed=len(selected), total=len(queued), current_file=relative)
                old = previous.get(relative)
                observed = path.stat()
                unchanged = (not verify_content and not rebuild and old is not None
                             and prior_stats.get(relative) == [observed.st_size, observed.st_mtime_ns])
                if unchanged:
                    metadata = {"sha256": old["sha256"], "encoding": old["encoding"], "stat": observed,
                                "used_fallback_encoding": bool(old["used_fallback_encoding"])}
                    metadata_cached += 1
                else:
                    metadata = _verify_file(path, root, encoding, lambda done: emit(
                        "reading", completed=len(selected), total=len(queued), current_file=relative, file_bytes_completed=done))
                    content_verified += 1
                info = metadata["stat"]
                file_stats[relative] = [info.st_size, info.st_mtime_ns]
                total_bytes += info.st_size
                cached = not rebuild and old is not None and old["sha256"] == metadata["sha256"]
                if cached:
                    skipped += 1
                    line_count, artifact = old["line_count"], old["artifact_kind"]
                    file_boundaries[relative] = prior_boundaries.get(relative, {})
                else:
                    # New files have no facts to remove. In particular, the
                    # FTS deletion joins scan existing units, making a cold
                    # import quadratic when repeated for every new file.
                    if old is not None:
                        _delete_business_rules(connection, relative)
                        _delete_file_facts(connection, relative)
                    facts = _Facts(connection, relative, metadata, source_format, lambda done: emit(
                        "parsing", completed=len(selected), total=len(queued), current_file=relative, file_completed=done, file_unit="lines"),
                        copybook_hint=relative in copy_hints)
                    for number, line in _physical_lines(path, metadata["encoding"], info):
                        facts.consume(number, line)
                    facts.finish()
                    line_count = facts.line_count
                    artifact = "copybook" if facts.copybook else "program" if facts.program else "unknown"
                    file_boundaries[relative] = dict(facts.boundaries)
                    connection.execute("INSERT INTO source_files VALUES (?,?,?,?,?,?,?,?)", (
                        relative, metadata["sha256"], metadata["encoding"], int(metadata["used_fallback_encoding"]),
                        source_format, artifact, line_count, datetime.now(timezone.utc).isoformat()))
                    updated += 1
                boundaries.extend({"relative_path": relative, "relation_type": "SPARSE_PARSER", "target_name": "",
                                   "status": reason, "count": count} for reason, count in file_boundaries[relative].items())
                if _identity(info) != _identity(_safe_path(root, relative).stat()):
                    raise ValueError("SOURCE_CHANGED_DURING_READ")
                distributions["encodings"][metadata["encoding"]] += 1
                distributions["format_hints"][source_format] += 1
                distributions["artifact_kinds"][artifact] += 1
                selected.append(relative)
                scan_details.append({"relative_path": relative, "bytes_scanned": info.st_size, "file_bytes": info.st_size,
                                     "truncated": False, "encoding": metadata["encoding"], "depth": depth})
                if closure:
                    dependencies = connection.execute("SELECT DISTINCT relation_type,target_name,metadata_json FROM relations "
                        "WHERE relative_path=? AND relation_type IN ('CALLS','CALL_TARGET_FROM','INCLUDES_COPY') "
                        "ORDER BY relation_type='INCLUDES_COPY'", (relative,)).fetchall()
                    for dependency in dependencies:
                        relation, target = dependency["relation_type"], dependency["target_name"]
                        detail = json.loads(dependency["metadata_json"])
                        candidates = (copy_paths if relation == "INCLUDES_COPY" else program_paths).get(target.casefold(), set())
                        status = ("DYNAMIC_TARGET" if relation == "CALL_TARGET_FROM" else
                                  "COPY_FORM_UNRESOLVED" if detail.get("boundary") == "copy_replacing_or_library_not_expanded" else
                                  "MISSING_SOURCE" if not candidates else "AMBIGUOUS_SOURCE" if len(candidates) != 1 else None)
                        if status:
                            boundaries.append({"relative_path": relative, "relation_type": relation, "target_name": target, "status": status})
                        if status is None or (status == "COPY_FORM_UNRESOLVED" and len(candidates) == 1):
                            target_path = next(iter(candidates))
                            if relation == "INCLUDES_COPY":
                                copy_hints.add(target_path)
                            if target_path not in queued:
                                queued.add(target_path)
                                queue.append((target_path, depth + int(relation == "CALLS")))
                emit("indexing", completed=len(selected), total=len(queued), current_file=relative)
            removed = set(previous) - set(selected)
            for relative in removed:
                _delete_business_rules(connection, relative)
                _delete_file_facts(connection, relative)
            if updated or removed or rebuild:
                _resolve_relations(connection)
                _resolve_copies(connection)
            # Whole-directory business indexing has the same dependency gaps
            # as entry closure; scanning every local file does not supply a
            # missing or ambiguous external object.
            unresolved = connection.execute("SELECT relative_path,relation_type,target_name,status,metadata_json "
                "FROM relations WHERE relation_type IN ('CALLS','CALL_TARGET_FROM','INCLUDES_COPY') "
                "AND (status != 'confirmed' OR relation_type='CALL_TARGET_FROM')").fetchall()
            known_boundaries = {tuple(sorted(boundary.items())) for boundary in boundaries}
            for relation in unresolved:
                detail = json.loads(relation["metadata_json"])
                status = ("DYNAMIC_TARGET" if relation["relation_type"] == "CALL_TARGET_FROM" else
                          "COPY_FORM_UNRESOLVED" if detail.get("boundary") == "copy_replacing_or_library_not_expanded" else
                          "AMBIGUOUS_SOURCE" if relation["status"] == "candidate" else "MISSING_SOURCE")
                boundary = {"relative_path": relation["relative_path"], "relation_type": relation["relation_type"],
                            "target_name": relation["target_name"], "status": status}
                key = tuple(sorted(boundary.items()))
                if key not in known_boundaries:
                    boundaries.append(boundary)
                    known_boundaries.add(key)
            digest = hashlib.sha256()
            for relative, sha in sorted(connection.execute("SELECT relative_path,sha256 FROM source_files"), key=lambda row: row[0].casefold()):
                digest.update(relative.encode() + b"\0" + sha.encode() + b"\n")
            snapshot = "sha256:" + digest.hexdigest()
            omission = {"status": "not_performed", "reason": "business_sparse_index"}
            updates = {"schema_version": SCHEMA_VERSION, "parser_version": PARSER_VERSION, "index_kind": "business_sparse",
                       "snapshot_id": snapshot, "source_root_hash": _content_hash(str(root)), "source_options": options_json,
                       "indexed_at_utc": datetime.now(timezone.utc).isoformat(), "file_stats": json.dumps(file_stats),
                       "business_file_boundaries": json.dumps(file_boundaries),
                       "copy_report": json.dumps(omission), "call_report": json.dumps(omission)}
            connection.executemany("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", updates.items())
        counts = _database_counts(connection)
        program_count = connection.execute("SELECT COUNT(*) FROM symbols WHERE symbol_type='Program'").fetchone()[0]
        copybook_count = connection.execute("SELECT COUNT(*) FROM symbols WHERE symbol_type='Copybook'").fetchone()[0]
        scope = {"kind": "selected_sources" if closure or include_paths is not None else "full_directory",
                 "mode": "entry_static_closure" if closure else "business_sparse_directory", "index_kind": "business_sparse",
                 "file_count": len(selected), "selected_file_count": len(selected), "catalog_file_count": len(known),
                 "selected_source_bytes": total_bytes, "total_scope_bytes": total_bytes,
                 "max_files": None, "max_depth": None, "max_total_source_bytes": None,
                 "dependency_scan": scan_details, "complete_dependency_closure": not boundaries,
                 "boundaries": boundaries, "truncated": False, "runtime_paths_verified": False,
                 "budget_kind": "streaming_sparse_facts", "full_statement_analysis_performed": False,
                 "source_text_buffer_bounded": True, "memory_usage_bounded": False}
        return {"schema_version": SCHEMA_VERSION, "parser_version": PARSER_VERSION, "index_kind": "business_sparse",
                "snapshot_id": snapshot, "source_options": options, "scope": scope, "selected_entry": selected_entry,
                "relative_paths": selected, "missing_dependencies": boundaries,
                "source_stat_manifest": file_stats, "database_counts": counts,
                "files": {"candidate": len(selected), "decoded": len(selected), "unreadable_or_binary": 0,
                          "indexed_or_updated": updated, "skipped_unchanged": skipped, "removed": len(removed),
                          "metadata_cache_reused": metadata_cached, "content_hash_verified": content_verified},
                "diagnostics": {"status": "ready", "warnings": [], "program_count": program_count,
                                "copybook_count": copybook_count, "files_without_symbols": 0},
                "distributions": {key: dict(value) for key, value in distributions.items()},
                "parser_rebuild_required": bool(previous) and rebuild, "source_options_rebuild_required": bool(previous) and prior.get("source_options") != options_json,
                "copy_expansion": omission, "call_bindings": omission,
                "relation_statuses": dict(connection.execute("SELECT status,COUNT(*) FROM relations GROUP BY status")),
                "database_path": str(database.resolve()), "privacy": {"network_calls": False, "source_stored_locally": True}}
    finally:
        connection.close()
