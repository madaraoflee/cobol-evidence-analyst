"""Observe actual actions, original source and delivered evidence in an ordinary local question."""
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_grep_acceptance import INSUFFICIENT, WRONG_FIELD, replay


class LocalGrepDeliveryTests(unittest.TestCase):
    @staticmethod
    def requests_for(result, target):
        return [number for number, row in enumerate(result["requests"])
                if isinstance(row["reply"], dict) and target in row["reply"].get("grep", [])]

    def assert_reached_deduction(self, result):
        requests = result["requests"]
        self.assertEqual(result["network_connections"], 0)
        self.assertEqual(result["status"], "completed", result["stop_reason"])
        during = result["while_waiting"]
        self.assertEqual(during["job_status"], "RUNNING")
        self.assertEqual(during["message_roles"], ["user"])
        self.assertTrue(any("CREDIT-AMT" in f["text"] and "1-RESERVECREDIT" in f["text"]
                            and any(source["relative_path"] == "start.cbl" for source in f["sources"])
                            for f in during["checked_findings"]))
        initial_paths = {p["path"] for p in requests[0]["supplied_pages"]}
        self.assertIn("start.cbl", initial_paths)
        self.assertFalse({"legacy/route.cbl", "archive/settlement.cbl"} & initial_paths)
        first_target = self.requests_for(result, "PROGRAM-ID. ADJ731")
        second_target = self.requests_for(result, "PROGRAM-ID. CUT526")
        self.assertTrue(first_target, "the model did not search for the entry program's assigned target")
        self.assertTrue(second_target, "the search never reached the second name, found only in the dispatcher")
        self.assertLess(first_target[0], second_target[0])
        second_paths = {p["path"] for p in requests[second_target[0]]["supplied_pages"]}
        self.assertIn("legacy/route.cbl", second_paths)
        for target, path in (("PROGRAM-ID. ADJ731", "legacy/route.cbl"),
                             ("PROGRAM-ID. CUT526", "archive/settlement.cbl")):
            actions = [a for row in requests for a in row["grep_actions"] if a.get("target") == target]
            self.assertTrue(actions, "literal was not actually searched: " + target)
            self.assertTrue(any(step["term"] == target and step.get("hit_file") == path and step["hit_line"] == 2
                                for row in requests for step in row["visible_search_steps"]),
                            "verified search location was not published while the answer was pending")
            self.assertTrue(any(a.get("complete") and a.get("checked_files") == len(result["files"])
                                and {"relative_path": path, "line": 2} in a.get("matches", []) for a in actions))
            self.assertTrue(any(p["path"] == path and p["id"] in a.get("read_ids", [])
                                for row in requests for a in row["grep_actions"] if a.get("target") == target
                                for p in row["supplied_pages"]), "hit was not opened from the source: " + path)
        answer = result["answer"]
        business = re.sub(r"\[ev_page_[a-f0-9]+\]|[\s`]", "", answer)
        for field in ("ITEM-TYPE", "1-RESERVECREDIT", "CREDIT-AMT", "NET-DUE", "POOL-AMOUNT", "REMAINING-DUE"):
            self.assertIn(field, business)
        reserve = business.index("1-RESERVECREDIT")
        refund = business.index("POOL-AMOUNT", reserve)
        self.assertRegex(business[reserve:refund], "跳过|不扣|保留")
        self.assertRegex(business[reserve:refund], r"大于(?:0|零)|为正|>0")
        self.assertRegex(business[reserve:refund], r"(?:否则|小于(?:或)?等于[0零]|不大于[0零]).{0,40}(?:跳过|保留|不调用)")
        self.assertRegex(business[refund:], r"严格小于|小于(?!或?等于)|[<]([^=]|$)")
        self.assertRegex(business[refund:], r"(?:相等或更大|大于(?:或)?等于|不(?:严格)?小于|>=|≥).{0,45}(?:跳过|不扣|保留|保持)")
        self.assertRegex(business, r"最终.{0,25}(?:应收|金额).{0,90}(?:减去|扣除|减掉|扣减)")
        self.assertRegex(business, r"100.{0,70}20.{0,70}30.{0,60}(?<!\d)50(?!\d)")
        self.assertRegex(business, r"80.{0,60}(?<!\d)80(?!\d)")
        self.assertNotIn(INSUFFICIENT, answer)
        self.assertEqual({ref["relative_path"] for ref in result["citations"]
                          if ref["evidence_id"] in result["inline_citations"]},
                         {"start.cbl", "legacy/route.cbl", "archive/settlement.cbl"})
        for path, required_line in (("start.cbl", 8), ("legacy/route.cbl", 4), ("archive/settlement.cbl", 5)):
            self.assertTrue(any(ref["relative_path"] == path and ref["evidence_id"] in answer
                                and ref["start_line"] <= required_line <= ref["end_line"]
                                for ref in result["citations"]), "missing cited source line in " + path)

    def test_insufficient_reply_triggers_repository_grep_then_source_led_search(self):
        result = replay("insufficient_then_grep")
        self.assertEqual(result["requests"][0]["reply"], INSUFFICIENT)
        self.assertTrue(any(row["grep_actions"] for row in result["requests"][1:]),
                        "source insufficiency ended or recycled the existing draft without grep")
        self.assert_reached_deduction(result)

    def test_model_can_follow_two_literal_searches_and_reopen_the_original_citations(self):
        result = replay("explicit_grep")
        self.assertEqual(result["requests"][0]["reply"], {"grep": ["PROGRAM-ID. ADJ731"]})
        self.assert_reached_deduction(result)

    def test_false_fields_and_arithmetic_are_corrected_from_the_three_read_programs(self):
        result = replay("wrong_amount_then_checked")
        wrong = [row["reply"] for row in result["requests"]
                 if isinstance(row["reply"], str) and WRONG_FIELD in row["reply"] and "1000" in row["reply"]]
        self.assertTrue(wrong, "the inaccurate draft never reached the real answer path")
        self.assertNotIn(WRONG_FIELD, result["answer"])
        self.assertNotIn("1000", result["answer"])
        self.assert_reached_deduction(result)

    def test_missing_second_target_retains_caller_rules_and_a_specific_evidence_gap(self):
        result = replay("missing_implementation")
        target = "PROGRAM-ID. CUT526"
        self.assertTrue(self.requests_for(result, target))
        searches = [a for row in result["requests"] for a in row["grep_actions"] if a.get("target") == target]
        self.assertTrue(searches)
        self.assertTrue(any(a.get("complete") and a.get("checked_files") == len(result["files"]) for a in searches))
        self.assertTrue(all(not a.get("matches") and not a.get("read_ids") for a in searches))
        self.assertEqual(result["status"], "partial")
        self.assertIn("1-RESERVECREDIT", result["answer"])
        self.assertIn("CUT526", result["answer"])
        self.assertNotIn("POOL-AMOUNT", result["answer"])
        self.assertNotIn("剩 50", result["answer"])
        self.assertNotIn('"grep"', result["answer"])
        self.assertFalse(any(p["relative_path"] == "archive/settlement.cbl" for p in result["citations"]))
        self.assertEqual(result["network_connections"], 0)
