"""Offline, versioned framework observations and request visibility checks.

The knowledge source declares interface meanings. Source bindings identify a
particular use; neither layer establishes execution or a successful I/O result.
"""

from collections import defaultdict
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time


SEMANTICS_VERSION = "framework-semantics/v1.8"
MAX_FILE_FACTS = 2048
MAX_REQUEST_FACTS = 32


def _knowledge(reference_path):
    from framework_rules import compile_framework_rules
    knowledge = compile_framework_rules(reference_path)
    digest = hashlib.sha256(json.dumps(knowledge, sort_keys=True, ensure_ascii=False,
                                      separators=(",", ":")).encode()).hexdigest()
    return knowledge, digest


def _schema(db):
    db.execute("CREATE TABLE IF NOT EXISTS framework_knowledge_cache "
               "(slot INTEGER PRIMARY KEY, digest TEXT NOT NULL, knowledge_json TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS framework_file_semantics "
               "(relative_path TEXT PRIMARY KEY, source_sha256 TEXT NOT NULL, "
               "knowledge_digest TEXT NOT NULL, version TEXT NOT NULL, facts_json TEXT NOT NULL, "
               "bindings_json TEXT NOT NULL DEFAULT '[]')")
    if "bindings_json" not in {row[1] for row in db.execute("PRAGMA table_info(framework_file_semantics)")}:
        db.execute("ALTER TABLE framework_file_semantics ADD COLUMN bindings_json TEXT NOT NULL DEFAULT '[]'")


def _copy_layout_digest(db, relative):
    from framework_layout import _copy_spec, _copy_target_paths, _rows, _MAX_LAYOUT_ITEMS
    pending, seen, rows, catalog = [relative], set(), [], {}
    while pending and len(seen) <= _MAX_LAYOUT_ITEMS:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        for edge in _rows(db, "SELECT r.*,u.normalized_text FROM relations r LEFT JOIN code_units u "
                "ON u.unit_id=r.from_entity_id WHERE r.relative_path=? AND r.relation_type='INCLUDES_COPY' "
                "ORDER BY r.relation_id", (path,)):
            parsed = _copy_spec(edge.get("normalized_text") or "")
            targets = _copy_target_paths(db, edge, parsed, catalog)
            versions = []
            for target in targets:
                record = db.execute("SELECT sha256 FROM source_files WHERE relative_path=?", (target,)).fetchone()
                versions.append((target, record[0] if record else None))
                if target not in seen:
                    pending.append(target)
            rows.append((path, edge["target_name"], edge["status"], edge["metadata_json"],
                         edge.get("normalized_text"), versions))
    return hashlib.sha256(json.dumps([rows, sorted(pending)], ensure_ascii=False).encode()).hexdigest()


def _copy_replacement_auditor(db, *, root=None, source_session=None, source_pages=(), check_cancel=None):
    """Check complete member text before trusting declaration-only replacement.

    Sparse data units omit arbitrary text. A replacement outside those units
    may create a persistent REPLACE/COPY/directive, so it needs full preprocessing.
    The call-local cache is shared by binding and layout checks.
    """
    from source_reading import _verified_lines
    from statement_facts import CobolLexError, clean_cobol_line, lex_cobol
    from framework_binding import _ELEMENTARY
    from framework_layout import _DECL
    from procedure_expansion import _replace_tokens
    cache, supplied, missing_sources = {}, None, {}

    def audit(relative, pairs):
        nonlocal supplied
        if relative not in cache:
            metadata = db.execute("SELECT * FROM source_files WHERE relative_path=?", (relative,)).fetchone()
            outside, issue = set(), None
            if metadata is None:
                return "framework_replacement_source_not_fully_checked"
            metadata = dict(metadata)
            spans = db.execute("SELECT start_line,end_line,normalized_text FROM code_units WHERE relative_path=? "
                "AND unit_type='DataItem' AND parse_status='complete' ORDER BY start_line,end_line", (relative,)).fetchall()
            try:
                if source_session is not None:
                    captured = source_session.capture(relative)
                    if captured.sha256 != metadata["sha256"]:
                        raise ValueError("SOURCE_VERSION_CHANGED")
                    lines = _verified_lines(source_session.mirror_root, metadata, check_cancel, 65536)
                elif root is not None:
                    lines = _verified_lines(root, metadata, check_cancel, 65536)
                else:
                    if supplied is None:
                        supplied = _source_evidence_lines(source_pages)
                    rows = supplied.get((relative, metadata["sha256"]), {})
                    if any(rows.get(number) is None for number in range(1, metadata["line_count"] + 1)):
                        missing_sources[relative] = {"relative_path": relative,
                            "source_sha256": metadata["sha256"], "start_line": 1,
                            "end_line": metadata["line_count"]}
                        raise ValueError("COMPLETE_MEMBER_SOURCE_REQUIRED")
                    lines = ((rows[number][0], False) for number in range(1, metadata["line_count"] + 1))
                offset, active_format = 0, metadata["format_hint"]
                for number, (raw, truncated) in enumerate(lines, 1):
                    if check_cancel:
                        check_cancel()
                    while offset < len(spans) and spans[offset][1] < number:
                        offset += 1
                    covered = offset < len(spans) and spans[offset][0] <= number <= spans[offset][1]
                    code, active_format, _ = clean_cobol_line(raw, active_format)
                    if truncated:
                        issue = "framework_replacement_source_not_fully_checked"
                    elif code:
                        try:
                            tokens = lex_cobol(code)
                            if covered:
                                # One indexed DataItem can share a physical
                                # line with unindexed text after its period.
                                end = next((index for index, token in enumerate(tokens)
                                            if token.kind == "symbol" and token.value == "."), None)
                                tokens = tokens[end + 1:] if end is not None else []
                            outside.update((token.kind, token.value) for token in tokens)
                        except CobolLexError:
                            issue = "framework_replacement_source_not_fully_checked"
            except (OSError, ValueError):
                issue = "framework_replacement_source_not_fully_checked"
            cache[relative] = outside, issue, [span[2] for span in spans]
        outside, issue, declarations = cache[relative]
        if issue:
            return issue
        for before, after in pairs:
            try:
                old, new = lex_cobol(before), lex_cobol(after)
            except CobolLexError:
                return "framework_layout_copy_form_not_supported"
            if len(old) != 1 or len(new) != 1:
                return "framework_layout_copy_form_not_supported"
            if old[0].kind == "symbol" or new[0].kind == "symbol":
                return "framework_layout_copy_form_not_supported"
            key = old[0].kind, old[0].value
            if key != (new[0].kind, new[0].value) and key in outside:
                return "framework_replacement_outside_data_declarations"
            if (key != (new[0].kind, new[0].value) and new[0].kind == "word"
                    and new[0].value in {"COPY", "REPLACE", "CBL", "PROCESS", "PROGRAM-ID"}):
                return "framework_replacement_changes_preprocessing"
        for declaration in declarations:
            transformed, changes, problem = _replace_tokens(declaration, pairs, {})
            if problem:
                return "framework_layout_copy_form_not_supported"
            if not changes:
                continue
            match = _DECL.fullmatch(transformed.strip())
            tail = match[3].strip().rstrip(".").strip() if match else None
            if (match is None or not 1 <= int(match[1]) <= 49
                    or tail and not _ELEMENTARY.fullmatch(tail)):
                return "framework_replacement_declaration_not_fully_parsed"
        return None
    audit.missing_sources = missing_sources
    return audit


def _copy_layout_resolver(db, relative, sha, *, replacement_audit=None):
    """Reuse the existing proven COPY layout, retaining each physical origin."""
    from framework_layout import _context, _reference

    def resolve(program):
        nodes, issues = _context(db, relative, program, replacement_audit=replacement_audit)
        if issues or not any(node["inclusions"] for node in nodes):
            return None
        grouped, uncertain = defaultdict(list), set()
        for node in nodes:
            unit = node["unit"]
            origins = [unit, *node["inclusions"]]
            if any(item.get("parse_status") != "complete" or not item.get("source_sha256")
                   or (item["relative_path"] == relative and item["source_sha256"] != sha)
                   for item in origins):
                return None
            node["binding_refs"] = [_reference(unit, "binding_declaration")]
            node["binding_refs"].extend(_reference(item, "copy_inclusion") for item in node["inclusions"])
            grouped[(program, node["name"])].append(node)
            if node["level"] == 66 or re.search(r"\b(?:REDEFINES|RENAMES|OCCURS)\b", node["tail"], re.I):
                uncertain.add((program, node["storage"]))
        return grouped, uncertain
    return resolve


def _selected_compilation_context(db, supplied=None):
    if supplied is not None:
        return supplied
    try:
        row = db.execute("SELECT value FROM metadata WHERE key='compilation_context'").fetchone()
        if row:
            return json.loads(row[0])
        options = db.execute("SELECT value FROM metadata WHERE key='source_options'").fetchone()
        return json.loads(options[0]).get("compilation_context") if options else None
    except (sqlite3.Error, ValueError, TypeError):
        return None


def _template_regex(template):
    parts = re.split(r"(X{2,8})", str(template).upper())
    return re.compile("".join("[A-Z0-9_$#@-]+" if re.fullmatch(r"X{2,8}", part)
                              else re.escape(part) for part in parts) + r"\Z")


def _candidate_paths(db, knowledge):
    """A coarse index filter avoids reopening unrelated files during ingestion."""
    targets = [_template_regex(row.get("target_template", ""))
               for row in knowledge.get("interfaces", [])]
    stages = {row["symbol"].upper() for row in knowledge.get("rules", [])
              if row.get("kind") == "section"}
    paths = set()
    if targets:
        for path, target in db.execute("SELECT DISTINCT relative_path,target_name FROM relations "
                                      "WHERE relation_type='CALLS'"):
            if any(pattern.fullmatch(target.upper()) for pattern in targets):
                paths.add(path)
    if stages:
        # Both parsers persist canonical uppercase definition symbols. Their
        # existing (symbol_type,name,program_name) index finds the few named
        # stages directly, without reading every source unit on each refresh.
        stage_names = sorted(stages)
        for start in range(0, len(stage_names), 500):
            batch = stage_names[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            paths.update(row[0] for row in db.execute(
                "SELECT relative_path FROM symbols WHERE symbol_type IN ('Section','Paragraph') "
                "AND name IN (" + placeholders + ")", batch))
    return paths


def _source_precedence(db, facts):
    """A manual never overrides an available or ambiguous source implementation."""
    targets = {row.get("target_name") for row in facts if row.get("relation_type") == "CALLS"}
    implemented = set()
    for target in targets:
        if db.execute("SELECT 1 FROM symbols WHERE symbol_type='Program' AND name=? LIMIT 1",
                      (target,)).fetchone():
            implemented.add(target)
    result = []
    for original in facts:
        row = deepcopy(original)
        if row.get("target_name") in implemented:
            row.update(dependency_covered=False, target_source_available=True,
                       reason="source_implementation_takes_precedence")
        else:
            row["target_source_available"] = False if row.get("relation_type") == "CALLS" else None
        result.append(row)
    return result


def refresh_framework_index(db, source_root, *, reference_path=None, check_cancel=None, progress=None):
    """Compile manuals and persist source-bound facts without any model call.

    Cache identity includes source content, the complete manual collection, and
    compiler versions. An unavailable manual invalidates its old interpretations.
    """
    from framework_binding import bind_framework_source
    from framework_layout import validate_framework_layouts
    from source_reading import _verified_lines
    if check_cancel:
        check_cancel()
    if progress:
        progress({"phase": "framework_semantics", "completed": 0, "total": None, "unit": "files"})
    knowledge, digest = _knowledge(reference_path)
    _schema(db)
    summary = {"schema_version": SEMANTICS_VERSION, "status": knowledge.get("status"),
               "document": knowledge.get("document"), "knowledge_digest": digest,
               "rule_count": len(knowledge.get("rules", [])),
               "interface_count": len(knowledge.get("interfaces", [])),
               "fact_count": 0, "matched_files": 0, "files_rebuilt": 0, "covered_external_calls": 0,
               "files_reused": 0, "boundaries": [], "network_calls": False,
               "runtime_verified": False}
    paths = _candidate_paths(db, knowledge)
    root = Path(source_root).resolve()
    replacement_audit = _copy_replacement_auditor(db, root=root, check_cancel=check_cancel)
    with db:
        db.execute("INSERT OR REPLACE INTO framework_knowledge_cache VALUES (1,?,?)",
                   (digest, json.dumps(knowledge, ensure_ascii=False)))
        db.execute("DELETE FROM framework_file_semantics WHERE relative_path NOT IN "
                   "(SELECT relative_path FROM source_files)")
        items = db.execute("SELECT relative_path,sha256,encoding,format_hint,line_count FROM source_files").fetchall()
        last_progress = time.monotonic()
        if progress:
            progress({"phase": "framework_semantics", "completed": 0, "total": len(items), "unit": "files"})
        for offset, item in enumerate(items, 1):
            if check_cancel:
                check_cancel()
            item = dict(item)
            relative = item["relative_path"]
            if progress and (offset == len(items) or time.monotonic() - last_progress >= .2):
                progress({"phase": "framework_semantics", "completed": offset - 1,
                          "total": len(items), "unit": "files", "current_file": relative})
                last_progress = time.monotonic()
            if relative not in paths:
                db.execute("DELETE FROM framework_file_semantics WHERE relative_path=?", (relative,))
                continue
            previous = db.execute("SELECT source_sha256,knowledge_digest,version,bindings_json "
                                  "FROM framework_file_semantics WHERE relative_path=?", (relative,)).fetchone()
            layout_digest = _copy_layout_digest(db, relative)
            previous_facts = json.loads(previous[3]) if previous else []
            if (previous and tuple(previous[:3]) == (item["sha256"], digest, SEMANTICS_VERSION)
                    and all(fact.get("layout_dependency_digest", layout_digest) == layout_digest for fact in previous_facts)):
                facts = previous_facts
                summary["files_reused"] += 1
            else:
                def lines():
                    for number, (line, truncated) in enumerate(
                            _verified_lines(root, item, check_cancel, 65536), 1):
                        # Preserve unknown lines so their possible persistent
                        # preprocessing effects withdraw binding coverage.
                        yield number, None if truncated else line
                try:
                    facts = bind_framework_source(lines(), knowledge, relative_path=relative,
                        source_sha256=item["sha256"], source_format=item["format_hint"],
                        layout_resolver=_copy_layout_resolver(db, relative, item["sha256"],
                            replacement_audit=replacement_audit))
                except (ValueError, OSError) as error:
                    summary["boundaries"].append({"relative_path": relative,
                        "reason": "framework_source_unavailable", "reason_code": str(error)[:160]})
                    db.execute("DELETE FROM framework_file_semantics WHERE relative_path=?", (relative,))
                    continue
                facts = facts[:MAX_FILE_FACTS]
                for fact in facts:
                    if fact.get("reason") == "function_value_not_bound" or fact.get("binding_evidence_refs"):
                        fact["layout_dependency_digest"] = layout_digest
                summary["files_rebuilt"] += 1
            if len(facts) >= MAX_FILE_FACTS:
                summary["boundaries"].append({"relative_path": relative,
                    "reason": "framework_file_fact_limit", "complete": False})
            summary["fact_count"] += len(facts)
            summary["matched_files"] += bool(facts)
            interpreted = validate_framework_layouts(db, _source_precedence(db, facts),
                replacement_audit=replacement_audit)
            from effect_contract import attach_effect_contracts
            interpreted = attach_effect_contracts(interpreted, knowledge,
                compilation_context=_selected_compilation_context(db))
            summary["covered_external_calls"] += sum(row.get("dependency_covered") is True for row in interpreted)
            db.execute("INSERT OR REPLACE INTO framework_file_semantics "
                "(relative_path,source_sha256,knowledge_digest,version,facts_json,bindings_json) VALUES (?,?,?,?,?,?)",
                (relative, item["sha256"], digest, SEMANTICS_VERSION,
                 json.dumps(interpreted, ensure_ascii=False), json.dumps(facts, ensure_ascii=False)))
        if progress:
            progress({"phase": "framework_semantics", "completed": len(items), "total": len(items), "unit": "files"})
    return summary


def _page_runs(pages):
    """Join overlapping/adjacent verified excerpts, never bridge omitted lines."""
    by_file = defaultdict(dict)
    formats = {}
    conflicts = set()
    for page in pages:
        if page.get("span_truncated") or page.get("include_chain"):
            continue
        key = (page.get("relative_path"), page.get("source_sha256"))
        text = page.get("source_text", page.get("text", ""))
        if not all(key) or not isinstance(text, str) or not text or not isinstance(page.get("start_line"), int):
            continue
        formats[key] = page.get("format_hint", "auto")
        for number, line in enumerate(text.split("\n"), page["start_line"]):
            if number > page.get("end_line", 0):
                break
            if number in by_file[key] and by_file[key][number] != line:
                conflicts.add(key)
            by_file[key][number] = line
    for key, lines in by_file.items():
        if key in conflicts:
            continue
        run, previous = [], None
        for number, line in sorted(lines.items()):
            if previous is not None and number != previous + 1:
                yield key, formats[key], run
                run = []
            run.append((number, line))
            previous = number
        if run:
            yield key, formats[key], run


def build_framework_facts(database_path, source_pages, *, reference_path=None, check_cancel=None,
                          source_session=None, compilation_context=None):
    """Use offline facts, rebinding changed manuals or older indexes locally."""
    from framework_binding import bind_framework_source
    from source_reading import _verified_lines
    knowledge, digest = _knowledge(reference_path)
    result = {"schema_version": SEMANTICS_VERSION, "status": knowledge.get("status"),
              "document": knowledge.get("document"), "compiler_version": knowledge.get("compiler_version"),
              "knowledge_digest": digest, "facts": [], "references": [], "source_requests": [],
              "network_calls": False, "runtime_verified": False}
    if not knowledge.get("rules"):
        return result
    facts, cached, file_metadata = {}, set(), {}
    with closing(sqlite3.connect(f"{Path(database_path).resolve().as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        replacement_audit = _copy_replacement_auditor(db, source_session=source_session,
            source_pages=source_pages, check_cancel=check_cancel)
        has_cache = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                               "AND name='framework_file_semantics'").fetchone()
        cache_column = ("bindings_json" if has_cache and "bindings_json" in
            {row[1] for row in db.execute("PRAGMA table_info(framework_file_semantics)")} else "facts_json")
        for key in dict.fromkeys((p.get("relative_path"), p.get("source_sha256")) for p in source_pages):
            if check_cancel:
                check_cancel()
            if not all(key):
                continue
            row = db.execute("SELECT * FROM source_files WHERE relative_path=?", (key[0],)).fetchone()
            if row is None or row["sha256"] != key[1]:
                cached.add(key)  # A stale page cannot establish a new binding.
                continue
            file_metadata[key] = dict(row)
            stored = db.execute(f"SELECT {cache_column} FROM framework_file_semantics WHERE relative_path=? "
                "AND source_sha256=? AND knowledge_digest=? AND version=?",
                (*key, digest, SEMANTICS_VERSION)).fetchone() if has_cache else None
            if stored:
                stored_facts = json.loads(stored[0])
                layout_digest = _copy_layout_digest(db, key[0])
                if all(fact.get("layout_dependency_digest", layout_digest) == layout_digest
                       for fact in stored_facts):
                    for fact in stored_facts:
                        facts[fact["fact_id"]] = fact
                    if len(stored_facts) < MAX_FILE_FACTS:
                        cached.add(key)
        # A partial excerpt cannot reveal persistent preprocessing directives,
        # duplicate fields or program boundaries elsewhere in the same file.
        # Capture a complete immutable file when request infrastructure exists.
        if source_session is not None:
            for key, metadata in file_metadata.items():
                if key in cached:
                    continue
                if check_cancel:
                    check_cancel()
                capture = source_session.capture(key[0])
                cached.add(key)
                if capture.sha256 != key[1]:
                    continue
                ranges = [(page["start_line"], page["end_line"])
                          for page in source_pages if (page.get("relative_path"), page.get("source_sha256")) == key]
                def complete_lines():
                    for number, (line, truncated) in enumerate(_verified_lines(
                            source_session.mirror_root, metadata, check_cancel, 65536), 1):
                        yield number, None if truncated else line
                bound = bind_framework_source(complete_lines(), knowledge, relative_path=key[0],
                    source_sha256=key[1], source_format=capture.source_format, focus_ranges=ranges,
                    layout_resolver=_copy_layout_resolver(db, key[0], key[1], replacement_audit=replacement_audit))
                for fact in bound:
                    facts[fact["fact_id"]] = fact
        for (relative, sha), format_hint, lines in _page_runs(source_pages):
            if (relative, sha) in cached:
                continue
            if check_cancel:
                check_cancel()
            owners = db.execute("SELECT program_name,start_line,end_line FROM code_units "
                "WHERE relative_path=? AND unit_type='Program' AND start_line<=? AND end_line>=?",
                (relative, lines[0][0], lines[-1][0])).fetchall()
            initial_program = None
            if len(owners) == 1 and db.execute("SELECT 1 FROM code_units WHERE relative_path=? "
                    "AND program_name=? AND unit_type='ProcedureSignature' AND end_line<? LIMIT 1",
                    (relative, owners[0]["program_name"], lines[0][0])).fetchone():
                initial_program = owners[0]["program_name"]
            for fact in bind_framework_source(lines, knowledge, relative_path=relative,
                    source_sha256=sha, source_format=format_hint, initial_program=initial_program,
                    layout_resolver=_copy_layout_resolver(db, relative, sha, replacement_audit=replacement_audit)):
                complete = lines[0][0] == 1 and lines[-1][0] == file_metadata[(relative, sha)]["line_count"]
                if not complete:
                    fact.update(dependency_covered=False, reason="complete_source_required_for_rebinding")
                if initial_program:
                    names = [fact.get("function_field"), fact.get("argument")]
                    for name in filter(None, names):
                        count = db.execute("SELECT COUNT(*) FROM code_units WHERE relative_path=? "
                            "AND program_name=? AND unit_type='DataItem' AND name=?",
                            (relative, initial_program, name)).fetchone()[0]
                        if count > 1:
                            fact.update(dependency_covered=False, reason="source_field_ambiguous")
                facts[fact["fact_id"]] = fact
        # Filter before applying the request limit: an early, unshown call must
        # not crowd a relevant call out merely because its file has many calls.
        from framework_layout import validate_framework_layouts
        candidates = visible_framework_facts(list(facts.values()), source_pages, knowledge.get("references", []))
        checked = _source_precedence(db, candidates)
        checked = validate_framework_layouts(db, checked, replacement_audit=replacement_audit)
        from effect_contract import attach_effect_contracts
        checked = attach_effect_contracts(checked, knowledge,
            compilation_context=_selected_compilation_context(db, compilation_context))
        # A selected call can be far from its parameter declarations. Expose
        # bounded missing spans so the caller may read them within its existing
        # budget; the fact still cannot survive until that source is supplied.
        requests = {tuple(item.values()): item for item in replacement_audit.missing_sources.values()}
        supplied_lines = _source_evidence_lines(source_pages)
        for fact in checked[:MAX_REQUEST_FACTS]:
            if not fact.get("dependency_covered"):
                continue
            for span in fact.get("source_ranges", []):
                if _span_evidence(fact, span, supplied_lines):
                    continue
                request = {"relative_path": span.get("relative_path", fact["relative_path"]),
                           "source_sha256": span.get("source_sha256", fact["source_sha256"]),
                           "start_line": span["start_line"], "end_line": span["end_line"]}
                requests[tuple(request.values())] = request
        result["source_requests"] = list(requests.values())[:MAX_REQUEST_FACTS]
        visible = visible_framework_facts(checked, source_pages, knowledge.get("references", []))
        result["omitted_fact_count"] = max(0, len(visible) - MAX_REQUEST_FACTS)
        result["facts"] = visible[:MAX_REQUEST_FACTS]
    ids = {key for fact in result["facts"] for key in fact.get("reference_ids", [])}
    for fact in result["facts"]:
        effect = fact.get("effect_contract", {})
        for candidate in effect.get("status_codes", []) + effect.get("explicit_configuration_candidates", []):
            ids.update(candidate.get("reference_ids", []))
    result["references"] = [ref for ref in knowledge.get("references", []) if ref["reference_id"] in ids]
    result["effect_contract_conflicts"] = [conflict for fact in result["facts"] for conflict in fact.get("effect_contract_conflicts", [])]
    return result


def _source_evidence_lines(pages):
    """Index actual supplied lines once, preserving version and overlap conflicts."""
    supplied = defaultdict(dict)
    for page in pages:
        key = (page.get("relative_path"), page.get("source_sha256"))
        text = page.get("source_text", page.get("text", ""))
        start, end = page.get("start_line"), page.get("end_line")
        if (page.get("span_truncated") or page.get("include_chain") or not all(key)
                or not page.get("evidence_id") or not isinstance(text, str) or not text
                or type(start) is not int or type(end) is not int or not 1 <= start <= end):
            continue
        lines = supplied[key]
        # Source pages use newline-joined physical rows. A final separator
        # therefore represents a real blank row, bounded by the supplied end.
        for number, line in enumerate(text.split("\n"), start):
            if number > end:
                break
            if number not in lines:
                lines[number] = (line, page["evidence_id"])
            elif lines[number] is not None and lines[number][0] != line:
                # Conflicting text cannot be repaired by picking a convenient
                # page, even if another page alone covers the complete span.
                lines[number] = None
    return supplied


def _span_evidence(fact, span, supplied_lines):
    key = (span.get("relative_path", fact.get("relative_path")),
           span.get("source_sha256", fact.get("source_sha256")))
    lines = supplied_lines.get(key, {})
    evidence = []
    for number in range(span["start_line"], span["end_line"] + 1):
        row = lines.get(number)
        if row is None:
            return []
        evidence.append(row[1])
    return list(dict.fromkeys(evidence))


def visible_framework_facts(facts, pages, references):
    """Only transmitted, untruncated source and rules can support a conclusion."""
    usable = {ref.get("reference_id") for ref in references
              if ref.get("text") and not ref.get("text_truncated")}
    supplied_lines = _source_evidence_lines(pages)
    result = []
    for original in facts:
        ids = original.get("reference_ids", [])
        if not ids or not set(ids) <= usable:
            continue
        spans = original.get("source_ranges", [])
        if not spans:
            continue
        evidence = []
        for span in spans:
            candidates = _span_evidence(original, span, supplied_lines)
            if not candidates:
                break
            evidence.extend(candidates)
        else:
            row = deepcopy(original)
            row["source_evidence_ids"] = list(dict.fromkeys(evidence))
            row["runtime_verified"] = False
            if row.get("effect_contract"):
                from effect_contract import visible_effect_contract
                row["effect_contract"] = visible_effect_contract(row["effect_contract"], usable)
            result.append(row)
    return result
