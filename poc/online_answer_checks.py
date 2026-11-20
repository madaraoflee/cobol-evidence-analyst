"""Check ordinary online answers against the physical lines in the model's own request."""
import re

from question_intent import business_sequence_requested
from response_language import localized_text
from source_availability import _source_availability_findings
from statement_facts import strip_inline_comment


_DISPLAY_LITERAL = re.compile(r"""DISPLAY\s+(?:'(?:[^']|'')*'|"(?:[^"]|"")*")\s*\.?""", re.I)
_MONEY_PROCESS = re.compile(
    r"扣减|扣減|抵扣|抵减|抵減|冲抵|沖抵|收款|收取|付款|清偿|清償|结算|結算|保费|保費|"
    r"\b(?:deduct\w*|offset\w*|pay(?:ment|ments)?|premiums?|funds|credits?|refunds?|settle\w*)\b", re.I)
_PRESENTATION = re.compile(r"显示|顯示|展示|提示|画面|畫面|屏幕|螢幕|\b(?:display\w*|messages?|screen)\b", re.I)


_ASSERTED_ORDER = re.compile(
    r"先.{1,90}?(?:再|然后|然後|随后|隨後|[，,、]\s*[后後](?!续|續))|优先于|優先於|先于|先於|"
    r"\bfirst\b.{1,140}?\b(?:then|next|followed by)\b|"
    r"\b(?:apply|applied|deduct(?:ed)?|offset|use[ds]?)\b.{1,80}?\bbefore\b", re.I)
_UNCERTAIN = re.compile(
    r"无法|無法|不能|未能|未确认|未確認|尚未|待核|假设|假設|可能|例如|比如|如果|是否|"
    r"并非|並非|不是|没有证据|沒有證據|暂无依据|暫無依據|未获证实|未獲證實|"
    r"\b(?:cannot|can['’]t|could|may|whether|unknown|unconfirmed|unverified|if|example|not|"
    r"no (?:evidence|proof|basis))\b", re.I)
_ORDER_REFERENCE = re.compile(
    r"^\s*(?:(?:这|這|该|該|上述|上面|此)(?:种|種|个|個|一)?(?:扣减|扣減|抵扣|执行|執行)?(?:顺序|順序|次序|说法|說法)|"
    r"(?:this|that|the above)\s+(?:execution\s+)?(?:sequence|order|claim)\b)", re.I)


def _claims_order(answer):
    # A delivery flag describes the answer as a whole. Only an affirmative
    # statement about order can contradict a display-only citation.
    answer = re.sub(r"\[E[0-9a-f]{24}\]", "", answer)
    sentences = [s.strip() for s in re.findall(r"[^。！？；.!?;\n]+[。！？；.!?;]?", answer)]
    for index, sentence in enumerate(sentences):
        if not _ASSERTED_ORDER.search(sentence) or _UNCERTAIN.search(sentence) or sentence.endswith(("?", "？")):
            continue
        following = sentences[index + 1] if index + 1 < len(sentences) else ""
        if _ORDER_REFERENCE.search(following) and _UNCERTAIN.search(following):
            continue
        return True
    return False


def _only_display_literals(page):
    if page["last_line_truncated"] or not page["citation"]["path"].lower().endswith((".cbl", ".cob", ".cobol", ".cpy")):
        return False
    found = False
    for line in page["text"].splitlines():
        if re.match(r"^[0-9 ]{6}[*/]", line):
            continue
        if re.match(r"^\d{6} ", line):
            line = line[7:]
        code = strip_inline_comment(line).strip()
        if code:
            if not _DISPLAY_LITERAL.fullmatch(code):
                return False
            found = True
    return found


def answer_source_issue(question, response, supplied):
    """Return a demonstrable contradiction, never estimate the business outcome."""
    originals = [{"relative_path": p["citation"]["path"], "source_text": p["text"],
                  "evidence_id": p["id"]} for p in supplied]
    answer = response["answer"]
    denials = []
    for finding in _source_availability_findings(answer, originals):
        # The online prompt contains selected pages, not a manifest of every
        # filename. A bare basename cannot identify a file in a subdirectory.
        path = finding["relative_path"]
        subject = answer[finding["_subject_start"]:finding["_subject_end"]]
        if "/" not in path or subject.replace("\\", "/").removeprefix("./") == path:
            denials.append(finding)
    if denials:
        return {"issue": "supplied_source_denied",
                "evidence_ids": list(dict.fromkeys(i for finding in denials for i in finding["evidence_ids"]))[:8],
                "instruction": "The named file has original lines in source_context. Use those lines to explain what is established. Follow unresolved dependencies or state the specific rule still missing."}
    if (_MONEY_PROCESS.search(question) and not _PRESENTATION.search(question)
            and business_sequence_requested(question) and _claims_order(answer)):
        ids = set(response["citations"])
        cited = [p for p in supplied if p["id"] in ids]
        if cited and len(cited) == len(ids) and all(_only_display_literals(p) for p in cited):
            return {"issue": "display_only_sequence", "evidence_ids": [p["id"] for p in cited[:8]],
                    "instruction": "The cited statements only display literal text. Locate the executable deduction, its caller and conditions, and read the original lines. If execution order is unresolved, preserve the specific gap."}
    return None


def retain_checked_location(state, issue, page):
    language = state["answer_requirements"]["answer_language"]
    pending = localized_text(language, "继续核对决定业务结果的处理逻辑。", "Trace the processing logic that determines the business outcome.", "繼續核對決定業務結果的處理邏輯。")
    if issue["issue"] == "display_only_sequence":
        pending = localized_text(language, "查找实际扣减金额的语句和调用顺序。", "Locate the amount deductions and their execution order.", "查找實際扣減金額的語句和調用順序。")
    if not state["answer"]:
        source = page["citation"]
        location = f"`{source['path']}` ({source['start']}–{source['end']})"
        state["answer"] = localized_text(language, f"已核对源码 {location}。", f"Verified source lines at {location}.", f"已核對源碼 {location}。") + f" [{page['id']}] " + pending
        state["answer_citations"] = [page["id"]]
    if pending not in state["remaining"] and len(state["remaining"]) < 32:
        state["remaining"].append(pending)
