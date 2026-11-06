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

_PROCESS_ORDER = re.compile(
    r"先后|先後|顺序|順序|次序|(?:优先|優先)(?:级|級|次序|顺序|順序)|"
    r"(?:优先|優先)(?=\s*(?:使用|应用|應用|处理|處理|执行|執行|扣减|扣減|抵扣|分配|结算|結算))|"
    r"\b(?:sequenc(?:e|ing|y)|priorit(?:y|ies|iz\w*|is\w*))\b|"
    r"\b(?:(?:what|which)\s+order|(?:order|ordering)\s+(?:of|for)|"
    r"(?:processing|application|execution)\s+order)\b", re.I)
_PROCESS_ACTION = re.compile(
    r"收取|收款|扣减|扣減|抵扣|抵减|抵減|使用|应用|應用|处理|處理|执行|執行|"
    r"分配|清偿|清償|偿付|償付|结算|結算|流转|流轉|步骤|步驟|流程|业务|業務|"
    r"\b(?:appl(?:y|ies|ied|ying)|process(?:es|ed|ing)?|execut(?:e[ds]?|ing|ion)|"
    r"deduct\w*|offset\w*|allocat\w*|settl\w*|steps?|stages?|workflow|business)\b", re.I)
_TECHNICAL_ORDER = re.compile(
    r"(?:字段|欄位|栏位|列|字节|位元組).{0,16}(?:排列|位置|顺序|順序|次序)|"
    r"排序(?:键|鍵|字段|欄位|栏位|列)|升序|降序|字典序|运算符|運算符|"
    r"\b(?:order\s+by|sort\s+(?:on\s+)?(?:ascending|descending)|"
    r"sort(?:ing)?\s+(?:keys?|fields?|columns?|order)|ascending|descending|"
    r"collation|endianness|byte\s+order|operator\s+(?:order|precedence)|"
    r"(?:field|column|byte)\s+(?:order|ordering|position))\b", re.I)
_SEQUENCE_VALUE = re.compile(
    r"\b(?:nextval|currval|(?:create|alter|drop)\s+sequence|"
    r"sequence\s+(?:number|generator|function|object|type))\b|"
    r"(?:序列|\bsequence\b)\s*(?:函数|函數|对象|物件|產生器|生成器)", re.I)
_NARROW_VALUE = re.compile(
    r"(?:字段|欄位|栏位|变量|變量|参数|參數)[^，,。.!?；;！？\n]{0,30}(?:取值|初值|类型|類型|长度|長度)|"
    r"\b(?:value|type|length)\s+of\b|\b(?:initial|default|return)\s+value\b", re.I)
_FIELD_DETAIL = re.compile(r"字段|欄位|栏位|变量|變量|参数|參數|"
                           r"\b(?:fields?|variables?|parameters?|arguments?)\b", re.I)
_FORMULA = re.compile(r"公式|算式|表达式|表達式|\b(?:formula|expression)s?\b", re.I)
_MULTIPLE_STEPS = re.compile(
    r"(?:各|每)(?:项|項|种|種|个|個|步)|多(?:项|項|种|種|个|個)|分别|分別|逐项|逐項|"
    r"\b(?:each|different|multiple|several|various|steps|stages)\b", re.I)
_NEGATED_REQUEST = re.compile(r"不要|不用|无需|無需|不必|不需要|别|別|"
                              r"\b(?:without|avoid|do\s+not|don't|don’t)\b", re.I)


def _negated_order_request(text, position):
    prefix = re.split(r"[，,。.!?！？；;\n]", text[max(0, position - 64):position])[-1]
    # A request to go beyond an explanation still requests that explanation.
    prefix = re.sub(r"(?:不要|不用|不必|无需|無需|不需要|别|別)(?:只|仅|僅)|"
                    r"\b(?:do\s+not|don't|don’t)\s+(?:only|just)\b", " ", prefix, flags=re.I)
    return bool(_NEGATED_REQUEST.search(prefix))


def business_sequence_requested(question, *, field_entities=(), source_scopes=()):
    """Nominate ordered processing, without deriving an order from its names.

    A request about processing precedence needs its steps and handoffs even
    without an overview keyword. Identifier spelling, record layout, sorting
    keys and a single expression do not establish that broader request.
    """
    if any(scope.get("exclusive") is not False for scope in source_scopes):
        return False
    text = str(question)
    for name in field_entities:
        if isinstance(name, str) and name:
            text = re.sub(r"(?<![A-Za-z0-9_$#@-])" + re.escape(name) +
                          r"(?![A-Za-z0-9_$#@-])", " ", text, flags=re.I)
    # SEQUENCE-NO and similar identifiers are subjects, not ordering requests.
    text = re.sub(r"(?<![A-Za-z0-9_$#@-])[A-Za-z][A-Za-z0-9_$#@]*"
                  r"(?:[-_][A-Za-z0-9_$#@]+)+(?![A-Za-z0-9_$#@-])", " ", text)
    def requested(pattern):
        return any(not _negated_order_request(text, match.start()) for match in pattern.finditer(text))
    if not requested(_PROCESS_ORDER) or not requested(_PROCESS_ACTION):
        return False
    if any(not _negated_order_request(text, match.start())
           for match in re.finditer(r"翻译|翻譯|\btranslat\w*\b", text, re.I)):
        return False
    if any(requested(pattern) for pattern in (_TECHNICAL_ORDER, _SEQUENCE_VALUE, _NARROW_VALUE)):
        return False
    if requested(_FIELD_DETAIL) and not requested(_MULTIPLE_STEPS):
        return False
    if requested(_FORMULA) and not requested(_MULTIPLE_STEPS):
        return False
    return True


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
