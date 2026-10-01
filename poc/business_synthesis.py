"""Question-specific synthesis guidance and unverified answer/source links."""

from __future__ import annotations

import re


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


def assess_answer_completion(answer):
    """Detect a reply made only of investigation or limitation statements.

    This is a conservative text check, not semantic verification. A concrete
    explanation alongside an uncertainty remains available as a partial answer.
    Citations and headings alone cannot turn a deferred answer into an analysis.
    """
    text = _REFERENCE.sub("", str(answer))
    text = re.sub(r"(?m)^\s*#{1,6}\s+.*$", "", text)
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
    incomplete = deferred and not substantive
    return {"status": "incomplete" if incomplete else "not_assessed",
            "reason": "investigation_without_business_answer" if incomplete else None,
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
    r"计算|計算|公式|怎么算|怎麼算|如何算|算出|\b(?:calculation|calculate[ds]?|calculating|formula|computed?)\b", re.I)
_EXPLICIT_DETAIL_REQUEST = re.compile(
    r"详细|詳細|详尽|詳盡|细致|細緻|完整|逐步|流程|\b(?:detailed|thorough|step.by.step|workflow)\b", re.I)
_INPUT_QUESTION = re.compile(
    r"初值|初始值|预设值|預設值|(?:输入|輸入|参数|參數).{0,12}(?:来源|來源|来自|來自|哪里|哪裡|何处|何處)|"
    r"\b(?:initial|default) values?\b|\b(?:input|parameter).{0,24}(?:source|origin|from)|\bwhere.{0,24}(?:input|parameter)\b", re.I)
_CONDITION_QUESTION = re.compile(r"条件|條件|何时|何時|什么时候|什麼時候|\b(?:conditions?|when)\b", re.I)
_ALTERNATIVE_QUESTION = re.compile(
    r"不满足|不滿足|否则|否則|归零|歸零|清零|例外|分支|\b(?:otherwise|else|exceptions?|branches?)\b", re.I)
_ANSWER_SIGNALS = {
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
        r"大于|大於|小于|小於|等于|等於|超过|超過|超出|低于|低於|达到|達到|正数|正數|正值|非正|[<>≤≥]|若|如果|当.{0,24}时|當.{0,24}時|"
        r"仅|僅|才|\b(?:if|when|unless|positive|negative|greater|less|exceeds?)\b", re.I),
    "result_adjustments": re.compile(
        r"否则|否則|不满足|不滿足|其他情况|其他情況|归零|歸零|清零|置零|为零|為零|"
        r"(?:为|為|设为|設為|[=＝])\s*0(?![\d.])|保留|保持|改写|改寫|"
        r"再加|再减|再減|调整|調整|四舍五入|四捨五入|舍入|捨入|截断|截斷|"
        r"\b(?:else|otherwise|zero|unchanged|round(?:ed|ing)?|adjust(?:ed|s)?|truncate[ds]?|"
        r"retain(?:ed|s)?|preserv(?:e[ds]?|ing))\b", re.I),
}
_ASPECT_LABELS = {"formula": "具体算式或运算关系", "inputs": "输入的初值、读取或传入来源",
                  "conditions": "计算适用的条件", "result_adjustments": "其他分支或结果调整"}


def answer_requirements(question, investigation, source_pages, *, answer_detail="detailed"):
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
    requested = {"formula": calculation,
                 "inputs": bool(_INPUT_QUESTION.search(question)) or detailed_calculation,
                 "conditions": detailed_calculation or bool(_CONDITION_QUESTION.search(question)),
                 "result_adjustments": detailed_calculation or bool(_ALTERNATIVE_QUESTION.search(question))}
    visible = {page.get("evidence_id") for page in source_pages if page.get("source_text")}
    requirements = []
    for item in investigation.get("required_items", []):
        kind = item.get("kind")
        identifiers = [identifier for identifier in item.get("evidence_ids", []) if identifier in visible]
        if (requested.get(kind) and item.get("status") == "SATISFIED"
                and item.get("candidate_count", 0) and identifiers):
            requirements.append({"kind": kind, "description": _ASPECT_LABELS[kind],
                                 "supplied_reference_ids": identifiers[:8]})
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
    missing = [item["kind"] for item in requirements
               if not _ANSWER_SIGNALS[item["kind"]].search(explanation)]
    completion.update(required_aspects=[item["kind"] for item in requirements], missing_aspects=missing)
    if missing and completion["status"] != "incomplete":
        completion.update(status="incomplete", reason="supplied_business_aspects_unexplained")
    completion["method"] = "bounded_text_and_coverage_signals"
    return completion


def needs_synthesis_review(question, answer, investigation, *, source_available=False, source_pages=(),
                           answer_detail="detailed"):
    """Nominate a bounded review when useful material got a blanket limitation."""
    items = investigation.get("required_items", [])
    supplied = source_available or any(item.get("evidence_ids") for item in items)
    if not supplied:
        return False
    completion = assess_business_answer(question, answer, investigation, source_pages, answer_detail=answer_detail)
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
                      "omitted_supplied_reference_ids": max(0, len(supplied) - 8)})
    gaps = investigation.get("open_gaps", [])
    external = list(dict.fromkeys(target for item in investigation.get("required_items", [])
        if item.get("reason") in {"external_implementation_unavailable", "runtime_target_unresolved"}
        for target in item.get("targets", [])))
    return {"detail_requested": wants_business_detail(question, answer_detail=answer_detail),
            "output_budget_tokens": max_output_tokens,
            "required_answer_aspects": answer_requirements(question, investigation, source_pages,
                                                            answer_detail=answer_detail),
            "available_source_paths": list(dict.fromkeys(page["relative_path"] for page in source_pages
                                                         if page.get("relative_path")))[:8],
            "supplied_material": items,
            "unavailable_or_runtime_targets": external[:8],
            "remaining_gaps": [{key: gap[key] for key in ("kind", "reason") if key in gap} for gap in gaps[:8]],
            "semantic_execution_verified": False,
            "task": ("先简要回答结论和关键条件，保留必要来源引用。" if not wants_business_detail(
                question, answer_detail=answer_detail) else "按问题需要充分解释，默认不因用户未写‘详细’而缩成概述。")
                + "综合当前原文支持的业务目的、处理先后、输入来源、计算口径、适用条件、例外和结果影响，"
                "只展开与问题有关的内容。关键结论逐项附实际来源引用。已有证据的结论直接说明；"
                "没有直接 COMPUTE 或具体数值不妨碍解释已知步骤、条件或符号关系；只限定未知数值或算法。"
                "内部尚可补读的程序、段落、赋值和依赖由调查工具读取，不要求用户补交已入库源码。"
                "缺外部实现或运行时目标只限制依赖它的结论，继续解释调用者已知的输入、条件和返回处理。"
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
