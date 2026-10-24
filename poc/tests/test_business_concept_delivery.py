"""Offline delivery and saved-result checks; scripted replies are not quality scores."""

from copy import deepcopy
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search
from run_store import RunStore
from test_business_concept_pipeline import EXPLANATION, SHALLOW, SOURCES


class BusinessConceptDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for relative, text in SOURCES.items():
            (self.source / relative).write_text(text, encoding="utf-8")
        self.database = self.root / "index.sqlite"
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.store = RunStore(self.root / "investigation-runs.sqlite")
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model",
                                       api_key="offline-only")
        self.question = "什么是 active contract"
        for owner, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                            (socket, "create_connection")):
            guard = patch.object(owner, name, side_effect=AssertionError("network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def invoke(self, replies, *, policy=None, history=None, resume_run_id=None):
        requests = []

        def transport(request):
            self.assertIsNone(resume_run_id, "A completed result must replay without a new request.")
            wire = json.loads(request.body)
            payload = json.loads(wire["messages"][-1]["content"])
            requests.append({"bytes": len(request.body), "payload": payload})
            pages = [page for context in payload.get("source_context", ())
                     for page in context.get("pages", ())]
            content = replies[min(len(requests) - 1, len(replies) - 1)]
            content += " " + " ".join(f"[{page['evidence_id']}]" for page in pages)
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": content},
                "finish_reason": "stop"}]}))

        arguments = {"policy": policy or AgentPolicy(max_model_requests=4, max_answer_revisions=1),
                     "framework_reference_path": "", "conversation_id": "concept-delivery",
                     "history": history or [], "transport": transport, "allow_network": False}
        if resume_run_id is not None:
            arguments["resume_run_id"] = resume_run_id
        output = run_business_chat(self.question, self.database, self.source, self.config, **arguments)
        return output, requests

    def test_completed_explanation_is_saved_and_replayed_without_changing_prior_history(self):
        history = [{"role": "user", "content": "上次的业务问题"},
                   {"role": "assistant", "content": "保留这条以前的业务说明。", "status": "ANALYZED"}]
        original_history = deepcopy(history)
        output, requests = self.invoke([SHALLOW, EXPLANATION], history=history)
        result = output["agent_result"]
        self.assertEqual(len(requests), 2)
        self.assertIn("1050", result["answer"])
        self.assertNotIn("还需要查看", result["answer"])
        self.assertEqual(result["narrative"]["text"], result["answer"])
        self.assertTrue(result["model_answer_recorded"])
        saved = self.store.load(result["run_id"])
        self.assertEqual(saved["state"], "completed")
        self.assertEqual(saved["checkpoint"]["result"], json.loads(json.dumps(output)))
        self.assertFalse(saved["uncertain_requests"])
        replayed, replay_requests = self.invoke([], history=history, resume_run_id=result["run_id"])
        self.assertEqual(replay_requests, [])
        self.assertEqual(replayed, json.loads(json.dumps(output)))
        self.assertEqual(self.store.load(result["run_id"]), saved)
        self.assertEqual(history, original_history)

    def test_minimum_request_budget_keeps_business_task_through_actual_delivery(self):
        policy = AgentPolicy(max_request_bytes=32768, max_model_requests=4, max_answer_revisions=1)
        output, requests = self.invoke([SHALLOW, EXPLANATION], policy=policy)
        self.assertEqual(len(requests), 2, output["agent_result"])
        for request in requests:
            self.assertLessEqual(request["bytes"], policy.max_request_bytes)
            contract = request["payload"]["response_contract"]["business_explanation"]
            text = json.dumps(contract).lower()
            for concept in ("newcomer", "purpose", "supplied"):
                self.assertIn(concept, text)
            pages = [page for context in request["payload"]["source_context"] for page in context["pages"]]
            source_text = "\n".join(page["source_text"] for page in pages)
            self.assertIn("COMPUTE OUTPUT-AMOUNT", source_text)
            self.assertIn("DISPLAY REPORT-AMOUNT", source_text)
        result = output["agent_result"]
        self.assertIn("1050", result["answer"])
        self.assertFalse(result["answer_completion"]["pending_investigation"])
        self.assertEqual(result["stop_reason"], "sufficient_material")

    def test_repeated_deferral_retains_received_body_in_saved_result(self):
        policy = AgentPolicy(max_model_requests=3, max_answer_revisions=1)
        output, requests = self.invoke([SHALLOW], policy=policy)
        result = output["agent_result"]
        self.assertGreaterEqual(len(requests), 2)
        self.assertLessEqual(len(requests), policy.max_model_requests)
        self.assertNotEqual(result["stop_reason"], "sufficient_material")
        self.assertIn("源码注释将 PROJECTION", result["answer"])
        self.assertTrue(result["answer_available"])
        self.assertTrue(result["model_answer_recorded"])
        saved = self.store.load(result["run_id"])
        self.assertEqual(saved["checkpoint"]["result"]["agent_result"]["answer"], result["answer"])
        self.assertFalse(saved["uncertain_requests"])

    def prepare_external_implementation_boundary(self):
        entry = SOURCES["entry.cbl"].replace("01 PROJECTED-AMOUNT PIC 9(8)V99.",
            "01 PROJECTED-AMOUNT PIC 9(8)V99.\n01 STATUS-CODE PIC 9.")
        entry = entry.replace("CALL 'REPORTER' USING PROJECTED-AMOUNT.",
            "CALL 'EXTERNAL-ENGINE' USING PROJECTED-AMOUNT STATUS-CODE.\n"
            "IF STATUS-CODE = 0\nCALL 'REPORTER' USING PROJECTED-AMOUNT\nEND-IF.")
        (self.source / "entry.cbl").write_text(entry, encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        return (
            "这项合同投影处理先计算预计金额，再根据外部服务返回的状态决定是否展示结果。"
            "PROJECTION 将合同金额交给 PROJECTOR，后者按输入金额乘 1.05 计算，"
            "通过第二个参数返回 PROJECTED-AMOUNT。主程序将该金额和状态字段交给 EXTERNAL-ENGINE；"
            "返回后仅在 STATUS-CODE 为 0 时调用 REPORTER，后者用 DISPLAY 展示收到的金额。"
            "若要确认外部引擎内部算法，还需要查看未提供的外部源码；本程序按返回状态决定是否输出结果。"
        )
    def test_missing_external_algorithm_does_not_restart_complete_visible_explanation(self):
        explanation = self.prepare_external_implementation_boundary()
        output, requests = self.invoke([explanation])
        result = output["agent_result"]
        frontier = [item for context in requests[0]["payload"]["source_context"]
                    for item in context.get("working_set", {}).get("frontier", ())]
        self.assertTrue(any(item.get("target_name") == "EXTERNAL-ENGINE"
                            and item.get("reason") == "external_implementation_unavailable"
                            for item in frontier), frontier)
        self.assertEqual(len(requests), 1, {"stop_reason": result["stop_reason"],
            "completion": result["answer_completion"], "frontier": frontier})
        self.assertIn("外部引擎内部算法", result["answer"])
        self.assertIn("STATUS-CODE", result["answer"])
        self.assertFalse(result["answer_completion"]["pending_investigation"])
        self.assertFalse(result["business_review"]["synthesis_review_attempted"])

    def test_external_boundary_alone_still_requests_visible_business_explanation(self):
        explanation = self.prepare_external_implementation_boundary()
        boundary_only = "若要解释这项业务，还需要查看未提供的外部源码。"
        output, requests = self.invoke([boundary_only, explanation])
        result = output["agent_result"]
        self.assertEqual(len(requests), 2, result["stop_reason"])
        self.assertIn(boundary_only, requests[1]["payload"]["draft_answer"])
        pages = [page for context in requests[1]["payload"]["source_context"]
                 for page in context["pages"]]
        self.assertEqual({page["relative_path"] for page in pages}, set(SOURCES))
        source_text = "\n".join(page["source_text"] for page in pages)
        self.assertIn("COMPUTE OUTPUT-AMOUNT", source_text)
        self.assertIn("IF STATUS-CODE = 0", source_text)
        self.assertIn("DISPLAY REPORT-AMOUNT", source_text)
        self.assertIn("PROJECTION 将合同金额交给 PROJECTOR", result["answer"])
        self.assertIn("返回后仅在 STATUS-CODE 为 0 时调用 REPORTER", result["answer"])
        self.assertNotIn(boundary_only, result["answer"])
        self.assertFalse(result["answer_completion"]["pending_investigation"])
        self.assertTrue(result["business_review"]["synthesis_review_attempted"])
        saved = self.store.load(result["run_id"])
        self.assertEqual(saved["checkpoint"]["result"]["agent_result"]["answer"], result["answer"])
        self.assertFalse(saved["uncertain_requests"])


if __name__ == "__main__":
    unittest.main()
