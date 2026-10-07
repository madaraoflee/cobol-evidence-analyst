"""Small question-routing hints, independent of source-language semantics."""

from __future__ import annotations

import re


_CALCULATION = re.compile(
    r"计算|計算|公式|算式|怎么算|怎麼算|如何算|算出|"
    r"\b(?:calculation|calculate[ds]?|calculating|formula|computed?)\b", re.I)
_RULE_REQUEST = re.compile(
    r"公式|算式|怎么算|怎麼算|如何算|"
    r"(?:如何|怎么|怎麼|怎样|怎樣)\s*(?:计算|計算)|"
    r"(?:计算|計算)(?:方法|方式|规则|規則|口径|口徑|逻辑|邏輯)|"
    r"(?:详细|詳細|详尽|詳盡|逐步|完整).{0,8}(?:计算|計算)(?:流程|过程|過程|步骤|步驟)|"
    r"四舍五入|四捨五入|舍入|捨入|"
    r"\b(?:formulas?|rounding|calculation (?:rules?|basis|logic|method))\b|"
    r"\bhow\s+(?:to\s+)?(?:calculate|compute)\b|"
    r"\bhow\s+(?:is|are|was|were|do|does|did|can|will|would|should)\b"
    r"[^.!?;,\n]{0,60}\b(?:calculate[ds]?|computed?)\b|"
    r"\b(?:detailed|step.by.step)\s+calculation\b", re.I)
_EXECUTION_REQUEST = re.compile(
    r"(?:是否|会不会|會不會|会否|會否|能否|还会|還會|还要|還要)"
    r"[^。！？；;?!\n]{0,32}(?:执行|執行|运行|運行|进行|進行|继续|繼續)|"
    r"(?:执行|執行|运行|運行|进行|進行)[^。！？；;?!\n]{0,8}(?:吗|嗎)|"
    r"(?:跳过|跳過|略过|略過|停止|终止|終止)[^。！？；;?!\n]{0,24}(?:计算|計算)|"
    r"(?:计算|計算)[^。！？；;?!\n]{0,24}(?:跳过|跳過|略过|略過|停止|终止|終止)|"
    r"\b(?:whether|will|would|does|do|is|are|was|were|can|could|should)\b"
    r"[^.!?;\n]{0,60}\b(?:run|runs|running|execute[ds]?|perform(?:ed)?|"
    r"skip(?:ped)?|continue|proceed|happen)\b|"
    r"\bskip(?:ped|ping)?\s+(?:(?:the|this|that|a)\s+)?calculation\b", re.I)


def calculation_intent(question):
    """Return rules, execution, or None; retain rules for ambiguous requests.

    A calculation mentioned only to ask whether it runs needs its control
    context, not an obligatory formula explanation. Explicit mathematical
    questions take precedence, including mixed execution/formula questions.
    """
    question = str(question)
    if not _CALCULATION.search(question):
        return None
    if _RULE_REQUEST.search(question):
        return "rules"
    clauses = re.split(r"[。！？?!；;\n]+|(?<=\.)\s+", question)
    calculation_clauses = [clause for clause in clauses if _CALCULATION.search(clause)]
    return ("execution" if all(_EXECUTION_REQUEST.search(clause) for clause in calculation_clauses)
            else "rules")
