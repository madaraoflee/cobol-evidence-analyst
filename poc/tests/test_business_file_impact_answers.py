"""Offline contracts for transmitted file evidence and bounded answer recovery.

Fake transport replies check literal visibility and orchestration only; they do
not evaluate a model or certify a runtime LF/PF mapping.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from business_synthesis import assess_answer_completion
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search
from structural_index import build_structural_index


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "file-impact-v1"
QUESTION = "DEBITFLOW 扣减流程影响哪些 LF 和哪些 field？"
LIMITATION = "目前不足以可靠列出 LF 和字段名。"


def pages(payload):
    return [page for bundle in payload["source_context"] for page in bundle["pages"]]


def response(text):
    return TransportResponse(200, json.dumps({"choices": [{"message": {
        "role": "assistant", "content": text}, "finish_reason": "stop"}]}, ensure_ascii=False))


class BusinessFileImpactAnswerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="file-impact-answer-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for folder in ("programs", "copybooks", "dds"):
            shutil.copytree(FIXTURE / folder, self.source / folder)
        self.database = self.root / "index.sqlite"
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "neutral-model",
                                       api_key="offline-only")
        self.requests = []
        for target, name in ((socket, "create_connection"), (socket.socket, "connect"),
                             (socket.socket, "connect_ex")):
            patcher = mock.patch.object(target, name, side_effect=AssertionError("offline test forbids network"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self):
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)

    def assert_request_sources(self, payload):
        supplied = pages(payload)
        identifiers = {page["evidence_id"] for page in supplied}
        for page in supplied:
            physical = (self.source / page["relative_path"]).read_text(encoding="utf-8")
            self.assertEqual(page["source_text"], "\n".join(
                physical.splitlines()[page["start_line"] - 1:page["end_line"]]))
            self.assertEqual(page["source_sha256"], hashlib.sha256(physical.encode()).hexdigest())
        observations = payload["question_investigation"].get("file_impact", {}).get("observations", [])
        for observation in observations:
            self.assertLessEqual(set(observation["evidence_ids"]), identifiers)
            for membership in observation.get("record_memberships", []):
                self.assertLessEqual(set(membership["evidence_ids"]), identifiers)
        return supplied, observations

    def ask(self, reply, *, question=QUESTION, policy=None, answer_detail="detailed"):
        self.requests.clear()

        def transport(request):
            envelope = json.loads(request.body)
            self.assertNotIn("tools", envelope)
            payload = json.loads(envelope["messages"][-1]["content"])
            self.assert_request_sources(payload)
            self.requests.append(payload)
            return response(reply(payload, len(self.requests)))

        return run_business_chat(question, self.database, self.source, self.config,
            transport=transport, allow_network=False, capture_context=True,
            framework_reference_path="", policy=policy or AgentPolicy(max_model_requests=3),
            answer_detail=answer_detail)

    def visible_answer(self, payload, number):
        supplied, observations = self.assert_request_sources(payload)
        text = "\n".join(page["source_text"] for page in supplied)
        for literal in ("REWRITE ACCTREC", "SUBTRACT DEBIT-AMOUNT FROM ACCTBAL",
                        "MOVE 'D' TO ACCTSTAT", "READ RATE-FILE", "WRITE AUDITREC"):
            self.assertIn(literal, text)
        writes = [row for row in observations if row["kind"] == "io_operation" and row["operation"] == "REWRITE"]
        self.assertEqual(writes[0]["file_name"], "ACCOUNT-FILE")
        self.assertEqual(writes[0]["assigned_name"], "ACCOUNTLF")
        self.assertFalse(writes[0]["external_identity_verified"])
        ref = writes[0]["evidence_ids"][0]
        return (f"已知 ACCOUNT-FILE 的 ACCTREC 被重写，ACCTBAL 扣减金额，ACCTSTAT 设为 D。[{ref}] "
                "RATE-FILE 是只读依赖；AUDITPOST 中 AUDIT-FILE 的 AUDITREC 有写入语句，属于间接影响候选。"
                "ACCOUNTLF 是 ASSIGN 对象，DDS 文件名匹配仅为候选；EXTERNALPOST 的副作用和动态目标尚未知。")

    def test_explicit_and_language_only_questions_supply_direct_readonly_and_indirect_evidence(self):
        self.build()
        for question in (QUESTION, "扣减流程影响哪些 lf 和哪些 field？",
                         "DEBITFLOW affects which files and fields?"):
            with self.subTest(question=question):
                output = self.ask(self.visible_answer, question=question)
                self.assertEqual(output["runner_status"], "COMPLETED")
                self.assertIn("ACCTBAL", output["agent_result"]["answer"])
                supplied = pages(self.requests[-1])
                self.assertIn("dds/ACCOUNTLF.dds", {page["relative_path"] for page in supplied})
                observations = self.requests[-1]["question_investigation"]["file_impact"]["observations"]
                self.assertTrue(any(row["kind"] == "dds_pfile" and
                    row["physical_file_names"] == ["ACCOUNTPF"] for row in observations))
                dependencies = [row for row in observations if row["kind"] == "dependency_reference"]
                self.assertTrue(dependencies)
                self.assertFalse(self.requests[-1]["question_investigation"]["file_impact"]["scope"]["runtime_paths_verified"])

    def test_missing_dds_keeps_known_files_and_fields_as_a_partial_answer(self):
        shutil.rmtree(self.source / "dds")
        self.build()
        output = self.ask(self.visible_answer)
        result = output["agent_result"]
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("ACCTBAL", result["answer"])
        self.assertTrue(any(gap["kind"] == "dds_definitions" for gap in
            result["investigation_state"]["question_investigation"]["open_gaps"]))
        self.assertFalse(any(row["kind"] == "dds_pfile" for row in
            self.requests[-1]["question_investigation"]["file_impact"]["observations"]))

    def test_blanket_enumeration_limitation_uses_one_review_with_known_evidence(self):
        self.build()
        output = self.ask(lambda payload, number: LIMITATION if number == 1 else self.visible_answer(payload, number))
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[1]["draft_answer"], LIMITATION)
        result = output["agent_result"]
        self.assertTrue(result["business_review"]["synthesis_review_attempted"])
        self.assertIn("ACCTBAL", result["answer"])
        self.assertNotEqual(result["business_review"]["answer_completion"]["status"], "incomplete")

    def test_repeated_blanket_limitation_is_never_marked_a_successful_analysis(self):
        self.build()
        output = self.ask(lambda payload, number: LIMITATION)
        result = output["agent_result"]
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["stop_reason"], "answer_incomplete")
        self.assertNotEqual(output["reason_code"], "BUSINESS_CHAT_COMPLETED")
        self.assertEqual(len(self.requests), 2)

    def test_generic_update_without_requested_identifiers_is_reviewed(self):
        self.build()
        output = self.ask(lambda payload, number: "该流程会执行扣减并更新记录。" if number == 1
                          else self.visible_answer(payload, number))
        self.assertEqual(len(self.requests), 2)
        self.assertIn("file_identifiers", self.requests[1]["answer_review"]["missing_aspects"])
        self.assertIn("field_identifiers", self.requests[1]["answer_review"]["missing_aspects"])
        self.assertIn("ACCTBAL", output["agent_result"]["answer"])

    def test_brief_one_request_still_supplies_files_and_retains_known_partial_answer(self):
        self.build()
        policy = AgentPolicy(max_model_requests=1)
        output = self.ask(self.visible_answer, policy=policy, answer_detail="brief")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertIn("ACCTBAL", output["agent_result"]["answer"])
        self.assertLessEqual(output["agent_result"]["metrics"]["source_characters"], policy.max_source_characters)
        self.assertLessEqual(output["agent_result"]["metrics"]["request_bytes"][0], policy.max_request_bytes)

    def test_structural_index_can_answer_known_io_without_sparse_arithmetic_rules(self):
        build_structural_index(self.source, self.database, source_format="free", quiet=True)
        ensure_repository_search(self.database, self.source)
        output = self.ask(self.visible_answer, policy=AgentPolicy(max_model_requests=2))
        self.assertTrue(self.requests[-1]["question_investigation"]["can_answer"])
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertIn("ACCTBAL", output["agent_result"]["answer"])

    def test_tight_budgets_only_report_observations_from_surviving_pages(self):
        self.build()
        policy = AgentPolicy(max_model_requests=1, max_source_characters=512,
            initial_source_characters=512, max_request_bytes=32768,
            max_reads_per_turn=1, read_source_characters=512)
        output = self.ask(lambda payload, number: "已供应的局部赋值是静态候选，缺失文件布局仍待补读。",
                          policy=policy)
        self.assertEqual(len(self.requests), 1)
        self.assertLessEqual(sum(len(page["source_text"]) for page in pages(self.requests[0])), 512)
        self.assertLessEqual(output["agent_result"]["metrics"]["request_bytes"][0], 32768)
        self.assertEqual(output["agent_result"]["status"], "PARTIAL")
        self.assert_request_sources(self.requests[0])

    def test_enumeration_language_does_not_discard_a_concrete_known_answer(self):
        for answer in (LIMITATION, "我無法可靠列出 LF 和欄位名。", "Cannot reliably list LF and field names."):
            self.assertEqual(assess_answer_completion(answer)["status"], "incomplete")
        answer = "ACCOUNT-FILE 写入 ACCTREC，ACCTBAL 扣减金额；无法可靠列出其他 LF 和字段名。"
        self.assertNotEqual(assess_answer_completion(answer)["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
