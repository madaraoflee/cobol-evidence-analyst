"""Bounded file/field observations from already supplied physical source pages.

These facts describe visible source syntax. They do not certify execution,
complete field lineage, external file identity, or a system LF/PF mapping.
"""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import PurePosixPath
import re

from business_index import _clean
from statement_facts import sentence_terminated
from structural_index import _extract_data_access, _unique_identifiers

_NAME = r"[A-Z][A-Z0-9_$#@-]*"
_LITERAL = r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\""
_START = re.compile(rf"^(?:\d\d?\s+{_NAME}|{_NAME}\s+SECTION|"
                    r"(?:IDENTIFICATION|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION|"
                    r"SELECT|FD|SD|COPY|CALL|PERFORM|READ|START|WRITE|REWRITE|DELETE|"
                    r"MOVE|COMPUTE|ADD|SUBTRACT|MULTIPLY|DIVIDE|IF|ELSE|END-|OPEN|CLOSE|EXIT|GOBACK)\b", re.I)


def _refs(lines):
    return {"evidence_ids": list(dict.fromkeys(identifier for line in lines for identifier in line[2])),
            "source_locations": [{"relative_path": lines[0][3], "start_line": lines[0][0],
                                  "end_line": lines[-1][0]}]}


def _join_refs(*items):
    return {"evidence_ids": list(dict.fromkeys(identifier for item in items for identifier in item["evidence_ids"])),
            "source_locations": list({(location["relative_path"], location["start_line"], location["end_line"]): location
                                      for item in items for location in item["source_locations"]}.values())}


def _mask_literals(text):
    masked, index = list(text), 0
    while index < len(text):
        if text[index] not in {"'", '"'}:
            index += 1
            continue
        start, quote = index, text[index]
        index += 1
        while index < len(text):
            if text[index] == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                index += 1
                break
            index += 1
        else:
            return "", False
        masked[start:index] = " " * (index - start)
    return "".join(masked), True


def _chunks(lines):
    pending, active_format = [], "auto"
    for line in lines:
        code, active_format, continuation = _clean(line[1], active_format)
        if not code:
            continue
        if pending and (not continuation and (_START.match(code) or re.fullmatch(rf"{_NAME}\.", code, re.I))):
            yield " ".join(row[1] for row in pending), pending, True
            pending = []
        pending.append((line[0], code, line[2], line[3]))
        if sentence_terminated(code) or len(pending) >= 64:
            yield " ".join(row[1] for row in pending), pending, sentence_terminated(code)
            pending = []
    if pending:
        yield " ".join(row[1] for row in pending), pending, False


def collect_file_impact(source_pages, *, max_observations=160, max_source_chars=1_000_000,
                        max_metadata_bytes=160_000):
    """Observe only consistent, untruncated supplied pages; never read a file.

    Missing pages break declaration context. Associations across such gaps are
    candidates, while an explicit assignment or I/O operand remains reportable.
    COPY expansion requires one supplied member with contiguous declarations
    starting at physical line 1 and a simple unqualified COPY statement.
    """
    if (type(max_observations) is not int or max_observations < 1 or type(max_source_chars) is not int or max_source_chars < 1
            or type(max_metadata_bytes) is not int or max_metadata_bytes < 1000):
        raise ValueError("File impact limits must be positive integers.")
    observations, boundaries, rows, hashes, conflicts = [], [], defaultdict(dict), defaultdict(set), set()
    total_chars, truncated, frontier_refs_left = 0, False, max_observations

    def boundary(reason, **context):
        nonlocal frontier_refs_left
        if len(boundaries) < max_observations:
            for key in ("evidence_ids", "source_locations"):
                if key in context:
                    context[key] = context[key][:min(4, frontier_refs_left)]
                    frontier_refs_left -= len(context[key])
            boundaries.append({"reason": reason, **context})

    def observe(kind, refs, **values):
        nonlocal truncated
        item = {"kind": kind, "status": "confirmed_source_syntax", **values, **refs}
        if len(observations) < max_observations:
            observations.append(item)
        else:
            truncated = True
        return item

    for page in source_pages:
        path, identifier, text = page.get("relative_path"), page.get("evidence_id"), page.get("source_text")
        first, last = page.get("start_line"), page.get("end_line")
        if not (isinstance(path, str) and isinstance(identifier, str) and identifier and isinstance(text, str)
                and type(first) is int and type(last) is int and first > 0 and last >= first):
            boundary("invalid_source_page")
            continue
        lines = text.splitlines()
        if page.get("span_truncated") or len(lines) != last - first + 1:
            boundary("incomplete_physical_page", relative_path=path, evidence_ids=[identifier])
            continue
        total_chars += len(text)
        if total_chars > max_source_chars:
            boundary("source_character_limit")
            truncated = True
            break
        hashes[path].add(page.get("source_sha256"))
        for number, raw in enumerate(lines, first):
            old = rows[path].get(number)
            if old and old[1] != raw:
                conflicts.add(path)
            elif old:
                old[2].append(identifier)
            else:
                rows[path][number] = (number, raw, [identifier], path)
    for path, values in hashes.items():
        if len(values) > 1:
            conflicts.add(path)
    for path in conflicts:
        rows.pop(path, None)
        boundary("conflicting_source_pages", relative_path=path)

    declarations, records, assignments, operations, copies, programs = [], [], [], [], [], defaultdict(list)
    field_counts = defaultdict(int)
    members = defaultdict(list)
    for path, visible in rows.items():
        ordered = sorted(visible.values())
        blocks, current = [], []
        for row in ordered:
            if current and row[0] != current[-1][0] + 1:
                blocks.append(current)
                current = []
            current.append(row)
        if current:
            blocks.append(current)
        if len(blocks) > 1:
            boundary("source_page_gap", relative_path=path)
        for block_number, block in enumerate(blocks):
            scope = (path, block_number)
            program, section, fd, record = None, None, None, None
            if PurePosixPath(path).suffix.lower() in {".dds", ".lf", ".pf"}:
                dds_record, dds_record_refs = None, None
                for row in block:
                    raw = row[1].upper()
                    if len(raw) > 5 and raw[:5].isdigit() and raw[5] == "A":
                        raw = "     A" + raw[6:]
                    if not re.match(r"^\s*A\s", raw) or re.match(r"\s*A\*", raw):
                        continue
                    code, lexical_complete = _mask_literals(raw)
                    if not lexical_complete:
                        boundary("dds_literal_continuation_not_supported", relative_path=path, **_refs([row]))
                        break
                    if match := re.match(rf"^\s*A\s+R\s+({_NAME})\b", code):
                        dds_record = match.group(1)
                        dds_record_refs = _refs([row])
                    match = re.search(r"\bPFILE\s*\(\s*([A-Z0-9_$#@/ -]+)\s*\)", code)
                    if match:
                        refs = _join_refs(_refs([row]), dds_record_refs) if dds_record_refs else _refs([row])
                        observe("dds_pfile", refs, relative_path=path, record_name=dds_record,
                                physical_file_names=match.group(1).split(),
                                identity_basis="DDS PFILE source clause; file path is not system object identity")
                continue
            chunks = list(_chunks(block))
            member_complete = all(re.match(rf"^\d\d?\s+{_NAME}\b", code, re.I)
                                  and sentence_terminated(code) and not re.search(r"\b(?:REDEFINES|OCCURS|RENAMES)\b", code, re.I)
                                  for code, _, _ in chunks)
            for code, lines, complete in chunks:
                upper, refs = code.upper().strip(), _refs(lines)
                plain = re.sub(_LITERAL, " ", upper)
                if match := re.match(rf"PROGRAM-ID\s*\.\s*({_NAME})\s*\.", upper):
                    program, section, fd, record = match.group(1), None, None, None
                    programs[program].append(refs)
                if re.match(r"^END\s+PROGRAM\b", upper):
                    program, section, fd, record = None, None, None, None
                if match := re.match(rf"^({_NAME})\s+SECTION\b", upper):
                    section, fd, record = match.group(1), None, None
                if upper.startswith("PROCEDURE DIVISION"):
                    section, fd, record = "PROCEDURE", None, None
                common = {"program_name": program, "relative_path": path}
                if match := re.fullmatch(rf"SELECT\s+(?:OPTIONAL\s+)?({_NAME})\s+ASSIGN\s+(?:TO\s+)?"
                                        rf"(?:(DISK|DATABASE|DYNAMIC)\s+)?({_LITERAL}|{_NAME})(?:\s+[^.]*)?\s*\.", upper):
                    modifier, assigned = match.group(2), match.group(3).strip("'\"")
                    item = observe("file_declaration", refs, **common, file_name=match.group(1),
                                   assigned_name=None if modifier == "DYNAMIC" or assigned in {"DISK", "DATABASE", "DYNAMIC"} else assigned,
                                   assignment_modifier=modifier, external_identity_verified=False)
                    if modifier == "DYNAMIC" or item["assigned_name"] is None:
                        item["dynamic_assignment_symbol"] = assigned
                        boundary("dynamic_or_incomplete_file_assignment", **common, **refs)
                    declarations.append((scope, item))
                if match := re.match(rf"^FD\s+({_NAME})(?:\s|\.)", upper):
                    fd, record = ((match.group(1), refs) if sentence_terminated(upper) else None), None
                    if fd is None:
                        boundary("incomplete_file_description", **common, **refs)
                elif re.match(r"^SD\b", upper):
                    fd, record = None, None
                if match := re.match(rf"^(\d\d?)\s+({_NAME})\b", upper):
                    if not sentence_terminated(upper):
                        record = None
                        boundary("incomplete_data_declaration", **common, **refs)
                        continue
                    level, name = int(match.group(1)), match.group(2)
                    if level < 50 and name != "FILLER":
                        field_counts[(scope, program, name)] += 1
                    if level == 1:
                        record = {"record_name": name, "fields": [], "refs": refs, "scope": scope,
                                  "program_name": program, "relative_path": path,
                                  "file_name": fd[0] if fd else None, "fd_refs": fd[1] if fd else refs}
                        records.append(record)
                    elif record and 1 < level < 50 and name != "FILLER":
                        record["fields"].append({"field_name": name, **refs})
                    elif level in {66, 77}:
                        record = None
                if match := re.fullmatch(rf"COPY\s+({_NAME})\s*\.", upper):
                    copies.append((scope, match.group(1), fd, record, refs, common))
                elif upper.startswith("COPY "):
                    boundary("copy_form_not_supported", **common, **refs)
                if match := re.match(rf"^(READ|START|WRITE|REWRITE|DELETE)\s+({_NAME})(?=\s|\.|$)", upper):
                    operation = match.group(1)
                    item = observe("io_operation", refs, **common, operation=operation,
                                   operand=match.group(2), operand_kind="record" if operation in {"WRITE", "REWRITE"} else "file",
                                   access_role="direct_write" if operation in {"WRITE", "REWRITE", "DELETE"} else "read_only_dependency")
                    operations.append((scope, item))
                if re.match(r"^(MOVE|COMPUTE|ADD|SUBTRACT|MULTIPLY|DIVIDE)\b", upper):
                    if not complete:
                        observe("assignment_candidate", refs, **common, status="candidate",
                                visible_identifiers=_unique_identifiers(upper)[:8], reason="statement_continuation_not_supplied")
                        boundary("statement_continuation_not_supplied", **common, **refs)
                        continue
                    tail = plain.split(None, 1)[1] if " " in plain else ""
                    if (re.search(r"\b(?:CORRESPONDING|CORR|REMAINDER|ON|END-|IF|ELSE)\b", plain)
                            or re.search(r"\b(?:OF|IN)\b|[()]", plain)
                            or re.search(r"\b(?:MOVE|COMPUTE|ADD|SUBTRACT|MULTIPLY|DIVIDE|CALL|PERFORM|READ|WRITE|REWRITE|DELETE|DISPLAY)\b", tail)):
                        boundary("assignment_form_not_supported", **common, **refs)
                        continue
                    reads, writes, _ = _extract_data_access(upper)
                    # GIVING writes only its result; the arithmetic input is not modified.
                    if " GIVING " in upper:
                        before, after = upper.split(" GIVING ", 1)
                        reads, writes = _unique_identifiers(before), _unique_identifiers(after)
                    if writes:
                        item = observe("field_assignment", refs, **common, read_fields=reads, written_fields=writes)
                        assignments.append((scope, item))
                if match := re.match(rf"^PERFORM\s+({_NAME})(?:\s+(?:THRU|THROUGH)\s+({_NAME}))?", upper):
                    if match.group(1) not in {"UNTIL", "VARYING", "WITH", "TIMES", "TEST", "FOREVER"}:
                        observe("dependency_reference", refs, **common, dependency_type="PERFORM", target_name=match.group(1),
                                range_end=match.group(2), effect_status="candidate_not_execution_proof")
                if match := re.match(rf"^CALL\s+({_LITERAL}|{_NAME})", upper):
                    target = match.group(1)
                    literal = target.startswith(("'", '"'))
                    observe("dependency_reference", refs, **common, dependency_type="CALL" if literal else "DYNAMIC_CALL",
                            target_name=target.strip("'\""), effect_status="candidate_not_execution_proof")
            if block[0][0] == 1 and len(blocks) == 1 and member_complete:
                members[PurePosixPath(path).stem.upper()].append((scope, path))

    records_by_scope = defaultdict(list)
    for record in records:
        records_by_scope[record["scope"]].append(record)
    for scope, name, fd, host_record, refs, common in copies:
        candidates = members.get(name, [])
        copy_records = records_by_scope.get(candidates[0][0], []) if len(candidates) == 1 else []
        if not fd or len(candidates) != 1 or len(copy_records) != 1:
            boundary("copy_record_membership_unresolved", target_name=name, **common, **refs)
            continue
        copied = copy_records[0]
        if host_record:
            boundary("copy_subordinate_layout_not_supported", target_name=name, **common, **refs)
            continue
        bound = {**copied, "scope": scope, "relative_path": common["relative_path"],
                        "program_name": common["program_name"], "file_name": fd[0],
                        "fd_refs": _join_refs(fd[1], refs), "copy_member": name}
        records.append(bound)
        field_counts[(scope, common["program_name"], bound["record_name"])] += 1
        for field in bound["fields"]:
            field_counts[(scope, common["program_name"], field["field_name"])] += 1

    declaration_index, assigned_index = defaultdict(list), defaultdict(list)
    record_index, file_index, field_index = defaultdict(list), defaultdict(list), defaultdict(list)
    record_files = defaultdict(set)
    for scope, declaration in declarations:
        declaration_index[(scope, declaration["program_name"], declaration["file_name"])].append(declaration)
        assigned_index[declaration["assigned_name"]].append(declaration)
    for record in records:
        prefix = (record["scope"], record["program_name"])
        record_index[(*prefix, record["record_name"])].append(record)
        file_index[(*prefix, record["file_name"])].append(record)
        if record["file_name"]:
            record_files[(*prefix, record["record_name"])].add(record["file_name"])
        for field in record["fields"]:
            field_index[(*prefix, field["field_name"])].append((record, field))
    for record in records:
        if record["file_name"]:
            if len(observations) >= max_observations:
                truncated = True
                boundary("record_binding_observation_limit", omitted_records_at_least=1)
                break
            visible_fields = record["fields"][:8]
            if len(record["fields"]) > 8:
                truncated = True
            observe("record_binding", _join_refs(record["fd_refs"], record["refs"], *visible_fields),
                    file_name=record["file_name"], record_name=record["record_name"],
                    fields=[field["field_name"] for field in visible_fields],
                    observed_field_count=len(record["fields"]), fields_truncated=len(record["fields"]) > 8,
                    program_name=record["program_name"], relative_path=record["relative_path"],
                    field_list_complete=False, **({"copy_member": record["copy_member"]} if "copy_member" in record else {}))
    visible_items = {id(item) for item in observations}
    for scope, item in operations:
        if id(item) not in visible_items:
            continue
        key = (scope, item["program_name"], item["operand"])
        local = (record_index if item["operand_kind"] == "record" else file_index).get(key, [])
        names = record_files.get(key, set())
        file_name = item["operand"] if item["operand_kind"] == "file" else next(iter(names)) if len(names) == 1 else None
        item["file_name"] = file_name
        limited_records = local[:8]
        visible_fields = [field for record in limited_records for field in record["fields"][:8]][:8]
        item["record_names"] = list(dict.fromkeys(record["record_name"] for record in limited_records))
        item["record_candidate_count"] = len(local)
        item["fields"] = list(dict.fromkeys(field["field_name"] for field in visible_fields))
        item["fields_truncated"] = len(local) > 8 or any(len(record["fields"]) > 8 for record in limited_records)
        truncated = truncated or item["fields_truncated"]
        item["file_binding_status"] = "confirmed_source_syntax" if file_name and len(local) == 1 else "candidate"
        # The FD/record evidence is needed even when SELECT was not supplied.
        item.update(_join_refs(item, *(record["fd_refs"] for record in limited_records),
                               *(record["refs"] for record in limited_records), *visible_fields))
        matched = declaration_index.get((scope, item["program_name"], file_name), [])
        if len(matched) == 1:
            item["assigned_name"] = matched[0]["assigned_name"]
            item.update(_join_refs(item, matched[0]))
        else:
            boundary("file_declaration_not_supplied_or_ambiguous", operand=item["operand"], **{key: item[key] for key in ("relative_path", "evidence_ids", "source_locations")})
        if item["operand_kind"] == "record" and not file_name:
            boundary("record_file_binding_not_supplied_or_ambiguous", record_name=item["operand"], **{key: item[key] for key in ("relative_path", "evidence_ids", "source_locations")})
        item["external_identity_verified"] = False
    memberships_left = max_observations
    for scope, item in assignments:
        if id(item) not in visible_items:
            continue
        item["record_memberships"], count = [], 0
        for name in item["written_fields"][:8]:
            key = (scope, item["program_name"], name)
            matches = field_index.get(key, [])
            count += len(matches)
            limit = min(8 - len(item["record_memberships"]), memberships_left)
            for record, field in matches[:limit]:
                ambiguous = field_counts.get(key, 0) != 1
                item["record_memberships"].append({"field_name": name, "record_name": record["record_name"],
                    "file_name": record["file_name"], "status": "candidate" if ambiguous else "confirmed_source_syntax",
                    "declaration_candidate_count": field_counts.get(key, 0),
                    **_join_refs(record["fd_refs"], record["refs"], field)})
                memberships_left -= 1
        if count > len(item["record_memberships"]):
            truncated = True
            item["memberships_truncated"] = True
            boundary("field_membership_limit", candidate_count=count, supplied_count=len(item["record_memberships"]),
                     relative_path=item["relative_path"], evidence_ids=item["evidence_ids"][:8])
    for item in observations:
        if item["kind"] == "dependency_reference" and item["dependency_type"] in {"CALL", "DYNAMIC_CALL"}:
            candidates = programs.get(item["target_name"], []) if item["dependency_type"] == "CALL" else []
            item["target_source_status"] = ("dynamic_target_unresolved" if item["dependency_type"] == "DYNAMIC_CALL"
                                           else "supplied_static_candidate" if len(candidates) == 1
                                           else "ambiguous_supplied_target" if candidates else "target_source_not_supplied")
            if len(candidates) == 1:
                item["target_evidence_ids"] = candidates[0]["evidence_ids"]
        if item["kind"] == "dds_pfile":
            hints = assigned_index.get(PurePosixPath(item["relative_path"]).stem.upper(), [])
            item["file_binding_candidates"] = [{"status": "candidate", "file_name": hint["file_name"],
                "assigned_name": hint["assigned_name"], "program_name": hint["program_name"],
                "basis": "matching source path stem only; system identity not verified", **_join_refs(item, hint)} for hint in hints[:8]]
            if len(hints) > 8:
                truncated = True
                boundary("dds_binding_candidate_limit", candidate_count=len(hints))
    # Keep derived metadata bounded independently of the supplied source size.
    fields_left, refs_left = max_observations * 2, max_observations * 4
    def bound_refs(item, allowance):
        nonlocal refs_left, truncated
        original = len(item["evidence_ids"]) + len(item["source_locations"])
        identifiers = item["evidence_ids"][:min(8, max(1, allowance // 2))]
        locations = item["source_locations"][:min(12, max(0, allowance - len(identifiers)))]
        item["evidence_ids"], item["source_locations"] = identifiers, locations
        refs_left -= len(identifiers) + len(locations)
        if len(identifiers) + len(locations) < original:
            truncated = True
            item["provenance_truncated"] = True
            if "file_binding_status" in item:
                item["file_binding_status"] = "candidate"
            if item.get("kind") == "record_binding" or "declaration_candidate_count" in item:
                item["status"] = "candidate"
    for index, item in enumerate(observations):
        allowance = max(2, refs_left - (len(observations) - index - 1) * 2)
        bound_refs(item, allowance)
        for key in ("written_fields", "read_fields", "fields", "record_names", "visible_identifiers"):
            if key in item:
                before = len(item[key])
                item[key] = item[key][:min(8, fields_left)]
                fields_left -= len(item[key])
                if len(item[key]) < before:
                    truncated = True
                    item[key + "_truncated"] = True
    for item in observations:
        for key in ("record_memberships", "file_binding_candidates"):
            kept = []
            for detail in item.get(key, []):
                if refs_left < 2:
                    truncated = True
                    item[key + "_truncated"] = True
                    continue
                bound_refs(detail, refs_left)
                kept.append(detail)
            if key in item:
                item[key] = kept
    if truncated:
        boundary("file_impact_budget_reached")
    result = {"type": "file_impact", "scope": {"source_scope": "supplied_complete_physical_lines",
            "runtime_paths_verified": False, "lf_pf_identity_verified": False,
            "field_lineage_complete": False}, "observations": observations, "boundaries": boundaries, "truncated": truncated}
    omitted = 0
    while len(json.dumps(result, ensure_ascii=False).encode("utf8")) > max_metadata_bytes:
        result["truncated"] = True
        if observations:
            omitted += 1
            observations.pop()
        elif boundaries:
            boundaries.pop()
        else:
            break
    if omitted:
        # Leave room for the explicit byte-budget frontier itself.
        notice = {"reason": "file_impact_metadata_byte_limit", "omitted_observations_at_least": omitted}
        boundaries.append(notice)
        while len(json.dumps(result, ensure_ascii=False).encode("utf8")) > max_metadata_bytes:
            if observations:
                observations.pop()
                notice["omitted_observations_at_least"] += 1
            elif len(boundaries) > 1:
                boundaries.pop(0)
            else:
                break
    return result
