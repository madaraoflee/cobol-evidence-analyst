"""Explicit reads may fit an entire source line within the existing total allowance."""

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
import repository_discovery
import test_business_chat as fixtures


class LongLineReadBudgetTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BusinessChatTests(
            methodName="test_model_can_read_a_deep_specific_branch_instead_of_analyzing_every_page")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        blocked = patch("socket.socket.connect", side_effect=AssertionError("Real network forbidden"))
        blocked.start()
        self.addCleanup(blocked.stop)

    def exercise_read(self, target_lines, *, read_characters=12000, source_characters=36000):
        fixture = self.fixture
        # Keep the file larger than the complete-root allowance so this tests
        # actual read actions rather than the small-file whole-source path.
        path = fixture.write("long.cbl", "LONG-WORK", "*> REQUEST-GATE entry\n" +
            ("*> unrelated context " + "x" * 180 + "\n") * 1200 +
            "\n".join(target_lines), "01 FLAG PIC X.\n01 REQUEST-STATE PIC X(12).")
        source = path.read_text(encoding="utf-8").splitlines()
        first = source.index(target_lines[0]) + 1
        last = first + len(target_lines) - 1
        self.assertGreater(path.stat().st_size, 128000)
        fixture.build()
        calls = []
        original = repository_discovery.read_repository_context

        def observed(*args, **kwargs):
            context = original(*args, **kwargs)
            calls.append({"start_line": kwargs.get("start_line"),
                "end_line": kwargs.get("end_line"), "max_chars": kwargs.get("max_chars"),
                "range_complete": context.get("range_complete"),
                "next_start_line": context.get("next_start_line"),
                "pages": context["pages"], "raw_fallback": context.get("raw_fallback", {})})
            return context

        def respond(payload, envelope):
            if len(fixture.requests) == 1:
                return fixtures.reply(json.dumps({"read": [{"relative_path": "long.cbl",
                    "start_line": first, "end_line": last}]}))
            pages = fixtures.source_pages(payload)
            ref = next((page["evidence_id"] for page in pages if "MOVE 'REVIEW'" in page["source_text"]),
                       pages[0]["evidence_id"])
            return fixtures.reply(f"处理进入复核步骤。[{ref}]")

        policy = AgentPolicy(max_model_requests=2, max_answer_revisions=0,
            max_business_context_actions_per_turn=0, read_source_characters=read_characters,
            max_source_characters=source_characters)
        with patch.object(repository_discovery, "read_repository_context", side_effect=observed):
            result = fixture.ask("REQUEST-GATE 的重试处理规则？", respond, policy=policy)["agent_result"]
        self.assertEqual(len(fixture.requests), 2)
        self.assertEqual(result["metrics"]["model_requests"], 2)
        self.assertEqual(result["metrics"]["policy"]["read_source_characters"], read_characters)
        self.assertEqual(result["metrics"]["policy"]["max_source_characters"], source_characters)
        relevant = [call for call in calls if call["start_line"] == first and call["end_line"] == last]
        for call in relevant:
            self.assertLessEqual(call["max_chars"], source_characters)
            for page in call["pages"]:
                self.assertEqual(page["source_text"], "\n".join(source[page["start_line"] - 1:page["end_line"]]))
                self.assertFalse(page.get("span_truncated"))
        payload = json.loads(fixture.requests[-1]["messages"][-1]["content"])
        return relevant, fixtures.source_pages(payload), first, result

    def assert_whole_long_line(self, repetitions):
        condition = "IF FLAG = 'Y' " + "OR FLAG = 'Y' " * repetitions
        calls, pages, _, _ = self.exercise_read([condition, "MOVE 'REVIEW' TO REQUEST-STATE", "END-IF."])
        self.assertEqual([call["max_chars"] for call in calls], [12000, 36000])
        self.assertFalse(calls[0]["range_complete"])
        self.assertTrue(calls[1]["range_complete"])
        self.assertTrue(any(condition in page["source_text"] for page in pages),
                        "The provider must receive the entire condition, not only its consequent")

    def test_fifteen_kilobyte_condition_is_supplied_whole(self):
        self.assert_whole_long_line(1100)

    def test_thirty_kilobyte_condition_is_supplied_whole(self):
        self.assert_whole_long_line(2200)

    def test_line_larger_than_total_allowance_remains_local_gap_without_repeated_expansion(self):
        condition = "IF FLAG = 'Y' " + "OR FLAG = 'Y' " * 3000
        calls, pages, first, result = self.exercise_read([condition, "MOVE 'REVIEW' TO REQUEST-STATE", "END-IF."])
        self.assertEqual([call["max_chars"] for call in calls], [12000, 36000])
        self.assertTrue(all(not call["range_complete"] and call["next_start_line"] == first for call in calls))
        self.assertFalse(any(condition in page["source_text"] for page in pages))
        self.assertNotEqual(result["stop_reason"], "sufficient_material")

    def test_ordinary_window_pagination_does_not_expand_read_allowance(self):
        lines = [f"DISPLAY 'RECORD-{index:03d}'." for index in range(100)]
        calls, _, first, _ = self.exercise_read(lines, read_characters=512, source_characters=4096)
        self.assertTrue(calls)
        self.assertTrue(all(call["max_chars"] == 512 for call in calls))
        self.assertFalse(calls[0]["range_complete"])
        self.assertGreater(calls[0]["next_start_line"], first)


if __name__ == "__main__":
    unittest.main()
