"""Bounded business explanation preferences, separate from source identity.

Saved instructions can ask for business purpose and handoffs as well as prose
format. They never supply source identifiers, facts, field references or scope.
"""

from __future__ import annotations

import re


_ACTION = re.compile(r"解释|解釋|说明|說明|讲清|講清|讲明|講明|讲解|講解|介绍|介紹|"
                     r"\b(?:explain|describe|clarify|cover|walk\s+through)\b", re.I)
_CONTENT = re.compile(
    r"业务(?:目的|用途|对象|對象|结果|結果|后果|後果|流程|过程|過程|概览|概覽)|"
    r"業務(?:目的|用途|對象|結果|後果|流程|過程|概覽)|"
    r"职责|職責|分工|数据交接|資料交接|数据流|資料流|上下游|下游使用|"
    r"调用方|調用方|被调(?:用)?方|被調(?:用)?方|谁.{0,8}(?:使用|负责|負責|发起|發起)|"
    r"\b(?:business\s+(?:purpose|role|object|outcome|consequence|process|workflow|overview)|"
    r"responsibilit\w*|data\s+flow|handoffs?|upstream|downstream|callers?|callees?)\b", re.I)
_NEWCOMER = re.compile(r"面向.{0,16}(?:新人|新手|不熟悉代码|不熟悉程式)|"
                       r"\b(?:for|to)\s+(?:a\s+)?(?:newcomer|beginner|nontechnical\s+reader)\b", re.I)
_NEGATED = re.compile(r"(?:不要|不必|不用|无需|無需|不需要|别|別|不再).{0,16}?"
                      r"(?:解释|解釋|说明|說明|讲|講|介绍|介紹|展开|展開)|"
                      r"\b(?:do\s+not|don't|never|no\s+need\s+to)\s+"
                      r"(?:explain|describe|clarify|cover|expand|walk\s+through)\b", re.I)
_ILLUSTRATIVE = re.compile(r"^\s*(?:例如|比如|示例|举例|舉例|for\s+example|e\.g\.)", re.I)
_CURRENT_LIMIT = re.compile(
    r"(?:只|仅|僅)(?:需|要|想)?(?:解释|解釋|说明|說明|回答|知道|了解|查看|看|读|讀|分析)|"
    r"(?:不要|不用|无需|無需)(?:展开|展開)|(?:一|两|兩|三)句(?:话|話)|"
    r"简短回答|簡短回答|简要回答|簡要回答|只给|只給|仅给|僅給|"
    r"翻译|翻譯|(?:字段|欄位|栏位|变量|變量|参数|參數).{0,30}(?:值|类型|類型|长度|長度|定义|定義)|"
    r"(?:返回码|返回碼|返回值|结果码|結果碼|状态码|狀態碼).{0,8}(?:是多少|是什么值|是什麼值|具体值|具體值|取值)|"
    r"(?:是否|会不会|會不會).{0,18}(?:调用|調用|呼叫)|"
    r"(?:哪个|哪個|哪些|谁|誰).{0,24}(?:程序|程式).{0,12}(?:调用|調用|使用)|"
    r"\b(?:only|just)\s+(?:explain|describe|answer|tell|read|inspect|analy[sz]e)|"
    r"\b(?:in\s+(?:one|two|three|a\s+single)\s+sentences?|brief\s+answer|"
    r"answer\s+briefly|translate|type\s+of|length\s+of|value\s+of)\b|"
    r"^\s*(?:what\s+is|what's)\s+(?:the\s+)?(?:return|result|status)\s+(?:code|value)\s*[?？.]*\s*$", re.I)


def saved_business_explanation_requested(question, answer_instructions="", *, field_entities=(), source_scopes=()):
    """Nominate existing business explanation work, never a presentation gate.

    Explicit current limits and bound fields/procedures take precedence. A
    positive request for purpose or handoffs can otherwise activate the same
    source-backed overview used by a direct business question. Pure formatting
    and negated requirements do not expand investigation.
    """
    if (not isinstance(answer_instructions, str) or not answer_instructions.strip()
            or field_entities or any(scope.get("exclusive") is not False for scope in source_scopes)
            or _CURRENT_LIMIT.search(str(question))):
        return False
    for clause in re.split(r"[\n。！？;；，,]+|(?<=[.!?])\s+", answer_instructions[:16000]):
        if _ILLUSTRATIVE.search(clause):
            continue
        actions = list(_ACTION.finditer(clause))
        negated = list(_NEGATED.finditer(clause))
        for index, action in enumerate(actions):
            if any(match.start() <= action.start() < match.end() for match in negated):
                continue
            # Scope negation to its own action. A sentence can reject code
            # translation and then positively request business explanation.
            end = actions[index + 1].start() if index + 1 < len(actions) else len(clause)
            target = clause[action.end():end]
            if _CONTENT.search(target) or _NEWCOMER.search(clause[:end]):
                return True
    return False
