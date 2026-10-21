"""Physical source coverage cannot establish missing business handling rules."""

import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search
from source_availability import repair_source_availability_assertions, source_availability_contradictions


class SemanticAbsenceTests(unittest.TestCase):
    def setUp(self):
        body = "MAIN.\nMOVE 1 TO AMOUNT.\nGOBACK."
        self.page = {"relative_path": "programs/rule.cbl", "source_text": body,
            "source_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "start_line": 1, "end_line": 3, "evidence_id": "ev_rule", "span_truncated": False}
        self.working_set = {"root_paths": ["programs/rule.cbl"], "source_manifest": [{
            "relative_path": "programs/rule.cbl", "sha256": self.page["source_sha256"], "line_count": 3}]}

    def test_missing_semantic_objects_are_not_source_denials(self):
        examples = (
            "如果调用在运行时发生异常，源码没有提供本程序的处理规则，不能从现有材料推出异常后的业务结果。",
            "本程序源码没有提供异常后的处理规则。",
            "当前源码没有给出错误返回后的补偿规则。",
            "rule.cbl 没有提供异常处理规则。",
            "本程序的异常处理规则在源码中没有提供。",
            "本程序源码中的错误处理逻辑未提供。",
            "当前源码没有提供返回结果保证。",
            "本程式源碼沒有提供例外處理規則。",
            "This program source has not provided exception handling rules.",
            "This program exception handling rules were not provided in the source.",
            "The handling rules in rule.cbl source were not supplied.",
            "The current source has not provided a guarantee of successful processing.",
        )
        for original in examples:
            with self.subTest(original=original):
                answer, repairs = repair_source_availability_assertions(
                    original, [self.page], self.working_set)
                self.assertEqual(answer, original)
                self.assertEqual(repairs, [])
                self.assertEqual(source_availability_contradictions(
                    original, [self.page], self.working_set), [])

    def test_actual_source_material_denials_still_repair(self):
        for original in (
            "本程序源码未提供。",
            "本轮未提供完整源码，只能部分分析。",
            "rule.cbl 的完整源码未提供。",
            "本轮未提供参数处理源码。",
            "rule.cbl 的字段处理源码未提供。",
            "The parameter processing source for rule.cbl was not supplied.",
            "The source for rule.cbl was not supplied.",
        ):
            with self.subTest(original=original):
                answer, repairs = repair_source_availability_assertions(
                    original, [self.page], self.working_set)
                self.assertNotEqual(answer, original)
                self.assertEqual(len(repairs), 1)

    def test_independent_semantic_prose_does_not_hide_a_physical_source_denial(self):
        original = "rule.cbl 源码未提供但异常处理规则仍未知。"
        answer, repairs = repair_source_availability_assertions(original, [self.page], self.working_set)
        self.assertEqual(len(repairs), 1)
        self.assertTrue(answer.endswith("但异常处理规则仍未知。"))
        original = "异常处理规则未知但rule.cbl源码未提供。"
        answer, repairs = repair_source_availability_assertions(original, [self.page], self.working_set)
        self.assertEqual(len(repairs), 1)
        self.assertTrue(answer.startswith("异常处理规则未知但"))
        original = "当前源码没有给出异常处理规则。rule.cbl 的完整源码未提供。"
        answer, repairs = repair_source_availability_assertions(original, [self.page], self.working_set)
        self.assertEqual(len(repairs), 1)
        self.assertTrue(answer.startswith("当前源码没有给出异常处理规则。"))

    def test_actual_draft_delivery_preserves_semantic_unknown_in_one_request(self):
        with tempfile.TemporaryDirectory(prefix="semantic-absence-") as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            body = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. AMOUNT-RULE.\nDATA DIVISION.\n"
                "WORKING-STORAGE SECTION.\n01 BASE-AMOUNT PIC 9(5) VALUE 10.\n"
                "01 TOTAL-AMOUNT PIC 9(6).\nPROCEDURE DIVISION.\nMAIN.\n"
                "COMPUTE TOTAL-AMOUNT = BASE-AMOUNT * 2.\nGOBACK.\n")
            (source / "rule.cbl").write_text(body, encoding="utf-8")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free",
                                 framework_reference_path="", quiet=True)
            ensure_repository_search(database, source)
            requests = []
            boundary = "如果运行时发生异常，源码没有提供本程序的处理规则，不能从现有材料推出异常后的业务结果。"

            def transport(request):
                payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
                requests.append(payload)
                page = next(page for bundle in payload["source_context"] for page in bundle["pages"]
                            if "COMPUTE TOTAL-AMOUNT" in page["source_text"])
                answer = f"金额按基础金额乘以二得到。[{page['evidence_id']}] " + boundary
                return TransportResponse(200, json.dumps({"choices": [{
                    "message": {"content": answer}, "finish_reason": "stop"}]}, ensure_ascii=False))

            with patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")):
                result = run_business_chat("rule.cbl 的金额怎么计算？", database, source,
                    CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
                                     api_key="offline-only"),
                    framework_reference_path="", transport=transport)["agent_result"]
            self.assertEqual(len(requests), 1)
            self.assertIn(boundary, result["answer"])
            self.assertFalse(result["business_review"]["answer_completion"]["source_availability_contradiction"])
            trace = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text())
            self.assertFalse(trace.get("source_availability_repairs"))


if __name__ == "__main__":
    unittest.main()
