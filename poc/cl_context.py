"""Bounded CL source facts and indexed batch/file-environment reading leads.

Commands describe possible source actions. They do not establish a live job,
successful command execution, an effective override or a resolved system object.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import PurePosixPath, Path
import re
import sqlite3

CL_CONTEXT_VERSION = "cl-source-context-v1"
CL_SUFFIXES = frozenset({".cl", ".clle", ".clp"})
_NAME = re.compile(r"^[A-Z_$#@][A-Z0-9_$#@-]*$", re.I)
_COMMANDS = {"CALL", "SBMJOB", "ADDLIBLE", "CHGLIBL", "OVRDBF", "DLTOVR", "MONMSG"}
_MAX_STATEMENT_LINES, _MAX_STATEMENT_CHARS = 64, 32702


def is_cl_path(path):
    return PurePosixPath(path).suffix.lower() in CL_SUFFIXES


def ensure_cl_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS cl_command_facts (
            occurrence_id TEXT PRIMARY KEY,
            relative_path TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            command TEXT NOT NULL,
            target_name TEXT,
            statement TEXT NOT NULL,
            facts_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cl_facts_path
            ON cl_command_facts(relative_path,start_line);
        CREATE INDEX IF NOT EXISTS idx_cl_facts_target
            ON cl_command_facts(target_name,relative_path);
    """)


def delete_cl_facts(db, path):
    db.execute("DELETE FROM cl_command_facts WHERE relative_path=?", (path,))


def _words(text):
    """Split CL values outside literals and nested parentheses."""
    values, start, depth, quote, index = [], None, 0, None, 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if not quote and not depth and char.isspace():
            if start is not None:
                values.append(text[start:index])
                start = None
        elif start is None:
            start = index
        index += 1
    if start is not None:
        values.append(text[start:])
    return values


def _parameters(text):
    result, diagnostics, index = {}, [], 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index == len(text):
            break
        match = re.match(r"[A-Z][A-Z0-9]*", text[index:], re.I)
        if not match:
            diagnostics.append("positional_or_unparsed_parameter")
            break
        name = match.group().upper()
        index += len(match.group())
        while index < len(text) and text[index].isspace():
            index += 1
        if index == len(text) or text[index] != "(":
            diagnostics.append("positional_or_unparsed_parameter")
            break
        index += 1
        start, depth, quote = index, 1, None
        while index < len(text) and depth:
            char = text[index]
            if quote:
                if char == quote:
                    if index + 1 < len(text) and text[index + 1] == quote:
                        index += 2
                        continue
                    quote = None
            elif char in "'\"":
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        if depth or quote:
            diagnostics.append("unclosed_parameter_or_literal")
            break
        if name in result:
            diagnostics.append("duplicate_parameter:" + name)
        result[name] = text[start:index - 1].strip()
    return result, diagnostics


def _call_parameters(text):
    """CALL permits the program and parameter list in positional form."""
    stripped = text.strip()
    if re.match(r"PGM\s*\(", stripped, re.I):
        return _parameters(stripped)
    values = _words(stripped)
    if not values:
        return {}, ["cl_program_parameter_missing"]
    params = {"PGM": values[0]}
    remainder = stripped[len(values[0]):].strip()
    if not remainder:
        return params, []
    if re.match(r"PARM\s*\(", remainder, re.I):
        more, diagnostics = _parameters(remainder)
        return {**params, **more}, diagnostics
    if remainder.startswith("(") and remainder.endswith(")"):
        params["PARM"] = remainder[1:-1].strip()
        return params, []
    params["PARM"] = remainder
    return params, []


def _object(value):
    """Preserve ambiguous library selectors instead of choosing an object."""
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1].replace(value[0] * 2, value[0])
    value = value.upper()
    parts = value.split("/")
    valid = len(parts) in {1, 2} and all(_NAME.fullmatch(part) or part in {"*LIBL", "*CURLIB"} for part in parts)
    return {"spelling": value, "name": parts[-1] if valid else None,
            "library": parts[0] if valid and len(parts) == 2 else None,
            "binding_status": "explicit_source_spelling" if valid and len(parts) == 2 and not parts[0].startswith("*")
                else "library_list_candidate" if valid else "dynamic_or_unsupported",
            "runtime_object_verified": False}


class CLSourceCollector:
    """Streaming lexical collector; each persisted row retains physical source."""
    def __init__(self, path, sha):
        self.path, self.sha = path, sha
        self.pending, self.code, self.comment, self.quote = [], "", False, None
        self.facts, self.boundaries = [], []
        self.controls, self.environment = [], []
        self.last_fact = None
        self.else_candidate = None

    def _clean(self, raw):
        # Numbered source exports may have six digits before the command.
        if re.match(r"^\d{6}\s", raw):
            raw = raw[6:]
        result, index = [], 0
        while index < len(raw):
            char = raw[index]
            if self.comment:
                end = raw.find("*/", index)
                if end < 0:
                    break
                self.comment, index = False, end + 2
                result.append(" ")
                continue
            if self.quote:
                result.append(char)
                if char == self.quote:
                    if index + 1 < len(raw) and raw[index + 1] == self.quote:
                        result.append(raw[index + 1])
                        index += 2
                        continue
                    self.quote = None
            elif raw.startswith("/*", index):
                self.comment, index = True, index + 2
                result.append(" ")
                continue
            elif char in "'\"":
                self.quote = char
                result.append(char)
            else:
                result.append(char)
            index += 1
        return "".join(result)

    def consume(self, number, raw):
        if raw is None:
            self.boundaries.append({"reason": "cl_source_line_unavailable", "start_line": number})
            self.pending, self.code = [], ""
            return
        cleaned = self._clean(raw)
        if not self.pending and not cleaned.strip():
            return
        self.pending.append((number, raw))
        continuation = bool(cleaned.rstrip().endswith(("+", "-")))
        marker = cleaned.rstrip()[-1:] if continuation else ""
        cleaned = cleaned.rstrip()[:-1] if continuation else cleaned
        self.code += cleaned.lstrip() if getattr(self, "join_marker", None) == "+" else cleaned
        self.join_marker = marker
        if self.quote and not continuation and not self.comment:
            self.boundaries.append({"reason": "cl_literal_continuation_missing", "start_line": self.pending[0][0]})
            self.pending, self.code, self.quote = [], "", None
            return
        if len(self.pending) >= _MAX_STATEMENT_LINES or len(self.code) > _MAX_STATEMENT_CHARS:
            self.boundaries.append({"reason": "cl_statement_budget", "start_line": self.pending[0][0]})
            self.pending, self.code, self.quote = [], "", None
        elif not continuation and not self.comment and not self.quote:
            self._flush()

    def _flush(self):
        if not self.pending:
            return
        code = re.sub(r"^[A-Z0-9_$#@-]+:\s*", "", self.code.strip(), flags=re.I)
        self._command(code)
        self.pending, self.code, self.join_marker = [], "", ""

    def _command(self, code, *, context="current_job_source", parent=None, guards=()):
        match = re.match(r"^([A-Z][A-Z0-9]*)\b", code, re.I)
        if not match:
            return
        command = match.group(1).upper()
        params, diagnostics = (_call_parameters(code[match.end():]) if command == "CALL"
                               else _parameters(code[match.end():]))
        source_ref = {"relative_path": self.path, "source_sha256": self.sha,
                      "start_line": self.pending[0][0], "end_line": self.pending[-1][0],
                      "statement": "\n".join(line for _, line in self.pending)}
        if command == "ENDDO":
            if self.controls:
                closed = self.controls.pop()
                self.else_candidate = closed if closed.get("command") == "IF" else None
            else:
                self.boundaries.append({"reason": "cl_control_scope_unmatched", "start_line": self.pending[0][0]})
            return
        if command in {"IF", "ELSE"}:
            nested = params.get("THEN") if command == "IF" else params.get("CMD")
            prior = self.else_candidate if command == "ELSE" else None
            guard = {"command": command, "condition": params.get("COND") if command == "IF" else
                     prior.get("condition") if prior else None, "source_line": self.pending[0][0],
                     "outcome": command == "IF", "outcome_evaluated": False, "source_ref": source_ref,
                     "scope_status": "source_branch" if command == "IF" or prior else "unknown"}
            if prior:
                guard["if_source_ref"] = prior["source_ref"]
            self.else_candidate = None
            if nested and nested.strip().upper() != "DO":
                self._command(nested, context=context, parent=command, guards=(*guards, guard))
                if command == "IF":
                    self.else_candidate = guard
            else:
                self.controls.append(guard)
            return
        if command in {"DO", "DOWHILE", "DOUNTIL", "DOFOR"}:
            self.controls.append({"command": command, "condition": params.get("COND"),
                                  "source_line": self.pending[0][0], "outcome_evaluated": False,
                                  "source_ref": source_ref, "scope_status": "source_loop"})
            return
        self.else_candidate = None
        if command not in _COMMANDS:
            if command not in {"PGM", "ENDPGM", "DCL", "DCLF"}:
                self.boundaries.append({"reason": "cl_command_effect_not_supported", "command": command,
                                        "start_line": self.pending[0][0]})
            self.last_fact = None
            return
        first, last = self.pending[0][0], self.pending[-1][0]
        raw = "\n".join(line for _, line in self.pending)
        identity = hashlib.sha256(f"{self.path}\0{self.sha}\0{first}\0{last}\0{context}\0{code}".encode()).hexdigest()
        fact = {"occurrence_id": "cl_" + identity[:32], "kind": "cl_" + command.lower(), "command": command,
                "relative_path": self.path, "source_sha256": self.sha, "start_line": first, "end_line": last,
                "statement": raw, "normalized_statement": code, "parameters": params,
                "status": "candidate" if diagnostics else "confirmed_source_syntax", "diagnostics": diagnostics,
                "command_context": context, "parent_command": parent, "guards": [*self.controls, *guards],
                "runtime_execution_verified": False, "runtime_object_verified": False}
        if any(guard.get("scope_status") == "unknown" for guard in fact["guards"]):
            fact["diagnostics"].append("cl_control_scope_unresolved")
            fact["status"] = "candidate"
        if command == "CALL":
            target = _object(params.get("PGM"))
            fact.update(target=target, target_name=target["name"] if target else None,
                        arguments=_words(params.get("PARM", "")),
                        environment_candidates=list(self.environment) if context == "current_job_source" else [],
                        environment_state_verified=False)
            if not target or target["binding_status"] == "dynamic_or_unsupported":
                fact["diagnostics"].append("cl_dynamic_program_target")
        elif command == "SBMJOB":
            fact.update(job_name=params.get("JOB"), job_description=_object(params.get("JOBD")),
                        initial_library_list=_words(params["INLLIBL"]) if "INLLIBL" in params else None,
                        initial_library_list_origin="source_parameter" if "INLLIBL" in params else "omitted_runtime_or_command_default",
                        submission_status="source_submission_candidate", actual_scheduler_verified=False,
                        inherited_overrides_verified=False)
        elif command == "OVRDBF":
            fact.update(file=_object(params.get("FILE")), tofile=_object(params.get("TOFILE")),
                        member=params.get("MBR"), declared_override_scope=params.get("OVRSCOPE"),
                        effective_override_verified=False, scope_resolution_requires="job_call_stack_and_activation_group")
        elif command == "DLTOVR":
            fact.update(file=params.get("FILE"), declared_delete_level=params.get("LVL"),
                        deleted_override_verified=False)
        elif command in {"ADDLIBLE", "CHGLIBL"}:
            fact.update(libraries=_words(params.get("LIB" if command == "ADDLIBLE" else "LIBL", "")),
                        position=params.get("POSITION") if command == "ADDLIBLE" else None,
                        current_library=params.get("CURLIB"), effective_library_list_verified=False)
        elif command == "MONMSG":
            fact.update(message_ids=_words(params.get("MSGID", "")), execution=params.get("EXEC"),
                        monitored_command_occurrence_id=self.last_fact.get("occurrence_id") if self.last_fact else None,
                        monitor_binding="source_predecessor_candidate", error_occurred_verified=False)
        self.facts.append(fact)
        if command in {"ADDLIBLE", "CHGLIBL", "OVRDBF", "DLTOVR"} and context == "current_job_source":
            self.environment.append(fact["occurrence_id"])
        if command == "SBMJOB" and params.get("CMD"):
            self._command(params["CMD"], context="submitted_job_candidate", parent="SBMJOB",
                guards=(*guards, {"command": "SBMJOB", "source_line": first, "source_ref": source_ref,
                                 "submission_succeeded_verified": False}))
            for child in self.facts:
                if child["start_line"] == first and child.get("parent_command") == "SBMJOB":
                    child["initial_library_list"] = fact["initial_library_list"]
                    child["job_description"] = fact["job_description"]
        elif command == "MONMSG" and params.get("EXEC"):
            self._command(params["EXEC"], context=context, parent="MONMSG",
                guards=(*guards, {"command": "MONMSG", "source_line": first, "message_ids": fact["message_ids"],
                                 "source_ref": source_ref,
                                 "error_occurred_verified": False}))
        self.last_fact = fact

    def finish(self):
        if self.pending:
            self.boundaries.append({"reason": "cl_unclosed_statement", "start_line": self.pending[0][0]})
        if self.comment:
            self.boundaries.append({"reason": "cl_unclosed_comment"})
        if self.controls:
            self.boundaries.append({"reason": "cl_control_scope_unclosed"})
        return self.facts


def persist_cl_facts(db, collector):
    facts = collector.finish()
    db.executemany("INSERT OR REPLACE INTO cl_command_facts VALUES (?,?,?,?,?,?,?,?,?)", [
        (fact["occurrence_id"], collector.path, collector.sha, fact["start_line"], fact["end_line"],
         fact["command"], fact.get("target_name"), fact["statement"], json.dumps(fact, ensure_ascii=False)) for fact in facts])
    return facts


def nominate_cl_evidence(database_path, paths=(), target_names=(), *, max_candidates=48):
    """Read indexed command facts by selected paths/program targets only."""
    if type(max_candidates) is not int or not 1 <= max_candidates <= 128:
        raise ValueError("CL_CANDIDATE_BUDGET_INVALID")
    candidates, frontier, hashes = [], [], {}
    with closing(sqlite3.connect(Path(database_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cl_command_facts'").fetchone():
            return [], [{"kind": "cl_batch_context", "reason": "cl_index_upgrade_required"}], {}
        scoped = list(dict.fromkeys(path for path in paths if is_cl_path(path)))[:8]
        names = list(dict.fromkeys(str(name).upper() for name in target_names if _NAME.fullmatch(str(name))))[:32]
        if names:
            slots = ",".join("?" for _ in names)
            callers = db.execute(f"SELECT DISTINCT relative_path FROM cl_command_facts WHERE target_name IN ({slots}) "
                "ORDER BY relative_path LIMIT 9", names).fetchall()
            scoped.extend(row[0] for row in callers[:8])
            if len(callers) > 8:
                frontier.append({"kind": "cl_batch_context", "reason": "cl_caller_path_budget"})
        # Follow CL source filename candidates for one upstream submitter level.
        upstream_names = [PurePosixPath(path).stem.upper() for path in scoped]
        if upstream_names:
            slots = ",".join("?" for _ in upstream_names)
            scoped.extend(row[0] for row in db.execute(f"SELECT DISTINCT relative_path FROM cl_command_facts "
                f"WHERE target_name IN ({slots}) ORDER BY relative_path LIMIT 9", upstream_names))
        scoped = list(dict.fromkeys(scoped))[:16]
        for path in scoped:
            target_priority = ("CASE WHEN f.target_name IN (" + ",".join("?" for _ in names) + ") THEN 0 "
                "WHEN f.command IN ('OVRDBF','DLTOVR','CHGLIBL','ADDLIBLE') THEN 1 ELSE 2 END,") if names else ""
            rows = db.execute("SELECT f.*,s.line_count AS file_line_count FROM cl_command_facts f JOIN source_files s "
                "ON s.relative_path=f.relative_path AND s.sha256=f.source_sha256 "
                "WHERE f.relative_path=? ORDER BY " + target_priority + "f.start_line,f.occurrence_id LIMIT ?",
                (path, *names, max_candidates + 1) if names else (path, max_candidates + 1)).fetchall()
            for row in rows:
                if len(candidates) >= max_candidates:
                    frontier.append({"kind": "cl_batch_context", "relative_path": path, "reason": "cl_candidate_budget"})
                    break
                hashes[path] = row["source_sha256"]
                candidates.append({"kind": "cl_batch_context", "relative_path": path, "start_line": row["start_line"],
                    "end_line": row["end_line"], "statement": row["statement"], "source_sha256": row["source_sha256"],
                    "file_line_count": row["file_line_count"],
                    "occurrence_id": row["occurrence_id"], "occurrence_ids": [row["occurrence_id"]],
                    "source_facts": [json.loads(row["facts_json"])],
                    "reads": [], "writes": [], "occurrence_count": 1})
    unique = {}
    for row in candidates:
        key = (row["relative_path"], row["start_line"], row["end_line"])
        if key in unique:
            unique[key]["occurrence_ids"].extend(row["occurrence_ids"])
            unique[key]["source_facts"].extend(row["source_facts"])
        else:
            unique[key] = row
    return list(unique.values()), frontier, hashes


def collect_cl_context(pages, *, max_observations=96, max_source_chars=262144):
    """Attach only facts reconstructed from consistent supplied physical pages."""
    rows, refs, hashes, conflicts, boundaries = {}, {}, {}, set(), []
    total = 0
    for page in pages:
        path = page.get("relative_path", "")
        if not is_cl_path(path):
            continue
        first, last, text, sha, eid = (page.get(key) for key in
            ("start_line", "end_line", "source_text", "source_sha256", "evidence_id"))
        if (not isinstance(text, str) or type(first) is not int or type(last) is not int or first < 1
                or last < first or len(text.splitlines()) != last-first+1 or page.get("span_truncated")
                or not isinstance(sha, str) or not eid):
            boundaries.append({"reason": "cl_incomplete_source_page", "relative_path": path})
            continue
        total += len(text)
        if total > max_source_chars:
            boundaries.append({"reason": "cl_source_character_budget"})
            break
        if path in hashes and hashes[path] != sha:
            conflicts.add(path)
        hashes[path] = sha
        visible = rows.setdefault(path, {})
        for number, line in enumerate(text.splitlines(), first):
            if number in visible and visible[number] != line:
                conflicts.add(path)
            visible[number] = line
            refs.setdefault((path, number), set()).add(eid)
    observations = []
    for path, visible in rows.items():
        if path in conflicts:
            boundaries.append({"reason": "cl_conflicting_source_pages", "relative_path": path})
            continue
        collector = None
        previous = 0
        for number, raw in sorted(visible.items()):
            if collector is None and number != 1:
                if previous == 0:
                    boundaries.append({"reason": "cl_source_scope_gap", "relative_path": path, "start_line": number})
                    boundaries.append({"reason": "cl_lexical_prefix_unavailable", "relative_path": path,
                                       "start_line": number})
                previous = number
                continue
            if collector is None or number != previous + 1:
                if collector:
                    collector.finish()
                    boundaries.extend({"relative_path": path, **item} for item in collector.boundaries)
                    observations.extend(collector.facts)
                    boundaries.append({"reason": "cl_source_scope_gap", "relative_path": path, "start_line": number})
                    boundaries.append({"reason": "cl_lexical_prefix_unavailable", "relative_path": path,
                                       "start_line": number})
                    collector = None
                    previous = number
                    continue
                collector = CLSourceCollector(path, hashes[path])
            collector.consume(number, raw)
            previous = number
        if collector:
            collector.finish()
            boundaries.extend({"relative_path": path, **item} for item in collector.boundaries)
            observations.extend(collector.facts)
    for fact in observations:
        fact["evidence_ids"] = sorted({eid for number in range(fact["start_line"], fact["end_line"] + 1)
                                       for eid in refs.get((fact["relative_path"], number), ())})
        fact["source_locations"] = [{key: fact[key] for key in
            ("relative_path", "source_sha256", "start_line", "end_line")}]
    if len(observations) > max_observations:
        boundaries.append({"reason": "cl_observation_budget", "remaining": len(observations)-max_observations})
    return {"version": CL_CONTEXT_VERSION, "observations": observations[:max_observations], "boundaries": boundaries[:32],
            "scope": {"source_commands_only": True, "runtime_jobs_verified": False,
                      "scheduler_verified": False, "effective_library_list_verified": False,
                      "effective_overrides_verified": False,
                      "unsupported_commands_may_affect_environment": True}}


def bind_indexed_cl_context(pages, candidates, *, max_observations=96, max_source_chars=262144):
    """Bind complete-index lexical facts to exact supplied statement text.

    An indexed full-file parse establishes that a late statement is code. Its
    physical statement still has to match the supplied version and text. Guard
    and preceding environment facts need their own supplied source before the
    corresponding context can be described as covered.
    """
    context = collect_cl_context([], max_observations=max_observations, max_source_chars=max_source_chars)
    rows, versions, conflicts, total = {}, {}, set(), 0
    for page in pages:
        path = page.get("relative_path", "")
        if not is_cl_path(path):
            continue
        first, last, text, sha, eid = (page.get(key) for key in
            ("start_line", "end_line", "source_text", "source_sha256", "evidence_id"))
        if (type(first) is not int or type(last) is not int or first < 1 or last < first
                or not isinstance(text, str) or len(text.splitlines()) != last-first+1
                or not isinstance(sha, str) or not sha or not eid or page.get("span_truncated")):
            context["boundaries"].append({"reason": "cl_incomplete_source_page", "relative_path": path})
            continue
        total += len(text)
        if total > max_source_chars:
            context["boundaries"].append({"reason": "cl_source_character_budget"})
            break
        if path in versions and versions[path] != sha:
            conflicts.add(path)
        versions[path] = sha
        visible = rows.setdefault(path, {})
        for number, raw in enumerate(text.splitlines(), first):
            previous = visible.get(number)
            if previous and previous[0] != raw:
                conflicts.add(path)
            visible[number] = (raw, set(previous[1]) | {eid} if previous else {eid})

    def covered(ref):
        if not isinstance(ref, dict):
            return []
        path, sha = ref.get("relative_path"), ref.get("source_sha256")
        first, last, statement = ref.get("start_line"), ref.get("end_line"), ref.get("statement")
        if (path in conflicts or versions.get(path) != sha or type(first) is not int or type(last) is not int
                or first < 1 or last < first or not isinstance(statement, str)):
            return []
        expected, visible = statement.splitlines(), rows.get(path, {})
        if len(expected) != last-first+1 or any(number not in visible or visible[number][0] != raw
                for number, raw in enumerate(expected, first)):
            return []
        return sorted({eid for number in range(first, last+1) for eid in visible[number][1]})

    observations, parsed_spans = {}, {}
    for candidate in candidates:
        for stored in candidate.get("source_facts", ()):
            if (not isinstance(stored, dict) or stored.get("relative_path") != candidate.get("relative_path")
                    or stored.get("source_sha256") != candidate.get("source_sha256")
                    or stored.get("start_line") != candidate.get("start_line")
                    or stored.get("end_line") != candidate.get("end_line")
                    or stored.get("statement") != candidate.get("statement")):
                continue
            ids = covered(stored)
            if not ids:
                continue
            # Recheck the full supplied statement with the same lexer. A
            # corrupt cached target cannot change a literal into a CALL.
            span_key = (stored["relative_path"], stored["source_sha256"], stored["start_line"], stored["end_line"])
            if span_key not in parsed_spans:
                collector = CLSourceCollector(stored["relative_path"], stored["source_sha256"])
                for number, raw in enumerate(stored["statement"].splitlines(), stored["start_line"]):
                    collector.consume(number, raw)
                collector.finish()
                parsed_spans[span_key] = {fact["occurrence_id"]: fact for fact in collector.facts}
            parsed = parsed_spans[span_key].get(stored.get("occurrence_id"))
            if not parsed or any(parsed.get(key) != stored.get(key) for key in
                                 ("command", "parameters", "target_name", "normalized_statement")):
                context["boundaries"].append({"reason": "cl_indexed_statement_mismatch",
                    "relative_path": stored["relative_path"], "start_line": stored["start_line"]})
                continue
            fact = deepcopy(stored)
            fact.update(evidence_ids=ids, lexical_basis="complete_indexed_source_parse_and_exact_supplied_statement",
                        binding_basis="indexed_complete_source_parse_and_supplied_span",
                        source_locations=[{key: stored[key] for key in
                            ("relative_path", "source_sha256", "start_line", "end_line")}])
            guard_ids, missing_guards = [], []
            for guard in fact.get("guards", ()):
                references = [guard.get("source_ref")]
                if guard.get("if_source_ref"):
                    references.append(guard["if_source_ref"])
                supplied_ids = [covered(ref) for ref in references]
                guard["source_supplied"] = all(supplied_ids)
                guard["evidence_ids"] = sorted({eid for group in supplied_ids for eid in group})
                guard_ids.extend(guard["evidence_ids"])
                if not guard["source_supplied"] or guard.get("scope_status") == "unknown":
                    missing_guards.append(guard.get("source_line"))
            fact["guard_evidence_ids"] = sorted(set(guard_ids))
            fact["missing_guard_source_lines"] = missing_guards
            observations[fact["occurrence_id"]] = fact
    for fact in observations.values():
        predecessors = fact.get("environment_candidates", ())
        missing = [identity for identity in predecessors if identity not in observations]
        fact["environment_evidence_ids"] = sorted({eid for identity in predecessors
            for eid in observations.get(identity, {}).get("evidence_ids", ())})
        fact["missing_environment_occurrence_ids"] = missing
        fact["control_scope_supplied"] = not fact["missing_guard_source_lines"]
        fact["environment_scope_supplied"] = not missing
        fact["scope_unknown"] = bool(fact["missing_guard_source_lines"] or missing)
        fact["scope_known"] = not fact["scope_unknown"]
        fact["source_context_coverage"] = "partial" if fact["scope_unknown"] else "supplied_indexed_source_context"
        if fact["scope_unknown"]:
            fact["status"] = "candidate"
    for path in sorted(conflicts):
        context["boundaries"].append({"reason": "cl_conflicting_source_pages", "relative_path": path})
    ordered = sorted(observations.values(), key=lambda fact: (fact["relative_path"], fact["start_line"], fact["occurrence_id"]))
    if len(ordered) > max_observations:
        context["boundaries"].append({"reason": "cl_observation_budget", "remaining": len(ordered)-max_observations})
    context["observations"] = ordered[:max_observations]
    context["boundaries"] = context["boundaries"][:32]
    return context
