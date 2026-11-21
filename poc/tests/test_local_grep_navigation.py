"""Regression tests for reading all physical hit locations within the ordinary question budgets."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_index import build_business_index
from local_source_grep import grep_repository
from repository_discovery import ensure_repository_search


class LocalGrepNavigationTests(unittest.TestCase):
    def setUp(self):
        from test_business_discovery_recovery import BusinessDiscoveryRecoveryTests
        self.case = BusinessDiscoveryRecoveryTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def build(self):
        build_business_index(self.case.source, self.case.database, source_format="free")
        ensure_repository_search(self.case.database, self.case.source)

    def ask(self, *, max_reads, max_requests=12, progress=None):
        def respond(payload):
            for page in payload["source_context"][0]["pages"]:
                if "SUBTRACT POOL FROM AMOUNT" in page["source_text"]:
                    return f'POOL 小于 AMOUNT 时扣除该笔款项，否则跳过。[{page["evidence_id"]}]'
            return '{"grep":["ADJ731"]}'
        return self.case.ask(respond, policy=AgentPolicy(max_reads_per_turn=max_reads, max_model_requests=max_requests),
                             progress=progress)

    def noisy_files(self, count):
        for index in range(count):
            self.case.write(f"noise-{index:02}.cbl", f"NOTICE-{index}", 'DISPLAY "ADJ731".')
        self.case.write("z-worker.cbl", "ADJ731", "IF POOL < AMOUNT\n SUBTRACT POOL FROM AMOUNT\nEND-IF.")
        self.build()

    def test_repeated_literal_opens_undelivered_matches_before_scanning_the_next_page(self):
        self.noisy_files(13)
        events = []
        def scan(*args, **kwargs):
            result = grep_repository(*args, **kwargs)
            events.append(result)
            return result
        with patch("local_source_grep.grep_repository", side_effect=scan):
            out = self.ask(max_reads=3)
        self.assertEqual(len(events), 2)
        self.assertTrue(events[0]["next_cursor"])
        self.assertTrue(events[1]["complete"])
        self.assertEqual(len(self.case.requests), 6)
        rows = [next(r for r in reversed(p["completed_actions"]) if r.get("tool") == "grep")
                for p in self.case.requests[1:]]
        self.assertEqual([r.get("pending_hit_count", 0) for r in rows], [9, 6, 3, 0, 0])
        self.assertTrue(all(not set(r) & {"next_cursor", "pending_hits"} for r in rows))
        self.assertEqual([len(r["read_ids"]) for r in rows], [3, 3, 3, 3, 2])
        self.assertEqual(out["agent_result"]["metrics"]["tool_calls"]["read"], 14)
        self.assertIn("否则跳过", out["agent_result"]["answer"])
        self.assertTrue(any(p["relative_path"] == "z-worker.cbl" and "SUBTRACT POOL FROM AMOUNT" in p["source_text"]
                            for p in self.case.requests[-1]["source_context"][0]["pages"]))

    def test_distant_hits_in_one_file_are_opened_while_neighbors_share_one_read(self):
        body = '\n'.join(['DISPLAY "ADJ731".', 'DISPLAY "ADJ731".', *(['CONTINUE.'] * 110),
                          'DISPLAY "ADJ731".', 'IF POOL < AMOUNT', ' SUBTRACT POOL FROM AMOUNT', 'END-IF.'])
        self.case.write("local.cbl", "ROUTE", body)
        self.build()
        result = self.ask(max_reads=2, max_requests=2)
        self.assertEqual(len(self.case.requests), 2)
        row, = [r for r in self.case.requests[-1]["completed_actions"] if r.get("tool") == "grep"]
        self.assertEqual(len(row["matches"]), 3)
        self.assertEqual(len(row["read_ids"]), 2)
        self.assertTrue(row["complete"])
        self.assertFalse(row.get("pending_hit_count"))
        self.assertEqual(result["agent_result"]["metrics"]["tool_calls"]["read"], 2)
        self.assertIn("否则跳过", result["agent_result"]["answer"])

    def test_repeated_search_passes_already_supplied_noise_without_exhausting_model_rounds(self):
        self.case.write("a-noise.cbl", "NOTICE", "\n".join(['DISPLAY "ADJ731".'] * 90))
        self.case.write("z-worker.cbl", "ADJ731", "IF POOL < AMOUNT\n SUBTRACT POOL FROM AMOUNT\nEND-IF.")
        self.build()
        events = []
        def respond(payload):
            if len(self.case.requests) == 1:
                return '{"read":[{"relative_path":"a-noise.cbl","start_line":1,"end_line":120}]}'
            for page in payload["source_context"][0]["pages"]:
                if "SUBTRACT POOL FROM AMOUNT" in page["source_text"]:
                    return f'POOL 小于 AMOUNT 时扣除，否则跳过。[{page["evidence_id"]}]'
            return '{"grep":["ADJ731"]}'

        out = self.case.ask(respond, policy=AgentPolicy(max_model_requests=4), progress=events.append)
        self.assertEqual(len(self.case.requests), 4)
        self.assertIn("否则跳过", out["agent_result"]["answer"])
        first = next(r for r in self.case.requests[2]["completed_actions"] if r.get("tool") == "grep")
        self.assertTrue(first["has_more"])
        self.assertTrue(first["read_ids"], "the first explicit lookup must still return its original")
        last = next(r for r in reversed(self.case.requests[3]["completed_actions"]) if r.get("tool") == "grep")
        self.assertEqual(last["already_supplied_matches"], 72)
        self.assertTrue(last["complete"])
        scans = [e["source_grep"] for e in events if "source_grep" in e]
        self.assertEqual(sum(row["matches"] for row in scans), 91)

    def test_prior_partial_file_does_not_allow_skipping_batches_of_hit_lines(self):
        self.case.write("a-noise.cbl", "NOTICE", "\n".join(['DISPLAY "ADJ731".'] * 90))
        self.build()
        def respond(payload):
            if len(self.case.requests) == 1:
                return '{"read":[{"relative_path":"a-noise.cbl","start_line":1,"end_line":65}]}'
            if len(self.case.requests) <= 3:
                return '{"grep":["ADJ731"]}'
            return "本段只显示通知。"

        self.case.ask(respond, policy=AgentPolicy(max_model_requests=4))
        rows = [r for r in self.case.requests[-1]["completed_actions"] if r.get("tool") == "grep"]
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["matches"][0]["line"] for r in rows], [8, 20])
        self.assertTrue(rows[-1]["read_ids"])
        self.assertNotIn("already_supplied_matches", rows[-1])

    def test_cancelled_question_resumes_unread_hit_locations_without_rescanning_the_library(self):
        self.noisy_files(6)
        class Paused(RuntimeError):
            pass

        def pause(event):
            if event.get("phase") == "waiting_for_model" and event.get("model_requests") == 2:
                raise Paused("stop before second model request")

        with self.assertRaises(Paused):
            self.ask(max_reads=2, progress=pause)
        self.assertEqual(len(self.case.requests), 1)
        with patch("local_source_grep.grep_repository", side_effect=AssertionError("scanned known locations again")):
            out = self.ask(max_reads=2)
        self.assertIn("否则跳过", out["agent_result"]["answer"])
        rows = [next(r for r in reversed(payload["completed_actions"]) if r.get("tool") == "grep")
                for payload in self.case.requests[1:]]
        self.assertEqual([r.get("pending_hit_count", 0) for r in rows], [5, 3, 1, 0])
        self.assertEqual(len({ref for r in rows for ref in r["read_ids"]}), 7)

    def test_model_request_limit_preserves_unread_locations_without_overstating_source(self):
        self.noisy_files(7)
        out = self.ask(max_reads=1, max_requests=2)
        self.assertEqual(len(self.case.requests), 2)
        row, = [r for r in self.case.requests[-1]["completed_actions"] if r.get("tool") == "grep"]
        self.assertTrue(row["complete"])
        self.assertEqual(len(row["read_ids"]), 1)
        self.assertEqual(row["pending_hit_count"], 7)
        self.assertEqual(out["agent_result"]["metrics"]["tool_calls"]["read"], 1)
        self.assertNotIn("否则跳过", out["agent_result"]["answer"])
        self.assertFalse(any(p["relative_path"] == "z-worker.cbl" for p in
                             self.case.requests[-1]["source_context"][0]["pages"]))
