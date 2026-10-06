"""Question-specific synthesis guidance and unverified answer/source links."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from business_behavior import build_behavior_guide
from file_impact_evidence import is_file_impact_question


_REFERENCE = re.compile(r"\[((?:ev[_:-]|fw:)[^\]\r\n]{1,160})\]")
_BRIEF = re.compile(r"(?:简短|簡短|简洁|簡潔|简要|簡要)(?:地)?\s*"
                    r"(?:回答|答复|答覆|说明|說明|解释|解釋|一点|一點|些|即可|就好|[。.!?？]?\s*$)|"
                    r"(?:只要|仅要|僅要|只给|只給|仅给|僅給)\s*(?:结论|結論)|"
                    r"(?:不要|不用|无需|無需)(?:展开|展開)|"
                    r"(?:一|两|兩|三)句话|(?:一|兩|两|三)句話|"
                    r"\b(?:keep (?:it|the answer) (?:brief|concise|short)|(?:please )?be brief|"
                    r"(?:answer|explain|respond) briefly|briefly (?:answer|explain|describe|summarize)|"
                    r"(?:brief|concise|short) (?:answer|response|explanation)|"
                    r"in (?:one|two|three|a single) sentences?)\b", re.I)
_LIMITATION = re.compile(
    r"(?:不足以|无法|無法|不能|未能|难以|難以|尚不能).{0,24}"
    r"(?:列出|列举|列舉|枚举|枚舉|罗列|羅列).{0,48}(?:字段|欄位|栏位|文件|\b(?:LF|PF|fields?|files?)\b)|"
    r"不足以.{0,12}(?:判断|判斷|确认|確認|解释|解釋|确定|確定|结论|結論|回答|分析|说明|說明|给出|給出)|"
    r"(?:无法|無法|不能|未能|难以|難以|尚未).{0,24}"
    r"(?:判断|判斷|确认|確認|解释|解釋|确定|確定|结论|結論|回答)|"
    r"(?:资料|資料|证据|證據|源码|源碼|代码|代碼|信息).{0,8}(?:不足|不够|不夠|不全)|"
    r"(?:补齐|補齊|补充|補充|提供).{0,16}(?:源码|源碼|代码|代碼)|"
    r"(?:找不到|找不着|找不著|(?:未|没|沒|没有|沒有|尚未|暂未|暫未)(?:能)?"
    r"(?:找到|查到|定位到|检索到|檢索到)).{0,40}"
    r"(?:源码|源碼|代码|代碼|原文|公式|算式|(?:计算|計算)(?:口径|口徑|规则|規則|逻辑|邏輯)|"
    r"(?:输入|輸入|赋值|賦值)(?:来源|來源))|"
    r"\b(?:insufficient (?:source|information|evidence|material|context|to (?:determine|confirm|explain|answer))|"
    r"(?:source|information|evidence|material|context) (?:is |are )?insufficient|"
    r"(?:(?:can|could|did|have)\s+not|can['’]t|(?:could|did|have)n['’]t|cannot|unable to)\s+"
    r"(?:yet\s+)?(?:find|locate).{0,48}"
    r"(?:source(?:\s+code)?|code|formulas?|calculation (?:rules?|basis|logic)|input sources?)|"
    r"(?:no|not any)\s+(?:(?:relevant|matching|complete)\s+)?"
    r"(?:source(?:\s+code)?|code|formulas?|calculation (?:rules?|basis|logic)|input sources?)"
    r".{0,48}(?:found|located|available)|"
    r"(?:cannot|can['’]t|unable to)\s+(?:reliably\s+|yet\s+)?(?:list|enumerate|identify)\b"
    r".{0,64}\b(?:LF|PF|fields?|files?)\b|"
    r"insufficient\s+to\s+(?:reliably\s+)?(?:list|enumerate|identify)\b.{0,64}\b(?:LF|PF|fields?|files?)\b|"
    r"cannot (?:yet )?(?:determine|confirm|explain|answer)|"
    r"unable to (?:determine|confirm|explain|answer)|need more source|provide source)\b", re.I)
_INVESTIGATION_STATEMENT = re.compile(
    r"^(?:我们|我們|我)?\s*(?:目前|当前|當前|现在|現在)?\s*(?:还|還|仍|尚)?\s*"
    r"(?:需要|需|必须|必須|有待|待|先|要|已经|已經|已)\s*(?:先|进一步|進一步)?\s*"
    r"(?:查找|补读|補讀|补查|補查|读取|讀取|核对|核對|确认|確認|检查|檢查|分析)|"
    r"^(?:(?:I|we)\s+)?(?:(?:still |first |already )?(?:need to|must|have to|have|had)|first)\s+"
    r"(?:find|search|read|locate|inspect|check|verify|review|checked|reviewed)\b", re.I)
_REPORTED_BEHAVIOR = re.compile(
    r"(?:程序|系统|系統|接口|函数|函數).{0,8}(?:返回|输出|輸出|显示|顯示|报告|報告)|"
    r"(?:返回|输出|輸出|显示|顯示|提示|记录|記錄|写入|寫入)\s*[「『“\"']|"
    r"\b(?:returns?|prints?|outputs?|displays?|reports?|writes?)\s+[\"'“]", re.I)
_FOLLOWING_EXPLANATION = re.compile(
    r"(?:再|并|並|然后|然後|接着|接著).{0,16}(?:计算|計算|乘|除|加|减|減|归零|歸零|赋值|賦值|设置|設置|返回)|"
    r"(?:时|時)(?:会|會|将|將|则|則|直接)?(?:拒绝|拒絕|返回|归零|歸零|计算|計算)|"
    r"\b(?:and|then)\s+(?:\w+\s+){0,3}(?:multiply|divide|add|subtract|calculate|compute|set|return)\b", re.I)
_CONDITIONAL_STATEMENT = re.compile(r"^(?:若|如果|当(?!前)|當(?!前)|只要|除非|一旦|(?:if|when|unless)\b)", re.I)
_EXPLICIT_DEFERRAL = re.compile(
    r"^(?:我|我们|我們).{0,120}(?:需要|必须|必須|待).{0,240}(?:才能|才可|再)(?:回答|解释|解釋|确认|確認)|"
    r"^(?:I|we)\s+(?:(?:still|first)\s+)?(?:need|must|have to)\b.{0,480}"
    r"\bbefore\s+(?:(?:I|we)\s+can\s+)?(?:answer|explain|confirm)\b", re.I)
_PENDING_INVESTIGATION = re.compile(
    r"(?:^|[。！？]|[.!?]\s+)[ \t]*(?:[-*]\s+)?(?:\*\*)?"
    r"(?:(?:接下来|接下來|下一步)[，, ]*)?"
    r"(?:(?:我们|我們|我)\s*(?:会|會|将|將|打算|计划|計劃|准备|準備)\s*"
    r"(?:再|先|继续|繼續|进一步|進一步)*\s*"
    r"(?:核对|核對|补查|補查|补读|補讀|查找|查阅|查閱|读取|讀取|检查|檢查|"
    r"追踪|追蹤|调查|調查|确认|確認|分析)|"
    r"(?:I|we)(?:\s+will|['’]ll)\s+(?:(?:now|next|first)\s+)?"
    r"(?:continue\s+(?:to\s+)?)?"
    r"(?:check(?:ing)?|read(?:ing)?|inspect(?:ing)?|investigate|investigating|"
    r"review(?:ing)?|verify|verifying|search(?:ing)?|trace|tracing)\b)", re.I | re.M)


def assess_answer_completion(answer):
    """Detect a reply made only of investigation or limitation statements.

    This is a conservative text check, not semantic verification. A concrete
    explanation alongside an uncertainty remains available as a partial answer.
    Citations and headings alone cannot turn a deferred answer into an analysis.
    """
    text = _REFERENCE.sub("", str(answer))
    text = re.sub(r"(?m)^\s*#{1,6}\s+.*$", "", text)
    # An explicit promise is unfinished work even when it follows a useful
    # partial explanation. Preserve that explanation; nominate a bounded
    # investigation instead of treating the promise as a completed answer.
    # Quoted examples and code are data, not commitments by the analyst.
    commitment_text = re.sub(r"```[^\n]*\n.*?(?:```|\Z)|~~~[^\n]*\n.*?(?:~~~|\Z)",
                             "", text, flags=re.S)
    commitment_text = re.sub(r"(?m)^\s*>.*$|`[^`\n]*`", "", commitment_text)
    commitment_text = re.sub(r'''“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|"[^"\n]*"|(?<!\w)'[^'\n]*' ''',
                             "", commitment_text, flags=re.X)
    pending_investigation = bool(_PENDING_INVESTIGATION.search(commitment_text))
    clauses = re.split(r"[\n。！？；;，,:：]+|(?<=[.!?])\s+|\b(?:but|however)\b", text, flags=re.I)
    deferred = substantive = limitation = False
    for clause in clauses:
        clause = re.sub(r"^[\s*#>\-:：]+|[\s*。.!?]+$", "", clause)
        clause = re.sub(r"^(?:但是|但|因此|所以|不过|不過|然而|而且)\s*", "", clause)
        if not clause or clause.casefold() in {"结论", "結論", "说明", "說明", "分析结果", "分析結果", "answer", "conclusion"}:
            continue
        if _EXPLICIT_DEFERRAL.search(clause):
            deferred = True
        elif (_REPORTED_BEHAVIOR.search(clause) or _FOLLOWING_EXPLANATION.search(clause)
              or _CONDITIONAL_STATEMENT.search(clause)):
            substantive = True
        elif _LIMITATION.search(clause):
            deferred = limitation = True
        elif _INVESTIGATION_STATEMENT.search(clause):
            deferred = True
        else:
            substantive = True
    incomplete = pending_investigation or deferred and not substantive
    return {"status": "incomplete" if incomplete else "not_assessed",
            "reason": "investigation_promised_without_completion" if pending_investigation else
                      "investigation_without_business_answer" if incomplete else None,
            "pending_investigation": pending_investigation,
            "limitation_detected": limitation,
            "method": "bounded_text_check", "semantic_verification": "unverified"}


def wants_business_detail(question, *, answer_detail="detailed"):
    """Business answers are developed by default; explicit brevity wins."""
    if answer_detail not in {"brief", "detailed"}:
        raise ValueError("answer_detail must be brief or detailed.")
    preference = re.sub(r"(?:不要|不用|无需|無需|别|別)(?:太)?(?:简短|簡短|简洁|簡潔|简要|簡要)", "", str(question))
    preference = re.sub(r"\b(?:do not|don't|don’t)\s+(?:be|keep (?:it|the answer))\s+"
                        r"(?:brief|concise|short)\b", "", preference, flags=re.I)
    return answer_detail != "brief" and not _BRIEF.search(preference)


_FORMULA_QUESTION = re.compile(
    r"计算|計算|公式|算式|怎么算|怎麼算|如何算|算出|\b(?:calculation|calculate[ds]?|calculating|formula|computed?)\b", re.I)
_EXPLICIT_DETAIL_REQUEST = re.compile(
    r"详细|詳細|详尽|詳盡|细致|細緻|完整|逐步|流程|\b(?:detailed|thorough|step.by.step|workflow)\b", re.I)
_INPUT_QUESTION = re.compile(
    r"初值|初始值|预设值|預設值|(?:输入|輸入|参数|參數).{0,12}(?:来源|來源|来自|來自|哪里|哪裡|何处|何處)|"
    r"\b(?:initial|default) values?\b|\b(?:input|parameter).{0,24}(?:source|origin|from)|\bwhere.{0,24}(?:input|parameter)\b", re.I)
_CONDITION_QUESTION = re.compile(r"条件|條件|何时|何時|什么时候|什麼時候|\b(?:conditions?|when)\b", re.I)
_ALTERNATIVE_QUESTION = re.compile(
    r"不满足|不滿足|否则|否則|归零|歸零|清零|例外|分支|\b(?:otherwise|else|exceptions?|branches?)\b", re.I)
_ANSWER_SIGNALS = {
    "file_io": re.compile(r"写入|寫入|更新|扣减|扣減|读取|讀取|只读|唯讀|删除|刪除|"
                          r"\b(?:write|writes|rewrite|update[ds]?|read[ -]?only|read[sn]?|delete[ds]?)\b", re.I),
    "formula": re.compile(
        r"[\w)]\s*[=＝×÷*/+]\s*[\w(]|\w\s+-\s+\w|乘|除|加|减|減|之和|之差|倍|等于|等於|"
        r"取值|取自|赋值|賦值|\b(?:multiply|multiplied|multiplying|multiplication|times|scaled?|"
        r"divide[ds]?|division|add(?:ed|ing)?|plus|sum|subtract(?:ed|ing)?|minus|difference|"
        r"equals?|product|double[ds]?|twice|COMPUTE|GIVING)\b", re.I),
    "inputs": re.compile(
        r"初值|初始|预设|預設|固定值|来自|來自|读取|讀取|接收|传入|傳入|赋值|賦值|设为|設為|设置|設置|"
        r"从.{0,24}(?:取|开始)|從.{0,24}(?:取|開始)|"
        r"\b(?:VALUE|MOVE|LINKAGE|READ|USING|from|initialized?|initially|loaded?|received?|passed?)\b", re.I),
    "conditions": re.compile(
        r"大于|大於|小于|小於|(?:等于|等於).{0,24}(?:时|時|则|則)|"
        r"(?:为|為)\s*(?:[+-]?\d+(?:\.\d+)?|零|[A-Z][A-Z0-9_-]*)\s*(?:时|時|则|則)|"
        r"超过|超過|超出|低于|低於|达到|達到|不足|正数|正數|正值|非正|[<>≤≥]|若|如果|当.{0,24}时|當.{0,24}時|"
        r"仅|僅|才|\b(?:if|when|unless|positive|negative|greater|less|exceeds?|insufficient)\b", re.I),
    "result_adjustments": re.compile(
        r"否则|否則|不满足|不滿足|其他情况|其他情況|归零|歸零|清零|置零|为零|為零|"
        r"(?:为|為|设为|設為|[=＝])\s*0(?![\d.])|保留|保持|改写|改寫|改为|改為|覆盖|覆蓋|"
        r"再加|再减|再減|调整|調整|四舍五入|四捨五入|舍入|捨入|截断|截斷|"
        r"\b(?:else|otherwise|zero|unchanged|round(?:ed|ing)?|adjust(?:ed|s)?|truncate[ds]?|"
        r"retain(?:ed|s)?|preserv(?:e[ds]?|ing))\b", re.I),
}
_ASPECT_LABELS = {"formula": "具体算式或运算关系", "inputs": "输入的初值、读取或传入来源",
                  "conditions": "当前处理适用的条件", "result_adjustments": "其他分支或结果调整",
                  "file_io": "原文已知的文件/record写入、只读依赖、准确字段名及定位"}

# An unfamiliar business paraphrase is not evidence of an omission. Only
# affirmative, vocabulary-limited boilerplate nominates a workflow revision;
# useful short explanations and domain terms remain available without a retry.
_GENERIC_BEHAVIOR_WORDS = re.compile(
    r"这个|這個|当前|當前|首先|然后|然後|接着|接著|最后|最後|"
    r"程序|系统|系統|流程|业务|業務|相关|相關|具体|具體|相应|相應|"
    r"根据|根據|按照|依据|依據|通过|通過|进行|進行|完成|执行|執行|"
    r"判断|判斷|校验|校驗|检查|檢查|验证|驗證|处理|處理|计算|計算|更新|"
    r"生成|输出|輸出|返回|条件|條件|规则|規則|配置|参数|參數|资格|資格|"
    r"数据|數據|信息|输入|輸入|记录|記錄|状态|狀態|结果|結果|"
    r"是否|满足|滿足|符合|要求|该|該|本|先|再|并|並|且|和|与|與|及|的|了|会|會|将|將|对|對|按|后|後|时|時|来|來|做|有|关|關|等|地|它|这|這|是|在|所|作|出|以|用|于|於|"
    r"\b(?:the|a|an|this|that|it|system|program|process|workflow|business|"
    r"first|then|finally|and|or|to|of|for|in|on|with|according|based|"
    r"validat\w*|check\w*|process\w*|calculat\w*|updat\w*|generat\w*|"
    r"return\w*|output\w*|perform\w*|condition\w*|rule\w*|configuration|"
    r"parameter\w*|eligibility|input\w*|data|information|record\w*|status|result\w*)\b", re.I)


def _generic_behavior_summary(text):
    if not re.search(r"校验|校驗|判断|判斷|处理|處理|计算|計算|更新|生成|"
                     r"\b(?:validat\w*|check\w*|process\w*|calculat\w*|updat\w*)\b", text, re.I):
        return False
    return not re.search(r"\w", _GENERIC_BEHAVIOR_WORDS.sub("", text))


def _behavior_requirements(guide):
    """Bind omission hints to observed syntax, never a universal answer template."""
    observations = guide.get("observations", [])
    requirements = []
    for kind, source_kinds, description in (
            ("behavior_conditions", {"conditions"}, "解释原文中的具体适用或分支条件，不只说按规则判断"),
            ("behavior_outcomes", {"file_io", "state_assignment"}, "解释已知处理怎样改变业务记录、状态或输出"),
            ("behavior_exits", {"early_exit"}, "解释条件分支中的退出及其影响范围，不推定整批停止或已提交")):
        references = list(dict.fromkeys(identifier for item in observations if item["kind"] in source_kinds
            and (item["kind"] != "file_io" or item.get("verb") in {"WRITE", "REWRITE", "DELETE"})
            for identifier in item["supplied_reference_ids"]))
        if references:
            requirements.append({"kind": kind, "description": description,
                                 "supplied_reference_ids": references[:8]})
    return requirements

_SOURCE_VERBS = re.compile(
    r"(?<![A-Z0-9_$#@-])(?:END-IF|END-EVALUATE|EVALUATE|WHEN|ELSE|IF|COMPUTE|"
    r"MOVE|ADD|SUBTRACT|MULTIPLY|DIVIDE|DISPLAY|CALL|PERFORM|GOBACK|STOP|"
    r"CONTINUE|READ|WRITE|REWRITE|SET|INITIALIZE|ACCEPT)(?![A-Z0-9_$#@-])", re.I)
_ARITHMETIC_VERBS = {"COMPUTE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE"}
_ROUNDING_ANSWER = re.compile(r"四舍五入|四捨五入|舍入|捨入|圆整|圓整|\bround(?:ed|ing)?\b", re.I)
_OVERRIDE_ANSWER = re.compile(
    r"改为|改為|改写|改寫|覆盖|覆蓋|重设|重設|归零|歸零|清零|置零|重新(?:计算|計算|赋值|賦值)|"
    r"随后|隨後|之后|之後|最后|最後|再(?:加|减|減|乘|除)|"
    r"\b(?:overrid(?:e[sn]?|den|ing)|replac(?:e[ds]?|ing)|later|then|finally|reset|recomput\w*)\b", re.I)


def _supplied_source_spans(source_pages, supplied):
    """Join only adjacent/identical overlapping physical excerpts of one version."""
    spans = []
    for page in sorted((page for page in source_pages
                        if page.get("evidence_id") in supplied and page.get("source_text")),
                       key=lambda page: (page.get("relative_path") or "", page.get("source_sha256") or "",
                                         page.get("start_line") or 0, page.get("end_line") or 0)):
        text_lines = page["source_text"].splitlines()
        start, end = page.get("start_line"), page.get("end_line")
        physical = isinstance(start, int) and isinstance(end, int) and end - start + 1 == len(text_lines)
        lines = dict(enumerate(text_lines, start)) if physical else {}
        key = (page.get("relative_path"), page.get("source_sha256"))
        previous = spans[-1] if spans else None
        if (physical and previous and previous["lines"] and previous["key"] == key
                and start <= max(previous["lines"]) + 1
                and all(number not in previous["lines"] or previous["lines"][number] == text
                        for number, text in lines.items())):
            previous["lines"].update(lines)
            previous["source_text"] = "\n".join(previous["lines"][number] for number in sorted(previous["lines"]))
            previous["supplied_reference_ids"].append(page["evidence_id"])
        else:
            spans.append({"key": key, "lines": lines, "source_text": page["source_text"],
                          "supplied_reference_ids": [page["evidence_id"]]})
    return spans


def _number_marker_covered(marker, text):
    """Normalize literal spellings only; no arithmetic expression is evaluated."""
    try:
        expected = Decimal(marker)
    except InvalidOperation:
        return False
    for number in re.findall(r"(?<![A-Z0-9_.])[+-]?\d+(?:\.\d+)?(?![A-Z0-9_.])", text, re.I):
        if Decimal(number) == expected:
            return True
    digits = "零一二三四五六七八九"

    def chinese_integer(value):
        if value < 10:
            return digits[value]
        for unit, label in ((1000, "千"), (100, "百"), (10, "十")):
            if value >= unit:
                head, remainder = divmod(value, unit)
                prefix = "" if unit == 10 and head == 1 else digits[head]
                suffix = "零" if remainder and unit >= 100 and remainder < unit // 10 else ""
                return prefix + label + suffix + (chinese_integer(remainder) if remainder else "")
        return ""

    # Common Chinese number spellings should not turn a correct short answer
    # into an omission. Larger/other spellings remain outside this text hint.
    absolute = abs(expected)
    if absolute < 10000:
        whole = int(absolute)
        fraction = format(absolute, "f").partition(".")[2].rstrip("0")
        number = chinese_integer(whole) + ("点" + "".join(digits[int(char)] for char in fraction) if fraction else "")
        number = ("负" if expected < 0 else "") + number
        return number in text or number.replace("负", "負").replace("点", "點") in text
    return False


def _supplied_calculation_hints(question, investigation, source_pages):
    """Nominate visible control/assignment syntax, never executed value flow.

    An indexed result-adjustment count includes the original formula itself.
    Inspect only its actually supplied pages and target writes instead, so an
    unconditional formula does not acquire an invented exception requirement.
    """
    from structural_index import _extract_data_access, _unique_identifiers, normalize_cobol_lines

    supplied = {identifier for item in investigation.get("required_items", [])
                if item.get("kind") in {"formula", "business_steps", "conditions", "result_adjustments"}
                and item.get("status") == "SATISFIED"
                for identifier in item.get("evidence_ids", [])}
    question_fields = set(re.findall(r"[A-Z][A-Z0-9_$#@-]*", str(question).upper()))
    conditions, adjustments, hints = False, False, []
    seen, scopes = set(), []
    for span in _supplied_source_spans(source_pages, supplied):
        lines, _ = normalize_cobol_lines(span["source_text"])
        source = "\n".join(line.text for line in lines)
        # Preserve offsets while keeping quoted DISPLAY text out of syntax.
        masked = re.sub(r"'([^']|'')*'|\"([^\"]|\"\")*\"",
                        lambda match: " " * len(match[0]), source)
        events = list(_SOURCE_VERBS.finditer(masked))
        stack, writes, unknown_branch = [], [], False
        for index, event in enumerate(events):
            verb = event[0].upper()
            end = events[index + 1].start() if index + 1 < len(events) else len(source)
            statement = source[event.start():end].strip()
            if verb in {"IF", "EVALUATE"}:
                stack.append([verb, index, 0])
            elif verb in {"ELSE", "WHEN"}:
                expected = "IF" if verb == "ELSE" else "EVALUATE"
                if stack and stack[-1][0] == expected:
                    stack[-1][2] += 1
                else:
                    unknown_branch = True
            elif verb in {"END-IF", "END-EVALUATE"}:
                expected = "IF" if verb == "END-IF" else "EVALUATE"
                if stack and stack[-1][0] == expected:
                    stack.pop()
                else:
                    unknown_branch = False
            elif verb in _ARITHMETIC_VERBS | {"MOVE"}:
                reads, targets, metadata = _extract_data_access(statement)
                syntax = masked[event.start():end]
                if verb in _ARITHMETIC_VERBS and re.search(r"\bGIVING\b", syntax, re.I):
                    giving = re.split(r"\bGIVING\b", syntax, flags=re.I)[1]
                    targets = _unique_identifiers(re.split(r"\b(?:ON|NOT|END-\w+)\b", giving)[0])
                    reads = [field for field in reads if field not in targets]
                if targets:
                    writes.append({"verb": verb, "targets": targets, "reads": reads, "statement": statement,
                                   "syntax": syntax, "unknown_branch": unknown_branch,
                                   "expression": metadata.get("expression", ""), "index": index,
                                   "branch": tuple((row[1], row[2]) for row in stack)})
            # A COBOL sentence terminator also closes implicit IF scopes.
            if re.search(r"(?<!\d)\.(?:\s|$)|(?<=\d)\.(?:\s|$)", masked[event.start():end]):
                stack = []
                unknown_branch = False
        scopes.append((span, writes))
    all_targets = {field for _, writes in scopes for row in writes for field in row["targets"]}
    arithmetic_targets = {field for _, writes in scopes for row in writes
                          if row["verb"] in _ARITHMETIC_VERBS for field in row["targets"]}
    arithmetic_inputs = {field for _, writes in scopes for row in writes
                         if row["verb"] in _ARITHMETIC_VERBS for field in row["reads"]}
    matched = (arithmetic_targets.intersection(question_fields) or
               all_targets.intersection(question_fields).difference(arithmetic_inputs))
    # A program-level question can supply many intermediate counters. Their
    # independent writes do not establish which one is the requested result.
    override_targets = matched or (arithmetic_targets if len(arithmetic_targets) == 1 else set())
    for span, writes in scopes:
        arithmetic = [row for row in writes if row["verb"] in _ARITHMETIC_VERBS]
        targets = ({field for row in writes for field in row["targets"]}.intersection(matched) if matched
                   else {field for row in arithmetic or writes for field in row["targets"]})
        for target in sorted(targets):
            relevant = [row for row in writes if target in row["targets"]]
            primary = next((row for row in relevant if row["verb"] in _ARITHMETIC_VERBS),
                           relevant[0] if relevant else None)
            if primary is None:
                continue
            conditions |= any(row["branch"] or row["unknown_branch"] for row in relevant)
            for row in relevant:
                row_hints = []
                if row["verb"] in _ARITHMETIC_VERBS and re.search(r"\bROUNDED\b", row["syntax"], re.I):
                    row_hints.append({"kind": "rounding", "target_field": target,
                                      "source_statement": row["statement"][:300],
                                      "supplied_reference_ids": span["supplied_reference_ids"][:8]})
                if row["index"] > primary["index"]:
                    primary_branch, branch = dict(primary["branch"]), dict(row["branch"])
                    if (row["unknown_branch"] or primary["unknown_branch"] or
                            any(key in branch and branch[key] != value for key, value in primary_branch.items())):
                        adjustments = True
                    elif target in override_targets:  # A later overwrite needs a located result target.
                        row_hints.append({"kind": "result_overrides", "target_field": target,
                                          "source_statement": row["statement"][:300],
                                          "value_markers": re.findall(r"(?<![\w-])[+-]?\d+(?:\.\d+)?(?![\w-])",
                                                                      row["expression"]),
                                          "supplied_reference_ids": span["supplied_reference_ids"][:8]})
                for hint in row_hints:
                    key = (span["key"], target, hint["kind"], row["statement"])
                    if key not in seen:
                        seen.add(key)
                        hints.append(hint)
                        adjustments = True
    return {"conditions": bool(conditions), "result_adjustments": bool(adjustments),
            "coverage_hints": hints[:32]}


def answer_requirements(question, investigation, source_pages, *, answer_detail="detailed", behavior_guide=None):
    """Nominate answer aspects only from question-relevant, supplied candidates.

    These are bounded lexical coverage hints, not a semantic answer score. A
    short answer can cover them; there is deliberately no minimum word count.
    """
    question = str(question)
    calculation = bool(_FORMULA_QUESTION.search(question))
    detail_requested = wants_business_detail(question, answer_detail=answer_detail)
    # Default depth governs the synthesis prompt. Coverage checks remain tied
    # to the question so a concrete short answer is not rejected for omitting
    # supplementary input origins that the user did not ask to enumerate.
    detailed_calculation = calculation and detail_requested and bool(_EXPLICIT_DETAIL_REQUEST.search(question))
    source_hints = (_supplied_calculation_hints(question, investigation, source_pages) if calculation
                    else {"conditions": False, "result_adjustments": False, "coverage_hints": []})
    requested = {"formula": calculation,
                 "file_io": is_file_impact_question(question),
                 "inputs": bool(_INPUT_QUESTION.search(question)) or detailed_calculation,
                 "conditions": detailed_calculation or bool(_CONDITION_QUESTION.search(question)) or source_hints["conditions"],
                 "result_adjustments": detailed_calculation or bool(_ALTERNATIVE_QUESTION.search(question)) or source_hints["result_adjustments"]}
    visible = {page.get("evidence_id") for page in source_pages if page.get("source_text")}
    requirements = []
    for item in investigation.get("required_items", []):
        kind = item.get("kind")
        identifiers = [identifier for identifier in item.get("evidence_ids", []) if identifier in visible]
        if (requested.get(kind) and (item.get("status") == "SATISFIED" or
                                     kind == "file_io" and item.get("status") == "PARTIAL")
                and item.get("candidate_count", 0) and identifiers):
            requirement = {"kind": kind, "description": _ASPECT_LABELS[kind],
                           "supplied_reference_ids": identifiers[:8]}
            if kind == "result_adjustments" and source_hints["coverage_hints"]:
                requirement["coverage_hints"] = source_hints["coverage_hints"]
            if kind == "file_io":
                observations = [row for row in investigation.get("file_impact", {}).get("observations", [])
                    if set(row.get("evidence_ids", [])).issubset(visible)]
                groups = {}
                if re.search(r"文件|记录|記錄|\b(?:LF|PF|files?|records?)\b", question, re.I):
                    groups["file_identifiers"] = list(dict.fromkeys(str(name) for row in observations
                        if row.get("kind") == "io_operation"
                        for name in (row.get("file_name"), row.get("assigned_name"), row.get("operand")) if name))[:32]
                if re.search(r"字段|栏位|欄位|\bfields?\b", question, re.I):
                    groups["field_identifiers"] = list(dict.fromkeys(str(name) for row in observations
                        for name in (row.get("written_fields", []) if row.get("kind") == "field_assignment"
                                     else row.get("fields", []) if row.get("kind") == "io_operation" else [])))[:32]
                requirement["identifier_groups"] = {key: names for key, names in groups.items() if names}
            requirements.append(requirement)
    if not calculation and not is_file_impact_question(question):
        guide = (build_behavior_guide(question, investigation, source_pages)
                 if behavior_guide is None else behavior_guide)
        requirements.extend(_behavior_requirements(guide))
    return requirements


def assess_business_answer(question, answer, investigation, source_pages, *, answer_detail="detailed"):
    """Detect obvious omissions without treating source supply as answer quality."""
    completion = assess_answer_completion(answer)
    requirements = answer_requirements(question, investigation, source_pages, answer_detail=answer_detail)
    text = _REFERENCE.sub("", str(answer))
    text = re.sub(r"\[([^\]\n]+)\]\([^\)\n]+\)", r"\1", text)
    text = re.sub(r"(?m)^\s*#{1,6}\s+.*$", "", text)
    # An uncertainty or a question restated in the reply is not an explanation
    # of a supplied aspect. Preserve substantive clauses for the coverage hints.
    clauses = re.split(r"[\n。！？；;，,:：]+|(?<=[.!?])\s+", text)
    explanation = "\n".join(clause for clause in clauses
        if (_REPORTED_BEHAVIOR.search(clause) or _FOLLOWING_EXPLANATION.search(clause)
            or _CONDITIONAL_STATEMENT.search(clause)
            or not (_LIMITATION.search(clause) or _INVESTIGATION_STATEMENT.search(clause)))
        and not re.search(r"[?？]", clause))
    generic_behavior = _generic_behavior_summary(explanation)
    missing = [item["kind"] for item in requirements
               if (generic_behavior if item["kind"].startswith("behavior_")
                   else not _ANSWER_SIGNALS[item["kind"]].search(explanation))]
    for item in requirements:
        for hint in item.get("coverage_hints", []):
            kind = hint["kind"]
            if kind == "rounding":
                covered = bool(_ROUNDING_ANSWER.search(explanation))
            else:
                markers = hint.get("value_markers", [])
                covered = (all(_number_marker_covered(marker, explanation) for marker in markers)
                           if markers else bool(_OVERRIDE_ANSWER.search(explanation)))
            if not covered and kind not in missing:
                missing.append(kind)
        for group, names in item.get("identifier_groups", {}).items():
            if not any(re.search(r"(?<![A-Z0-9_$#@-])" + re.escape(name) + r"(?![A-Z0-9_$#@-])",
                                 explanation, re.I) for name in names):
                missing.append(group)
    completion.update(required_aspects=[item["kind"] for item in requirements], missing_aspects=missing)
    if missing and completion["status"] != "incomplete":
        completion.update(status="incomplete", reason="supplied_business_aspects_unexplained")
    completion["method"] = "bounded_text_and_coverage_signals"
    return completion


def needs_synthesis_review(question, answer, investigation, *, source_available=False, source_pages=(),
                           answer_detail="detailed", assessment=None):
    """Nominate a bounded review when useful material got a blanket limitation."""
    items = investigation.get("required_items", [])
    supplied = source_available or any(item.get("evidence_ids") for item in items)
    if not supplied:
        return False
    completion = (assess_business_answer(question, answer, investigation, source_pages, answer_detail=answer_detail)
                  if assessment is None else assessment)
    if completion["status"] == "incomplete":
        return True
    # A useful explanation can legitimately bound an unavailable implementation.
    # That known boundary does not call for another synthesis of the same facts.
    if any(item.get("reason") in {"external_implementation_unavailable", "runtime_target_unresolved"}
           for item in items):
        return False
    return completion["limitation_detected"]


def build_analysis_brief(question, investigation, source_pages, framework_references, max_output_tokens,
                         *, answer_detail="detailed"):
    """Bind the synthesis brief to excerpts surviving actual request trimming."""
    visible = {page["evidence_id"] for page in source_pages if page.get("evidence_id")}
    visible.update(row["reference_id"] for row in framework_references if row.get("reference_id"))
    items = []
    for item in investigation.get("required_items", [])[:8]:
        supplied = [identifier for identifier in item.get("evidence_ids", []) if identifier in visible]
        items.append({"kind": item["kind"], "status": item["status"],
                      "reason": item.get("reason"), "supplied_reference_ids": supplied[:8],
                      **({"framework_fact_ids": item["framework_fact_ids"],
                          "framework_reference_ids": item.get("framework_reference_ids", [])}
                         if item.get("framework_fact_ids") else {}),
                      "omitted_supplied_reference_ids": max(0, len(supplied) - 8)})
    gaps = investigation.get("open_gaps", [])
    external = list(dict.fromkeys(target for item in investigation.get("required_items", [])
        if item.get("reason") in {"external_implementation_unavailable", "runtime_target_unresolved"}
        for target in item.get("targets", [])))
    behavior_guide = build_behavior_guide(question, investigation, source_pages)
    return {"detail_requested": wants_business_detail(question, answer_detail=answer_detail),
            "output_budget_tokens": max_output_tokens,
            "required_answer_aspects": answer_requirements(question, investigation, source_pages,
                                                            answer_detail=answer_detail, behavior_guide=behavior_guide),
            **({"behavior_guide": behavior_guide} if behavior_guide.get("observations") else {}),
            "available_source_paths": list(dict.fromkeys(page["relative_path"] for page in source_pages
                                                         if page.get("relative_path")))[:8],
            "supplied_material": items,
            "unavailable_or_runtime_targets": external[:8],
            "remaining_gaps": [{key: gap[key] for key in ("kind", "reason") if key in gap} for gap in gaps[:8]],
            "semantic_execution_verified": False,
            "task": ("先简要回答结论和关键条件，保留必要来源引用。" if not wants_business_detail(
                question, answer_detail=answer_detail) else "按问题需要充分解释，默认不因用户未写‘详细’而缩成概述。")
                + "解释当前系统实际实现的业务行为、处理先后、输入来源、计算口径、适用条件、例外和结果影响，"
                "只展开与问题有关的内容。关键结论逐项附实际来源引用。已有证据的结论直接说明；"
                "没有直接 COMPUTE 或具体数值不妨碍解释已知步骤、条件或符号关系；只限定未知数值或算法。"
                "内部尚可补读的程序、段落、赋值和依赖由调查工具读取，不要求用户补交已入库源码。"
                "缺外部实现或运行时目标只限制依赖它的结论，继续解释调用者已知的输入、条件和返回处理。"
                "framework_facts 是离线绑定到当前调用点的框架规则；可以说明其约定操作，"
                "不把已由该规则解释的公共调用重复列为资料缺失，也不据此推断运行结果或缺失算式。"
                "最终计算按源码顺序串起适用条件、分支、舍入和后续结果覆盖；"
                "不能把中间算式写成无条件的最终结果，后续赋值有影响时明确其优先关系。"
                "将校验、按配置处理、更新状态展开为具体条件、实际采用的值或日期及结果，让用户能判断自己的场景；"
                "业务对象和后果是正文主线，标识和调用列表不能代替解释。不要推测历史设计动机或行业惯例。"
                "behavior_guide 只提示本轮可见的语法位置；结合完整上下文解释具体条件、满足与不满足时的处理、"
                "输出或状态变化，以及跳过或失败后哪些步骤继续、哪些不执行。不能把源码排列顺序当执行顺序，"
                "不能把单笔返回推定为整批停止，或把写入推定为已提交。原文未证明的分支后果须明确限定。"
                "不把必答项状态、检索覆盖或索引数量写成业务结论或完整值流证明。"}


def link_answer_claims(answer, allowed):
    """Record paragraph-level citations; this does not establish entailment."""
    claims = []
    omitted = 0
    fenced = False
    paragraphs, current = [], []
    for line in str(answer).splitlines():
        if re.match(r"\s*(```|~~~)", line):
            fenced = not fenced
        if fenced or re.match(r"\s*(```|~~~|#{1,6}\s)", line):
            continue
        if not line.strip():
            if current:
                paragraphs.append("\n".join(current))
                current = []
        else:
            current.append(line)
    if current:
        paragraphs.append("\n".join(current))
    for paragraph in paragraphs:
        statement = _REFERENCE.sub("", paragraph).strip()
        if not statement:
            continue
        if len(claims) >= 64:
            omitted += 1
            continue
        identifiers = list(dict.fromkeys(identifier for identifier in _REFERENCE.findall(paragraph)
                                         if identifier in allowed))
        support = []
        for identifier in identifiers:
            row = allowed[identifier]
            support.append({"reference_id": identifier, **{key: row[key] for key in
                ("kind", "relative_path", "start_line", "end_line", "source_sha256", "document_sha256", "include_chain")
                if key in row}})
        claims.append({"claim_id": f"answer-paragraph-{len(claims) + 1}",
            "claim_type": "business_explanation", "text": statement[:4000],
            "text_truncated": len(statement) > 4000, "verification": "unverified",
            "evidence_ids": [identifier for identifier in identifiers if allowed[identifier].get("kind") == "source_page"],
            "framework_reference_ids": [identifier for identifier in identifiers if allowed[identifier].get("kind") == "framework_reference"],
            "support": support, "support_interpretation": "cited_excerpt_not_verified_entailment"})
    review = {"scope": "answer_citations", "claim_count": len(claims),
              "cited_claim_count": sum(bool(claim["support"]) for claim in claims),
              "uncited_claim_count": sum(not claim["support"] for claim in claims),
              "omitted_claim_count": omitted, "semantic_verification": "unverified"}
    return claims, review
