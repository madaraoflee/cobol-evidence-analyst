"""Small, source-bound reading prompts for explaining implemented behavior.

The observations describe visible syntax, not executed paths, business intent,
or a verified relationship between statements. They require no model request.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import replace
import re


_NARROW_FACT = re.compile(
    r"字段.{0,12}(?:类型|類型|长度|長度|定义|定義)|"
    r"(?:类型|類型|长度|長度|初值|初始值|默认值|默認值|预设值|預設值)|"
    r"(?:在哪|哪里|哪裡|何处|何處).{0,12}(?:定义|定義|声明|聲明)|"
    r"(?:输入|輸入|参数|參數).{0,12}(?:来源|來源|来自|來自)|"
    r"\b(?:data type|type of|length|initial value|default value|defined|declared|located|"
    r"where\b.{0,30}\b(?:input|parameter)|(?:input|parameter) source)\b", re.I)
_SPECIFIC_BEHAVIOR = re.compile(
    r"流程|处理过程|處理過程|处理步骤|處理步驟|"
    r"(?:为什么|為什麼|为何|為何).{0,40}(?:跳过|跳過|生成|出账|出賬|拒绝|拒絕|返回|失败|失敗|更新|改变|改變)|"
    r"(?:什么|什麼|哪些).{0,12}(?:条件|條件|情况|情況).{0,30}(?:生成|出账|出賬|跳过|跳過|更新|拒绝|拒絕|返回)|"
    r"(?:状态|狀態).{0,12}(?:变化|變化|改变|改變|转移|轉移|决定|決定)|"
    r"\b(?:workflow|processing (?:steps|flow)|state transition|"
    r"why\b.{0,60}\b(?:skip\w*|generat\w*|reject\w*|fail\w*|return\w*|updat\w*)|"
    r"how\b.{0,40}\b(?:process\w*|decid\w*|handl\w*|chang\w*))\b", re.I)
_BEHAVIOR = re.compile(
    r"(?:逻辑|邏輯)(?!\s*(?:标志|標誌|字段|欄位|栏位|变量|變量|类型|類型))|"
    r"业务规则|業務規則|处理规则|處理規則|业务功能|業務功能|业务含义|業務含義|"
    r"(?:如何|怎么|怎麼)(?:处理|處理)|返回(?:标志|標誌|状态|狀態|结果|結果|码|碼|值)|"
    r"(?:生成|出账|出賬|跳过|跳過|拒绝|拒絕).{0,12}(?:条件|條件|规则|規則)|"
    r"(?<![\w-])logic(?![\w-])|\b(?:business rules?|flow|behavior|behaviour|"
    r"return (?:flag|status|code|result))\b", re.I)
_VERBS = re.compile(
    r"(?<![A-Z0-9_$#@-])(?:END-EVALUATE|END-PERFORM|END-IF|EVALUATE|WHEN|"
    r"ELSE|IF|MOVE|SET|INITIALIZE|CALL|PERFORM|READ|WRITE|REWRITE|DELETE|"
    r"START|OPEN|CLOSE|GOBACK|STOP|EXIT|GO|CONTINUE|DISPLAY|ACCEPT|"
    r"COMPUTE|ADD|SUBTRACT|MULTIPLY|DIVIDE|STRING|UNSTRING|INSPECT)"
    r"(?![A-Z0-9_$#@-])", re.I)
_UNIT_BOUNDARY = re.compile(
    r"^\s*(?:[A-Z0-9_$#@-]+\s*(?:SECTION)?\s*\.|"
    r"(?:IDENTIFICATION|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION\b|"
    r"PROGRAM-ID\b|END\s+PROGRAM\b)", re.I)
_QUOTED = re.compile(r"'(?:[^']|'')*(?:'|\Z)|\"(?:[^\"]|\"\")*(?:\"|\Z)")
_SENTENCE_END = re.compile(r"\.(?:\s|$)")
_IO_VERBS = {"READ", "WRITE", "REWRITE", "DELETE", "START", "OPEN", "CLOSE"}
_MAX_OBSERVATIONS = 12
_MAX_STATEMENT = 240
_MAX_SCAN_CHARACTERS = 80000


def wants_behavior_explanation(question):
    """Recognize a behavior question without turning field facts into flows."""
    question = str(question)
    if _SPECIFIC_BEHAVIOR.search(question):
        return True
    if _NARROW_FACT.search(question):
        return False
    return bool(_BEHAVIOR.search(question))


def _statement_observations(source_text):
    """Yield isolated syntax observations, resetting scope at every code unit."""
    from structural_index import normalize_cobol_lines

    # The shared normalizer strips inline comments without inspecting quotes.
    # Shield only literal markers on each physical line before normalization;
    # the two-character marker preserves source columns and line provenance.
    marker_number = 0xE000
    marker = chr(marker_number) * 2
    while marker in source_text:
        marker_number += 1
        marker = chr(marker_number) * 2
    protected = "\n".join(_QUOTED.sub(lambda match: match[0].replace("*>", marker), line)
                           for line in source_text.splitlines())
    lines, _ = normalize_cobol_lines(protected)
    lines = tuple(replace(line, text=line.text.replace(marker, "*>")) for line in lines)
    chunks, current = [], []
    for line in lines:
        # A source excerpt may contain several paragraphs or programs. Their
        # physical order does not establish a call, path, or shared IF scope.
        if _UNIT_BOUNDARY.match(line.text) and not _VERBS.match(line.text.strip()):
            if current:
                chunks.append(current)
                current = []
        else:
            current.append(line)
    if current:
        chunks.append(current)
    for chunk in chunks:
        source = "\n".join(line.text for line in chunk)
        offsets, offset = [], 0
        for line in chunk:
            offsets.append(offset)
            offset += len(line.text) + 1
        masked = _QUOTED.sub(lambda match: " " * len(match[0]), source)
        # Embedded languages need their own parser. Never interpret their
        # identifiers or keywords as COBOL control or file operations.
        masked = re.sub(r"\bEXEC\b.*?(?:\bEND-EXEC\b|\Z)",
                        lambda match: " " * len(match[0]), masked, flags=re.I | re.S)
        events = list(_VERBS.finditer(masked))
        branches = []
        for index, event in enumerate(events):
            verb = event[0].upper()
            end = events[index + 1].start() if index + 1 < len(events) else len(source)
            statement = source[event.start():end].strip()
            syntax = masked[event.start():end]
            kind = None
            if verb in {"IF", "EVALUATE"}:
                branches.append(verb)
                kind = "conditions"
            elif verb in {"WHEN", "ELSE"}:
                kind = "conditions"
            elif verb in {"END-IF", "END-EVALUATE"}:
                expected = "IF" if verb == "END-IF" else "EVALUATE"
                if branches and branches[-1] == expected:
                    branches.pop()
                else:
                    branches = []
            elif verb in _IO_VERBS:
                kind = "file_io"
            elif verb in {"MOVE", "SET", "INITIALIZE"}:
                kind = "state_assignment"
            elif verb == "CALL":
                kind = "call"
            elif verb == "PERFORM":
                kind = ("iteration" if re.search(r"\b(?:UNTIL|VARYING|TIMES|FOREVER)\b", syntax, re.I)
                        else "call")
            elif branches and (verb == "GOBACK" or
                    verb == "STOP" and re.match(r"STOP\s+RUN\b", syntax, re.I) or
                    verb == "EXIT" and re.match(r"EXIT\s+(?:PROGRAM|PARAGRAPH|SECTION|PERFORM)\b", syntax, re.I)):
                kind = "early_exit"
            if kind:
                last_character = event.start() + len(source[event.start():end].rstrip()) - 1
                first_line = chunk[bisect_right(offsets, event.start()) - 1].start_line
                last_line = chunk[bisect_right(offsets, last_character) - 1].end_line
                yield {"kind": kind, "verb": verb,
                       "source_statement": statement,
                       "_start_line": first_line, "_end_line": last_line}
            # Periods close implicit IF scopes too; decimal points do not.
            if _SENTENCE_END.search(syntax):
                branches = []


def build_behavior_guide(question, investigation, source_pages):
    """Nominate bounded reading anchors from supplied workflow evidence only."""
    result = {"observations": [], "semantic_execution_verified": False,
              "interpretation": "visible_syntax_not_execution",
              "omitted_observations": 0, "source_scan_truncated": False}
    if not wants_behavior_explanation(question):
        return result
    items = investigation.get("required_items", [])
    steps = {identifier for item in items
             if item.get("kind") == "business_steps" and item.get("status") in {"SATISFIED", "PARTIAL"}
             for identifier in item.get("evidence_ids", [])}
    visible = {page.get("evidence_id") for page in source_pages if page.get("source_text")}
    if not steps.intersection(visible):
        return result
    supplied = steps | {identifier for item in items
        if item.get("kind") == "conditions" and item.get("status") in {"SATISFIED", "PARTIAL"}
        for identifier in item.get("evidence_ids", [])}
    # Import at call time: synthesis uses this helper when assembling a brief.
    from business_synthesis import _supplied_source_spans

    candidates, seen, counts = [], set(), {}
    pages_by_id = {page.get("evidence_id"): page for page in source_pages}
    scanned = total = 0
    for span in _supplied_source_spans(source_pages, supplied):
        source = span["source_text"]
        span_first_line = min(span["lines"]) if span["lines"] else None
        remaining = _MAX_SCAN_CHARACTERS - scanned
        if len(source) > remaining:
            # Avoid making a cut midway through a statement look complete.
            prefix = source[:max(0, remaining)]
            last_line_end = prefix.rfind("\n")
            source = prefix[:last_line_end] if last_line_end >= 0 else ""
            result["source_scan_truncated"] = True
        scanned += len(source)
        for observation in _statement_observations(source):
            # A prefix can change the meaning of a condition or literal. Omit
            # long statements from this compact guide; their raw pages remain.
            if len(observation["source_statement"]) > _MAX_STATEMENT:
                total += 1
                continue
            key = (span["key"], observation["kind"], observation["source_statement"])
            if key in seen:
                continue
            seen.add(key)
            total += 1
            kind = observation["kind"]
            counts[kind] = counts.get(kind, 0) + 1
            references = span["supplied_reference_ids"]
            if span_first_line is not None:
                first = span_first_line + observation["_start_line"] - 1
                last = span_first_line + observation["_end_line"] - 1
                references = [identifier for identifier in references
                    if pages_by_id[identifier]["start_line"] <= last
                    and pages_by_id[identifier]["end_line"] >= first]
            if not references or len(references) > 8:
                continue
            # Keep a small candidate pool with room for each kind, so many
            # assignments cannot hide the first branch or file operation.
            if counts[kind] <= _MAX_OBSERVATIONS:
                observation["supplied_reference_ids"] = list(dict.fromkeys(references))
                observation["_order"] = total
                candidates.append(observation)
    selected, included = [], set()
    for kind in ("conditions", "early_exit", "file_io", "state_assignment", "call", "iteration"):
        first = next((row for row in candidates if row["kind"] == kind), None)
        if first:
            selected.append(first)
            included.add(first["_order"])
    for row in candidates:
        if len(selected) >= _MAX_OBSERVATIONS:
            break
        if row["_order"] not in included:
            selected.append(row)
            included.add(row["_order"])
    result["observations"] = [{key: value for key, value in row.items() if not key.startswith("_")}
                              for row in sorted(selected, key=lambda row: row["_order"])]
    result["omitted_observations"] = max(0, total - len(selected))
    return result
