"""Check source-availability assertions against the material of one answer.

Full-file coverage requires a matching versioned manifest and contiguous physical
source ranges. Navigation, expanded COPY text, or an arbitrary excerpt cannot
establish that the complete file was supplied.
"""

from collections import defaultdict
import re


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
        row.pop("versions")
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
_CONDITIONAL = re.compile(r"^\s*(?:若|如果|假如|当(?!前)|當(?!前)|除非|(?:if|when|unless)\b)", re.I)
_REPORTED = re.compile(r"历史|歷史|上次|之前|曾称|曾稱|示例|例子|提示|日志|日誌|错误消息|錯誤消息|"
    r"\b(?:previous|historical|example|message|log|reported|quoted)\b", re.I)
_CALLEE = re.compile(r"被调用|被調用|被调|被調|调用|調用|外部|下游|"
    r"\b(?:callee|external|calls?|called implementation)\b", re.I)


def source_availability_contradictions(answer, source_pages, working_set=None):
    """Return only denials contradicted by a named, actually supplied source.

Unqualified partial-code statements are checked only when they explicitly name
a source. A complete root does not establish a complete repository or complete
dependency implementations.
"""
    available = supplied_source_availability(source_pages, working_set)
    if not available:
        return []
    text = re.sub(r"```[^\n]*\n.*?(?:```|\Z)|~~~[^\n]*\n.*?(?:~~~|\Z)", "", str(answer), flags=re.S)
    text = re.sub(r"(?m)^\s*>.*$", "", text)
    text = re.sub(r'''“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|"[^"\n]*"|(?<!\w)'[^'\n]*' ''', "", text, flags=re.X)
    # Inline filenames are common prose, while inline example sentences are
    # quoted data. Retain a lone path token, never an availability assertion.
    text = re.sub(r"`([^`\n]+)`", lambda match: match.group(1) if re.fullmatch(
        r"[A-Z0-9_$#@./\\-]+|[^\s`]+\.[A-Z][A-Z0-9]{0,9}", match.group(1), re.I) else "", text)
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
    for clause in re.split(r"[\n。！？；;，,]+|(?<=[.!?])\s+", text):
        clause = re.sub(r"^[\s*#>\-:：]+", "", clause)
        if (_NEGATED.search(clause) or _CONDITIONAL.search(clause) or _REPORTED.search(clause)
                or _OTHER_MATERIAL.search(clause)):
            continue
        assertion = _ABSENCE.search(clause) or _PARTIAL.search(clause)
        if assertion is None:
            continue
        if re.match(r"\s*(?:时|時|的?情况下|的?情況下|\b(?:when|if|unless)\b)", clause[assertion.end():], re.I):
            continue
        matches = list(named_source.finditer(clause))
        if not matches:
            continue
        # A sentence can mention a supplied caller and an absent callee. Bind
        # the assertion to the nearest explicit filename rather than any hit.
        nearest = min(matches, key=lambda match: min(abs(match.start() - assertion.start()),
                                                     abs(match.end() - assertion.start())))
        name = _path(nearest.group(1)).casefold()
        path = names.get(name)
        if path is None:
            continue
        callee = _CALLEE.search(clause)
        if callee and nearest.end() <= callee.start() < assertion.start():
            continue
        row = available.get(path)
        if row is None:
            continue
        # "Cannot give a complete answer" describes answer depth, not source
        # completeness. Only qualifiers attached before the denial apply here.
        full_denial = bool(_PARTIAL.search(clause) or _FULL.search(
            clause[max(0, nearest.start() - 16):assertion.end()]))
        if full_denial and not row["complete_source_supplied"]:
            continue
        # A filename is enough to identify physical source, even when the
        # sentence omits the word "code" (for example "rule.cbl 未提供").
        findings.append({"relative_path": path,
            "assertion": "complete_source_not_supplied" if full_denial else "source_not_supplied",
            "evidence_ids": row.get("complete_evidence_ids", row["evidence_ids"]) if full_denial else row["evidence_ids"],
            **({"source_sha256": row["complete_source_sha256"], "line_count": row["line_count"],
                "supplied_ranges": row["supplied_ranges"]}
               if row["complete_source_supplied"] else {})})
    return findings
