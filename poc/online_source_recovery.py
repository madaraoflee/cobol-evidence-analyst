"""Bounded navigation leads for ordinary online questions. Only original pages are evidence."""
from itertools import zip_longest
import re


# Keep question grammar from occupying the small first batch of literal probes.
_QUERY_WORDS = frozenset("""
a an all and are as at be before after by can could describe details do does each explain find for
from how i if in include including is it list me of on or order our place please process rule rules
sequence show source system tell that the their them then these this through to using what when
where which with would you your calculate calculated computation formula conditions applies applied apply used only
""".split())
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*")
_QUOTED = re.compile(r"""[`"'“‘「]([^`"'”’」\r\n]{2,80})[`"'”’」]""")
_FOLLOW_UP = re.compile(r"上轮|上輪|前面|上次|刚才|剛才|上一|这个分支|這個分支|\b(previous|earlier|that branch)\b", re.I)


# These translations supply tentative source spellings, never business rules.
_BUSINESS_TERMS = (
    (("余额", "餘額", "結餘", "结余"), "balance"),
    (("退款", "退费", "退費"), "refund"),
    (("保费", "保費"), "premium"),
    (("暂收", "暫收", "暂挂", "暫掛"), "suspense"),
    (("佣金",), "commission"),
    (("折扣",), "discount"),
    (("应收", "應收"), "receivable"),
    (("利息",), "interest"),
)
_FILE = re.compile(r"(?<![A-Za-z0-9_./\\-])(?:[A-Za-z0-9_$#@-]+/)*[A-Za-z0-9_$#@-]+"
                   r"\.(?:cbl|cob|cobol|cpy|rpg|rpgle|sqlrpgle|clle|sql|jcl)(?![A-Za-z0-9_-])", re.I)
_RESTRICT = re.compile(
    r"(?:只(?:看|读|讀|分析|查)|[仅僅](?:限|看|分析)|限定(?:在)?)\s*[`'\"“‘]?(?:@path@|"
    r"(?:这|這|当前|當前|上轮|上輪|指定)(?:一|个|個|的)?(?:程序|文件|段落)|[A-Za-z0-9_$#@-]+\s*(?:程序|文件|段落))"
    r"|\b(?:only|restricted to|limited to)\s+(?:(?:look|at|read|search|in|within|review|inspect|analy[sz]e|the)\s+)*"
    r"(?:[`'\"]?@path@|(?:this|current|that)\s+(?:file|program|routine)|(?:file|program|routine)\s+[A-Za-z0-9_$#@-]+)"
    r"|@path@[`'\"]?\s+only\b", re.I)
_CALLS = re.compile(r"调[用度]|調[用度]|子程序|子程式|\b(?:calls?|called|callee|subroutine)\b", re.I)


def named_scope(question):
    paths = list(dict.fromkeys(_FILE.findall(question)))[:3]
    restriction = re.sub(r"不要只|不再只|不必只|不用只|\bnot (?:only|limited to)\b", "", question, flags=re.I)
    return paths, bool(_RESTRICT.search(_FILE.sub("@path@", restriction))), bool(_CALLS.search(question))


def question_terms(question, observations):
    # Model-proposed vocabulary from a failed index lookup can supply source-language
    # aliases even when the question has none. A literal probe can match inside names.
    fragments = [_FILE.sub(" ", question)]
    for row in reversed(observations):
        action = row.get("action", {})
        if action.get("tool") == "search" and isinstance(action.get("text"), str):
            fragments.append(action["text"])
            if len(fragments) == 4:
                break
    quoted, identifiers, words = [], [], []
    for fragment in fragments:
        quoted.extend(_QUOTED.findall(fragment))
        for word in _WORD.findall(fragment):
            if len(word) < 3 or word.casefold() in _QUERY_WORDS:
                continue
            target = identifiers if (word.isupper() or "-" in word or "_" in word) else words
            target.append(word)
    unique = {}
    translated = [term for names, term in _BUSINESS_TERMS if any(name in question for name in names)]
    for value in quoted + identifiers + words + translated:
        value = value.strip()
        if 2 <= len(value) <= 80:
            unique.setdefault(value.casefold(), value)
    return list(unique.values())[:6]


def history_reads(question, history):
    if not _FOLLOW_UP.search(question):
        return []
    for message in reversed(history):
        hints = message.get("citation_hints", [])
        if message.get("role") == "assistant" and hints:
            return [{"tool": "read", "path": hint["path"], "start_line": hint["start"],
                     "end_line": hint["end"]} for hint in hints[:4]]
    return []


def follow_locations(query, question, terms, observations, *, page_again):
    # A name in a relation is a lead. Locate it in originals, even when the
    # callee's filename differs from its program name. Never chase a dynamic target.
    paths, restricted, calls_requested = named_scope(question)
    groups = {term.casefold(): [] for term in terms}
    named = [{"path": path, "line": 1} for path in paths]
    targets = {}
    if calls_requested and not restricted:
        for row in observations:
            action, result = row.get("action", {}), row.get("result", {})
            if action.get("tool") != "relations" or action.get("path") not in paths:
                continue
            for relation in result.get("relations", []):
                name = relation.get("target_name")
                if (relation.get("relation_type") == "CALLS" and relation.get("status") == "confirmed"
                        and not relation.get("metadata", {}).get("dynamic") and isinstance(name, str)
                        and re.fullmatch(r"[A-Za-z0-9_#$@-]{1,80}", name)):
                    named.append({"path": action["path"], "line": relation["start_line"]})
                    targets.setdefault(name.casefold(), name)
                    if len(targets) >= 4:
                        break
    groups.update({key: [] for key in targets if key not in groups})
    cursors = {}
    for row in observations:
        action, result = row.get("action", {}), row.get("result", {})
        key = str(action.get("text", "")).casefold()
        if key not in groups or not isinstance(result, dict) or "error" in result:
            continue
        if action.get("tool") == "locate_global" and not restricted:
            groups[key].extend(result.get("matches", []))
            cursors[key] = result.get("next_cursor")
        elif action.get("tool") == "locate" and action.get("path") in paths:
            groups[key].extend({"path": action["path"], "line": line} for line in result.get("matches", []))
    if page_again and not restricted:
        pages = [{"tool": "locate_global", "text": name, "limit": 8} for key, name in targets.items()
                 if key not in cursors]
        pages.extend({"tool": "locate_global", "text": term, "cursor": cursors[term.casefold()], "limit": 8}
                     for term in terms if not groups[term.casefold()] and cursors.get(term.casefold()))
        if pages:
            return "paging", pages[:8]
    # Interleave terms so a frequent literal cannot take every read. Read around
    # the anchor to show adjacent conditions and calls, merging overlapping ranges.
    groups[""] = named
    ranges, line_counts = [], {}
    for row in zip_longest(*groups.values()):
        for hit in row:
            if not isinstance(hit, dict) or type(hit.get("line")) is not int or not isinstance(hit.get("path"), str):
                continue
            path, line = hit["path"], hit["line"]
            if path not in line_counts:
                file = query.reader.file(path)
                line_counts[path] = file["line_count"] if file else 0
            if not 1 <= line <= line_counts[path]:
                continue
            start, end = max(1, line - 8), min(line_counts[path], line + 16)
            for span in ranges:
                if (span["path"] == path and start <= span["end_line"] and end >= span["start_line"]
                        and max(end, span["end_line"]) - min(start, span["start_line"]) < 64):
                    span["start_line"] = min(start, span["start_line"])
                    span["end_line"] = max(end, span["end_line"])
                    break
            else:
                ranges.append({"tool": "read", "path": path, "start_line": start, "end_line": end,
                               "max_characters": 8000})
                if len(ranges) >= 6:
                    return "attempted", ranges
    return "attempted", ranges
