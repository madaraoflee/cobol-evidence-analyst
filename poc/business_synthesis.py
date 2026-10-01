"""Question-specific synthesis guidance and unverified answer/source links."""

from __future__ import annotations

import re


_REFERENCE = re.compile(r"\[((?:ev[_:-]|fw:)[^\]\r\n]{1,160})\]")
_DETAIL = re.compile(r"详细|詳細|详尽|詳盡|细致|完整|流程|目的|原理|来龙去脉|"
                     r"\b(?:detailed|thorough|workflow|flow|purpose)\b", re.I)


def wants_business_detail(question):
    return bool(_DETAIL.search(str(question)))


def needs_synthesis_review(question, answer, investigation):
    """Nominate a bounded review when useful material got a blanket limitation."""
    if not wants_business_detail(question):
        return False
    supplied = any(item.get("evidence_ids") for item in investigation.get("required_items", []))
    return supplied and bool(re.search(r"不足以|无法.{0,12}(?:判断|确认|解释|确定)|"
        r"(?:补齐|补充|提供).{0,16}(?:源码|代码)|"
        r"\b(?:insufficient|cannot determine|need more source|provide source)\b", str(answer), re.I))


def build_analysis_brief(question, investigation, source_pages, framework_references, max_output_tokens):
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
    return {"detail_requested": wants_business_detail(question),
            "output_budget_tokens": max_output_tokens,
            "available_source_paths": list(dict.fromkeys(page["relative_path"] for page in source_pages
                                                         if page.get("relative_path")))[:8],
            "supplied_material": items,
            "unavailable_or_runtime_targets": external[:8],
            "remaining_gaps": [{key: gap[key] for key in ("kind", "reason") if key in gap} for gap in gaps[:8]],
            "semantic_execution_verified": False,
            "task": "综合当前原文支持的业务目的、处理先后、输入来源、计算口径、适用条件、例外和结果影响，"
                "只展开与问题有关的内容。关键结论逐项附实际来源引用。已有证据的结论直接说明；"
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
