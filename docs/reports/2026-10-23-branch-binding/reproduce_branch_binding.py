"""Offline branch-binding regression; no model, filesystem source, or network use."""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "poc"))
from claim_ledger import review_answer_claims
from claim_support import supplied_statement_facts
from business_synthesis import assess_business_answer

SOURCE = """IDENTIFICATION DIVISION.
PROGRAM-ID. BRANCH-RULE.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 IN-AMOUNT PIC S99.
01 OUT-AMOUNT PIC S999.
PROCEDURE DIVISION.
MAIN.
IF IN-AMOUNT > ZERO
COMPUTE OUT-AMOUNT = IN-AMOUNT * 2
ELSE
COMPUTE OUT-AMOUNT = IN-AMOUNT * 3
END-IF.
GOBACK.
"""
GOOD = "在 IN-AMOUNT > ZERO 的条件下，OUT-AMOUNT = IN-AMOUNT * 2；否则 OUT-AMOUNT = IN-AMOUNT * 3。[ev_rule]"
BAD = "在 IN-AMOUNT > ZERO 的条件下，OUT-AMOUNT = IN-AMOUNT * 3；否则 OUT-AMOUNT = IN-AMOUNT * 2。[ev_rule]"
page = dict(evidence_id="ev_rule", relative_path="rule.cbl", source_text=SOURCE,
            source_sha256=hashlib.sha256(SOURCE.encode()).hexdigest(), start_line=1,
            end_line=len(SOURCE.splitlines()))
investigation = {"required_items": [dict(kind=kind, status="SATISFIED", candidate_count=1,
                                        evidence_ids=["ev_rule"])
                                    for kind in ["formula", "conditions", "result_adjustments"]]}
results = []
for label, answer in [("correct_branches", GOOD), ("reversed_branches", BAD)]:
    rendered, ledger = review_answer_claims(answer, [page], {"ev_rule": page}, source_format="free")
    results.append(dict(case=label, question="OUT-AMOUNT 如何计算？", expected_answer=GOOD,
                        input_answer=answer, output_answer=rendered, ledger=ledger.to_dict(),
                        assessment=assess_business_answer("OUT-AMOUNT 如何计算？", rendered,
                                                          investigation, [page])))
print(json.dumps(dict(source=SOURCE, source_sha256=page["source_sha256"],
                      facts=supplied_statement_facts(page, source_format="free"),
                      cases=results), ensure_ascii=False, indent=2))
