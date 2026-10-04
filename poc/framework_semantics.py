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


SEMANTICS_VERSION = "framework-semantics/v1.3"
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
            if previous and tuple(previous[:3]) == (item["sha256"], digest, SEMANTICS_VERSION):
                facts = json.loads(previous[3])
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
                        source_sha256=item["sha256"], source_format=item["format_hint"])
                except (ValueError, OSError) as error:
                    summary["boundaries"].append({"relative_path": relative,
                        "reason": "framework_source_unavailable", "reason_code": str(error)[:160]})
                    db.execute("DELETE FROM framework_file_semantics WHERE relative_path=?", (relative,))
                    continue
                facts = facts[:MAX_FILE_FACTS]
                summary["files_rebuilt"] += 1
            if len(facts) >= MAX_FILE_FACTS:
                summary["boundaries"].append({"relative_path": relative,
                    "reason": "framework_file_fact_limit", "complete": False})
            summary["fact_count"] += len(facts)
            summary["matched_files"] += bool(facts)
            interpreted = validate_framework_layouts(db, _source_precedence(db, facts))
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
        if not all(key) or not isinstance(text, str) or not isinstance(page.get("start_line"), int):
            continue
        formats[key] = page.get("format_hint", "auto")
        for number, line in enumerate(text.splitlines(), page["start_line"]):
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
                          source_session=None):
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
                    source_sha256=key[1], source_format=capture.source_format, focus_ranges=ranges)
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
                    source_sha256=sha, source_format=format_hint, initial_program=initial_program):
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
        checked = validate_framework_layouts(db, checked)
        # A selected call can be far from its parameter declarations. Expose
        # bounded missing spans so the caller may read them within its existing
        # budget; the fact still cannot survive until that source is supplied.
        requests = {}
        for fact in checked[:MAX_REQUEST_FACTS]:
            if not fact.get("dependency_covered"):
                continue
            for span in fact.get("source_ranges", []):
                if _span_evidence(fact, span, source_pages):
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
    result["references"] = [ref for ref in knowledge.get("references", []) if ref["reference_id"] in ids]
    return result


def _span_evidence(fact, span, pages):
    return [p["evidence_id"] for p in pages if not p.get("span_truncated") and not p.get("include_chain")
        and p.get("relative_path") == span.get("relative_path", fact.get("relative_path"))
        and p.get("source_sha256") == span.get("source_sha256", fact.get("source_sha256"))
        and p.get("evidence_id") and isinstance(p.get("start_line"), int)
        and p["start_line"] <= span["start_line"] and p.get("end_line", 0) >= span["end_line"]
        and len(p.get("source_text", p.get("text", "")).splitlines()) >= span["end_line"] - p["start_line"] + 1]


def visible_framework_facts(facts, pages, references):
    """Only transmitted, untruncated source and rules can support a conclusion."""
    usable = {ref.get("reference_id") for ref in references
              if ref.get("text") and not ref.get("text_truncated")}
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
            candidates = _span_evidence(original, span, pages)
            if not candidates:
                break
            evidence.append(candidates[0])
        else:
            row = deepcopy(original)
            row["source_evidence_ids"] = list(dict.fromkeys(evidence))
            row["runtime_verified"] = False
            result.append(row)
    return result
