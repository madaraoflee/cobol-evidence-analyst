"""Nominate narrow SQL prose risks against the excerpts actually supplied.

This is a bounded heuristic, not a SQL interpreter or semantic proof. It does
not read files, infer vendor status codes, edit answers, or request a model.
"""

from __future__ import annotations

import hashlib
import json
import re


_MAX_ANSWER = 48000
_MAX_SOURCE = 80000
_MAX_RISKS = 12
_MAX_REVIEW_BYTES = 8000
_MAX_SOURCE_REFS = 3
_MAX_SPAN_TEXT = 512
_CITATION = re.compile(r"\[([A-Za-z][A-Za-z0-9_:.-]*)\](?!\()")
_SQL_SUBJECT = re.compile(r"(?<![A-Z0-9_$#@-])(?:SQL|SQLCODE|SQLSTATE|SELECT|queries|query)"
                          r"(?![A-Z0-9_$#@-])|查询|查詢|查表|查出", re.I)
_LOCK_EFFECT = re.compile(
    r"(?:不(?:会|會|再|需|需要)?|无须|無須|无需|無需)(?:加锁|加鎖|锁定|鎖定|阻塞)"
    r"|(?:不会|不會|绝不|絕不)(?:获取|取得|持有|獲取)(?:任何)?锁"
    r"|(?:必然|一定|保证|保證|总会|總會)(?:[^。；;\n]{0,12})(?:加锁|加鎖|锁定|鎖定|阻塞)"
    r"|\b(?:does?\s+not|will\s+not|never|cannot|can't|won't)\s+"
    r"(?:acquire\s+(?:any\s+)?locks?|lock|block)\b"
    r"|\b(?:lock[- ]free|non[- ]blocking)\b"
    r"|\b(?:always|guaranteed\s+to)\s+(?:locks?|blocks?|acquires?\s+locks?)\b", re.I)
_DB_EVENT = re.compile(
    r"多(?:条|條|笔|筆|个|個)?行|多条记录|多條記錄|查(?:不|未)到|没有(?:查到|匹配记录)|"
    r"沒有(?:查到|匹配記錄)|数据库(?:报错|超时|死锁)|資料庫(?:報錯|逾時|死鎖)|"
    r"\b(?:multiple\s+(?:rows|records)|more\s+than\s+one\s+(?:row|record)|"
    r"no\s+(?:matching\s+)?rows?|not\s+found|database\s+(?:error|timeout|deadlock))\b", re.I)
_BRANCH_MAPPING = re.compile(
    r"(?:全部|一律|都会|都會|会|會|将|將|就|必然|一定)?(?:走|进入|進入|落到|落入|返回)"
    r"[^。；;\n]{0,72}(?:ELSE|分支|响应码|響應碼|返回码|返回碼|['\"`]\w+['\"`])"
    r"|\b(?:go(?:es)?|enter(?:s)?|take(?:s)?|fall(?:s)?|return(?:s)?|route(?:s)?)\b"
    r"[^.;\n]{0,72}(?:\bELSE\b|\bbranch\b|\berror\b|\bfailure\b|\bcode\b|['\"`]\w+['\"`])", re.I)
_QUALIFIER = re.compile(
    r"不能|无法|無法|未能|不(?:能|可|足以|确定|確定|保证|保證|代表|意味着|意味著)|"
    r"并非|並非|不是|未(?:知|确定|確定|证明|證明|保证|保證)|尚未|可能|或许|或許|"
    r"\b(?:cannot|can\s+not|can't|unclear|unknown|unproven|uncertain|"
    r"may|might|could|not\s+(?:necessarily|proven|established|guaranteed)|"
    r"no\s+(?:evidence|proof|guarantee)|does?\s+not\s+(?:imply|mean|establish|prove|guarantee))\b", re.I)
_CONDITION = re.compile(r"如果|假如|假设|假設|若|倘若|\b(?:if|assuming|provided|unless)\b", re.I)
_STATUS_CONDITION = re.compile(
    r"(?<![A-Z0-9_-])(?:SQLCODE|SQLSTATE)(?![A-Z0-9_-])[^。；;\n]{0,28}"
    r"(?:[=<>]|非零|不为零|不為零|等于|等於|不等于|不等於|\b(?:nonzero|non-zero|zero|equals?)\b)", re.I)
_FIELD_DESCRIPTION = re.compile(r"^\s*(?:字段|欄位|栏位|标志|標誌|列|\b(?:field|flag|column)\b)", re.I)
_OTHER_SUBJECT = re.compile(r"其他(?:程序|服务|查詢|查询)|另一(?:程序|服务|查询|查詢)|"
                            r"\b(?:another|other|unrelated)\s+(?:query|program|service)\b", re.I)
_INDIRECT_ROUTE_QUESTION = re.compile(r"是否|能否|\bwhether\b", re.I)
_UNKNOWN_ROUTE = re.compile(r"哪(?:条|條|一条|一條|个|個|种|種)|何种|何種|什么|甚麼|"
                            r"\b(?:which|what)\b", re.I)
_POST_QUALIFIER = re.compile(
    r"(?:仍|尚|还|還|并|並)?未(?:得到|获得|獲得|被)?(?:证实|證實|证明|證明|保证|保證)|"
    r"\b(?:remains?|is)\s+(?:unproven|unverified|uncertain)\b|"
    r"\b(?:has\s+not\s+been|is\s+not)\s+(?:proven|established|guaranteed)\b", re.I)


def _masked_prose(text):
    """Keep offsets while excluding literal quotations and inline identifiers."""
    pattern = r"`[^`\n]*`|“[^”\n]*”|「[^」\n]*」|『[^』\n]*』|\"[^\"\n]*\"|'[^'\n]*'"
    return re.sub(pattern, lambda match: " " * len(match[0]), text)


def _paragraphs(answer):
    fenced, offset = False, 0
    for line in answer.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and not stripped.startswith((">", "#")) and line.strip():
            yield offset, line
        offset += len(line)


def _segments(paragraph):
    # Citations immediately after punctuation belong to that sentence, not
    # the next sentence on the same physical line.
    for match in re.finditer(
            r"[^。！？!?；;\n]+(?:[。！？!?；;](?:\s*\[[A-Za-z][A-Za-z0-9_:.-]*\])*)?",
            paragraph):
        yield match.start(), match[0]


def _qualified(text, position):
    # An earlier qualification in a different clause must not excuse a later
    # categorical assertion after "but" or a comma.
    prefix = re.split(r"[，,；;]|\bbut\b|但是|但", text[:position], flags=re.I)[-1]
    return bool(_QUALIFIER.search(prefix[-140:]))


def _post_qualified(text, position):
    suffix = re.split(r"[，,；;]|\bbut\b|但是|但", text[position:], maxsplit=1, flags=re.I)[0]
    return bool(_POST_QUALIFIER.search(suffix[:140]))


def _source_observations(source_pages):
    from answer_grounding import _eligible_pages, _fragment_units, _sql_output_observations
    from statement_facts import sql_code_only

    pages, characters, truncated = [], 0, False
    for page in _eligible_pages(source_pages):
        if ("repository_orientation" in page.get("selection_reasons", [])
                or not isinstance(page.get("relative_path"), str)):
            continue
        if len(pages) >= 128 or characters + len(page["source_text"]) > _MAX_SOURCE:
            truncated = True
            break
        pages.append(page)
        characters += len(page["source_text"])
    observations, parser_truncated = _sql_output_observations(pages)
    seen = {(row["relative_path"], row["source_sha256"], row["start_line"], row["end_line"])
            for row in observations}
    for page in pages:
        for unit in _fragment_units(page["source_text"], "auto"):
            if unit.name != "EXEC_SQL":
                continue
            code, complete = sql_code_only(unit.normalized_text)
            # Unsupported SQL suffixes such as FOR UPDATE remain SQL reading
            # boundaries. Recognizing a closed SELECT envelope does not prove
            # its locking effect or promote its partial parser status.
            if (not complete or code.split()[:3] != ["EXEC", "SQL", "SELECT"]
                    or not re.search(r"\bEND-EXEC\s*\.?\s*$", code, re.I)
                    or not re.search(r"\bINTO\s+:[A-Z0-9_$#@-]+", code, re.I)):
                continue
            first = page["start_line"] + unit.start_line - 1
            last = page["start_line"] + unit.end_line - 1
            key = page["relative_path"], page["source_sha256"], first, last
            if key in seen:
                continue
            seen.add(key)
            observations.append({"relative_path": key[0], "source_sha256": key[1],
                "start_line": first, "end_line": last, "include_chain": page.get("include_chain", []),
                "supplied_reference_ids": [page["evidence_id"]]})
    return observations, truncated or parser_truncated


def assess_sql_answer_boundaries(answer, source_pages):
    """Return review nominations, never label an assertion proven false.

    Only complete supplied SELECT INTO statements nominate a SQL context.
    Literal quotations, questions, uncertainty, and explicit status-code
    conditions do not by themselves assert a database execution outcome.
    """
    text = str(answer)
    observations, source_truncated = _source_observations(source_pages)
    result = {"method": "bounded_sql_boundary_heuristic", "semantic_verification": False,
              "review_required": False, "risks": [], "source_scan_truncated": source_truncated,
              "answer_scan_truncated": len(text) > _MAX_ANSWER, "omitted_risk_count": 0,
              "report_byte_limit": _MAX_REVIEW_BYTES}
    if not observations:
        return result
    for paragraph_offset, paragraph in _paragraphs(text[:_MAX_ANSWER]):
        if not _SQL_SUBJECT.search(paragraph) or _OTHER_SUBJECT.search(paragraph):
            continue
        citations = set(_CITATION.findall(paragraph))
        matched = [row for row in observations
                   if not citations or citations.intersection(row["supplied_reference_ids"])]
        if not matched:
            continue
        for segment_offset, segment in _segments(paragraph):
            if "?" in segment or "？" in segment:
                continue
            segment_citations = set(_CITATION.findall(segment))
            scoped = [row for row in matched if not segment_citations
                      or segment_citations.intersection(row["supplied_reference_ids"])]
            if not scoped:
                continue
            refs = [{key: row[key] for key in ("relative_path", "source_sha256", "start_line",
                                              "end_line", "supplied_reference_ids")}
                    for row in scoped[:_MAX_SOURCE_REFS]]
            for ref, row in zip(refs, scoped):
                ref["include_chain_sha256"] = hashlib.sha256(json.dumps(
                    row.get("include_chain", []), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            prose = _masked_prose(segment)
            kinds = []
            for match in _LOCK_EFFECT.finditer(prose):
                negated_guarantee = (re.match(r"保证|保證|一定|必然|always|guaranteed", match[0], re.I)
                    and re.search(r"(?:未|不|没有|沒有|\bnot)\s*$", prose[:match.start()], re.I))
                if (_qualified(prose, match.start())
                        or negated_guarantee or _post_qualified(prose, match.end())
                        or _CONDITION.search(prose[:match.start()])
                        or _FIELD_DESCRIPTION.match(prose[match.end():])):
                    continue
                kinds.append(("SQL_RUNTIME_EFFECT_UNSUPPORTED",
                    "SELECT syntax alone does not establish locking or blocking behavior.",
                    "只解释实际 SQL 的取数动作。不要由只读/SELECT 推出不加锁、不阻塞或特定锁效果；"
                    "缺少适用的执行约定时，明确这些运行效果不能从当前源码确定。"))
                break
            events = list(_DB_EVENT.finditer(prose))
            mappings = list(_BRANCH_MAPPING.finditer(segment))
            for event in events:
                if _qualified(prose, event.start()):
                    continue
                mapping = next((item for item in mappings if item.start() >= event.start()), None)
                if mapping is None or _qualified(prose, mapping.start()):
                    continue
                if (_INDIRECT_ROUTE_QUESTION.search(prose[event.end():mapping.start()])
                        or _UNKNOWN_ROUTE.search(mapping[0])):
                    continue
                if (_CONDITION.search(prose[:mapping.start()])
                        and _STATUS_CONDITION.search(prose[:mapping.start()])):
                    continue
                kinds.append(("SQL_EVENT_BRANCH_UNSUPPORTED",
                    "A database event is mapped to a branch without a supplied event-to-status contract.",
                    "区分数据库事件与源码条件。只按实际 SQLCODE/SQLSTATE 条件和赋值解释分支；"
                    "不要替多行、未找到或数据库异常补出返回码，也不要断言其落在某分支之外。"
                    "若没有事件到状态码的明确约定，就限定该事件对应哪条路径尚不能确定。"))
                break
            start = paragraph_offset + segment_offset
            for kind, reason, hint in kinds:
                risk = {"kind": kind, "answer_span": {
                    "start": start, "end": start + len(segment), "text": segment[:_MAX_SPAN_TEXT],
                    "sha256": hashlib.sha256(segment.encode()).hexdigest(),
                    "text_truncated": len(segment) > _MAX_SPAN_TEXT},
                    "source_refs": refs, "omitted_source_refs": max(0, len(scoped) - len(refs)),
                    "reason": reason, "revision_hint": hint, "support_status": "requires_review",
                    "semantic_verification": False}
                result["review_required"] = True
                result["risks"].append(risk)
                if (len(result["risks"]) > _MAX_RISKS
                        or len(json.dumps(result, ensure_ascii=False).encode()) > _MAX_REVIEW_BYTES - 128):
                    result["risks"].pop()
                    result["omitted_risk_count"] += 1
                    result["answer_scan_truncated"] = True
    return result
