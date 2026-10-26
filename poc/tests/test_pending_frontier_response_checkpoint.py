"""An interrupted received action retains older work without replaying it twice."""
from dataclasses import replace
from contextlib import nullcontext
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]
from business_chat import run_business_chat
from company_api import TransportResponse
from run_store import RunStore
import test_business_action_budget as fixtures


class PendingFrontierResponseCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.BusinessActionBudgetTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def recover(self, *, fresh, action, at_checkpoint=False):
        case = self.case
        requests = []

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            if len(requests) == 1:
                text = json.dumps(action)
            else:
                page = next(page for page in case.pages(payload)
                            if page["relative_path"] == "detail.cbl")
                self.assertIn("COMPUTE FINAL-AMOUNT = EXTRA-AMOUNT * 3.", page["source_text"])
                text = f"补充程序将附加金额乘以三，得到最终金额。[{page['evidence_id']}]"
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text},
                                                                   "finish_reason": "stop"}]}))

        def progress(event):
            if requests and event.get("phase") == "retrieving":
                raise RuntimeError("paused after received action")

        options = dict(transport=transport, framework_reference_path="", answer_detail="brief",
            conversation_id="pending-frontier-response",
            policy=replace(case.policy, max_model_requests=4))
        question = "VALUEFLOW 的金额如何计算？"
        checkpoint_writer = RunStore.checkpoint

        def interrupt_saved_action(store, run_id, checkpoint):
            checkpoint_writer(store, run_id, checkpoint)
            if requests and checkpoint.get("frontier_response_round"):
                raise RuntimeError("paused after received action")

        interruption = (patch.object(RunStore, "checkpoint", interrupt_saved_action)
                        if at_checkpoint else nullcontext())
        with interruption, self.assertRaisesRegex(RuntimeError, "paused after received action"):
            run_business_chat(question, case.database, case.source, case.config, progress=progress,
                investigation_seed={"frontier": [{"tool": "focus", "arguments": [path]}
                    for path in ("rule.cbl", "detail.cbl")]}, **options)
        store = RunStore(case.root / "investigation-runs.sqlite")
        with store.connect() as db:
            run_id = db.execute("SELECT run_id FROM investigation_runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
        checkpoint = store.load(run_id)["checkpoint"]
        self.assertEqual(checkpoint["frontier"][0], {"tool": "focus", "arguments": ["detail.cbl"]})
        self.assertTrue(any(row["tool"] in action for row in checkpoint["frontier"][1:]))
        output = run_business_chat(question, case.database, case.source, case.config,
            resume_run_id=run_id, start_new_investigation=fresh, **options)
        result = output["agent_result"]
        self.assertTrue(result["model_answer_recorded"])
        self.assertEqual(len(requests), 2)
        self.assertEqual(sum(row.get("focus") == ["detail.cbl"]
            for row in requests[-1]["completed_actions"]), 1)
        self.assertIn("附加金额乘以三", result["answer"])
        return requests[-1], result

    def test_explicit_retry_retains_older_pending_focus_and_new_navigation(self):
        payload, _ = self.recover(fresh=True, action={"search_concepts": "amount"})
        self.assertEqual(sum(row.get("search_concepts") == "amount"
            for row in payload["completed_actions"]), 1)

    def test_received_response_replay_executes_new_navigation_once(self):
        payload, _ = self.recover(fresh=False, action={"search_concepts": "amount"})
        self.assertEqual(sum(row.get("search_concepts") == "amount"
            for row in payload["completed_actions"]), 1)

    def test_received_search_deferred_by_remaining_allowance_survives_checkpoint(self):
        self.recover(fresh=True, action={"search": ["DETAILRULE"]}, at_checkpoint=True)


if __name__ == "__main__":
    unittest.main()
