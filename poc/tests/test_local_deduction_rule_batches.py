"""Exercise extra batches of matching source members through a real local conversation."""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_grep_acceptance as acceptance


class LocalDeductionRuleBatchTests(unittest.TestCase):
    def replay_with_keyword_matches(self, *, extra_step="", add_notices=False, report_extra_step=None):
        scenario = "insufficient_then_grep"
        files = acceptance.originals(scenario)
        files["z-target/start.cbl"] = files.pop("start.cbl")
        for i in range(56):
            body = extra_step if i == 55 else ""
            files[f"x-unit-{i:03}.cbl"] = (
                f'IDENTIFICATION DIVISION.\nPROGRAM-ID. MEMBER-{i}.\nPROCEDURE DIVISION.\n'
                f'DISPLAY "reserve refund".\n{body}GOBACK.').encode()
        if add_notices:
            for i in range(56, 65):
                files[f"x-unit-{i:03}.cbl"] = (
                    f'IDENTIFICATION DIVISION.\nPROGRAM-ID. NOTICE-{i}.\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n'
                    '01 DISPLAY-FLAG PIC X.\n01 MESSAGE-TEXT PIC X(80).\nPROCEDURE DIVISION.\n'
                    'IF DISPLAY-FLAG = "Y"\n MOVE "reserve refund" TO MESSAGE-TEXT\nEND-IF.\nGOBACK.').encode()
        if report_extra_step is not None:
            from test_local_scale_report_gaps import REPORT
            for i in range(9):
                report = REPORT.replace("BANNER.", f"BANNER{i}.")
                if i == 0:
                    report = report.replace("GOBACK.", report_extra_step + "GOBACK.")
                files[f"hint{i}.cbl"] = report.encode()
        with patch.object(acceptance, "originals", return_value=files):
            return acceptance.replay(scenario)

    def test_checked_deduction_exhausts_empty_batches_without_requesting_the_same_answer_again(self):
        result = self.replay_with_keyword_matches()
        replies = [row["reply"] for row in result["requests"]]
        self.assertEqual(replies[:3], [acceptance.INSUFFICIENT, {"grep": ["PROGRAM-ID. ADJ731"]},
                                       {"grep": ["PROGRAM-ID. CUT526"]}])
        self.assertEqual(len(replies), 4)
        self.assertIn("剩 50", replies[-1])
        self.assertEqual(result["status"], "completed", result["stop_reason"])
        cited_paths = {row["relative_path"] for row in result["citations"] if row["evidence_id"] in result["answer"]}
        self.assertEqual(cited_paths, {"z-target/start.cbl", "legacy/route.cbl", "archive/settlement.cbl"})

    def test_already_supplied_complete_notice_programs_do_not_repeat_the_verified_answer(self):
        result = self.replay_with_keyword_matches(add_notices=True)
        requests = result["requests"]
        self.assertEqual(len(requests), 4)
        self.assertEqual(result["status"], "completed", result["stop_reason"])
        visible = {row["path"] for row in requests[-1]["supplied_pages"]}
        self.assertTrue({f"x-unit-{i:03}.cbl" for i in range(56, 65)} <= visible)
        self.assertIn("剩 50", requests[-1]["reply"])

    def test_every_report_counter_input_is_checked_before_finishing_on_the_first_complete_reply(self):
        result = self.replay_with_keyword_matches(add_notices=True, report_extra_step="")
        self.assertEqual(result["status"], "completed", result["stop_reason"])
        requests = result["requests"]
        self.assertEqual(len(requests), 4)
        self.assertIn("剩 50", requests[-1]["reply"])
        self.assertIn("80", result["answer"])
        visible = {row["path"] for row in requests[-1]["supplied_pages"]}
        self.assertTrue({f"hint{i}.cbl" for i in range(9)} <= visible)

    def test_an_unexplained_amount_mixed_into_report_counters_still_reaches_the_model(self):
        result = self.replay_with_keyword_matches(add_notices=True, report_extra_step=(
            'IF LATE-FEE > 0\n SUBTRACT LATE-FEE FROM OTHER-DUE\nEND-IF.\n'))
        requests = result["requests"]
        self.assertIn("剩 50", requests[3]["reply"])
        self.assertGreater(len(requests), 4)
        self.assertTrue(any(page["path"] == "hint0.cbl" for request in requests[4:]
                            for page in request["supplied_pages"]))
        self.assertEqual(result["status"], "partial")

    def test_time_reserve_during_local_batch_checks_retains_the_answer_and_pending_work(self):
        from local_literal_call_path import prove_literal_call_path
        from question_budget import QuestionBudget

        reserve_reached = False
        original_stop = QuestionBudget.should_stop_local_investigation

        def verify_then_reach_reserve(*args, **kwargs):
            nonlocal reserve_reached
            result = prove_literal_call_path(*args, **kwargs)
            reserve_reached = bool(result)
            return result

        with patch("local_literal_call_path.prove_literal_call_path", side_effect=verify_then_reach_reserve), patch.object(
                QuestionBudget, "should_stop_local_investigation", lambda budget: reserve_reached or original_stop(budget)):
            result = self.replay_with_keyword_matches()
        self.assertTrue(reserve_reached)
        self.assertEqual(len(result["requests"]), 4)
        self.assertEqual(result["status"], "partial")
        self.assertIn("CREDIT-AMT", result["answer"])
        self.assertTrue(result["inline_citations"])

    def test_a_real_deduction_in_the_last_batch_still_reaches_the_model_and_stays_open(self):
        result = self.replay_with_keyword_matches(extra_step=(
            'IF ITEM-TYPE = "CREDIT"\n SUBTRACT ALT-AMT FROM NET-DUE\nEND-IF.\n'))
        requests = result["requests"]
        self.assertIn("剩 50", requests[3]["reply"])
        self.assertGreater(len(requests), 4)
        self.assertTrue(any(page["path"] == "x-unit-055.cbl" for request in requests[4:]
                            for page in request["supplied_pages"]))
        self.assertEqual(result["status"], "partial")
