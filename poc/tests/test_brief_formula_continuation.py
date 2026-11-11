"""Explicit continuation advances brief formula work inside existing allowances."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.extend([str(ROOT), str(ROOT / "poc")])
from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search
from run_store import RunStore


class BriefFormulaContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        for index in range(12):
            (self.source / f"calc-{index:02d}.cbl").write_text(
                f"IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRY-{index:02d}.\n"
                "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 PREMIUM-AMT PIC 9(6)V99 VALUE 0.\n"
                "01 BASIS PIC 9(5) VALUE 100.\n01 FACTOR PIC 9V99 VALUE 1.25.\n"
                f"PROCEDURE DIVISION.\nMAIN.\nCOMPUTE PREMIUM-AMT = BASIS * FACTOR + {index}.\nGOBACK.\n",
                encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True,
            quiet=True, framework_reference_path="")
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "synthetic-model", api_key="offline-only")
        self.store = RunStore(self.root / "investigation-runs.sqlite")
        self.requests = []
        self.network_guard = patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)

    @staticmethod
    def pages(payload):
        return [page for bundle in payload.get("source_context", []) for page in bundle.get("pages", [])]

    @staticmethod
    def reply(text):
        return TransportResponse(200, json.dumps({"choices": [{"message": {
            "role": "assistant", "content": text}, "finish_reason": "stop"}]}))

    def transport(self, request):
        payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
        self.requests.append(payload)
        # The responder sees only actual delivered source; fixture expectations
        # are never used to invent missing source or complete the investigation.
        lines = []
        for page in self.pages(payload):
            for statement in page["source_text"].splitlines():
                if statement.strip().startswith("COMPUTE PREMIUM-AMT ="):
                    formula = statement.strip().removeprefix("COMPUTE ").rstrip(".")
                    lines.append(f"{page['relative_path']}：{formula}。[{page['evidence_id']}] "
                        "这项保费由基础金额、计费系数及该项调整共同确定。")
        return self.reply("\n\n".join(lines))

    def invoke(self, prior=None, *, policy=None, continue_investigation=True):
        options = ({"resume_run_id": prior["run_id"], "continue_investigation": continue_investigation}
                   if prior else {})
        return run_business_chat("PREMIUM-AMT 如何计算？", self.database, self.source, self.config,
            transport=self.transport, allow_network=False, framework_reference_path="", answer_detail="brief",
            policy=policy or AgentPolicy(max_model_requests=1, max_answer_revisions=0),
            conversation_id="ordinary-formula", **options)["agent_result"]

    def saved(self, result):
        return self.store.load(result["run_id"])

    def supplied(self):
        return {page["relative_path"] for request in self.requests for page in self.pages(request)}

    def test_one_request_brief_continuations_supply_all_twelve_paths_and_retain_saved_work(self):
        expected = {f"calc-{index:02d}.cbl" for index in range(12)}
        current = self.invoke()
        first = self.saved(current)
        first_frontier = first["checkpoint"]["investigation_frontier_state"]
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.supplied()), 6, "Initial brief retrieval keeps its existing allowance")
        self.assertEqual(set(first_frontier["known_paths"]), expected)
        self.assertEqual(len(first_frontier["active_paths"]), 8)
        self.assertEqual(len(first_frontier["remaining_paths"]), 4)
        frontiers = [first_frontier]
        for _ in range(4):
            before = self.saved(current)
            old_answer = current["answer"]
            previous_requests = len(self.requests)
            current = self.invoke(current)
            after = self.saved(current)
            self.assertEqual(len(self.requests) - previous_requests, 1)
            self.assertEqual(current["metrics"]["model_requests"], 1)
            self.assertEqual(len(after["requests"]), 1)
            self.assertNotEqual(after["run_id"], before["run_id"])
            self.assertEqual(self.store.load(before["run_id"]), before)
            self.assertEqual(self.requests[previous_requests]["draft_answer"], old_answer)
            old_ids = {page["evidence_id"] for page in before["checkpoint"]["source_pages"]}
            new_ids = {page["evidence_id"] for page in after["checkpoint"]["source_pages"]}
            self.assertLessEqual(old_ids, new_ids)
            self.assertNotIn("repository_inventory_state", after["checkpoint"])
            frontier = after["checkpoint"]["investigation_frontier_state"]
            self.assertEqual(set(frontier["known_paths"]), expected)
            frontiers.append(frontier)
            if self.supplied() == expected and not frontier["remaining_paths"]:
                break
        self.assertEqual(self.supplied(), expected)
        self.assertTrue(set(first_frontier["active_paths"]) <= set(frontiers[-1]["completed_paths"]))
        self.assertFalse(set(first_frontier["active_paths"]) & set(frontiers[-1]["active_paths"]))
        self.assertEqual(set(frontiers[-1]["completed_paths"]) | set(frontiers[-1]["active_paths"]), expected)

    def test_continuation_with_disabled_local_tools_keeps_pending_work_and_request_limit(self):
        prior = self.invoke()
        before = self.saved(prior)
        supplied = self.supplied()
        current = self.invoke(prior, policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0,
            max_reads_per_turn=0, max_business_context_actions_per_turn=0))
        after = self.saved(current)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(current["metrics"]["model_requests"], 1)
        self.assertEqual(self.supplied(), supplied)
        self.assertEqual(after["checkpoint"]["investigation_frontier_state"]["known_paths"],
            before["checkpoint"]["investigation_frontier_state"]["known_paths"])
        self.assertTrue(after["checkpoint"]["investigation_frontier_state"]["remaining_paths"])
        self.assertEqual(self.store.load(before["run_id"]), before)

    def test_single_pass_brief_continuation_keeps_existing_retrieval_behavior(self):
        policy = AgentPolicy(max_model_requests=1, max_answer_revisions=0, investigation_mode="single_pass")
        prior = self.invoke(policy=policy)
        self.assertEqual(len(self.supplied()), 6)
        current = self.invoke(prior, policy=policy)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(current["metrics"]["model_requests"], 1)
        self.assertEqual(len(self.supplied()), 8, "Single-pass retains its existing restored-batch retrieval")
        self.assertEqual(len(self.saved(current)["checkpoint"]["investigation_frontier_state"]["remaining_paths"]), 4)

    def test_answer_reserve_stops_continuation_reads_without_discarding_pending_paths(self):
        prior = self.invoke()
        before = self.saved(prior)
        supplied = self.supplied()
        with patch("business_chat.QuestionBudget.should_stop_local_investigation", return_value=True):
            current = self.invoke(prior)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(current["metrics"]["model_requests"], 1)
        self.assertEqual(self.supplied(), supplied)
        self.assertEqual(self.saved(current)["checkpoint"]["investigation_frontier_state"]["known_paths"],
            before["checkpoint"]["investigation_frontier_state"]["known_paths"])
        self.assertTrue(self.saved(current)["checkpoint"]["investigation_frontier_state"]["remaining_paths"])
        self.assertEqual(self.store.load(before["run_id"]), before)

    def test_reconnect_without_explicit_continuation_replays_without_new_requests(self):
        prior = self.invoke()
        before = self.saved(prior)
        replay = self.invoke(prior, continue_investigation=False)
        self.assertEqual(replay["run_id"], prior["run_id"])
        self.assertEqual(replay["answer"], prior["answer"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.saved(prior), before)


if __name__ == "__main__":
    unittest.main()
