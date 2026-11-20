"""Local user-visible progress, deduction coverage and completion on the same source question."""
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from local_progressive_acceptance import DRAFT, FULL, INCLUSIVE, replay


class LocalProgressiveDeliveryTests(unittest.TestCase):
    def assert_completed(self, run, *, inclusive=False):
        self.assertEqual(run["network_connections"], 0)
        self.assertEqual(run["job_status"], "COMPLETED")
        self.assertEqual(run["status"], "completed", run["stop_reason"])
        self.assertEqual(run["message_roles"], ["user", "assistant"])
        self.assertFalse(run["remaining_interim_findings"])
        self.assertEqual({row["relative_path"] for row in run["citations"]}, {"cash.cbl", "return.cbl"})
        self.assertEqual({row["relative_path"]: (row["start_line"], row["end_line"])
                          for row in run["requests"][0]["pages"]}, {"cash.cbl": (1, 10), "return.cbl": (1, 7)})
        text = re.sub(r"[\s`\"]", "", run["answer"])
        for word in ("1-RESERVECREDIT", "CREDIT-AMT", "NET-DUE", "APPLYREFUND", "REFUND-BALANCE", "跳过", "最终"):
            self.assertIn(word, text)
        qualify = text.index("1-RESERVECREDIT")
        call = text.index("大于0", qualify)
        refund = text.index("REFUND-BALANCE", call)
        self.assertLess(qualify, call)
        self.assertLess(call, refund)
        self.assertRegex(text[qualify:call], "其他类型跳过准备金|ITEM-TYPE不等于.{0,30}跳过")
        self.assertRegex(text[call:refund], "否则跳过退款|小于或等于0.{0,8}跳过整个子程序")
        self.assertRegex(text, "最终.{0,35}(初始应收|进入流程时的金额).{0,8}减去.{0,8}实际")
        self.assertIn("按引用", text)
        applied = "小于或等于" if inclusive else "(?:严格)?小于(?!或等于)"
        skipped = "只有更大才保留|大于DUE.{0,8}跳过" if inclusive else "相等或更大则保留|大于或等于DUE.{0,8}跳过"
        self.assertRegex(text[refund:], "REFUND-BALANCE" + applied + "DUE")
        self.assertRegex(text[refund:], skipped)

    def test_shallow_reply_is_revised_after_a_cited_interim_finding(self):
        run = replay("shallow_then_complete")
        during = run["while_first_reply_held"]
        self.assertEqual(during["message_roles"], ["user"])
        self.assertFalse(during["model_draft"])
        findings = during["checked_findings"]
        self.assertTrue(findings, "already supplied full original programs but had no checked rule to show")
        main = [f for f in findings if any(s["relative_path"] == "cash.cbl" for s in f["sources"])]
        self.assertTrue(any("1-RESERVECREDIT" in f["text"] and "CREDIT-AMT" in f["text"] for f in main))
        self.assertGreaterEqual(len(run["requests"]), 2, "accepted the model's one-sentence shallow answer")
        self.assertEqual(run["requests"][0]["scripted_answer"], DRAFT)
        self.assertTrue(any(row["has_revision_draft"] and row["requests_order_skips_and_result"]
                            for row in run["requests"][1:]))
        self.assert_completed(run)

    def test_already_complete_reply_is_delivered_once_as_completed(self):
        run = replay("already_complete")
        self.assert_completed(run)
        self.assertEqual(len(run["requests"]), 1)

    def test_changed_equality_rejects_the_old_rule_and_retains_the_new_result(self):
        run = replay("changed_equality")
        self.assertGreaterEqual(len(run["requests"]), 2)
        self.assertEqual(run["requests"][0]["scripted_answer"], FULL)
        self.assertEqual(run["requests"][-1]["scripted_answer"], INCLUSIVE)
        self.assert_completed(run, inclusive=True)
        self.assertNotIn("退款 80 仍为 80", run["answer"])

    def assert_preserved_partial(self, run):
        self.assertEqual(run["status"], "partial")
        self.assertEqual(set(run["originals"]), {"cash.cbl"})
        self.assertTrue(run["stop_reason"])
        self.assertTrue(run["citations"])
        for source in run["citations"]:
            self.assertEqual(source["relative_path"], "cash.cbl")
        text = run["answer"]
        self.assertIn("1-RESERVECREDIT", text)
        self.assertIn("跳过", text)
        self.assertIn("最终应收", text)
        self.assertRegex(text, "待核|待查|未核|尚需|仍需|仍缺|还需|未确认")
        self.assertNotIn("退款 80", run["answer"])

    def test_missing_callee_cannot_be_marked_complete(self):
        self.assert_preserved_partial(replay("missing_callee"))

    def test_single_request_budget_keeps_checked_findings_beside_a_partial_reply(self):
        run = replay("one_request_missing_callee")
        self.assertEqual(len(run["requests"]), 1)
        self.assert_preserved_partial(run)

    def test_single_request_budget_allows_a_complete_source_proven_reply(self):
        run = replay("one_request_complete_sources")
        self.assertEqual(len(run["requests"]), 1)
        self.assertEqual(run["requests"][0]["scripted_answer"], DRAFT)
        self.assert_completed(run)

    def test_missing_callee_cannot_publish_an_invented_refund_rule_or_amount(self):
        run = replay("unsupported_refund_missing_callee")
        self.assertGreaterEqual(len(run["requests"]), 2)
        self.assertTrue(all(row["scripted_answer"] == INCLUSIVE for row in run["requests"]))
        self.assert_preserved_partial(run)
        text = re.sub(r"\[ev_page_[a-f0-9]+\]", "", run["answer"])
        self.assertIn("APPLYREFUND", text)
        self.assertNotIn("REFUND-BALANCE", text)
        self.assertNotIn("退款余额小于或等于", text)
        self.assertNotRegex(text, r"\b(?:30|50|80|100)\b")


if __name__ == "__main__":
    unittest.main()
