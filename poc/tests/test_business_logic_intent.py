"""Offline business-logic questions retain the existing bounded step investigation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from business_map import build_business_map
from company_api import APIClientError, CompanyAPIConfig
from question_investigation import build_question_investigation
from repository_discovery import ensure_repository_search


class BusinessLogicIntentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.text = (
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. BILL-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 BASE-AMOUNT PIC 9(7) VALUE 10.\n"
            "01 FEE-AMOUNT PIC 9(7) VALUE 2.\n"
            "01 BILL-AMOUNT PIC 9(7) VALUE ZERO.\n"
            "01 BILL-LOGIC PIC X VALUE 'Y'.\n"
            "PROCEDURE DIVISION.\nMAIN.\n"
            "IF BILL-LOGIC = 'Y'\n"
            "COMPUTE BILL-AMOUNT = BASE-AMOUNT + FEE-AMOUNT\n"
            "ELSE\nMOVE ZERO TO BILL-AMOUNT\nEND-IF.\nGOBACK.\n"
        )
        (self.source / "billing.cbl").write_text(self.text, encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", quiet=True,
                             framework_reference_path="")
        ensure_repository_search(self.database, self.source)

    def investigate(self, question, *, complete=False):
        lines = self.text.splitlines()
        end = len(lines) if complete else 4
        page = {"relative_path": "billing.cbl", "start_line": 1, "end_line": end,
                "source_text": "\n".join(lines[:end]),
                "source_sha256": hashlib.sha256((self.source / "billing.cbl").read_bytes()).hexdigest(),
                "evidence_id": "ev_logic_source", "selection_reasons": ["question_match"]}
        return build_question_investigation(question,
            build_business_map(self.database, self.source, question),
            database_path=self.database, source_pages=[page])

    def test_logic_questions_plan_missing_business_steps_in_all_supported_languages(self):
        for question in ("出 bill 的逻辑是什么？", "出 bill 的邏輯是什麼？",
                         "说说 bill 这段逻辑", "bill 逻辑里怎样跳过不出账的记录？",
                         "What is the bill logic?"):
            with self.subTest(question=question):
                result = self.investigate(question)
                step = next(item for item in result["required_items"] if item["kind"] == "business_steps")
                self.assertGreater(step["candidate_count"], 0)
                self.assertGreater(step["missing_count"], 0)
                self.assertFalse(result["can_answer"])
                self.assertTrue(any(action["reason"] == "business_steps_source_not_supplied"
                                    for action in result["planned_actions"]))
                self.assertLessEqual(len(result["planned_actions"]), 4)

    def test_complete_source_does_not_create_unnecessary_step_reads(self):
        result = self.investigate("出 bill 的逻辑是什么？", complete=True)
        step = next(item for item in result["required_items"] if item["kind"] == "business_steps")
        self.assertEqual(step["status"], "SATISFIED")
        self.assertTrue(result["can_answer"])
        self.assertEqual(result["planned_actions"], [])

    def test_single_field_facts_do_not_request_a_business_flow_investigation(self):
        for question in ("BILL-AMOUNT 的字段类型是什么？", "BILL-LOGIC 的初值是什么？",
                         "BILL-LOGIC 这个逻辑标志是什么类型？",
                         "BILL-LOGIC 這個邏輯標誌是什麼類型？",
                         "BILL-LOGIC 这个逻辑字段的长度是多少？",
                         "BILL-LOGIC 这个逻辑变量的初值是什么？",
                         "BILL-LOGIC 的逻辑类型是什么？",
                         "What is the data type of BILL-LOGIC?"):
            with self.subTest(question=question):
                result = self.investigate(question)
                self.assertEqual(result["required_items"], [])
                self.assertEqual(result["planned_actions"], [])

    def test_actual_request_contains_step_obligations_for_the_natural_question(self):
        captured = []

        def transport(request):
            captured.append(json.loads(json.loads(request.body)["messages"][-1]["content"]))
            raise APIClientError("OFFLINE_CAPTURE_ONLY")

        config = CompanyAPIConfig("https://gateway.example.invalid/v1", "neutral-model",
                                  api_key="synthetic-credential")
        with mock.patch("socket.create_connection", side_effect=AssertionError("offline only")):
            run_business_chat("出 bill 的逻辑是什么？", self.database, self.source, config,
                transport=transport, framework_reference_path="", policy=AgentPolicy(max_model_requests=1))
        self.assertEqual(len(captured), 1)
        payload = captured[0]
        self.assertIn("business_steps", {item["kind"]
            for item in payload["question_investigation"]["required_items"]})
        supplied = "\n".join(page["source_text"] for bundle in payload["source_context"]
                             for page in bundle["pages"])
        self.assertIn("COMPUTE BILL-AMOUNT = BASE-AMOUNT + FEE-AMOUNT", supplied)


if __name__ == "__main__":
    unittest.main()
