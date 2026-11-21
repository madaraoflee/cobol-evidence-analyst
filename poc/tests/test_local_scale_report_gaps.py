"""Report counters can be scoped only after their complete local source was supplied."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from business_chat import _literal_call_path_gaps
import local_grep_acceptance as workbench
from local_literal_call_path import supplied_report_counters
import test_local_literal_call_path as fixture


REPORT = '''IDENTIFICATION DIVISION.
PROGRAM-ID. BANNER.
PROCEDURE DIVISION.
IF DISPLAY-FLAG = "Y"
 MOVE "reserve refund" TO MESSAGE-TEXT
END-IF.
IF PAGE-NO > 10
 SUBTRACT 1 FROM PAGE-NO
END-IF.
GOBACK.'''
STORAGE = ('DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 DISPLAY-FLAG PIC X.\n'
           '01 MESSAGE-TEXT PIC X(80).\n77 PAGE-NO PIC 9(4).\n')


class LocalScaleReportGapTests(unittest.TestCase):
    def replay(self, extra):
        files = workbench.originals("explicit_grep")
        for i in range(9):
            text = REPORT.replace("BANNER.", f"BANNER{i}.")
            if extra and i == 0:
                text = text.replace("GOBACK.", extra + "\nGOBACK.")
            files[f"hint{i}.cbl"] = text.encode()
        with patch.object(workbench, "originals", return_value=files):
            return workbench.replay("explicit_grep")

    def test_mixed_current_and_prior_inputs_close_only_for_the_verified_report_counter(self):
        result = self.replay("")
        self.assertEqual((result["status"], result["stop_reason"]), ("completed", "sufficient_material"))
        self.assertEqual({r["relative_path"] for r in result["citations"]
                          if r["evidence_id"] in result["inline_citations"]},
                         {"start.cbl", "legacy/route.cbl", "archive/settlement.cbl"})
        for term in ("1-RESERVECREDIT", "CREDIT-AMT", "POOL-AMOUNT", "跳过", "50", "80"):
            self.assertIn(term, result["answer"])

    def test_report_with_a_real_fee_stays_partial(self):
        result = self.replay("IF LATE-FEE > 0\n SUBTRACT LATE-FEE FROM OTHER-DUE\nEND-IF.")
        self.assertEqual((result["status"], result["stop_reason"]), ("partial", "question_evidence_incomplete"))

    def original_case(self):
        case = fixture.LocalLiteralCallPathTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_missing_stale_or_transformed_report_and_unknown_fields_remain_open(self):
        case = self.original_case()
        chain, answer = case.case()
        name = "report.cbl"
        page = case.originals.page(name, REPORT)
        proof = case.proof([*chain, page], answer)
        gap = {"kind": "inputs", "status": "PARTIAL", "reason": "input_source_not_located"}
        source = {"relative_path": name, "source_sha256": page["source_sha256"], "field": "PAGE-NO"}
        def classify(proof, ref=page, entry=source, missing=0, count=1):
            item = {**gap, "fields": [entry["field"]], "missing_count": missing, "unknown_input_count": count,
                    "unknown_input_sources": [entry], "evidence_ids": [ref["evidence_id"]]}
            return _literal_call_path_gaps([gap], {"required_items": [item]}, {}, proof,
                {ref["evidence_id"]: ref}, [*chain, page])[0]
        self.assertEqual(classify(proof), [])
        for entry in ({**source, "field": "NET-DUE"}, {**source, "field": "LINE-NO"},
                      {**source, "relative_path": "unread.cbl"}, {**source, "source_sha256": "old"}):
            with self.subTest(entry=entry):
                self.assertEqual(classify(proof, entry=entry), [gap])
        self.assertEqual(classify(proof, count=2), [gap])
        self.assertEqual(classify(proof, missing=1), [gap])
        short = case.originals.page(name, REPORT, 1, 4)
        self.assertEqual(classify(proof, ref=short), [gap])
        for view in (short, dict(page, source_sha256="old"), dict(page, span_truncated=True),
                     dict(page, copy_instance="elsewhere"), dict(page, source_text=REPORT.replace("10", "99"))):
            with self.subTest(view=view):
                self.assertEqual(classify(case.proof([*chain, view], answer)), [gap])

    def test_report_storage_and_operations_must_be_presentation_only(self):
        case = self.original_case()
        declared = REPORT.replace("PROCEDURE DIVISION.", STORAGE + "PROCEDURE DIVISION.")
        def counters(text):
            page = case.originals.page("report.cbl", text)
            return supplied_report_counters([page], source_session=case.originals.session,
                                            required_ids=[page["evidence_id"]])
        self.assertEqual(set(counters(declared)), {"PAGE-NO"})
        self.assertEqual(set(counters(REPORT.replace("PAGE-NO", "LINE-COUNT"))), {"LINE-COUNT"})
        self.assertEqual(counters(REPORT[:REPORT.index("IF PAGE-NO")] + "GOBACK."), {})
        for text in (declared.replace("PAGE-NO PIC", "PAGE-NO REDEFINES MESSAGE-TEXT PIC"),
                     declared.replace("PAGE-NO PIC", "PAGE-NO EXTERNAL PIC"),
                     declared.replace("WORKING-STORAGE", "LINKAGE"),
                     REPORT.replace("PROCEDURE DIVISION.", "PROCEDURE DIVISION USING PAGE-NO."),
                     REPORT.replace("TO MESSAGE-TEXT", "TO REFUND-ROUTINE"),
                     REPORT.replace("IF PAGE-NO", "IF OTHER-FIELD"), REPORT.replace("FROM PAGE-NO", "FROM DUE"),
                     REPORT.replace("GOBACK.", 'CALL "REPORT-WRITER".\nGOBACK.')):
            with self.subTest(text=text):
                self.assertIsNone(counters(text))


if __name__ == "__main__":
    unittest.main()
