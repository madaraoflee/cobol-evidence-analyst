from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import _action_reply_invalid, _actions, run_business_chat
from business_index import build_business_index
from business_map import build_business_map
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search, retrieve_repository_context


CALLER = """IDENTIFICATION DIVISION.
PROGRAM-ID. ORDERFLOW.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 WRITER-STATUS PIC 99.
LINKAGE SECTION.
01 REQUEST-STATE PIC X.
01 ADDON-COUNT PIC 99.
01 ADDON-AMOUNT PIC 9(7)V99.
01 SKIP-WRITE PIC X.
01 PROCESS-STATUS PIC 99.
01 WRITE-REQUESTED PIC X.
PROCEDURE DIVISION USING REQUEST-STATE ADDON-COUNT
    ADDON-AMOUNT SKIP-WRITE PROCESS-STATUS WRITE-REQUESTED.
PROCESS-REQUEST.
    MOVE ZERO TO PROCESS-STATUS WRITER-STATUS.
    MOVE 'N' TO WRITE-REQUESTED.
    IF REQUEST-STATE NOT = 'A'
        MOVE 11 TO PROCESS-STATUS
        GOBACK
    END-IF.
    IF ADDON-COUNT = ZERO
        MOVE 12 TO PROCESS-STATUS
        GOBACK
    END-IF.
    IF SKIP-WRITE = 'Y'
        MOVE 13 TO PROCESS-STATUS
        GOBACK
    END-IF.
    IF ADDON-AMOUNT = ZERO
        MOVE 14 TO PROCESS-STATUS
        GOBACK
    END-IF.
    MOVE 'Y' TO WRITE-REQUESTED.
    CALL 'ITEMWRITE' USING
        BY CONTENT ADDON-COUNT ADDON-AMOUNT
        BY REFERENCE WRITER-STATUS
        ON EXCEPTION
            MOVE 91 TO PROCESS-STATUS
        NOT ON EXCEPTION
            MOVE WRITER-STATUS TO PROCESS-STATUS
    END-CALL.
    GOBACK.
"""

WRITER = """IDENTIFICATION DIVISION.
PROGRAM-ID. ITEMWRITE.
DATA DIVISION.
LINKAGE SECTION.
01 WRITE-COUNT PIC 99.
01 WRITE-AMOUNT PIC 9(7)V99.
01 WRITE-STATUS PIC 99.
PROCEDURE DIVISION USING WRITE-COUNT WRITE-AMOUNT WRITE-STATUS.
CHECK-ITEMS.
    MOVE ZERO TO WRITE-STATUS.
    IF WRITE-COUNT > 6
        MOVE 21 TO WRITE-STATUS
        GOBACK
    END-IF.
    IF WRITE-AMOUNT > 50000
        MOVE 22 TO WRITE-STATUS
    END-IF.
    GOBACK.
"""


class BusinessSourceNavigationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.write("programs/ORDERFLOW.CBL", CALLER)
        self.write("variants/current/ITEMWRITE.CBL", WRITER)

    def write(self, path, text):
        target = self.source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def build(self, *, duplicate_callee=False, duplicate_caller=False):
        if duplicate_callee:
            self.write("variants/archive/ITEMWRITE.CBL",
                       WRITER.replace("WRITE-COUNT > 6", "WRITE-COUNT > 4")
                             .replace("MOVE 21 TO WRITE-STATUS", "MOVE 41 TO WRITE-STATUS"))
        if duplicate_caller:
            self.write("variants/archive/ORDERFLOW.CBL",
                       CALLER.replace("MOVE 11 TO PROCESS-STATUS", "MOVE 51 TO PROCESS-STATUS"))
        build_business_index(self.source, self.database, source_format="free",
                             quiet=True, verify_content=True)
        ensure_repository_search(self.database, self.source)

    def contexts(self, question, **kwargs):
        mapping = build_business_map(self.database, self.source, question, **kwargs)
        context = retrieve_repository_context(self.database, self.source, question,
                                             max_pages=12, max_chars=32000, **kwargs)
        return mapping, context

    def assert_caller_available(self, question):
        mapping, context = self.contexts(question)
        self.assertFalse(mapping["source_identity"]["hard_constraint"])
        self.assertIn("programs/ORDERFLOW.CBL", mapping["selected_paths"])
        pages = [p for p in context["pages"] if p["relative_path"] == "programs/ORDERFLOW.CBL"]
        self.assertTrue(pages, context["source_identity"])
        self.assertTrue(any("MOVE 11 TO PROCESS-STATUS" in p["source_text"] for p in pages))

    def test_plain_caller_does_not_require_program_phrase(self):
        self.build()
        for question in (
            "ORDERFLOW 什么情况下会 skip 附加条目写 ITEMWRITE？（调用的是 ITEMWRITE.CBL）",
            "为什么 ORDERFLOW 有时候不调用 ITEMWRITE 写附加条目？",
        ):
            with self.subTest(question=question):
                self.assert_caller_available(question)

    def test_business_negation_keeps_caller_with_duplicate_callee(self):
        self.build(duplicate_callee=True)
        self.assert_caller_available(
            "ORDERFLOW 请求不是活动状态时，是不是就不调用 ITEMWRITE？（调用的是 ITEMWRITE.CBL）")

    def test_reversed_call_description_still_supplies_caller(self):
        self.build()
        for question in (
            "ITEMWRITE 由 ORDERFLOW 调用；ORDERFLOW 在哪些情况下不发起这次写入？",
            "ITEMWRITE.CBL 是 ORDERFLOW 调用的写入程序。请说明 ORDERFLOW 跳过它的条件。",
        ):
            with self.subTest(question=question):
                self.assert_caller_available(question)

    def test_model_search_can_correct_soft_focus_with_real_caller_definition(self):
        self.build(duplicate_callee=True)
        requests = []

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            if len(requests) == 1:
                content = '{"search":["ORDERFLOW"]}'
            else:
                self.assertFalse(payload["business_map"]["source_identity"]["hard_constraint"])
                self.assertEqual(payload["business_map"]["source_identity"]["selection_source"], "search_terms")
                self.assertEqual(payload["business_map"]["source_identity"]["direct_paths"],
                                 ["programs/ORDERFLOW.CBL"])
                pages = [p for bundle in payload["source_context"] for p in bundle.get("pages", [])]
                page = next(p for p in pages if p["relative_path"] == "programs/ORDERFLOW.CBL"
                            and "MOVE 11 TO PROCESS-STATUS" in p["source_text"])
                content = ("ORDERFLOW先初始化状态0、写入请求N。非A、零数量、Y标志、零金额"
                           "依次返回11、12、13、14，首个命中后不再判断且不CALL。全部通过才Y并CALL；"
                           "异常91，正常回传内部WRITER-STATUS。被调实现有多个候选，不能默选。"
                           f"[{page['evidence_id']}]")
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": content}, "finish_reason": "stop"}]}, ensure_ascii=False))

        output = run_business_chat(
            "ORDERFLOW 什么情况下不调用 ITEMWRITE？（调用的是 ITEMWRITE.CBL）",
            self.database, self.source,
            CompanyAPIConfig("https://offline.example.invalid/v1", "neutral-model", api_key="offline-test-key"),
            transport=transport, allow_network=False, framework_reference_path="",
            policy=AgentPolicy(max_model_requests=2),
        )
        self.assertEqual(len(requests), 2)
        self.assertGreaterEqual(output["agent_result"]["metrics"]["tool_calls"]["search"], 1)
        self.assertIn("全部通过", output["agent_result"]["answer"])

    def test_unknown_expansion_terms_do_not_turn_soft_focus_into_hard_absence(self):
        self.build()
        mapping, context = self.contexts(
            "ORDERFLOW 为什么不调用 ITEMWRITE？", search_terms=["附加条目未处理原因"])
        self.assertFalse(mapping["source_identity"]["hard_constraint"])
        self.assertNotEqual(mapping["source_identity"]["status"], "not_found")
        self.assertIn("programs/ORDERFLOW.CBL", {p["relative_path"] for p in context["pages"]})

    def run_subject_action(self, action, expected_path):
        requests = []

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            identity = payload["business_map"]["source_identity"]
            self.assertFalse(identity["hard_constraint"])
            if len(requests) == 1:
                self.assertEqual(identity["direct_paths"], ["programs/ORDERFLOW.CBL"])
                content = json.dumps(action)
            else:
                self.assertEqual(identity["direct_paths"], [expected_path])
                pages = [p for bundle in payload["source_context"] for p in bundle.get("pages", [])]
                page = next(p for p in pages if p["relative_path"] == expected_path)
                content = f"已读取指定程序原文，条件按顺序判断，命中后返回。[{page['evidence_id']}]"
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": content}, "finish_reason": "stop"}]}, ensure_ascii=False))

        result = run_business_chat(
            "ORDERFLOW 这个程序，什么情况下调用 ITEMWRITE（调用的是 ITEMWRITE.CBL）？",
            self.database, self.source,
            CompanyAPIConfig("https://offline.example.invalid/v1", "neutral-model", api_key="offline-test-key"),
            transport=transport, allow_network=False, framework_reference_path="",
            policy=AgentPolicy(max_model_requests=2),
        )
        self.assertEqual(len(requests), 2)
        self.assertIn("已读取指定程序原文", result["agent_result"]["answer"])
        return result

    def test_model_focus_corrects_a_resolved_soft_subject_hint(self):
        self.build()
        self.run_subject_action({"focus": ["variants/current/ITEMWRITE.CBL"]},
                                "variants/current/ITEMWRITE.CBL")

    def test_model_search_for_callee_preserves_resolved_caller_subject(self):
        self.build()
        result = self.run_subject_action({"search": ["ITEMWRITE"]}, "programs/ORDERFLOW.CBL")
        self.assertGreaterEqual(result["agent_result"]["metrics"]["tool_calls"]["search"], 1)

    def test_mixed_focus_paths_are_rejected_atomically(self):
        self.build()
        self.run_subject_action(
            {"focus": ["variants/current/ITEMWRITE.CBL", "missing/ITEMWRITE.CBL"]},
            "programs/ORDERFLOW.CBL")

    def test_rejected_focus_keeps_the_last_confirmed_focus(self):
        self.build()
        requests = []
        selected = "variants/current/ITEMWRITE.CBL"

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            identity = payload["business_map"]["source_identity"]
            if len(requests) == 1:
                self.assertEqual(identity["direct_paths"], ["programs/ORDERFLOW.CBL"])
                content = json.dumps({"focus": [selected]})
            elif len(requests) == 2:
                self.assertEqual(identity["direct_paths"], [selected])
                content = '{"focus":["missing/ITEMWRITE.CBL"]}'
            else:
                self.assertEqual(identity["direct_paths"], [selected])
                self.assertEqual(identity["selection_source"], "focus")
                pages = [p for bundle in payload["source_context"] for p in bundle.get("pages", [])]
                page = next(p for p in pages if p["relative_path"] == selected)
                content = f"数量超过6时返回21；否则再判断金额上限。[{page['evidence_id']}]"
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": content}, "finish_reason": "stop"}]}, ensure_ascii=False))

        output = run_business_chat(
            "ORDERFLOW 这个程序，什么情况下调用 ITEMWRITE（调用的是 ITEMWRITE.CBL）？",
            self.database, self.source,
            CompanyAPIConfig("https://offline.example.invalid/v1", "neutral-model", api_key="offline-test-key"),
            transport=transport, allow_network=False, framework_reference_path="",
            policy=AgentPolicy(max_model_requests=3),
        )
        self.assertEqual(len(requests), 3)
        self.assertIn("数量超过6", output["agent_result"]["answer"])

    def test_malformed_focus_and_zero_search_budget_never_execute_focus(self):
        valid_path = "variants/current/ITEMWRITE.CBL"
        for paths in ([valid_path, None], [valid_path] * 9, [], ["x" * 501]):
            with self.subTest(paths=paths):
                text = json.dumps({"focus": paths})
                action = _actions(text)
                self.assertIsNone(action)
                self.assertTrue(_action_reply_invalid(text, action))
        policy = AgentPolicy(max_model_requests=2, max_searches_per_turn=0)
        focus_reply = json.dumps({"focus": [valid_path]})
        self.assertIsNone(_actions(focus_reply, policy))
        self.assertTrue(_action_reply_invalid(focus_reply, None))
        self.build()
        requests = []

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            self.assertEqual(payload["business_map"]["source_identity"]["direct_paths"],
                             ["programs/ORDERFLOW.CBL"])
            if len(requests) == 1:
                content = focus_reply
            else:
                pages = [p for bundle in payload["source_context"] for p in bundle.get("pages", [])]
                page = next(p for p in pages if p["relative_path"] == "programs/ORDERFLOW.CBL")
                content = f"请求非活动时返回11，不调用写入程序。[{page['evidence_id']}]"
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": content}, "finish_reason": "stop"}]}, ensure_ascii=False))

        output = run_business_chat(
            "ORDERFLOW 这个程序，什么情况下调用 ITEMWRITE（调用的是 ITEMWRITE.CBL）？",
            self.database, self.source,
            CompanyAPIConfig("https://offline.example.invalid/v1", "neutral-model", api_key="offline-test-key"),
            transport=transport, allow_network=False, framework_reference_path="", policy=policy)
        self.assertTrue(requests)
        self.assertEqual(output["agent_result"]["metrics"]["tool_calls"]["search"], 0)

    def test_missing_exact_path_is_not_replaced_by_search_or_history(self):
        self.build()
        mapping, context = self.contexts(
            "请说明 missing/ORDERFLOW.CBL 为什么不写附加条目。",
            search_terms=["programs/ORDERFLOW.CBL"], prior_paths=["programs/ORDERFLOW.CBL"])
        for identity in (mapping["source_identity"], context["source_identity"]):
            self.assertTrue(identity["hard_constraint"])
            self.assertEqual(identity["status"], "not_found")
            self.assertEqual(identity["direct_paths"], [])
        self.assertEqual(context["pages"], [])

    def test_exact_callee_version_path_cannot_mix_another_definition(self):
        self.build(duplicate_callee=True)
        for chosen, other, expected, forbidden in (
            ("current", "archive", "MOVE 21 TO WRITE-STATUS", "MOVE 41 TO WRITE-STATUS"),
            ("archive", "current", "MOVE 41 TO WRITE-STATUS", "MOVE 21 TO WRITE-STATUS"),
        ):
            with self.subTest(chosen=chosen):
                path = f"variants/{chosen}/ITEMWRITE.CBL"
                mapping, context = self.contexts(f"请说明 {path} 的拒绝条件。",
                    search_terms=[f"variants/{other}/ITEMWRITE.CBL"])
                self.assertTrue(mapping["source_identity"]["hard_constraint"])
                self.assertEqual(mapping["source_identity"]["direct_paths"], [path])
                self.assertEqual({p["relative_path"] for p in context["pages"]}, {path})
                supplied = "\n".join(p["source_text"] for p in context["pages"])
                self.assertIn(expected, supplied)
                self.assertNotIn(forbidden, supplied)

    def test_duplicate_callee_is_not_declared_a_unique_implementation(self):
        self.build(duplicate_callee=True)
        mapping, context = self.contexts("请解释 ITEMWRITE 内部拒绝写入的条件。")
        identity = mapping["source_identity"]
        self.assertEqual(identity["status"], "ambiguous")
        candidates = {path for candidate in identity["candidates"] for path in candidate["relative_paths"]}
        self.assertEqual(candidates, {"variants/current/ITEMWRITE.CBL", "variants/archive/ITEMWRITE.CBL"})
        self.assertNotEqual(len(identity["direct_paths"]), 1)
        for page in context["pages"]:
            self.assertIn(page["source_text"], (self.source / page["relative_path"]).read_text())

    def test_duplicate_caller_is_disambiguated_only_by_exact_path(self):
        self.build(duplicate_caller=True)
        for path, expected, forbidden in (
            ("programs/ORDERFLOW.CBL", "MOVE 11 TO PROCESS-STATUS", "MOVE 51 TO PROCESS-STATUS"),
            ("variants/archive/ORDERFLOW.CBL", "MOVE 51 TO PROCESS-STATUS", "MOVE 11 TO PROCESS-STATUS"),
        ):
            with self.subTest(path=path):
                mapping, context = self.contexts(f"请说明 {path} 的跳过调用条件。")
                self.assertTrue(mapping["source_identity"]["hard_constraint"])
                self.assertEqual(mapping["source_identity"]["direct_paths"], [path])
                caller_pages = [p for p in context["pages"] if "PROGRAM-ID. ORDERFLOW." in p["source_text"]]
                self.assertTrue(caller_pages)
                self.assertEqual({p["relative_path"] for p in caller_pages}, {path})
                supplied = "\n".join(p["source_text"] for p in caller_pages)
                self.assertIn(expected, supplied)
                self.assertNotIn(forbidden, supplied)


if __name__ == "__main__":
    unittest.main()
