"""Conservative, local review of how supplied business relationships are mentioned.

This is not a semantic grader. Recognized wording can establish only that an
aspect was mentioned; unfamiliar prose is unknown, not an omission. A retry
hint is limited to an identifiable purpose-only sentence or a names-only
inventory that omits relationships actually available in the request.
"""

import re


_GROUPS = ("purpose_candidates", "entries", "programs", "relationships", "data_roles", "visible_rules")
_CALL_RELATIONS = {"requests_program_call", "submits_call", "requests_documented_framework_operation"}
_DATA_RELATIONS = {"reads_record_from", "positions_file_cursor", "writes_record_to", "rewrites_record_in",
                   "deletes_record_from", "reads_sql_table", "requests_sql_table_change",
                   "dds_record_declares_pfile", "requests_file_override"}
_REFERENCE = re.compile(r"\[(?:ev[_:-]|ref[_:-]|fw:)[^\]\n]+\]")
_HANDOFF = re.compile(
    r"调用|調用|呼叫|驱动|驅動|启动|啟動|提交|传入|传递|传出|傳入|傳遞|傳出|"
    r"回传|回傳|返回|交给|交給|接收|交接|提供给|提供給|→|->|"
    r"\b(?:call\w*|invok\w*|dispatch\w*|submit\w*|pass\w*|return\w*|hand\w*|receiv\w*|feed\w*)\b", re.I)
_CALLER = re.compile(r"调用方|調用方|呼叫方|呼叫端|调用者|調用者|作业|作業|入口|"
                     r"\b(?:caller|calling\s+program|job|entry|orchestrat\w*)\b", re.I)
_DATA_ACTION = re.compile(
    r"读取|讀取|读入|讀入|取出|查询|查詢|提供|输入|輸入|来源|來源|来自|來自|"
    r"写出|写入|寫出|寫入|输出|輸出|产出|產出|承接|保存|更新|删除|刪除|覆盖|覆蓋|指向|→|->|"
    r"\b(?:quer\w*|lookup|look\s+up|read\w*|input|source|provid\w*|suppl\w*|output|writ\w*|"
    r"stor\w*|feed\w*|consum\w*|updat\w*|delet\w*|overrid\w*|assign\w*)\b", re.I)
_DATA_OBJECT = re.compile(r"文件|数据表|資料表|余额表|餘額表|记录|記錄|输入参数|輸入參數|输出参数|輸出參數|"
                          r"\b(?:files?|tables?|database|records?|input\s+parameters?|output\s+parameters?)\b", re.I)
_PURPOSE = re.compile(r"(?:是(?:一[个個]?)?|主要)?(?:用[于以]|旨在|帮助|幫助|帮|幫|负责|負責|服务于|服務於|整理|整合)|"
                      r"\b(?:helps?|prepares?|organizes?|organises?|serves?|is\s+(?:used\s+for|responsible\s+for))\b", re.I)
_NON_PURPOSE_PROCESS = re.compile(
    r"先|然后|然後|随后|隨後|之后|之後|否则|否則|如果|若|当.{0,16}时|當.{0,16}時|"
    r"转换|轉換|转成|轉成|生成|产生|產生|传|傳|返回|写|寫|读取|讀取|取出|从|從|→|->|"
    r"\b(?:then|after|before|otherwise|if|when|from|into|becomes?|transform\w*|generat\w*|return\w*)\b", re.I)
_INVENTORY_LABELS = re.compile(
    r"程序|程式|名称|名稱|清单|清單|文件|数据表|資料表|对象|對象|入口|如下|包括|相关|相關|和|与|與|及|有|是|"
    r"\b(?:programs?|names?|inventory|files?|tables?|objects?|entries|entry|list|and|are|is|includes?)\b", re.I)


def _rows(orientation, group):
    values = orientation.get(group, ())
    if not isinstance(values, (list, tuple)):
        return []
    return [row for row in values[:128] if isinstance(row, dict)
            and isinstance(row.get("supplied_reference_ids"), (list, tuple))
            and any(isinstance(ref, str) and ref for ref in row["supplied_reference_ids"])]


def _names(row):
    names = []
    for key in ("name", "program_name", "target_name", "relative_path", "source_path", "file_name",
                "assigned_name", "table_name", "record_name"):
        name = row.get(key)
        if isinstance(name, str) and 0 < len(name) <= 1024:
            names.append(name)
    return list(dict.fromkeys(names))


def _contains(text, name):
    return bool(re.search(r"(?<![A-Za-z0-9_$#@-])" + re.escape(name)
                          + r"(?![A-Za-z0-9_$#@-])", text, re.I))


def _any_name(text, names):
    return any(_contains(text, name) for name in names)


def _inventory_only(text, names):
    residual = text
    for name in sorted(names, key=len, reverse=True):
        residual = re.sub(r"(?<![A-Za-z0-9_$#@-])" + re.escape(name)
                          + r"(?![A-Za-z0-9_$#@-])", "", residual, flags=re.I)
    residual = _INVENTORY_LABELS.sub("", residual)
    return bool(text.strip()) and not re.search(r"[^\W\d_]", residual, re.UNICODE)


def _purpose_only(text, program_names):
    """Recognize a narrow synopsis, not all short answers or unknown vocabulary."""
    clauses = [part.strip() for part in re.split(r"[。！？.!?;；\n]+", text) if part.strip()]
    if len(clauses) != 1 or len(clauses[0]) > 200 or not _PURPOSE.search(clauses[0]):
        return False
    return (_any_name(clauses[0], program_names) and not _NON_PURPOSE_PROCESS.search(clauses[0])
            and not _CALLER.search(clauses[0]) and not _DATA_OBJECT.search(clauses[0]))


def _requirements(orientation):
    facts = {group: _rows(orientation, group) for group in _GROUPS}
    result = {"business_purpose": [], "program_handoffs": [], "data_flow": []}

    def add(aspect, relation, row, *, left=(), right=()):
        refs = [ref for ref in row.get("supplied_reference_ids", ()) if isinstance(ref, str) and ref][:16]
        item = {"relation": relation, "left_names": list(left), "right_names": list(right),
                "supplied_reference_ids": refs}
        if row.get("handoff_id"):
            item["handoff_id"] = row["handoff_id"]
        if item not in result[aspect] and len(result[aspect]) < 48:
            result[aspect].append(item)

    for row in facts["purpose_candidates"]:
        add("business_purpose", "documented_purpose", row)
    retained = {row.get("orientation_id") for values in facts.values() for row in values
                if isinstance(row.get("orientation_id"), str)}
    edges = []
    for row in _rows(orientation, "handoffs"):
        ids = row.get("source_fact_ids")
        if not isinstance(ids, (list, tuple)) or not ids or not all(isinstance(x, str) and x in retained for x in ids):
            continue
        edges.append(row)
        relation = row.get("relation")
        aspect = "program_handoffs" if relation in _CALL_RELATIONS else "data_flow" if relation in _DATA_RELATIONS else None
        if aspect and isinstance(row.get("from"), dict) and isinstance(row.get("to"), dict):
            add(aspect, relation, row, left=_names(row["from"]), right=_names(row["to"]))
    # Older orientation versions contain no edges. Only explicit observations
    # nominate relationships; coincident program and file names never do.
    if not edges:
        for row in facts["relationships"]:
            if (row.get("kind") == "program_call" and row.get("target_binding") == "literal_request"
                    or row.get("command") == "CALL") and isinstance(row.get("target_name"), str):
                add("program_handoffs", "explicit_call", row,
                    left=[name for name in (row.get("program_name"), row.get("relative_path")) if isinstance(name, str)],
                    right=[row["target_name"]])
        for row in facts["data_roles"]:
            if row.get("kind") in {"io_operation", "sql_table_access"}:
                add("data_flow", row["kind"], row,
                    left=[row["program_name"]] if isinstance(row.get("program_name"), str) else [],
                    right=[name for name in _names(row) if name not in {row.get("program_name"), row.get("relative_path")}])
    # USING proves a parameter boundary even when no caller implementation is
    # supplied. It does not establish which interface parameters are outputs.
    for row in facts["entries"]:
        if row.get("kind") == "program_entry" and re.search(r"\bUSING\b", str(row.get("statement", "")), re.I):
            add("program_handoffs", "parameter_boundary", row, right=_names(row))
    return result, facts


def review_business_explanation(answer, orientation):
    """Return separate mention/omission hints without grading free-language meaning.

    Callers should only use ``obvious_missing_aspects`` for an existing bounded
    revision opportunity. ``mentioned_unverified`` is not a quality pass and
    ``not_assessable`` must never cause a retry by itself.
    """
    orientation = orientation if isinstance(orientation, dict) else {}
    answer = answer if isinstance(answer, str) else ""
    limited = len(answer) > 65536
    text = _REFERENCE.sub("", answer[:65536])
    text = re.sub(r"\[([^\]\n]+)\]\([^\)\n]+\)", r"\1", text)
    text = re.sub(r"(?m)^\s*#{1,6}\s+.*$", "", text)
    requirements, facts = _requirements(orientation)
    all_names = list(dict.fromkeys(name for rows in facts.values() for row in rows for name in _names(row)))
    programs = list(dict.fromkeys(name for row in facts["programs"] for name in _names(row)))
    inventory = _inventory_only(text, all_names)
    synopsis = _purpose_only(text.replace("`", ""), programs)
    clauses = [part for part in re.split(r"[。！？\n;；]+|(?<=[.!?])\s+", text) if part.strip()]
    results = {}
    for aspect, candidates in requirements.items():
        items = []
        for item in candidates:
            mentioned = False
            for clause in clauses:
                if aspect == "business_purpose":
                    mentioned = bool(_PURPOSE.search(clause)) and not inventory
                elif aspect == "program_handoffs":
                    named_pair = (_any_name(clause, item["left_names"])
                                  and _any_name(clause, item["right_names"]))
                    caller_boundary = _CALLER.search(clause) and (
                        _any_name(clause, item["right_names"]) or len(facts["programs"]) == 1)
                    mentioned = bool(_HANDOFF.search(clause) and (named_pair or caller_boundary))
                else:
                    mentioned = bool(_DATA_ACTION.search(clause) and (
                        _any_name(clause, item["right_names"]) or _DATA_OBJECT.search(clause)))
                if mentioned:
                    break
            # No conclusions about omitted prose are made after clipping or
            # from a truncated projection, nor from an unfamiliar paraphrase.
            obvious = (not mentioned and (inventory or synopsis or not text.strip()) and not limited
                       and not orientation.get("truncated"))
            items.append({**item, "status": "mentioned_unverified" if mentioned else
                          "obvious_omission" if obvious else "not_assessable"})
        states = {item["status"] for item in items}
        status = ("no_supplied_basis" if not items else "obvious_omission" if "obvious_omission" in states else
                  "mentioned_unverified" if states == {"mentioned_unverified"} else "not_assessable")
        results[aspect] = {"status": status, "items": items}
    missing = [aspect for aspect, result in results.items() if result["status"] == "obvious_omission"]
    return {"method": "source_bound_relationship_mentions_v1", "semantic_verification": False,
            "aspects": results, "obvious_missing_aspects": missing, "revision_recommended": bool(missing),
            "recognized_purpose_only": synopsis, "recognized_names_only": inventory,
            "answer_review_truncated": limited,
            "limitation": "Mention signals do not verify direction, conditions, correctness, completeness or readability."}
