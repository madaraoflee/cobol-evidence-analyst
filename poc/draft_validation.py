"""Review a candidate against actual supplied sources before it is retained.

This reuses the bounded syntax checker; it does not certify arbitrary prose.
Coverage is measured after local semantic edits, on the same visible answer.
"""

from answer_fact_completion import complete_supplied_answer
from business_synthesis import assess_business_answer
from claim_ledger import review_answer_claims
from source_availability import repair_source_availability_assertions


def validate_answer_draft(question, answer, investigation, source_pages, references=(), *,
                          working_set=None, source_format="auto", compilation_context=None,
                          answer_detail="detailed", answer_language="zh-CN", source_fact_cache=None):
    allowed = {page["evidence_id"]: {**page, "kind": "source_page"} for page in source_pages}
    allowed.update({row["reference_id"]: {**row, "kind": "framework_reference"} for row in references})
    answer, repairs = repair_source_availability_assertions(
        answer, source_pages, working_set, answer_language=answer_language)
    answer, additions = complete_supplied_answer(question, answer, investigation, source_pages,
        source_format=source_format, compilation_context=compilation_context,
        answer_detail=answer_detail, answer_language=answer_language, source_fact_cache=source_fact_cache)
    answer, ledger = review_answer_claims(answer, source_pages, allowed,
        source_format=source_format, compilation_context=compilation_context,
        answer_language=answer_language, source_fact_cache=source_fact_cache, working_set=working_set)
    assessment = assess_business_answer(question, answer, investigation, source_pages,
        answer_detail=answer_detail, working_set=working_set)
    return {"answer": answer, "assessment": assessment, "ledger": ledger.to_dict(),
            "source_repairs": repairs, "fact_completion": additions}


def revision_regression(previous, candidate):
    """Reject introduced, demonstrable errors independently of word coverage.

    An unsupported free-form explanation is not automatically an error. Both
    drafts must be reviewed against the same version-bound material by callers.
    """
    def errors(review):
        return {(entry.get("claim_type"), entry.get("target_entity"),
                 tuple(entry.get("expression") or ()), tuple(entry.get("condition") or ()),
                 entry.get("effect")) for entry in review["ledger"]["entries"]
                if entry.get("support_status") == "contradicted" or entry.get("invalid_evidence_ids")}

    if errors(candidate) - errors(previous):
        return "ANSWER_FACTS_REGRESSED"
    def sql_risks(review):
        return {(row["kind"], (row.get("answer_span") or {}).get("text", "").strip())
                for row in review["assessment"].get("sql_boundary_review", {}).get("risks", [])}

    if sql_risks(candidate) - sql_risks(previous):
        return "ANSWER_BOUNDARIES_REGRESSED"
    lost = set(candidate["assessment"]["missing_aspects"]) - set(previous["assessment"]["missing_aspects"])
    return "ANSWER_COVERAGE_REGRESSED" if lost else None
