"""Same-question delivery regression, with a deliberately incomplete model reply."""
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from online_progressive_acceptance import DRAFT, replay


class OnlineProgressiveDeliveryTests(unittest.TestCase):
    def run_scenario(self, *, variant=False, pause=False):
        with tempfile.TemporaryDirectory() as root, patch("socket.socket.connect",
                side_effect=AssertionError("network prohibited")) as network:
            result = replay(variant, pause, Path(root))
            network.assert_not_called()
            return result["states"]

    def assert_first_finding(self, state):
        self.assertTrue(state["has_pending_preview"], "first original was read but no interim business text")
        self.assertEqual(state["assistant_messages"], 0, "interim finding committed as a separate final answer")
        self.assertEqual(len({c["path"] for c in state["sources"]}), 1)
        self.assertIn("减去", state["answer"])
        self.assertIn("跳过", state["answer"])
        self.assertNotIn(DRAFT, state["answer"])
        self.assertNotIn("最终", state["answer"], "one side of the call chain is still missing")

    def assert_complete(self, state, *, variant=False):
        self.assertEqual(state["status"], "answered")
        self.assertEqual(state["assistant_messages"], 1)
        self.assertFalse(state["has_pending_preview"])
        paths = {"bill.cbl", "rebate.cbl"} if variant else {"cash.cbl", "return.cbl"}
        self.assertEqual({citation["path"] for citation in state["sources"]}, paths)
        text = re.sub(r'[`"\s]', "", state["answer"])
        account = "OPEN-SUM" if variant else "NET-DUE"
        pool = "RETURN-POOL" if variant else "REFUND-BALANCE"
        worker = "NET-LEFT" if variant else "DUE"
        credit = "RESERVE-UNITS" if variant else "CREDIT-AMT"
        code = "CLASS-CODE" if variant else "ITEM-TYPE"
        tag = "7-RESERVE" if variant else "1-RESERVECREDIT"
        floor = "12" if variant else "0"
        subject = code + "等于" + tag
        enter = account + "大于" + floor
        take = pool + ("小于或等于" if variant else "小于") + worker
        skip = pool + ("大于" if variant else "大于或等于") + worker
        self.assertIn(subject, text)
        self.assertIn(enter, text)
        self.assertIn(take, text)
        self.assertLess(text.index(subject), text.index(enter))
        self.assertLess(text.index(enter), text.index(take))
        for condition in (code + "不等于" + tag, account + "小于或等于" + floor, skip):
            with self.subTest(condition=condition):
                self.assertRegex(text, re.escape(condition) + "[^。]{0,30}跳过")
        self.assertIn("从" + account + "中减去" + credit, text)
        self.assertIn("从" + worker + "中减去" + pool, text)
        self.assertIn("最终" + account, text)
        self.assertRegex(text, "减去各步实际生效的扣减额|减去实际扣减")
        self.assertIn("按引用", text)
        self.assertIn("保留进入时的金额", text)

    def test_first_visible_finding_then_complete_deduction_answer(self):
        states = self.run_scenario()
        self.assert_first_finding(states["after_first_verified_read"])
        self.assertTrue(states["waiting_for_final_reply"]["has_pending_preview"])
        self.assert_complete(states["delivered"])

    def test_explicit_resume_preserves_early_finding_and_delivers_once(self):
        states = self.run_scenario(pause=True)
        self.assert_first_finding(states["after_first_verified_read"])
        limited = states["budget_reached"]
        self.assertEqual(limited["status"], "needs_continuation")
        self.assertTrue(limited["has_pending_preview"])
        self.assertTrue(limited["sources"])
        self.assertTrue(states["waiting_for_final_reply"]["has_pending_preview"])
        self.assert_complete(states["delivered"])

    def test_changed_names_threshold_and_inclusive_comparison_change_final_rule(self):
        states = self.run_scenario(variant=True)
        self.assert_first_finding(states["after_first_verified_read"])
        self.assert_complete(states["delivered"], variant=True)
        self.assertNotIn("NET-DUE", states["delivered"]["answer"])
        self.assertNotIn("REFUND-BALANCE", states["delivered"]["answer"])


if __name__ == "__main__":
    unittest.main()
