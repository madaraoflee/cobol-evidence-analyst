"""Only operational evidence should occupy the local deduction question's early findings."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_grep_acceptance as workbench
from local_deduction_review import merge_checked_findings
import test_local_deduction_review as fixture


def report_source(program, body):
    return f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {program}.\nPROCEDURE DIVISION.\n{body}\nGOBACK."


class LocalScaleFindingTests(unittest.TestCase):
    def test_workbench_shows_the_first_verified_debit_without_report_labels(self):
        files = workbench.originals("explicit_grep")
        for i in range(9):
            body = 'IF DISPLAY-FLAG = "Y"\n MOVE "reserve refund" TO MESSAGE-TEXT\nEND-IF.'
            files[f"hint{i}.cbl"] = report_source(f"BANNER{i}", body).encode()
        with patch.object(workbench, "originals", return_value=files):
            result = workbench.replay("explicit_grep")
        findings = result["while_waiting"]["checked_findings"]
        self.assertEqual(result["while_waiting"]["message_roles"], ["user"])
        self.assertTrue(any(page["path"].startswith("hint") for page in result["requests"][0]["supplied_pages"]))
        self.assertEqual([row["sources"][0]["relative_path"] for row in findings], ["start.cbl"])
        self.assertIn("1-RESERVECREDIT", findings[0]["text"])
        self.assertIn("CREDIT-AMT", findings[0]["text"])
        self.assertIn("跳过", findings[0]["text"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["network_connections"], 0)

    def test_program_name_or_text_payload_alone_cannot_make_report_operations_a_deduction_rule(self):
        local = fixture.LocalDeductionReviewTests()
        local.setUp()
        self.addCleanup(local.doCleanups)
        text = report_source("REFUND-REPORT", 'IF PRINT-ENABLED = "Y"\n MOVE "reserve refund" TO NOTE-TEXT\n'
                             'END-IF.\nIF PAGE-COUNT > 10\n SUBTRACT 1 FROM PAGE-COUNT\nEND-IF.')
        page = local.page("report.cbl", text)
        self.assertFalse(local.proof([page], workbench.QUESTION).checked_findings)
        ordinary = local.proof([page], "REFUND-REPORT 的 NOTE-TEXT 什么时候赋值？")
        self.assertTrue(any("NOTE-TEXT" in row["text"] for row in ordinary.checked_findings))

    def test_new_guarded_debit_displaces_an_earlier_assignment_in_a_full_progress_snapshot(self):
        local = fixture.LocalDeductionReviewTests()
        local.setUp()
        self.addCleanup(local.doCleanups)
        items = [local.page(f"setup{i}.cbl", report_source(f"SETUP{i}",
                 f'IF RUN-FLAG = "Y"\n MOVE "ROOM{i}" TO REFUND-TARGET-{i}\nEND-IF.')) for i in range(8)]
        previous = local.proof(items, workbench.QUESTION).ranked_findings
        self.assertEqual(len(previous), 8)
        self.assertTrue(all("REFUND-TARGET" in r["text"] for _, r in previous))
        fee = local.page("settle.cbl", workbench.WORKER.replace("POOL-AMOUNT", "REFUND-POOL"))
        current = local.proof([*items, fee], workbench.QUESTION).ranked_findings
        merged = merge_checked_findings(previous, current)
        self.assertEqual(len(merged), 8)
        self.assertEqual(merged[0][1]["sources"][0]["evidence_id"], fee["evidence_id"])
        self.assertIn("REFUND-POOL", merged[0][1]["text"])
        self.assertIn("跳过", merged[0][1]["text"])
        self.assertEqual(merged, merge_checked_findings(merged, current))


if __name__ == "__main__":
    unittest.main()
