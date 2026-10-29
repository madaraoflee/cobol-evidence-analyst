"""Check source-availability assertions against the material of one answer.

Full-file coverage requires a matching versioned manifest and contiguous physical
source ranges. Navigation, expanded COPY text, or an arbitrary excerpt cannot
establish that the complete file was supplied.
"""

from collections import defaultdict
import re

from response_language import localized_text, resolve_answer_language


def _path(value):
    return str(value or "").replace("\\", "/").removeprefix("./")


def supplied_source_availability(source_pages, working_set=None):
    """Rebuild coverage from actual pages; never trust a completeness flag alone."""
    manifest = defaultdict(dict)
    for row in (working_set or {}).get("source_manifest", []):
        path, digest, total = _path(row.get("relative_path")), row.get("sha256"), row.get("line_count")
        if path and digest and type(total) is int and total > 0:
            manifest[path][digest] = total
    supplied = {}
    for page in source_pages:
        path = _path(page.get("relative_path"))
        text = str(page.get("source_text", ""))
        if (not path or not text.strip() or page.get("include_chain")
                or page.get("copy_instance") or page.get("replacement_trace")
                or "repository_orientation" in page.get("selection_reasons", [])):
            continue
        row = supplied.setdefault(path, {"relative_path": path, "source_supplied": True,
            "complete_source_supplied": False, "evidence_ids": [], "versions": defaultdict(list)})
        if page.get("evidence_id"):
            row["evidence_ids"].append(page["evidence_id"])
        first, last, digest = page.get("start_line"), page.get("end_line"), page.get("source_sha256")
        if (digest and not page.get("span_truncated") and type(first) is int and type(last) is int
                and 0 < first <= last and len(text.split("\n")) == last - first + 1):
            row["versions"][digest].append((first, last, page.get("evidence_id")))
    for path, row in supplied.items():
        for digest, total in manifest.get(path, {}).items():
            cursor = 1
            ranges = sorted(row["versions"].get(digest, []), key=lambda span: span[:2])
            for first, last, _ in ranges:
                if first > cursor:
                    break
                cursor = max(cursor, last + 1)
            if cursor > total:
                row["complete_source_supplied"] = True
                row["complete_source_sha256"] = digest
                row["line_count"] = total
                row["supplied_ranges"] = [{"start_line": first, "end_line": last} for first, last, _ in ranges]
                row["complete_evidence_ids"] = list(dict.fromkeys(identifier for _, _, identifier in ranges if identifier))
                break
        row["supplied_versions"] = [
            {"source_sha256": digest,
             "supplied_ranges": [{"start_line": first, "end_line": last} for first, last, _ in ranges],
             "evidence_ids": list(dict.fromkeys(identifier for _, _, identifier in ranges if identifier))}
            for digest, ranges in row.pop("versions").items()]
        row["evidence_ids"] = list(dict.fromkeys(row["evidence_ids"]))
    return supplied


_ABSENCE = re.compile(
    r"(?:没有|沒有|没|沒|尚未|未|未能|不曾)(?:被)?\s*(?:提供|供应|供應|给出|給出|拿到|取得|获得|獲得|读取|讀取)|"
    r"(?:不在|未在|没有在|沒有在).{0,10}(?:提供|供应|供應|上下文)|"
    r"(?:没有|沒有|没|沒|尚无|尚無|缺少|缺失)\s*(?:完整|全文)?(?:源码|源碼|代码|代碼|原文|实现|實現)|"
    r"\b(?:not\s+(?:been\s+)?(?:provided|supplied|available|included|read)|"
    r"(?:has|have)\s+not\s+(?:been\s+)?(?:provided|supplied|read)|unavailable|missing)\b", re.I)
_PARTIAL = re.compile(
    r"(?:只能|仅能|僅能|仅|僅|只).{0,24}(?:看到|看见|看見|拿到|取得|读取|讀取|获得|獲得|有).{0,40}(?:部分|片段|节录|節錄)|"
    r"(?:只|仅|僅)(?:提供|供应|供應)(?:了)?\s*(?:部分|片段|节录|節錄)|"
    r"(?:源码|源碼|代码|代碼|原文).{0,12}(?:不完整|不全)|"
    r"\b(?:only\s+(?:see|have|received|read).{0,40}(?:part|excerpts?|fragments?)|"
    r"only\s+(?:a\s+|some\s+)?(?:part|excerpts?|fragments?).{0,40}|"
    r"(?:source|code|text).{0,24}(?:incomplete|partial))\b", re.I)
_FULL = re.compile(r"完整|全文|全量|全部|\b(?:complete|whole|entire|full)\b", re.I)
_OTHER_MATERIAL = re.compile(
    r"运行|運行|运行值|實際值|实际值|输入|輸入|参数|參數|数据库|資料庫|配置|约定|約定|事务|事務|"
    r"版本|字段|欄位|栏位|"
    r"\b(?:runtime|inputs?|parameters?|values?|database|configuration|transactions?|contract|version|fields?)\b", re.I)
_NEGATED = re.compile(
    r"不是|并非|並非|并不是|並不是|不代表|不意味着|不意味著|不能说|不能說|不准确|不準確|不属实|不屬實|"
    r"\b(?:not\s+(?:true|correct|the case)|incorrect\s+to\s+say|false\s+to\s+say)\b", re.I)
_CONDITIONAL = re.compile(r"^\s*(?:若|如果|假如|假设|假設|当(?!前)|當(?!前)|除非|(?:if|when|unless)\b)", re.I)
_REPORTED = re.compile(r"历史|歷史|上次|之前|曾称|曾稱|示例|例子|提示|日志|日誌|错误消息|錯誤消息|"
    r"\b(?:previous|historical|example|message|log|reported|quoted)\b", re.I)
_CALLEE = re.compile(r"被调用|被調用|被调|被調|调用|調用|外部|下游|"
    r"\b(?:callee|external|calls?|called implementation)\b", re.I)
_CURRENT_SUBJECT = re.compile(
    r"当前|當前|目前|本轮|本輪|本次|(?:这|這|该|該|此|本)(?:个|個|份)?(?:程序|程式|文件|源码|源碼|代码|代碼)|"
    r"\b(?:this|current)\s+(?:program|file|source|code|turn)\b", re.I)
_SOURCE_MATERIAL = re.compile(r"源码|源碼|代码|代碼|原文|全文|\b(?:source|code)\b", re.I)
_SOURCE_EXPLANATION = re.compile(
    r"(?:源码|源碼|代码|代碼|原文)(?:的)?(?:完整|全部|业务|業務)?(?:解读|解讀|解释|解釋|分析|说明|說明)|"
    r"\b(?:explanation|analysis|interpretation)\s+of\s+(?:the\s+)?(?:source|code)\b", re.I)
_BROADER_SCOPE = re.compile(
    r"依赖|依賴|关联|關聯|全库|全庫|仓库|倉庫|项目|項目|系统|系統|其他|其它|所有(?:程序|文件|源码|源碼|代码|代碼)|"
    r"\b(?:dependenc\w*|repository|project|system|other|all\s+(?:files|programs)|copybooks?)\b", re.I)


def _source_availability_findings(answer, source_pages, working_set=None):
    """Return denials contradicted by an identified, actually supplied source.

An unnamed current-source denial requires one explicit root and verified whole
file coverage. A complete root does not establish a complete repository,
dependency implementations, or a complete business explanation.
"""
    available = supplied_source_availability(source_pages, working_set)
    if not available:
        return []
    roots = list(dict.fromkeys(_path(path) for path in (working_set or {}).get("root_paths", []) if path))
    current_root = roots[0] if len(roots) == 1 and available.get(roots[0], {}).get("complete_source_supplied") else None
    # Preserve exact offsets while excluding quoted data from assertions.
    def mask(match):
        return re.sub(r"[^\r\n]", " ", match[0])
    text = re.sub(r"```[^\n]*\n.*?(?:```|\Z)|~~~[^\n]*\n.*?(?:~~~|\Z)", mask, str(answer), flags=re.S)
    text = re.sub(r"(?m)^[ \t]*>.*$", mask, text)
    text = re.sub(r'''“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|"[^"\n]*"|(?<!\w)'[^'\n]*' ''', mask, text, flags=re.X)
    # Inline filenames are common prose, while inline example sentences are
    # quoted data. Retain a lone path token, never an availability assertion.
    text = re.sub(r"`([^`\n]+)`", lambda match: " " + match.group(1) + " " if re.fullmatch(
        r"[A-Z0-9_$#@./\\-]+|[^\s`]+\.[A-Z][A-Z0-9]{0,9}", match.group(1), re.I) else mask(match), text)
    # Citation IDs can follow the previous sentence's punctuation. They identify
    # evidence, not a newly named program that should override the current root.
    citation_ids = {identifier for row in available.values() for identifier in row["evidence_ids"]}
    if citation_ids:
        text = re.sub(r"\[(?:" + "|".join(re.escape(identifier) for identifier in
            sorted(citation_ids, key=len, reverse=True)) + r")\]", mask, text)
    aliases = defaultdict(set)
    known_paths = set(available) | {_path(row.get("relative_path"))
        for row in (working_set or {}).get("source_manifest", []) if row.get("relative_path")}
    for path in known_paths:
        aliases[path.casefold()].add(path)
        aliases[path.rsplit("/", 1)[-1].casefold()].add(path)
    # Resolve unique basenames; do not conflate same-name implementations.
    names = {name: next(iter(paths)) for name, paths in aliases.items() if len(paths) == 1}
    named_source = re.compile(r"(?<![A-Z0-9_$#@./\\-])(" + "|".join(re.escape(name) for name in
        sorted(names, key=len, reverse=True)) + r"|[A-Z0-9_$#@./\\-]+\.[A-Z][A-Z0-9]{0,9}|"
        r"[A-Z][A-Z0-9_$#@]*[-_][A-Z0-9_$#@-]+)(?![A-Z0-9_$#@/\\-]|\.[A-Z0-9])", re.I)
    findings = []
    separators = list(re.finditer(r"[\n。！？；;，,]+|(?<=[.!?])\s+", text))
    spans = zip([0, *(match.end() for match in separators)],
                [*(match.start() for match in separators), len(text)])
    for clause_start, clause_end in spans:
        raw_clause = text[clause_start:clause_end]
        clause = re.sub(r"^[\s*#>\-:：]+", "", raw_clause)
        clause_start += len(raw_clause) - len(clause)
        if (_NEGATED.search(clause) or _CONDITIONAL.search(clause) or _REPORTED.search(clause)):
            continue
        assertion = _ABSENCE.search(clause) or _PARTIAL.search(clause)
        if assertion is None:
            continue
        # Processing source remains source even when its name contains field
        # or parameter; bind the nearest object instead of excluding the clause.
        material = list(_SOURCE_MATERIAL.finditer(clause))
        processing_sources = list(re.finditer(
            r"(?:字段|欄位|栏位|参数|參數).{0,12}(?:源码|源碼|代码|代碼)|"
            r"\b(?:field|parameter)\s+(?:handling|processing)\s+(?:source|code)\b", clause, re.I))
        other = [match for match in _OTHER_MATERIAL.finditer(clause)
                 if not any(source.start() <= match.start() < source.end() for source in processing_sources)]
        nearest_source = min(material, key=lambda match: abs(match.end() - assertion.start()), default=None)
        nearest_other = min(other, key=lambda match: abs(match.end() - assertion.start()), default=None)
        if re.search(r"版本|\bversion\b", clause, re.I):
            continue  # A different version's absence cannot be disproved by this one.
        if (nearest_other and (nearest_source is None
                or abs(nearest_other.end() - assertion.start()) < abs(nearest_source.end() - assertion.start()))):
            continue
        if _SOURCE_EXPLANATION.search(clause):
            continue
        if (re.match(r"\s*(?:时|時|则|則|就|的?情况下|的?情況下|\b(?:when|if|unless)\b)", clause[assertion.end():], re.I)
                or re.search(r"如果|假设|假設|假如|\b(?:if|unless)\b", clause[:assertion.start()], re.I)):
            continue
        matches = [match for match in named_source.finditer(clause)
                   if (names.get(_path(match[1]).casefold()) is not None
                       or not (re.match(r"[ `*]*(?:字段|欄位|栏位|参数|參數|field\b|parameter\b)", clause[match.end():], re.I)
                               or re.search(r"(?:字段|欄位|栏位|参数|參數|field|parameter)[ `*]*$", clause[:match.start()], re.I)))]
        # A shared denial of several named sources cannot be repaired by
        # replacing its nearest filename: that would assert the others exist.
        if len(matches) > 1 and any(re.fullmatch(r"[ `*]*(?:和|及|与|與|、|and|&)[ `*]*",
                clause[left.end():right.start()], re.I) for left, right in zip(matches, matches[1:])):
            continue
        if not matches:
            if (current_root is None or not _CURRENT_SUBJECT.search(clause)
                    or not _SOURCE_MATERIAL.search(clause) or _CALLEE.search(clause)
                    or _BROADER_SCOPE.search(clause) or _SOURCE_EXPLANATION.search(clause)):
                continue
            path = current_root
            full_denial = bool(_PARTIAL.search(clause) or _FULL.search(clause))
        else:
            # Bind a named denial to its nearest filename; never reinterpret an
            # unknown callee or ambiguous basename as the current root.
            nearest = min(matches, key=lambda match: min(abs(match.start() - assertion.start()),
                                                         abs(match.end() - assertion.start())))
            if "|" in clause[nearest.end():assertion.start()]:
                continue  # Separate table cells need their own subject binding.
            name = _path(nearest.group(1)).casefold()
            path = names.get(name)
            if path is None:
                continue
            callee = _CALLEE.search(clause)
            if callee and nearest.end() <= callee.start() < assertion.start():
                continue
            # Answer depth alone is not a source-completeness assertion.
            full_denial = bool(_PARTIAL.search(clause) or _FULL.search(
                clause[max(0, nearest.start() - 16):assertion.end()]))
        row = available.get(path)
        if row is None:
            continue
        if (full_denial or processing_sources) and not row["complete_source_supplied"]:
            continue
        requested_range = re.search(r"(?:第)?(?P<first>\d+)\s*(?:[-–~至到]\s*(?P<last>\d+))?\s*行|"
                                    r"\b(?:lines?|L)\s*(?P<en_first>\d+)(?:\s*[-–~]\s*(?P<en_last>\d+))?",
                                    clause, re.I)
        range_evidence = None
        if requested_range:
            first = int(requested_range["first"] or requested_range["en_first"])
            last = int(requested_range["last"] or requested_range["en_last"] or first)
            for version in row["supplied_versions"]:
                cursor = first
                for span in sorted(version["supplied_ranges"], key=lambda item: item["start_line"]):
                    if span["start_line"] > cursor:
                        break
                    cursor = max(cursor, span["end_line"] + 1)
                if first > 0 and last >= first and cursor > last:
                    range_evidence = version
                    break
            if range_evidence is None:
                continue
        # A filename is enough to identify physical source, even when the
        # sentence omits the word "code" (for example "rule.cbl 未提供").
        findings.append({"_clause_start": clause_start, "_clause_end": clause_end,
            "_assertion_start": clause_start + assertion.start(),
            "_assertion_end": clause_start + assertion.end(),
            "_subject_start": clause_start + (nearest.start() if matches else _CURRENT_SUBJECT.search(clause).start()),
            "relative_path": path,
            "assertion": "complete_source_not_supplied" if full_denial else "source_not_supplied",
            "evidence_ids": row.get("complete_evidence_ids", row["evidence_ids"]),
            **({"requested_range": {"start_line": first, "end_line": last},
                "supplied_versions": [range_evidence]} if range_evidence else
               {"supplied_versions": row["supplied_versions"]} if not row["complete_source_supplied"] else {}),
            **({"source_sha256": row["complete_source_sha256"], "line_count": row["line_count"],
                "supplied_ranges": row["supplied_ranges"]}
               if row["complete_source_supplied"] else {})})
    return findings


def source_availability_contradictions(answer, source_pages, working_set=None):
    """Report source denials contradicted by actual supplied material."""
    return [{key: value for key, value in finding.items() if not key.startswith("_")}
            for finding in _source_availability_findings(answer, source_pages, working_set)]


_REFUSAL_TAIL = re.compile(
    r"^(?:(?:因此|所以|故|从而|從而|因而)?(?:目前|本轮|本輪)?(?:无法|無法|不能)(?:完整|全面|继续|繼續)?"
    r"(?:解答|回答|分析|解读|解讀|说明|說明)|(?:只能|仅能|僅能)(?:做|作|进行|進行)?"
    r"(?:部分|局部|有限)(?:分析|解读|解讀|回答|说明|說明)|"
    r"(?:(?:therefore|so)\s+)?(?:I\s+)?(?:cannot|can't)\s+(?:(?:fully|completely)\s+)?(?:answer|analy[sz]e))"
    r"[ \t.!]*$", re.I)


def repair_source_availability_assertions(answer, source_pages, working_set=None, *, answer_language=None):
    """Repair false source denials locally and preserve independent business prose.

    Returned records retain the original wording and version/range evidence.
    Quotes, conditionals, runtime inputs and unresolved dependencies are untouched.
    """
    text = str(answer)
    language = answer_language or resolve_answer_language(text)
    findings = _source_availability_findings(text, source_pages, working_set)
    repairs = []
    for finding in findings:
        start, end = finding["_clause_start"], finding["_clause_end"]
        subject = min(finding["_subject_start"], finding["_assertion_start"])
        prefix = text[start:subject].strip(" `*")
        if prefix and not re.fullmatch(r"但|但是|不过|不過|然而|因为|因為|because|but|however|the source for", prefix, re.I):
            start = subject
        if start and text[start - 1] == "`":
            start -= 1
        while end > start and text[end - 1] in ".!?":
            end -= 1
        suffix = text[finding["_assertion_end"]:end]
        # The object may follow the absence verb, as in "未提供完整源码".
        suffix = re.sub(r"^[ \t`*]*(?:(?:了|的|本轮|本輪|完整|全部|全文)\s*)*"
                        r"(?:(?:字段|欄位|栏位|参数|參數)(?:的)?(?:处理|處理)?)?"
                        r"(?:源码|源碼|代码|代碼|原文|全文|source|code)?[ \t`*]*", "", suffix, count=1, flags=re.I)
        if suffix and not _REFUSAL_TAIL.fullmatch(suffix):
            end -= len(suffix)
        else:
            tail = re.match(r"[，,][ \t]*([^。！？；;\n]+)", text[end:])
            if tail and _REFUSAL_TAIL.fullmatch(tail[1]):
                end += tail.end()
        path = finding["relative_path"]
        complete = bool(finding.get("source_sha256"))
        replacement = localized_text(language,
            f"`{path}` 的完整源码已提供" if complete else f"`{path}` 的相关源码已提供",
            f"The complete source for `{path}` was supplied" if complete else f"Relevant source for `{path}` was supplied",
            f"`{path}` 的完整源碼已提供" if complete else f"`{path}` 的相關源碼已提供")
        replacement += "".join(f"[{identifier}]" for identifier in finding["evidence_ids"][:2])
        record = {key: value for key, value in finding.items() if not key.startswith("_")}
        record.update(original_text=text[start:end], replacement_text=replacement,
                      start_offset=start, end_offset=end, reason_code="SUPPLIED_SOURCE_DENIAL_CORRECTED")
        repairs.append(record)
    for record in reversed(repairs):
        text = text[:record["start_offset"]] + record["replacement_text"] + text[record["end_offset"]:]
    return text, repairs
