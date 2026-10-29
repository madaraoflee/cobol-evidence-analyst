"""Question-specific synthesis guidance and unverified answer/source links."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation

from business_behavior import build_behavior_guide
from business_explanation_cache import cache_business_explanation
from business_explanation_review import review_business_explanation
from file_impact_evidence import is_file_impact_question
from question_intent import calculation_intent
from question_planner import business_concept_requested
from source_availability import source_availability_contradictions
from answer_preferences import current_business_explanation_limited, saved_business_explanation_requested


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
    r"(?:查看|查找|补读|補讀|补查|補查|读取|讀取|核对|核對|确认|確認|检查|檢查|分析)|"
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
# A bare observation that syntax exists is not an explanation of its business
# effect. Keep this narrow: operands, amounts and business nouns do not match.
_SYNTAX_OBSERVATION_ONLY = re.compile(
    r"(?:(?:当前|當前|目前|本轮|本輪)(?:可见|可見|看到|看到的)?|(?:当前|當前|目前)?"
    r"(?:只能|仅能|僅能|仅|僅|只)?(?:看到|可见|可見))"
    r"(?:的)?(?:是|有|只有|仅有|僅有)?(?:一(?:个|個|条|條|些|段))?"
    r"(?:赋值|賦值|状态检查|狀態檢查|条件判断|條件判斷|调用|調用|返回|MOVE|COMPUTE|IF|CALL)"
    r"(?:的)?(?:语句|語句|指令|段落|代码|代碼)?|"
    r"(?:the\s+)?(?:current\s+)?(?:source|code|excerpt)\s+(?:only\s+)?"
    r"(?:contains?|shows?|has)\s+(?:(?:a|an|one|some)\s+)?"
    r"(?:assignments?|status\s+checks?|MOVE|COMPUTE|IF|CALL)(?:\s+(?:statements?|instructions?))?",
    re.I)
_RUNTIME_EVIDENCE_LIMIT = re.compile(
    r"生产当天|生產當天|生产环境实际|生產環境實際|(?:是否|有无|有無)(?:曾经|曾經|已經|已经)?实际(?:执行|執行|运行|運行)|"
    r"实际(?:输入|輸入|返回|传入|傳入)(?:值|金额|金額|结果|結果)|"
    r"日志|日誌|运行记录|運行記錄|\b(?:whether\b.{0,40}\b(?:ran|executed)|actual\s+(?:input|return|runtime)\s+values?|"
    r"production\s+(?:execution|inputs?|values?)|runtime\s+(?:values?|records?)|logs?)\b", re.I)


def _syntax_observation_only(clause):
    text = clause.strip(" *`。.!?").replace("`", "")
    if re.search(r"[\u3400-\u9fff]", text):
        text = re.sub(r"\s+", "", text)
    return bool(_SYNTAX_OBSERVATION_ONLY.fullmatch(text))
_SOURCE_SELECTION_LIMITATION = re.compile(
    r"^(?:(?:现有|現有|当前|當前|本轮|本輪|目前|所给|所給)?"
    r"(?:材料|资料|資料|证据|證據|上下文)(?:中|里|裡)?\s*)?"
    r"(?:(?:目前|当前|當前|本轮|本輪)\s*)?(?:还|還|仍|尚|暂|暫)?"
    r"(?:没有|沒有|没|沒|未)(?:选定|選定|选择|選擇|指定)"
    r"(?:相关|相關|对应|對應|具体|具體|实际|實際|任何)?(?:的)?(?:源代码|源代碼|源码|源碼)|"
    r"^(?:源代码|源代碼|源码|源碼)(?:还|還|仍|尚|暂|暫)?"
    r"(?:没有|沒有|没|沒|未)(?:被)?(?:选定|選定|选择|選擇|指定)|"
    r"^(?:no (?:relevant )?source(?: code)? (?:has been|is) selected|"
    r"(?:the )?(?:current |supplied )?(?:material|context) (?:has|contains) no selected source)", re.I)
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
_SOURCE_READING_NEED = re.compile(
    r"(?:^|[，,])\s*(?:(?:若|如果)?(?:要|想|进一步|進一步)?"
    r"(?:说明|說明|解释|解釋|讲清|講清|分析).{0,160}?[，,]\s*)?"
    r"(?:还|還|仍|尚)?(?:需要|需|必须|必須|有待)\s*(?:先|继续|繼續|进一步|進一步)?"
    r"(?:查看|查阅|查閱|阅读|閱讀|读取|讀取|检查|檢查|核对|核對|分析)|"
    r"^(?:(?:to|in order to)\s+(?:explain|describe|understand)\b.{0,160}?[, ]+)"
    r"(?:(?:I|we)\s+)?(?:(?:still|first)\s+)?(?:need\s+to|must|have\s+to)\s+"
    r"(?:read|inspect|review|check|examine|trace)\b|"
    r"^(?:(?:I|we)\s+)?(?:still\s+)?(?:need\s+to|must|have\s+to)\s+"
    r"(?:read|inspect|review|check|examine|trace)\b", re.I)
_SOURCE_READING_OBJECT = re.compile(
    r"源码|源碼|源代码|源代碼|主流程|主程序|子程序|子程式|实现|實現|调用链|調用鏈|"
    r"\b(?:source(?:\s+code)?|implementation|main\s+(?:flow|program|procedure)|"
    r"subprogram|callee|call\s+chain)\b", re.I)
_OPTIONAL_OR_RUNTIME_READING = re.compile(
    r"如需|(?:如果|若)(?:你|您)|(?:如果|若)需要(?:更多|更详细|更詳細|进一步|進一步)|需要的话|需要的話|"
    r"日志|日誌|运行记录|運行記錄|生产配置|生產配置|实测|實測|"
    r"无需|無需|不用|不必|不需要|"
    r"\b(?:if\s+(?:you|needed)|logs?|runtime\s+(?:records?|configuration)|"
    r"production\s+configuration|no\s+need|(?:do\s+not|don't)\s+need)\b", re.I)


def _unavailable_external_reading(sentence, source_pages, working_set):
    """Bind a local source limit to unavailable dependencies, not unread code."""
    from source_availability import _ABSENCE, _CALLEE, supplied_source_availability

    targets = {str(row.get("target_name", "")).upper() for row in (working_set or {}).get("frontier", ())
               if row.get("reason") == "external_implementation_unavailable" and row.get("target_name")}
    if not targets or not _ABSENCE.search(sentence) or not _CALLEE.search(sentence):
        return False
    if re.search(r"主流程|主程序|主程式|\bmain\s+(?:flow|program|procedure)\b", sentence, re.I):
        return False
    available = supplied_source_availability(source_pages, working_set)
    names = {name for path in available for name in (path, path.rsplit("/", 1)[-1],
                                                   path.rsplit("/", 1)[-1].rsplit(".", 1)[0])}
    for page in source_pages:
        if page.get("relative_path") in available:
            names.update(re.findall(r"\bPROGRAM-ID\s*\.\s*([A-Z0-9_$#@-]+)",
                                    str(page.get("source_text", "")), re.I))
    def mentions(name):
        return re.search(r"(?<![A-Za-z0-9_$#@./-])" + re.escape(name)
                         + r"(?![A-Za-z0-9_$#@./-])", sentence, re.I)
    if any(mentions(name) for name in names if name):
        return False
    # A named dependency must match the actual missing edge. Generic external
    # source wording can describe that edge but cannot rebind another identifier.
    named = {name.upper() for name in re.findall(
        r"(?<![A-Z0-9_$#@-])[A-Z][A-Z0-9_$#@]*[-_][A-Z0-9_$#@-]+", sentence, re.I)}
    return not named or named <= targets


def _source_explanation_deferred(text, source_pages=(), working_set=None):
    """Nominate unfinished source reading, not optional offers or runtime facts."""
    remaining, local_limits = [], []
    for sentence in re.split(r"[\n。！？；;]+|(?<=[.!?])\s+", text):
        sentence = re.sub(r"^[\s*#>\-:：]+", "", sentence)
        if _OPTIONAL_OR_RUNTIME_READING.search(sentence) or _REPORTED_BEHAVIOR.search(sentence):
            remaining.append(sentence)
            continue
        need = _SOURCE_READING_NEED.search(sentence)
        if need and _SOURCE_READING_OBJECT.search(sentence[need.end():]):
            if _unavailable_external_reading(sentence, source_pages, working_set):
                local_limits.append(sentence)
                continue
            return True
        remaining.append(sentence)
    if local_limits:
        # A local boundary qualifies an explanation; it cannot itself be the
        # explanation. Reuse the existing completion check on the remaining
        # prose, excluding those boundaries so their "if..." cannot count as
        # a business condition. No boundary sentences remain for recursion.
        explanation = "\n".join(remaining).strip()
        return not explanation or assess_answer_completion(explanation)["status"] == "incomplete"
    return False


def assess_answer_completion(answer, *, source_pages=(), working_set=None):
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
    commitment_text = re.sub(r"(?m)^\s*>.*$", "", commitment_text)
    commitment_text = re.sub(r"`([^`\n]*)`", lambda match: match[1] if re.fullmatch(
        r"[A-Za-z0-9_$#@./-]+", match[1]) else "", commitment_text)
    commitment_text = re.sub(r'''“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|"[^"\n]*"|(?<!\w)'[^'\n]*' ''',
        lambda match: match[0][1:-1] if re.fullmatch(r"[A-Za-z0-9_$#@./-]+", match[0][1:-1]) else "",
        commitment_text, flags=re.X)
    pending_source_explanation = _source_explanation_deferred(commitment_text, source_pages, working_set)
    pending_investigation = bool(_PENDING_INVESTIGATION.search(commitment_text)) or pending_source_explanation
    selection_statements = set()
    for clause in re.split(r"[\n。！？；;，,:：]+|(?<=[.!?])\s+", commitment_text):
        clause = re.sub(r"^[\s*#>\-:：]+|[\s*。.!?]+$", "", clause)
        clause = re.sub(r"^(?:但是|但|因此|所以|不过|不過|然而|而且)\s*", "", clause)
        selection = _SOURCE_SELECTION_LIMITATION.search(clause)
        conditional_tail = selection and re.match(
            r"\s*(?:时|時|的?情况下|的?情況下|\b(?:when|if|unless)\b)", clause[selection.end():], re.I)
        negated = re.search(r"并不是|並不是|并非|並非|不是.{0,12}(?:限制|问题|問題)|"
                            r"不成立|不准确|不準確|不属实|不屬實", clause)
        if (selection and not conditional_tail and not negated
                and not (_REPORTED_BEHAVIOR.search(clause) or _CONDITIONAL_STATEMENT.search(clause))):
            selection_statements.add(clause)
    clauses = re.split(r"[\n。！？；;，,:：]+|(?<=[.!?])\s+|\b(?:but|however)\b", text, flags=re.I)
    deferred = substantive = limitation = selection_limitation = False
    syntax_observation = runtime_limit = source_or_action_limit = False
    for clause in clauses:
        clause = re.sub(r"^[\s*#>\-:：]+|[\s*。.!?]+$", "", clause)
        clause = re.sub(r"^(?:但是|但|因此|所以|不过|不過|然而|而且)\s*", "", clause)
        if not clause or clause.casefold() in {"结论", "結論", "说明", "說明", "分析结果", "分析結果", "answer", "conclusion"}:
            continue
        if _EXPLICIT_DEFERRAL.search(clause):
            deferred = True
            source_or_action_limit = True
        elif (_REPORTED_BEHAVIOR.search(clause) or _FOLLOWING_EXPLANATION.search(clause)
              or _CONDITIONAL_STATEMENT.search(clause)):
            substantive = True
        elif clause in selection_statements:
            deferred = limitation = selection_limitation = True
            source_or_action_limit = True
        elif _syntax_observation_only(clause):
            syntax_observation = True
            continue
        elif _LIMITATION.search(clause):
            deferred = limitation = True
            if _RUNTIME_EVIDENCE_LIMIT.search(clause):
                runtime_limit = True
            else:
                source_or_action_limit = True
        elif _INVESTIGATION_STATEMENT.search(clause):
            deferred = True
            source_or_action_limit = True
        else:
            substantive = True
    runtime_only = syntax_observation and runtime_limit and not source_or_action_limit
    incomplete = pending_investigation or deferred and not substantive and not runtime_only
    return {"status": "incomplete" if incomplete else "not_assessed",
            "reason": "source_explanation_deferred" if pending_source_explanation else
                      "investigation_promised_without_completion" if pending_investigation else
                      "investigation_without_business_answer" if incomplete else None,
            "pending_investigation": pending_investigation,
            "pending_source_explanation": pending_source_explanation,
            "limitation_detected": limitation,
            "source_selection_limitation_detected": selection_limitation,
            "method": "bounded_text_check", "semantic_verification": "unverified"}


def wants_business_detail(question, *, answer_detail="detailed"):
    """Business answers are developed by default; explicit brevity wins."""
    if answer_detail not in {"brief", "detailed"}:
        raise ValueError("answer_detail must be brief or detailed.")
    preference = re.sub(r"(?:不要|不用|无需|無需|别|別)(?:太)?(?:简短|簡短|简洁|簡潔|简要|簡要)", "", str(question))
    preference = re.sub(r"\b(?:do not|don't|don’t)\s+(?:be|keep (?:it|the answer))\s+"
                        r"(?:brief|concise|short)\b", "", preference, flags=re.I)
    return answer_detail != "brief" and not _BRIEF.search(preference)


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
        r"(?:非零|零|[+-]?\d+(?:\.\d+)?)\s*分支|"
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
                  "accumulation": "说明实际累加接收字段如何保留原值并加入本次增量",
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


_BUSINESS_OVERVIEW = re.compile(
    r"业务(?:处理)?(?:目的|用途|作用|功能|流程|过程|逻辑|概览|介绍)|"
    r"業務(?:處理)?(?:目的|用途|作用|功能|流程|過程|邏輯|概覽|介紹)|"
    r"(?:从|從|按)(?:业务|業務)(?:角度|视角|視角).{0,64}(?:流程|过程|過程)|"
    r"整体(?:功能|流程|处理)|整體(?:功能|流程|處理)|"
    r"(?:程序|程式|模块|模塊)(?:的)?(?:功能|用途|作用)|"
    r"(?:新人|新手).{0,48}(?:程序|程式|功能)|"
    r"(?:程序|程式|功能).{0,12}(?:做什么|做什麼|解决什么|解決什麼)|"
    r"\b(?:business (?:purpose|function|workflow|process|logic|overview)|"
    r"end.to.end (?:business |processing )?(?:flow|process)|"
    r"what (?:does|is) (?:this |the )?(?:program|function).{0,24}(?:do|for))\b", re.I)
_NARRATIVE_ASPECTS = {
    "business_purpose": ("purpose_candidates",
        "用原文说明或适用手册解释处理目的，并区分文档说明、合理解释与已见规则；不猜业务名称"),
    "program_roles": ("relationships",
        "说明已见入口和程序在业务步骤中的职责与联系，不只列调用指令或程序名"),
    "data_roles": ("data_roles",
        "说明已见表或文件提供什么输入、承接什么输出、在哪一步被读写，不只列标识和操作码"),
}
_TECHNICAL_SCAFFOLD = re.compile(
    r"调用|呼叫|赋值|賦值|设置|設置|设为|設為|置为|置為|"
    r"读写|讀寫|读取|讀取|写入|寫入|复制|複製|拷贝|拷貝|包含|引用|定义|定義|"
    r"段落|字段|欄位|栏位|变量|變量|指令|语句|語句|源码|源碼|代码|代碼|"
    r"标识|標識|清单|清單|列表|名称|名稱|职责|職責|说明|說明|用途|目的|功能|"
    r"文件|数据表|資料表|如下|依次|接着|接著|给|給|把|为|為|到|"
    r"(?<![A-Za-z])(?:call(?:s|ed|ing)?|move[ds]?|assign(?:s|ed|ment)?|"
    r"read[sn]?|writ(?:e[sn]?|ing)|cop(?:y|ies|ied)|statement[s]?|instruction[s]?|"
    r"identifier[s]?|field[s]?|variable[s]?|paragraph[s]?|section[s]?|"
    r"source|code|list(?:s|ed)?|name[s]?|follow(?:s|ing)?|then)(?![A-Za-z])", re.I)


def wants_business_overview(question, *, answer_detail="detailed", field_entities=(), source_scopes=(),
                            answer_instructions=""):
    """A broad explanation request, not every detailed or relationship reply."""
    if current_business_explanation_limited(question):
        return False
    topics = _BUSINESS_OVERVIEW.findall(str(question))
    text = str(question)
    # An explicit purpose-only request does not require every available
    # program/data relationship merely because its subject says "purpose".
    purpose_only = re.search(
        r"(?:只|仅|僅)(?:需|要|想)?(?:说明|說明|解释|解釋|知道|了解).{0,32}"
        r"(?:业务目的|業務目的|业务用途|業務用途|程序用途|程式用途)(?=$|[。？！?！；;，,])|"
        r"\b(?:only|just)\s+(?:(?:explain|describe|tell\s+me)\s+)?(?:the\s+)?(?:business\s+)?"
        r"purpose(?:\s+of\s+[^.!?;,\n]{1,64})?(?=$|[.!?;,])", text, re.I)
    if purpose_only and not re.search(
            r"(?:还|還|也|并|並|以及).{0,12}(?:流程|交接|职责|職責)|"
            r"\b(?:also|and)\b.{0,24}\b(?:workflow|flow|handoffs?|roles?)\b", text, re.I):
        return False
    focused_relation = bool(re.search(
        r"哪个|哪個|是否|\b(?:which|whether)\b|"
        r"(?:只|仅|僅)(?:解释|解釋|说明|說明|关注|關注).{0,96}"
        r"(?:输入|輸入|输出|輸出|返回|错误|錯誤|异常|異常|某个|某個)|"
        r"\b(?:only|just)\b.{0,96}\b(?:input|output|return|error|failure|specific)\b", text, re.I))
    cross_role_explanation = (bool(re.search(r"配合|串联|串聯|协作|協作|关系|關係|"
                                            r"\b(?:fit together|interact\w*|relationships?)\b", text, re.I))
        and not focused_relation
        and bool(re.search(r"批处理|批次|在线|在線|联机|聯機|(?<![A-Za-z])(?:batch|online)(?![A-Za-z])", text, re.I))
        and bool(re.search(r"程序|程式|\bprograms?\b", text, re.I))
        and bool(re.search(r"(?:数据|資料|数据库|資料庫)表|表(?=的|中|里|裡|和|与|與|、|，|。|\s|$)|"
                           r"文件|檔案|\b(?:tables?|files?)\b", text, re.I)))
    saved_explanation = saved_business_explanation_requested(question, answer_instructions,
        field_entities=field_entities, source_scopes=source_scopes)
    return saved_explanation or (wants_business_detail(question, answer_detail=answer_detail)
            and (bool(topics) or cross_role_explanation
                 or business_concept_requested(question, field_entities=field_entities, source_scopes=source_scopes))
            and (calculation_intent(str(question)) != "rules"
                 or any(not re.search(r"逻辑|邏輯|\blogic\b", topic, re.I) for topic in topics)))


def _technical_inventory_only(answer):
    """Recognize obvious syntax inventories, never score unfamiliar prose.

    Removing exact code/name tokens and low-information scaffolding leaves
    domain language intact. Any remaining explanatory wording makes this check
    abstain; it does not prove the answer correct or complete.
    """
    text = _REFERENCE.sub("", str(answer))
    text = re.sub(r"(?m)^\s*#{1,6}\s+.*$", "", text)
    text = re.sub(r"```[^\n]*\n.*?(?:```|\Z)|~~~[^\n]*\n.*?(?:~~~|\Z)", "", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    text = re.sub(r"(?<![\w.-])[\w./-]+\.(?:cbl|cob|cobol|cpy|copy|cl|clle|sql|dds)(?![\w.-])",
                  "", text, flags=re.I)
    text = re.sub(r"""'(?:[^']|'')*'|"(?:[^"]|"")*" """, "", text, flags=re.X)
    text = re.sub(r"(?<![A-Za-z0-9_$#@-])[A-Z][A-Z0-9_$#@-]*(?![A-Za-z0-9_$#@-])", "", text)
    text = _TECHNICAL_SCAFFOLD.sub("", text)
    text = _GENERIC_BEHAVIOR_WORDS.sub("", text)
    return not re.search(r"[^\W\d_]", text, re.UNICODE)


def _business_narrative_requirements(question, orientation, source_pages, *, answer_detail="detailed",
                                   field_entities=(), source_scopes=(), answer_instructions=""):
    """Only supplied orientation facets can nominate a business explanation."""
    if not wants_business_overview(question, answer_detail=answer_detail, field_entities=field_entities,
                                   source_scopes=source_scopes, answer_instructions=answer_instructions):
        return []
    visible = {page.get("evidence_id") for page in source_pages if page.get("source_text")}
    available = set(orientation.get("available_facets", ()))
    requirements = []
    for kind, (group, description) in _NARRATIVE_ASPECTS.items():
        if kind not in available:
            continue
        rows = list(orientation.get(group, ()))
        if kind == "program_roles":
            rows += [row for row in orientation.get("entries", ()) if row.get("command") == "SBMJOB"]
            if orientation.get("visible_rules") or orientation.get("data_roles"):
                rows += list(orientation.get("entries", ())) + list(orientation.get("programs", ()))
        references = list(dict.fromkeys(identifier for row in rows if isinstance(row, dict)
            for identifier in row.get("supplied_reference_ids", ()) if identifier in visible))
        if references:
            requirements.append({"kind": kind, "description": description,
                                 "supplied_reference_ids": references[:8]})
    return requirements


def _business_explanation_contract(question, orientation, *, answer_detail="detailed", field_entities=(), source_scopes=(),
                                   answer_instructions=""):
    from answer_markdown import BUSINESS_RUNTIME_BOUNDARY_POLICY

    overview = wants_business_overview(question, answer_detail=answer_detail, field_entities=field_entities,
                                      source_scopes=source_scopes, answer_instructions=answer_instructions)
    return {"mode": "business_overview" if overview else "direct_answer",
        "available_facets": orientation.get("available_facets", []),
        "semantic_verification": False,
        "task": ("面向不熟悉代码的读者，把有依据的业务目的、批处理作业或联机功能、程序职责、表文件角色、"
                 "业务规则与输入输出/异常串成连贯解释。先回答处理对象和用途，再按实际顺序说明入口与输入、"
                 "条件与处理、结果及交付；结合已供应的子程序实现和参数衔接，把每层处理对业务结果的作用讲清楚。"
                 "仅在来源支持时说明批处理和在线功能如何交接，表和文件怎样提供输入或承接输出。"
                 "某个外部实现缺失时，仍解释调用方已见的请求、传入值、返回检查与后续用途，把未知限定在该步骤内部。"
                 "没有依据的环节不补造。程序/文件名用于追溯，不代替用途说明；已供应的实现应在本次解释中使用。"
                 if overview else
                 "直接解释当前所问关系、规则或结果；不为补齐业务概览而扩展无关章节。")
                + "source_documentation 是原文说明，source_syntax 是可见语句，framework_convention 是适用约定；"
                  "据此作出的业务理解明确标注推断，均不证明生产运行。缺口只限定受影响的具体判断。"
                + (BUSINESS_RUNTIME_BOUNDARY_POLICY if overview else "")}

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


def _source_instance_key(page):
    """Keep physical versions and COPY transformations distinct across excerpts."""
    return (page.get("relative_path"), page.get("source_sha256"),
            json.dumps(page.get("include_chain") or [], sort_keys=True),
            json.dumps(page.get("replacement_trace") or [], sort_keys=True),
            json.dumps(page.get("copy_instance"), sort_keys=True))


def _supplied_source_spans(source_pages, supplied):
    """Join adjacent/identical overlapping excerpts only within one source instance."""
    spans = []
    for page in sorted((page for page in source_pages
                        if page.get("evidence_id") in supplied and page.get("source_text")),
                       key=lambda page: (page.get("relative_path") or "", page.get("source_sha256") or "",
                                         *_source_instance_key(page)[2:],
                                         page.get("start_line") or 0, page.get("end_line") or 0)):
        text_lines = page["source_text"].splitlines()
        start, end = page.get("start_line"), page.get("end_line")
        physical = isinstance(start, int) and isinstance(end, int) and end - start + 1 == len(text_lines)
        lines = dict(enumerate(text_lines, start)) if physical else {}
        key = _source_instance_key(page)
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
    from statement_facts import data_references
    from structural_index import _extract_data_access, normalize_cobol_lines

    supplied = {identifier for item in investigation.get("required_items", [])
                if item.get("kind") in {"formula", "business_steps", "conditions", "result_adjustments"}
                and item.get("status") == "SATISFIED"
                for identifier in item.get("evidence_ids", [])}
    from question_planner import question_references
    question_refs = tuple(reference for identity in question_references(question)
                          for reference in data_references(identity))
    conditions, adjustments, hints = False, False, []
    seen, scopes = set(), []
    scoped_pages = []
    scope_ranges = (investigation.get("source_scope") or {}).get("ranges", [])
    for page in source_pages:
        limits = [scope for scope in scope_ranges if scope["relative_path"] == page.get("relative_path") and scope.get("exclusive") is not False
                  and scope.get("source_sha256") == page.get("source_sha256")]
        if not limits:
            scoped_pages.append(page)
            continue
        raw = str(page.get("source_text", "")).splitlines()
        for scope in limits:
            first, last = max(scope["start_line"], page["start_line"]), min(scope["end_line"], page["end_line"])
            if first <= last:
                scoped_pages.append({**page, "start_line": first, "end_line": last,
                    "source_text": "\n".join(raw[first - page["start_line"]:last - page["start_line"] + 1])})
    for span in _supplied_source_spans(scoped_pages, supplied):
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
    references = {field: data_references(field) for field in all_targets | arithmetic_inputs}

    def matches(fields):
        # Select coverage candidates from this supplied material only. A bare
        # reference can nominate every qualified occurrence of the same name;
        # it does not resolve ambiguity or establish a storage identity. An
        # explicit qualifier/index/modification must match its lexical DataRef.
        return {field for field in fields for candidate in references[field]
                if any(requested.name == candidate.name
                       and (not requested.qualifiers or requested.qualifiers == candidate.qualifiers)
                       and (not requested.subscripts or requested.subscripts == candidate.subscripts)
                       and (requested.reference_modification is None
                            or requested.reference_modification == candidate.reference_modification)
                       for requested in question_refs)}

    matched = (matches(arithmetic_targets) or
               matches(all_targets).difference(arithmetic_inputs))
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


@cache_business_explanation
def answer_requirements(question, investigation, source_pages, *, answer_detail="detailed", behavior_guide=None,
                        business_orientation=None, answer_instructions=""):
    """Nominate answer aspects only from question-relevant, supplied candidates.

    These are bounded lexical coverage hints, not a semantic answer score. A
    short answer can cover them; there is deliberately no minimum word count.
    """
    question = str(question)
    scoped_question = question
    for scope in (investigation.get("source_scope") or {}).get("ranges", []):
        name = scope.get("procedure_name")
        if name:
            scoped_question = re.sub(r"(?<![A-Za-z0-9_$#@-])" + re.escape(name) +
                                     r"(?![A-Za-z0-9_$#@-])", " ", scoped_question, flags=re.I)
    calculation = calculation_intent(scoped_question) == "rules"
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
    accumulation = _supplied_accumulations(scoped_question, investigation, source_pages)
    if accumulation:
        requirements.append({"kind": "accumulation", "description": _ASPECT_LABELS["accumulation"],
            "supplied_reference_ids": list(dict.fromkeys(identifier for fact in accumulation
                for identifier in fact["supplied_reference_ids"])), "coverage_hints": accumulation})
    if not calculation and not is_file_impact_question(question):
        guide = (build_behavior_guide(question, investigation, source_pages)
                 if behavior_guide is None else behavior_guide)
        requirements.extend(_behavior_requirements(guide))
    plan = investigation.get("analysis_plan") or {}
    field_entities, source_scopes = plan.get("entities", ()), plan.get("source_scopes", ())
    if (wants_business_overview(question, answer_detail=answer_detail, field_entities=field_entities,
                               source_scopes=source_scopes, answer_instructions=answer_instructions)
            and investigation.get("state") not in {"unresolved", "ambiguous"}):
        if business_orientation is None:
            from business_orientation import build_business_orientation
            business_orientation = build_business_orientation(question, investigation, source_pages)
        requirements.extend(_business_narrative_requirements(question, business_orientation, source_pages,
            answer_detail=answer_detail, field_entities=field_entities, source_scopes=source_scopes,
            answer_instructions=answer_instructions))
    return requirements


def _supplied_accumulations(question, investigation, source_pages):
    """Bind the explicit operation to actual supplied, versioned statements."""
    from question_planner import accumulation_requested, select_accumulations
    if not accumulation_requested(question):
        return []
    from claim_support import supplied_statement_facts
    items = investigation.get("required_items", [])
    operation = next((item for item in items if item.get("kind") == "accumulation_step"), None)
    relevant = [operation] if operation is not None else [item for item in items if item.get("kind") in
        {"formula", "business_steps", "result_adjustments"}]
    supplied = {identifier for item in relevant if item.get("status") == "SATISFIED"
                for identifier in item.get("evidence_ids", [])}
    plan = investigation.get("analysis_plan") or {}
    paths = set(plan.get("paths", []))
    ranges = (investigation.get("source_scope") or {}).get("ranges", [])
    facts, seen = [], set()
    for original in source_pages:
        if original.get("evidence_id") not in supplied or paths and original.get("relative_path") not in paths:
            continue
        scoped = [scope for scope in ranges if scope.get("relative_path") == original.get("relative_path")
                  and scope.get("exclusive") is not False]
        slices = scoped or [None]
        for scope in slices:
            page = original
            if scope is not None:
                if scope.get("source_sha256") != page.get("source_sha256"):
                    continue
                first = max(scope["start_line"], page["start_line"])
                last = min(scope["end_line"], page["end_line"])
                if first > last:
                    continue
                lines = page["source_text"].splitlines()
                page = {**page, "start_line": first, "end_line": last,
                        "source_text": "\n".join(lines[first-original["start_line"]:last-original["start_line"]+1])}
            for fact in supplied_statement_facts(page):
                if fact.get("predicate") != "assignment_statement":
                    continue
                key = (page.get("relative_path"), page.get("source_sha256"), fact["occurrence_id"],
                       fact["start_line"], fact["target"],
                       json.dumps(page.get("include_chain"), sort_keys=True),
                       json.dumps(page.get("replacement_trace"), sort_keys=True), page.get("copy_instance"))
                if key in seen:
                    continue
                seen.add(key)
                facts.append({**fact, "relative_path": page["relative_path"],
                    "source_sha256": page["source_sha256"], "include_chain": page.get("include_chain"),
                    "replacement_trace": page.get("replacement_trace"), "copy_instance": page.get("copy_instance"),
                    "supplied_reference_ids": [page["evidence_id"]]})
    return [{**fact, "kind": "accumulation"} for fact in
            select_accumulations(question, facts, entities=plan.get("entities", []))]


_ACCUMULATION_ACTION = re.compile(
    r"累加|加[入进進到上]|计入|計入|"
    r"\b(?:accumulat(?:e[ds]?|ing)|add(?:ed|ing)?\b.{0,100}\b(?:to|into)|"
    r"increas(?:e[ds]?|ing)\b.{0,60}\bby)\b", re.I)
_ACCUMULATION_DENIAL = re.compile(
    r"不会|不會|不再|不(?:累加|计入|計入|加入)|未(?:曾|执行|執行|累加)|"
    r"\b(?:not|never|cannot|can't|doesn't|don't|isn't|aren't)\b", re.I)
_ACCUMULATION_SCALE = re.compile(
    r"一半|半数|半數|[两兩二三四五六七八九十]倍|乘以|除以|"
    r"\b(?:half|double|twice|multiply|multiplied|divide|divided)\b|[*/]", re.I)


def _accumulation_covered(answer, hint, *, unique_candidate):
    """A conservative coverage hint, not proof of the prose's semantics."""
    from claim_ledger import _assertions, _segments
    from claim_support import statement_tokens
    from question_planner import additive_terms
    from statement_facts import assignment_facts, data_references

    def same_sum(expression):
        expected, actual = additive_terms(hint["expression"]), additive_terms(expression)
        return expected is not None and actual is not None and sorted(expected) == sorted(actual)

    def names_present(text):
        names = [hint["target_field"], *(reference.identity for term in hint["increment_terms"]
                   for reference in data_references(" ".join(term)))]
        return all(re.search(r"(?<![A-Za-z0-9_$#@-])" + re.escape(name) +
                             r"(?![A-Za-z0-9_$#@-])", text, re.I) for name in names)

    def numeric_increment_covered(text):
        numeric = re.compile(r"[+-]?\d+(?:\.\d+)?\Z")
        expected = {Decimal(token) for term in hint["increment_terms"] for token in term if numeric.fullmatch(token)}
        observed = {Decimal(token) for token in re.findall(
            r"(?<![A-Za-z0-9_.-])[+-]?\d+(?:\.\d+)?(?![A-Za-z0-9_-]|\.\d)", text)}
        # Seeing the expected 1 does not excuse a conflicting extra 999.
        if not observed.issubset(expected):
            return False
        digits = "零一二三四五六七八九"
        number_chars = "零〇一二三四五六七八九十百千万萬亿億点點"
        english = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
        number_words = ("zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
                        "fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|"
                        "sixty|seventy|eighty|ninety|hundred|thousand|million|billion")
        for number in expected - observed:
            if not (number == int(number) and 0 <= number < 10):
                return False
            word = english[int(number)]
            compound = re.search(r"\b(?:" + number_words + r")(?:[ -]+and)?[ -]+" + word + r"\b|\b" +
                word + r"[ -]+(?:and[ -]+)?(?:" + number_words + r")\b", text, re.I)
            if not (re.search("(?<![" + number_chars + "])" + digits[int(number)] +
                              "(?![" + number_chars + "])", text)
                    or not compound and re.search(r"(?<![A-Za-z-])" + word + r"(?![A-Za-z-])", text, re.I)):
                return False
        return True

    for segment, inherited, table_header, derived_context in _segments(str(answer)):
        ids = set(_REFERENCE.findall(segment) or inherited)
        text = _REFERENCE.sub("", segment).replace("`", "")
        clauses = re.split(r"[；;，,]|\bbut\b", text, flags=re.I)
        def denial_text(clause):
            return re.sub(r"\bnot\s+(?:only|just|merely)\b", " ", clause, flags=re.I)
        if any(_ACCUMULATION_DENIAL.search(denial_text(clause)) and _ACCUMULATION_ACTION.search(clause)
               and re.search(r"(?<![A-Za-z0-9_$#@-])" + re.escape(hint["target_field"]) +
                             r"(?![A-Za-z0-9_$#@-])", clause, re.I) for clause in clauses):
            continue
        # Unrelated persistence uncertainty must not erase an earlier ADD.
        for clause in clauses:
            if (_LIMITATION.search(clause) or _INVESTIGATION_STATEMENT.search(clause)
                    or _ACCUMULATION_DENIAL.search(denial_text(clause)) or "?" in clause or "？" in clause):
                continue
            all_assertions = [row for row in _assertions(clause, table_header=table_header, derived_context=derived_context)
                              if row.get("predicate") == "assignment_statement"]
            assertions = [row for row in all_assertions if row.get("target") == hint["target_field"]]
            if any(same_sum(row.get("expression", ())) for row in assertions):
                return True
            add = re.search(r"\bADD\b[^。；;\n]*", clause, re.I)
            if add:
                assignment = assignment_facts(add[0].strip().rstrip("。.! "))
                if assignment is not None and any(target == hint["target_field"] and same_sum(
                        statement_tokens(expression) or ())
                        for target, expression in assignment.expressions):
                    return True
                if assignment is not None:
                    continue  # An explicit different ADD is not an unnamed alias.
            if all_assertions or not _ACCUMULATION_ACTION.search(clause):
                continue
            if _ACCUMULATION_SCALE.search(clause) or not numeric_increment_covered(clause):
                continue
            # A cited unique source operation can be described in business
            # language without repeating English field names. Multiple targets
            # require explicit identity; a generic total/constant is not enough.
            if names_present(clause):
                return True
            explicit_names = re.findall(r"(?<![A-Za-z0-9_$#@-])[A-Z][A-Z0-9_$#@-]{1,}(?![A-Za-z0-9_$#@-])", clause)
            if (unique_candidate and not explicit_names and ids.intersection(hint["supplied_reference_ids"])
                    and not re.search(r"(?<![\w-])\d+(?:\.\d+)?", clause)):
                return True
    return False


_UNIVERSAL_RESULT = re.compile(
    r"(?:其他|其它|其余|其餘)(?:情况|情況|情形).{0,12}(?:一律|统一|統一|全部|都)|"
    r"(?:任何|所有|全部|一切)[^，,。；;|]{0,16}(?:异常|異常|错误|錯誤|失败|失敗)|"
    r"(?:无论|無論|不论|不論).{0,24}(?:异常|異常|错误|錯誤|失败|失敗)|"
    r"\b(?:all|any|every)\s+(?:(?:other|possible|runtime)\s+)?(?:exceptions?|errors?|failures?|cases?)\b|"
    r"\botherwise\s+always\b", re.I)
_INVALID_INPUT = re.compile(
    r"(?:输入|輸入|传入|傳入|参数|參數|键|鍵|号码|號碼).{0,24}(?:非法|无效|無效|格式不符|零值)|"
    r"(?:非法|无效|無效|格式不符).{0,24}(?:输入|輸入|参数|參數|键|鍵)|"
    r"(?:零|0)(?:键|鍵|输入|輸入)|"
    r"\b(?:invalid|malformed|zero)\s+(?:input|parameter|argument|key|identifier)\b", re.I)
_RESULT_ASSERTION = re.compile(
    r"返回|回傳|响应码|響應碼|应答码|應答碼|结果码|結果碼|"
    r"(?:查询|查詢).{0,12}(?:失败|失敗|不到|无结果|無結果)|"
    r"(?:给|給|回|设为|設為|置为|置為)\s*[\"']?[0-9]+|"
    r"\b(?:returns?|response\s+code|result\s+code|(?:query|lookup)\s+(?:fails?|finds?\s+nothing))\b", re.I)
_RESULT_UNCERTAINTY = re.compile(
    r"不(?:能|会|會|是|保证|保證|一定|代表|等于|等於|意味)|无法|無法|未必|并非|並非|可能|取决于|取決於|"
    r"\b(?:not|cannot|can't|never|may|might|depends?|no\s+guarantee)\b", re.I)
_RETURN_PATH_SCOPE = re.compile(
    r"(?:正常|成功)(?:返回|回傳)|(?:执行|執行|运行|運行|控制流).{0,16}(?:到达|到達|走到|回到|进入|進入).{0,8}(?:IF|判断|判斷|分支)|"
    r"(?:到达|到達|走到|回到).{0,8}(?:IF|判断|判斷)|"
    r"(?:仅|只|僅).{0,8}(?:静态|靜態|源码|源碼).{0,8}(?:分支|条件|條件)|"
    r"\b(?:normal(?:ly)?\s+return|returns?\s+normally|control\s+(?:reaches|returns)|"
    r"reaches?\s+(?:the\s+)?(?:IF|condition|branch)|only\s+(?:the\s+)?static\s+branch)\b", re.I)


_POST_CALL_PROBLEM = re.compile(
    r"(?:即使|即便|无论|無論|不论|不論).{0,80}(?:异常|異常|出错|出錯|出问题|出問題|失败|失敗)|"
    r"\b(?:even\s+if|regardless\s+of).{0,80}(?:errors?|failures?|fails?|throws?|exception)\b", re.I)
_POST_CALL_CONTINUE = re.compile(
    r"无条件.{0,12}继续|無條件.{0,12}繼續|(?:仍|也|总会|總會).{0,20}(?:继续|繼續)|"
    r"不会.{0,12}(?:改变|影响).{0,12}(?:流程|后续)|不會.{0,12}(?:改變|影響).{0,12}(?:流程|後續)|"
    r"\b(?:still|always|unconditionally)\s+(?:continue|run|proceed|finish)|"
    r"\b(?:will\s+not|won't)\s+(?:change|affect).{0,20}(?:flow|steps?)\b", re.I)


_EMPTY_INPUT = re.compile(r"空(?:输入|輸入|文件|檔案)|(?:输入|輸入|文件).{0,8}(?:为空|為空|无记录|無記錄)|\bempty\s+(?:input|file)\b", re.I)
_ZERO_LOOP = re.compile(
    r"(?:循环体|循環體)\s*(?:一次|一遍|一轮|一輪)?\s*(?:也|都)?\s*(?:不(?:会|會)?(?:进入|進入|进|進|执行|執行)|没有进入|未进入)|"
    r"(?:一次|一遍|一轮|一輪)\s*(?:循环体|循環體)\s*(?:也|都)?\s*不(?:进|進|执行|執行)|"
    r"\b(?:never\s+enters?\s+(?:the\s+)?loop\s+body|zero\s+loop\s+iterations)\b", re.I)
_SUCCESS_ONLY = re.compile(
    r"(?:只(?:有|在)?|仅(?:在)?|僅(?:在)?).{0,24}(?:查询成功|查詢成功|查出结果|查出結果|查到结果|查到結果|成功).{0,20}(?:赋值|賦值|设置|設置|写入|寫入)|"
    r"\bonly.{0,40}(?:success|successful|result\s+found).{0,40}(?:assign(?:ed|s)?|set|writ(?:e|es|ten|ing))", re.I)
_STATUS_ROLE = re.compile(r"(?:响应|響應|返回|状态|狀態|结果|結果)(?:码|碼|标识|標識)|\b(?:response|return|status|result)\s+(?:code|indicator)\b", re.I)
_DATABASE_CHECK_DENIAL = re.compile(
    r"(?:没有|沒有|未见|未見)\s*(?:文件\s*[/、和]\s*)?(?:数据库|資料庫|SQLCODE|SQL)\s*"
    r"(?:状态|狀態|结果|結果)?(?:检查|檢查|判断|判斷)(?=$|[，,、:：|]|但|而)|"
    r"\bno\s+(?:database\s+(?:status|result)|SQLCODE)\s+checks?(?=$|[,.|]|\s+but\b)", re.I)


def _pending_quoted_result_check(clause, start, end):
    """A quoted proposition to verify is not an asserted return guarantee.

    Only the matched proposition is excluded. A different target or a later
    affirmative clause cannot inherit this pending-review frame.
    """
    pattern = re.compile(
        r"(?:若要|如果要)(?:知道|确认|確認|判定|验证|驗證)\s*[“「『]"
        r"(?P<claim>[^”」』\n]{1,300})[”」』]\s*[，,]\s*"
        r"(?:还需|還需|仍需|尚需|需要)(?:确认|確認|核对|核對|验证|驗證)")
    return any(match.start("claim") <= start < end <= match.end("claim")
               for match in pattern.finditer(clause))


def _structured_review_boundaries(pages, kinds):
    """Captured syntax witnesses, deliberately narrower than execution proof."""
    from claim_support import supplied_statement_facts, statement_tokens
    from statement_parser import parse_statements
    from structural_index import normalize_cobol_lines
    from syntax_evidence import _bodies

    result = []
    for page in pages:
        text = page["source_text"]
        if (len(text) > 16000 or page.get("include_chain") or page.get("replacement_trace")
                or type(page.get("start_line")) is not int or type(page.get("end_line")) is not int
                or page["start_line"] < 1 or page["end_line"] < page["start_line"]
                or len(text.splitlines()) != page["end_line"]-page["start_line"]+1):
            continue
        lines, hint = normalize_cobol_lines(text)
        normalized = "\n".join(line.text for line in lines)
        if (len(re.findall(r"\bPROGRAM-ID\s*\.", normalized, re.I)) != 1
                or re.search(r"(?m)^\s*>>|\b(?:COPY|REDEFINES|RENAMES)\b", normalized, re.I)):
            continue
        def record(kind, first, last, **extra):
            result.append({"kind": kind, "relative_path": page["relative_path"],
                "source_sha256": page["source_sha256"], "start_line": first, "end_line": last,
                "supplied_reference_ids": [page["evidence_id"]], **extra})
        facts = None
        if "empty_input_zero_loop" in kinds and "PERFORM" in normalized.upper():
            facts = supplied_statement_facts(page, include_declarations=True)
            bodies, _ = _bodies(text, page["start_line"])
            # Earlier paragraphs may reset the flag or transfer control. This
            # narrow witness does not solve those paths or infer re-entry.
            for _, body, first, source_format in bodies if len(bodies) == 1 else []:
                parsed = parse_statements(body, start_line=first, source_format=source_format, max_tokens=4096)
                if not parsed.complete or parsed.boundaries:
                    continue
                for position, node in enumerate(parsed.nodes):
                    if (node.kind != "PERFORM" or not node.loop or node.loop.get("kind") != "until"
                            or not node.children or node.children[0].kind != "READ" or node.guards):
                        continue
                    reader = node.children[0]
                    if not re.search(r"\bAT\s+END\b", reader.text, re.I) or "END-READ" not in reader.text.upper():
                        continue
                    condition = statement_tokens(node.loop.get("condition", ""))
                    if not condition or len(condition) != 3 or condition[1] not in {"=", "EQUAL"}:
                        continue
                    target, _, stop = condition
                    declarations = [fact for fact in facts if fact["predicate"] == "declaration_value"
                                    and fact.get("target") == target and fact.get("compilation_status") != "candidate"]
                    if len(declarations) != 1 or len(declarations[0].get("expression", ())) != 1:
                        continue
                    # Only the visible first-initialization path is a review
                    # witness. VALUE is never asserted to reset on every call.
                    value = declarations[0]["expression"][0]
                    try:
                        initial, terminal = Decimal(value), Decimal("0" if stop in {"ZERO", "ZEROS", "ZEROES"} else stop)
                    except InvalidOperation:
                        continue
                    if initial == terminal or any(item.kind not in {"OPEN", "CONTINUE"} for item in parsed.nodes[:position]):
                        continue
                    record("read_inside_until_loop", declarations[0]["start_line"], node.span.end_line,
                           control_field=target, declared_initial=value, stop_value=stop,
                           loop_start_line=node.span.start_line, read_start_line=reader.span.start_line,
                           read_end_line=reader.span.end_line)
        if kinds.intersection({"success_only_status_assignment", "database_status_check_denial"}):
            # Require a real complete SQL request before a complete IF/ELSE;
            # a bare field name or another program's branch cannot bind this.
            from answer_grounding import answer_grounding_risks
            sql = [row for row in answer_grounding_risks([page])["reasons"]
                   if row["kind"] == "sql_select_into_effect_boundary"]
            if not sql:
                continue
            for index, line in enumerate(lines):
                if not re.fullmatch(r"IF\s+SQLCODE\s*=\s*(?:0|ZERO|ZEROS|ZEROES)(?:\s+THEN)?", line.text.strip(), re.I):
                    continue
                # Parse physical source, including comments/blank lines, so
                # assignment and END-IF spans cannot shift after normalization.
                window = "\n".join(text.splitlines()[line.start_line-1:line.start_line+63])
                ending = re.search(r"\bEND-IF\b", window, re.I)
                if not ending or ending.end() > 4096 or hint == "mixed":
                    continue
                first = page["start_line"] + line.start_line - 1
                parsed = parse_statements(window[:ending.end()], start_line=first,
                                          source_format="fixed" if hint == "fixed" else "free", max_tokens=1024)
                if not parsed.complete or parsed.boundaries or len(parsed.nodes) != 1:
                    continue
                node = parsed.nodes[0]
                if node.kind != "IF" or len(node.children) != 1 or len(node.else_children) != 1:
                    continue
                yes, no = node.children[0], node.else_children[0]
                if (yes.kind != "MOVE" or no.kind != "MOVE" or yes.effects_unknown or no.effects_unknown
                        or len(yes.writes) != 1 or yes.writes != no.writes):
                    continue
                preceding = [row for row in sql if row["end_line"] < first]
                if len(preceding) != 1:
                    continue
                program = re.search(r"\bPROGRAM-ID\s*\.\s*([A-Z][A-Z0-9_-]*)", normalized, re.I)
                if program is None:
                    continue
                record("status_assigned_in_both_sql_branches", preceding[0]["start_line"], node.span.end_line,
                       target_name=yes.writes[0], condition_start_line=first,
                       program_name=program[1].upper(),
                       success_assignment_line=yes.span.start_line, failure_assignment_line=no.span.start_line)
    return result


def _assertion_prefix(clause, position):
    """Earlier independent claims cannot negate this assertion's own scope."""
    separators = list(re.finditer(r"[，,、:：|]", clause[:position]))
    return separators[-1].end() if separators else 0


def _local_assertion_scope(clause, end):
    # A contrasting clause starts a new commitment. A qualification attached
    # to an earlier assertion cannot silently narrow a later stronger claim.
    transitions = list(re.finditer(r"但(?:是)?|然而|不过|不過|\b(?:but|however)\b", clause[:end], re.I))
    return clause[transitions[-1].end() if transitions else 0:end]


def _cl_continuation_boundaries(pages):
    """Locate CL call/next-command source only; do not infer abnormal return."""
    from cl_context import collect_cl_context, is_cl_path
    eligible = [page for page in pages if is_cl_path(page.get("relative_path", ""))]
    if not eligible:
        return []
    observations = collect_cl_context(eligible, max_observations=96, max_source_chars=80000)["observations"]
    result = []
    for call in observations:
        if (call.get("command") != "CALL" or call.get("status") != "confirmed_source_syntax"
                or not call.get("target_name") or call.get("diagnostics")):
            continue
        following = next((row for row in observations
                          if row["relative_path"] == call["relative_path"]
                          and row["source_sha256"] == call["source_sha256"]
                          and row["start_line"] > call["end_line"]
                          and row.get("status") == "confirmed_source_syntax"), None)
        if following is None:
            continue
        supporting = [page for page in eligible
                      if page["relative_path"] == call["relative_path"]
                      and page["source_sha256"] == call["source_sha256"]
                      and page.get("start_line", 0) <= call["start_line"]
                      and page.get("end_line", 0) >= following["end_line"]
                      and len(page["source_text"].splitlines()) == page["end_line"]-page["start_line"]+1]
        if supporting:
            result.append({"kind": "cl_call_continuation_boundary", "target_name": call["target_name"],
                "relative_path": call["relative_path"], "source_sha256": call["source_sha256"],
                "start_line": call["start_line"], "end_line": following["end_line"],
                "supplied_reference_ids": [page["evidence_id"] for page in supporting]})
    return result


def _numeric_rejection_pages(pages):
    """Prepare only complete, small captured declarations for a review exception."""
    from collections import Counter
    from call_bindings import parse_signature
    from claim_support import supplied_statement_facts
    from structural_index import DATA_ITEM_RE, normalize_cobol_lines

    result = []
    for page in pages:
        if len(page["source_text"]) > 16000:
            continue
        lines, _ = normalize_cobol_lines(page["source_text"])
        normalized = "\n".join(line.text for line in lines)
        signatures = re.findall(r"\bPROCEDURE\s+DIVISION\b[^.]*\.", normalized, re.I)
        if len(signatures) != 1 or len(re.findall(r"\bPROGRAM-ID\s*\.", normalized, re.I)) != 1:
            continue
        try:
            parameters = {item.name for item in parse_signature(signatures[0]) if item.mode == "REFERENCE"}
        except ValueError:
            continue
        fields = Counter(match.group(2).upper() for line in lines
                         if (match := DATA_ITEM_RE.match(line.text)))
        result.append((page, supplied_statement_facts(page), fields, parameters))
    return result


def _explicit_numeric_rejection(clause, page, fact, fields, parameters):
    """Recognize a named parameter set immediately before a complete return.

    This only declines a review nomination. It does not establish input
    validity, caller storage, successful execution, or general prose truth.
    """
    from statement_parser import parse_statements
    from structural_index import normalize_cobol_lines

    target = fact.get("target")
    expression = fact.get("expression", ())
    guards = fact.get("guards", ())
    if (fact.get("compilation_status") == "candidate" or not fact.get("program_name")
            or fact.get("statement_kind") != "MOVE" or len(expression) != 1
            or target not in parameters or fields[target] != 1 or len(guards) != 1):
        return False
    guard = guards[0]
    checked = re.fullmatch(r"IF\s+([A-Z][A-Z0-9_-]*)\s+(?:IS\s+)?NOT\s+NUMERIC",
                           str(guard.get("condition", "")), re.I)
    if (guard.get("kind") != "IF" or guard.get("outcome") not in (True, "true")
            or not checked or fields[checked[1].upper()] != 1):
        return False
    field = checked[1].upper()
    mentioned = {name for name in fields if re.search(
        r"(?<![A-Z0-9_-])" + re.escape(name) + r"(?![A-Z0-9_-])", clause, re.I)}
    value = expression[0].strip("'\"")
    if (field not in parameters or mentioned != {field, target} or not value
            or not re.search(r"(?<![\w])" + re.escape(value) + r"(?![\w])", clause)
            or not re.search(r"非法|无效|無效|格式不符|\b(?:invalid|malformed)\b", clause, re.I)):
        return False
    first = guard.get("start_line")
    if not isinstance(first, int) or not page["start_line"] <= first <= page["end_line"]:
        return False
    physical = page["source_text"].splitlines()
    snippet = "\n".join(physical[first-page["start_line"]:first-page["start_line"]+64])
    lines, _ = normalize_cobol_lines(snippet)
    normalized = "\n".join(line.text for line in lines)
    ending = re.search(r"\bEND-IF\b", normalized, re.I)
    if ending is None or ending.end() > 4096:
        return False
    parsed = parse_statements(normalized[:ending.end()], source_format="free", max_tokens=1024)
    if not parsed.complete or parsed.boundaries or len(parsed.nodes) != 1:
        return False
    control = parsed.nodes[0]
    body = control.children
    return (control.kind == "IF" and not control.else_children and len(body) == 2
            and not any(node.effects_unknown for node in body)
            and body[0].kind == "MOVE" and body[0].expressions == ((target, expression[0]),)
            and (body[1].kind == "GOBACK" or body[1].kind == "EXIT" and body[1].exit_kind == "PROGRAM"))


def semantic_risk_candidates(answer, source_pages):
    """Nominate narrow overclaim risks, never decide prose truth or edit text.

    A candidate requires an actual supplied external-operation boundary. The
    bounded review only asks for a source-grounded scope check; a citation,
    candidate or absence of candidates is not semantic verification.
    """
    text = str(answer)[:48000]
    nominated = []
    fenced, table_columns = False, []
    lines = text.splitlines()[:600]
    for line_index, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        if re.match(r"^\s*#{1,6}\s", line):
            table_columns = []
            continue
        ids = set(_REFERENCE.findall(line))
        clean = _REFERENCE.sub("", line).replace("`", "").replace("**", "")
        table_row = clean.strip().startswith("|") and clean.strip().endswith("|")
        if not table_row:
            table_columns = []
        cells = [cell.strip() for cell in clean.strip().strip("|").split("|")] if table_row else []
        if (table_row and line_index + 1 < len(lines)
                and re.fullmatch(r"\s*\|?[ :|-]+\|?\s*", lines[line_index + 1])
                and "-" in lines[line_index + 1]):
            table_columns = [index for index, cell in enumerate(cells)
                             if _RESULT_ASSERTION.search(cell)]
            continue
        table_result = any(index < len(cells) and re.fullmatch(r"[\"']?[+-]?\d+(?:\.\d+)?[\"']?", cells[index])
                           for index in table_columns)
        for clause in re.split(r"[。；;]+|(?<=[！？])|(?<=[.!?])\s+", clean):
            if (not clause.strip() or "?" in clause or "？" in clause
                    or re.search(r"是否|能否|会不会|會不會|\bwhether\b", clause, re.I)):
                continue
            zero_loop = _ZERO_LOOP.search(clause)
            success_only = _SUCCESS_ONLY.search(clause)
            check_denial = _DATABASE_CHECK_DENIAL.search(clause)
            if check_denial:
                prefix = clause[_assertion_prefix(clause, check_denial.start()):check_denial.start()]
                if (not _RESULT_UNCERTAINTY.search(prefix)
                        and not re.search(r"若|如果|假设|假設|需核|待核|\bif\b", prefix, re.I)):
                    nominated.append(("database_status_check_denial", clause.strip()[:1200], ids))
            if (zero_loop and _EMPTY_INPUT.search(clause)
                    and not _RESULT_UNCERTAINTY.search(clause[_assertion_prefix(clause, zero_loop.start()):zero_loop.start()])):
                nominated.append(("empty_input_zero_loop", clause.strip()[:1200], ids))
            if (success_only and not _RESULT_UNCERTAINTY.search(
                    clause[_assertion_prefix(clause, success_only.start()):success_only.start()])):
                nominated.append(("success_only_status_assignment", clause.strip()[:1200], ids))
            problem = _POST_CALL_PROBLEM.search(clause)
            continuing = _POST_CALL_CONTINUE.search(clause, problem.end()) if problem else None
            if (problem and continuing and not _RETURN_PATH_SCOPE.search(_local_assertion_scope(clause, continuing.end()))
                    and not _RESULT_UNCERTAINTY.search(clause[_assertion_prefix(clause, problem.start()):continuing.start()])
                    and not re.search(r"(?:不能|不会|不會|未必|不一定).{0,8}(?:继续|繼續)", continuing[0])):
                nominated.append(("unconditional_post_call_continuation", clause.strip()[:1200], ids))
            if _RESULT_ASSERTION.search(clause) or table_result:
                for kind, pattern in (("unconditional_exception_result", _UNIVERSAL_RESULT),
                                      ("unvalidated_input_outcome", _INVALID_INPUT)):
                    for match in pattern.finditer(clause):
                        # A negation is scoped to this assertion; a later caveat
                        # cannot retract an earlier positive guarantee implicitly.
                        outcome = _RESULT_ASSERTION.search(clause, match.end())
                        assertion_end = outcome.end() if outcome else match.end()
                        if _RESULT_UNCERTAINTY.search(clause[_assertion_prefix(clause, match.start()):assertion_end]):
                            continue
                        if (kind == "unconditional_exception_result"
                                and _pending_quoted_result_check(clause, match.start(), assertion_end)):
                            continue
                        if (kind == "unconditional_exception_result"
                                and _RETURN_PATH_SCOPE.search(_local_assertion_scope(clause, assertion_end))):
                            continue
                        nominated.append((kind, clause.strip()[:1200], ids))
                        break
                    if len(nominated) >= 12:
                        break
            if len(nominated) >= 12:
                break
        if len(nominated) >= 12:
            break
    if not nominated:
        return []
    versions = {}
    for page in source_pages:
        versions.setdefault(page.get("relative_path"), set()).add(page.get("source_sha256"))
    pages, characters = [], 0
    for page in source_pages[:128]:
        body = page.get("source_text")
        if (page.get("span_truncated") or not page.get("evidence_id") or not isinstance(body, str)
                or not re.fullmatch(r"[a-fA-F0-9]{64}", str(page.get("source_sha256", "")))
                or len(versions.get(page.get("relative_path"), ())) != 1):
            continue
        if characters + len(body) > 80000:
            continue
        characters += len(body)
        pages.append(page)
    if not pages:
        return []
    from answer_grounding import answer_grounding_risks
    boundaries = [row for row in answer_grounding_risks(pages)["reasons"]
                  if row["kind"] in {"sql_select_into_effect_boundary", "call_implementation_not_supplied",
                                     "runtime_call_target", "call_reference_effect_boundary"}]
    if any(kind == "unconditional_post_call_continuation" for kind, _, _ in nominated):
        boundaries.extend(_cl_continuation_boundaries(pages))
    structural_kinds = {kind for kind, _, _ in nominated}.intersection({
        "empty_input_zero_loop", "success_only_status_assignment", "database_status_check_denial"})
    if structural_kinds:
        boundaries.extend(_structured_review_boundaries(pages, structural_kinds))
    if not boundaries:
        return []
    validation_pages = None
    answer_ids = set(_REFERENCE.findall(text))
    result = []
    for kind, clause, ids in nominated:
        boundary_kind = {"unconditional_post_call_continuation": "cl_call_continuation_boundary",
                         "empty_input_zero_loop": "read_inside_until_loop",
                         "success_only_status_assignment": "status_assigned_in_both_sql_branches",
                         "database_status_check_denial": "status_assigned_in_both_sql_branches"}.get(kind)
        special = {"cl_call_continuation_boundary", "read_inside_until_loop", "status_assigned_in_both_sql_branches"}
        selected = [row for row in boundaries if row["kind"] == boundary_kind] if boundary_kind else [
            row for row in boundaries if row["kind"] not in special]
        if kind == "unconditional_post_call_continuation":
            selected = [row for row in selected if re.search(
                r"(?<![A-Z0-9_-])" + re.escape(row["target_name"]) + r"(?![A-Z0-9_-])", clause, re.I)]
        if kind == "empty_input_zero_loop":
            before_loop = clause[:_ZERO_LOOP.search(clause).start()]
            # A named run-state assumption is not the declaration's initial
            # state. Do not resolve that hypothesis by borrowing VALUE.
            if re.search(r"若|如果|假[设設定]|已|调用前|調用前|(?:进入|進入).{0,8}前|"
                         r"\b(?:if|when|assuming|already|before|prior)\b", before_loop, re.I):
                selected = [row for row in selected if not re.search(
                    r"(?<![A-Z0-9_-])" + re.escape(row["control_field"]) + r"(?![A-Z0-9_-])",
                    before_loop, re.I)]
        if kind == "success_only_status_assignment":
            # Bind prose roles through the answer's own explicit parameter
            # table, then still require the actual target in both source arms.
            # A different output in an earlier comma/contrast clause cannot
            # lend its identity to this success-only assignment assertion.
            assignment = _SUCCESS_ONLY.search(clause)
            # Writing one particular value only on success is different from
            # writing the target only on success. This check does not prove
            # value-specific claims, so leave those outside this candidate.
            if re.match(r"\s*(?:为|為|成|=|[:：]|[\"']|[+-]?\d|\b(?:to|as)\b)",
                        clause[assignment.end():], re.I):
                continue
            subject = _local_assertion_scope(clause, assignment.start())
            subject = subject[_assertion_prefix(subject, len(subject)):]
            role_targets = set()
            if _STATUS_ROLE.search(subject):
                for table_line in text.splitlines():
                    cells = [cell.strip().replace("`", "").replace("**", "") for cell in table_line.strip().strip("|").split("|")]
                    if len(cells) >= 2 and re.fullmatch(r"[A-Z][A-Z0-9_-]*", cells[0], re.I) and _STATUS_ROLE.search(" ".join(cells[1:])):
                        role_targets.add(cells[0].upper())
            selected = [row for row in selected if re.search(
                r"(?<![A-Z0-9_-])" + re.escape(row["target_name"]) + r"(?![A-Z0-9_-])", subject, re.I)
                or role_targets == {row["target_name"]}]
        if kind == "database_status_check_denial":
            # A named different program cannot borrow this program's check.
            # Generic wording still needs the usual unique/cited source bind.
            named = set(re.findall(r"(?<![A-Z0-9_-])[A-Z][A-Z0-9_-]{2,}(?![A-Z0-9_-])", clause))
            denial = _DATABASE_CHECK_DENIAL.search(clause)
            subject = re.search(r"(?<![A-Z0-9_-])([A-Z][A-Z0-9_-]{2,})\s*(?:程序|源码|源碼)?\s*$",
                                clause[:denial.start()], re.I)
            if subject:
                named.add(subject[1].upper())
            named.difference_update({"SQL", "SQLCODE", "IF", "ELSE"})
            selected = [row for row in selected if not named or named == {row["program_name"]}]
        relevant = [row for row in selected if ids.intersection(row["supplied_reference_ids"])]
        binding_basis = "assertion_cited_boundary"
        cited = [row for row in selected if answer_ids.intersection(row["supplied_reference_ids"])]
        if not ids:
            if len({(row["relative_path"], row["source_sha256"]) for row in cited}) == 1:
                relevant, binding_basis = cited, "unique_answer_cited_boundary"
            elif len({(row["relative_path"], row["source_sha256"]) for row in selected}) == 1:
                relevant, binding_basis = selected, "unique_supplied_boundary"
        if not relevant:
            continue
        if kind == "unvalidated_input_outcome":
            if validation_pages is None:
                validation_pages = _numeric_rejection_pages(pages)
            if any(_explicit_numeric_rejection(clause, page, fact, fields, parameters)
                   for page, facts, fields, parameters in validation_pages
                   if any(row["relative_path"] == page["relative_path"]
                          and row["source_sha256"] == page["source_sha256"] for row in relevant)
                   for fact in facts):
                continue
        feedback = (
            "核对“没有数据库状态检查”的否定结论。实际已供应的同一程序在 SQL 请求后有 "
            "IF SQLCODE = 0，并在两分支给同一状态参数赋值。说明已存在的状态判断，"
            "与未展示完整异常处理或不能保证所有异常返回分别表达；此候选不证明真实执行结果。"
            if kind == "database_status_check_denial" else
            "核对空输入时循环体零次进入的断言。当前完整原文把 READ/AT END 放在 PERFORM 循环体内，"
            "并显示与停止值不同的声明初值；只要已到达该 READ/AT END，就已进入循环体，"
            "这与未进入 NOT AT END 业务处理分支不同。"
            "结合首次初始化及实际到达循环的前提解释，不把 VALUE 当每次调用重置，也不据此保证外部文件操作成功。"
            if kind == "empty_input_zero_loop" else
            "核对只有查出结果/成功才给该输出赋值的排他结论。实际同一 SQLCODE IF 的两条分支均对该目标赋值，"
            "成功分支与 ELSE 分支应分别说明；区分查询数据输出和状态参数，不把正常返回条件省略或把两类字段混为一谈。"
            if kind == "success_only_status_assignment" else
            "核对这句从静态 CALL 后续命令推出被调用程序发生任何问题后仍继续的保证。"
            "实际原文只显示调用与后续命令的位置；须区分控制流正常返回后的顺序，和调用异常时是否返回。"
            "按已供应的调用/异常处理约定限定本句，不能把未检查返回状态推导为异常不会改变流程。"
            if kind == "unconditional_post_call_continuation" else
            "核对这句/表格中的全异常或无条件返回保证。当前原文显示操作请求和后续状态分支，"
            "不能仅据此推出所有异常均能正常返回并到达赋值处。保留有据结论，在该句就地写明控制流前提；"
            "若实际来源已支持更强保证，请引用具体处理规则。"
            if kind == "unconditional_exception_result" else
            "核对这句/表格把非法、格式不符或零输入直接映射到失败结果的推断。"
            "没有输入校验不证明查询必失败，也不确定外部接口如何处理无效字节。"
            "按实际输入拒绝分支或已供应接口约定说明；否则把该输入结果保留为具体未知。")
        result.append({"kind": kind, "status": "review_candidate", "assertion": clause,
                       "feedback": feedback, "semantic_verification": False,
                       "binding_basis": binding_basis,
                       "source_refs": [{key: row[key] for key in
                           ("relative_path", "source_sha256", "start_line", "end_line", "supplied_reference_ids")}
                           for row in relevant[:4]]})
    return result


def _assignment_only_synopsis(answer):
    """Recognize only a vocabulary-limited assignment/return paraphrase.

    This nominates an existing business-explanation review for saved content
    requirements. Unfamiliar domain language stays unassessed, never rejected.
    """
    text = _REFERENCE.sub("", str(answer)[:65536])
    if not re.search(r"设置|設置|设为|設為|赋值|賦值|\b(?:set|sets|assign\w*|MOVE)\b", text, re.I):
        return False
    text = re.sub(r"(?m)^\s*#{1,6}\s+.*$", "", text)
    text = re.sub(r"(?<![A-Za-z0-9_$#@-])[A-Z][A-Z0-9_$#@-]*(?![A-Za-z0-9_$#@-])", "", text)
    text = re.sub(r"'[^']*'|\"[^\"]*\"|[+-]?\d+(?:\.\d+)?", "", text)
    text = re.sub(r"程序|程式|结果码|結果碼|状态码|狀態碼|结果|結果|字段|欄位|变量|變量|"
                  r"设置|設置|设为|設為|赋值|賦值|返回|回传|回傳|调用方|調用方|调用者|調用者|"
                  r"然后|然後|随后|隨後|最后|最後|先|再|并|並|把|将|將|给|給|为|為|的|是|和|与|與|"
                  r"\b(?:the|a|an|program|code|field|variable|result|status|set|sets|assign\w*|"
                  r"to|and|then|return\w*|caller|calling\s+program)\b", "", text, flags=re.I)
    return not re.search(r"[^\W\d_]", text, re.UNICODE)


def assess_business_answer(question, answer, investigation, source_pages, *, answer_detail="detailed", working_set=None,
                           answer_instructions=""):
    """Detect obvious omissions without treating source supply as answer quality."""
    completion = assess_answer_completion(answer, source_pages=source_pages, working_set=working_set)
    # Only the excerpts actually sent to this answer can contradict a global
    # source-selection denial. Navigation, an unresolved identity, and a local
    # missing callee implementation cannot establish that contradiction.
    source_supplied = (investigation.get("state") != "unresolved" and any(
        str(page.get("source_text", "")).strip()
        and "repository_orientation" not in page.get("selection_reasons", [])
        for page in source_pages))
    selection_contradiction = source_supplied and completion["source_selection_limitation_detected"]
    availability_contradictions = source_availability_contradictions(answer, source_pages, working_set)
    if selection_contradiction or availability_contradictions:
        completion.update(status="incomplete", reason="source_availability_contradiction")
    completion["source_availability_contradiction"] = bool(selection_contradiction or availability_contradictions)
    completion["source_availability_contradictions"] = availability_contradictions
    requirements = answer_requirements(question, investigation, source_pages, answer_detail=answer_detail,
                                       answer_instructions=answer_instructions)
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
        and not re.search(r"[?？]", clause)
        and not _syntax_observation_only(clause))
    generic_behavior = _generic_behavior_summary(explanation)
    technical_inventory = _technical_inventory_only(answer)
    plan = investigation.get("analysis_plan") or {}
    if saved_business_explanation_requested(question, answer_instructions,
            field_entities=plan.get("entities", ()), source_scopes=plan.get("source_scopes", ())):
        technical_inventory = technical_inventory or _assignment_only_synopsis(answer)
    missing = [item["kind"] for item in requirements if item["kind"] != "accumulation"
               if (technical_inventory or generic_behavior if item["kind"] in _NARRATIVE_ASPECTS
                   else generic_behavior if item["kind"].startswith("behavior_")
                   else not _ANSWER_SIGNALS[item["kind"]].search(explanation))]
    for item in requirements:
        for hint in item.get("coverage_hints", []):
            kind = hint["kind"]
            if kind == "accumulation":
                covered = _accumulation_covered(answer, hint, unique_candidate=len(item["coverage_hints"]) == 1)
            elif kind == "rounding":
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
    narrative = [item["kind"] for item in requirements if item["kind"] in _NARRATIVE_ASPECTS]
    if narrative:
        from business_orientation import build_business_orientation
        relation_review = review_business_explanation(answer,
            build_business_orientation(question, investigation, source_pages))
        aspect_names = {"business_purpose": "business_purpose", "program_handoffs": "program_roles",
                        "data_flow": "data_roles"}
        missing.extend(aspect_names[kind] for kind in relation_review["obvious_missing_aspects"]
                       if aspect_names[kind] in narrative and aspect_names[kind] not in missing)
        completion["business_explanation_review"] = {
            **relation_review,
            "technical_inventory_only": technical_inventory, "required_aspects": narrative,
            "missing_aspects": [kind for kind in missing if kind in _NARRATIVE_ASPECTS]}
    completion.update(required_aspects=[item["kind"] for item in requirements], missing_aspects=missing)
    if missing and completion["status"] != "incomplete":
        completion.update(status="incomplete", reason="supplied_business_aspects_unexplained")
    risks = semantic_risk_candidates(answer, source_pages)
    completion["semantic_risk_candidates"] = risks
    if risks:
        completion["missing_aspects"] = list(dict.fromkeys([
            *completion["missing_aspects"], *("semantic_scope_" + risk["kind"] for risk in risks)]))
        completion.update(status="incomplete", reason="semantic_scope_review_needed")
    completion["method"] = "bounded_text_and_coverage_signals"
    return completion


def needs_synthesis_review(question, answer, investigation, *, source_available=False, source_pages=(),
                           answer_detail="detailed", assessment=None, working_set=None, answer_instructions=""):
    """Review a detected omission or unfinished answer against supplied material."""
    items = investigation.get("required_items", [])
    supplied = source_available or any(item.get("evidence_ids") for item in items)
    if not supplied:
        return False
    completion = (assess_business_answer(question, answer, investigation, source_pages, answer_detail=answer_detail,
                                      working_set=working_set, answer_instructions=answer_instructions)
                  if assessment is None else assessment)
    # A local qualification is normal in a detailed explanation. Its wording
    # alone is not an omitted business point and cannot justify regenerating
    # the same answer from the same evidence. Blanket deferrals, outstanding
    # investigation and supplied-aspect omissions are already incomplete.
    # The caller independently revises when it acquires new source material.
    return completion["status"] == "incomplete"


def _analysis_brief_task(detail_requested, syntax_present):
    """Keep the canonical guidance stable while allowing a wire-only summary."""
    return (("简要给出结论、决定性条件和来源。" if not detail_requested else "给出结论及足以推导该结论的业务解释。")
                + "以用户所问的对象、场景和假设为范围，沿相关条件、赋值、调用及返回处理说明结果。"
                "required_answer_aspects 是从词面和可见源码提取的候选要点，按问题选择；"
                "未执行的路径说明为何跳过及其结果，只有用户问其算法时才展开计算。"
                "没有直接 COMPUTE 时仍可根据赋值、分支与调用解释已知规则。"
                "计算发生时核对输入、顺序、舍入和后续覆盖；跨程序参数沿每一层传递与入口声明追踪。"
                "异常或状态码逐项绑定产生它的调用点、条件和后续动作，再作范围一致的归纳。"
                "framework_facts 中适用的约定可解释当前调用；缺少实现与约定时，"
                "解释可见的操作请求、传入值、状态检查与后续分支。"
                "call_effect_boundaries 逐调用标明需要核对的效果：call_implementation_not_supplied 的调用"
                "按‘发起某操作请求并检查返回状态’描述；即使操作码名为保存、恢复或锁定，"
                "正文、表格和总结均只陈述调用方可见动作，外部效果由实际实现或适用约定支持。"
                "按问题所问时点区分调用前传入值与调用后返回值：actual_parameters 给出实参与传递方式，"
                "mutable_outputs 中的参数及下级字段可能被外部调用改写；返回后的值须沿返回约定或后续本地赋值追踪。"
                "mutable_output_groups.observed_fields 列出从当前调用方或明确 COPY 引入的具体下级字段，"
                "这些字段同样遵守调用后的取值规则；按当前问题选取相关字段解释。"
                "SQL 的 host_outputs 是 INTO 输出，须结合执行语义与后续赋值判断；处理分支中未出现 MOVE，"
                "本身不构成这些输出仍保留调用前值的依据。问题仅问某次调用的输入时，解释到该输入形成即结束。"
                + ("syntax_guide 的 IF outcome=false 表示条件不成立；"
                   "EVALUATE prior_branches_must_not_match 表示此前分支均不命中。"
                   "syntax_order_in_unit 是源码顺序，effects_unknown 表示该语句效果尚未解析。"
                   if syntax_present else "")
                + "behavior_guide 是语法位置导航；结合相关原文推导。内部可读缺口用工具补查，"
                "真实缺失只限定受影响的结论。正文围绕业务结果推进，关键判断附实际来源引用。")


def project_analysis_brief(brief):
    """Replace only our known generic guidance; retain every source-bound fact.

    Unknown/custom guidance is preserved. This projection does not change the
    canonical report, source coverage, obligations, syntax or call-effect facts.
    """
    if not isinstance(brief, dict):
        return brief
    syntax = brief.get("syntax_guide")
    expected = _analysis_brief_task(bool(brief.get("detail_requested")),
                                    isinstance(syntax, dict) and bool(syntax.get("facts")))
    if brief.get("task") != expected:
        return brief
    projected = {**brief, "task": (
        "按当前问题、场景、假设和时点解释结论，引用已供应原文。"
        "required_answer_aspects/behavior_guide 是候选导航，不证明执行。"
        "保留控制条件、输入、顺序、舍入、后续覆盖及各层实参传递。"
        "逐调用解释异常/状态、输入与返回值：未知外部效果、mutable_outputs及其下级字段、"
        "SQL INTO输出不得假定保持调用前值，须有返回约定或后续赋值。"
        "应用适用framework_facts；缺少实现时仅说明操作请求及状态检查。"
        "syntax_guide 的false为条件不成立，prior_branches_must_not_match为之前分支均未命中。"
        "源码顺序不等于执行路径，effects_unknown仍未知。"
        "补查可读缺口，未知只限定受影响结论，按系统和response_contract交付。") }
    # The system message already carries this exact policy. Keep canonical
    # reports intact and remove only known generic repetition from the wire.
    contract = brief.get("business_explanation_contract")
    expected_contract = _business_explanation_contract("业务目的", brief.get("business_orientation", {}))
    if contract == expected_contract:
        projected["business_explanation_contract"] = {**contract,
            "task": "按系统的业务解释规则及 response_contract 串联已供应的职责和数据交接；"
                    "business_orientation 的事实和 handoffs 均须对照实际来源，不能推定生产运行。"}
    if "business_orientation" in projected:
        from business_orientation import project_business_orientation
        projected["business_orientation"] = project_business_orientation(projected["business_orientation"])
    return projected


def build_analysis_brief(question, investigation, source_pages, framework_references, max_output_tokens,
                         *, answer_detail="detailed", business_map=None, framework_facts=(), answer_instructions=""):
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
    orientation = {}
    plan = investigation.get("analysis_plan") or {}
    field_entities, source_scopes = plan.get("entities", ()), plan.get("source_scopes", ())
    if wants_business_overview(question, answer_detail=answer_detail, field_entities=field_entities,
                               source_scopes=source_scopes, answer_instructions=answer_instructions):
        from business_orientation import build_business_orientation
        orientation = build_business_orientation(question, investigation, source_pages, business_map=business_map,
            framework_references=framework_references, framework_facts=framework_facts)
    from syntax_evidence import build_inline_syntax_guide
    syntax_guide = build_inline_syntax_guide(source_pages, include_priority_branches=True)
    from answer_grounding import answer_grounding_risks
    call_boundaries = answer_grounding_risks(source_pages, business_map=business_map,
        framework_references=framework_references, framework_facts=framework_facts)
    external = list(dict.fromkeys([*external, *(row["target"] for row in call_boundaries["reasons"]
        if row["kind"] in {"call_implementation_not_supplied", "runtime_call_target"} and row.get("target"))]))
    return {"detail_requested": wants_business_detail(question, answer_detail=answer_detail),
            "output_budget_tokens": max_output_tokens,
            "required_answer_aspects": answer_requirements(question, investigation, source_pages,
                answer_detail=answer_detail, behavior_guide=behavior_guide, business_orientation=orientation,
                answer_instructions=answer_instructions),
            **({"business_orientation": orientation} if orientation.get("available_facets") else {}),
            "business_explanation_contract": _business_explanation_contract(
                question, orientation, answer_detail=answer_detail, field_entities=field_entities,
                source_scopes=source_scopes, answer_instructions=answer_instructions),
            **({"behavior_guide": behavior_guide} if behavior_guide.get("observations") else {}),
            **({"syntax_guide": syntax_guide} if syntax_guide.get("facts") else {}),
            **({"call_effect_boundaries": call_boundaries} if call_boundaries["required"] else {}),
            "available_source_paths": list(dict.fromkeys(page["relative_path"] for page in source_pages
                                                         if page.get("relative_path")))[:8],
            "supplied_material": items,
            "unavailable_or_runtime_targets": external[:8],
            "remaining_gaps": [{key: gap[key] for key in ("kind", "reason") if key in gap} for gap in gaps[:8]],
            "semantic_execution_verified": False,
            "task": _analysis_brief_task(wants_business_detail(question, answer_detail=answer_detail),
                                          bool(syntax_guide.get("facts")))}


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
