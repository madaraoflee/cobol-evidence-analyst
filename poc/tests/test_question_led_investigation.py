from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyze_source import analyze_source
from business_investigation import search_suggestions
from company_api import CompanyAPIConfig, TransportResponse


def reply(text):
    return TransportResponse(200, json.dumps({"choices": [{"message": {
        "role": "assistant", "content": text}, "finish_reason": "stop"}]}, ensure_ascii=False))


class QuestionLedInvestigationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "output"
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="local-test-key")
        self.reference = self.root / "reference.md"
        self.reference.write_text("# Record operations\n## Fetching a row\n"
                                  "FETCHROW requests a record by the supplied key. A zero status means a row was returned.\n"
                                  "## Holding a slot\nHOLDROW requests a reservation; inspect the returned status before proceeding.\n", encoding="utf-8")

    def write(self, path, name, body, data=""):
        target = self.source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
                          f"WORKING-STORAGE SECTION.\n{data}\nPROCEDURE DIVISION.\nMAIN.\n{body}\nGOBACK.\n", encoding="utf-8")

    def fixture(self):
        self.write("entry.cbl", "ENTRY", "CALL 'ALLOCATE' USING REQUEST-DATA.")
        self.write("allocation.cbl", "ALLOCATE", "MOVE 'FETCHROW' TO ACTION-CODE.\n"
                   "CALL 'REMOTE-STORE' USING ACTION-CODE REQUEST-DATA RETURN-STATUS.\n"
                   "IF RETURN-STATUS = ZERO AND HOLD-DAYS <= HOLD-LIMIT\n"
                   "MOVE 'RESERVED' TO REQUEST-STATE\nEND-IF.",
                   "01 HOLD-LIMIT PIC 99 VALUE 14.\n01 HOLD-DAYS PIC 99.\n01 ACTION-CODE PIC X(12).\n"
                   "01 RETURN-STATUS PIC 9.\n01 REQUEST-STATE PIC X(12).\nCOPY SHARED-DATA.")
        self.write("configuration.cbl", "CONFIGURE", "MOVE 14 TO WINDOW-DAYS.", "01 WINDOW-DAYS PIC 99.")
        self.write("unrelated.cbl", "PACKAGING", "DISPLAY PACKAGE-WEIGHT.", "COPY SHARED-DATA.\n01 PACKAGE-WEIGHT PIC 9(8).")
        (self.source / "SHARED-DATA.cpy").write_text("01 REQUEST-DATA.\n05 REQUEST-KEY PIC X(12).\n", encoding="utf-8")

    def run_question(self, question, transport):
        report = analyze_source(self.source, self.output, question=question, index_mode="catalog",
                                analysis_mode="business", source_format="free", reading_strategy="full_chain",
                                config=self.config, transport=transport, allow_network=True,
                                framework_reference_path=self.reference, capture_api_responses=True)
        result = json.loads((self.output / "agent-result.json").read_text(encoding="utf-8"))
        return report, result

    def test_question_is_translated_and_refined_from_actual_repository_excerpts(self):
        self.fixture()
        requests, search_payloads, source_paths = [], [], set()

        def transport(request):
            payload = json.loads(request.body)
            requests.append(payload)
            self.assertNotIn("tools", payload)
            self.assertNotIn("response_format", payload)
            supplied = json.loads(payload["messages"][-1]["content"])
            if supplied.get("stage") == "repository_search":
                search_payloads.append(supplied)
                if len(search_payloads) == 1:
                    return reply('{"queries":["HOLD-LIMIT"]}')
                if len(search_payloads) == 2:
                    excerpts = json.dumps(supplied["findings"], ensure_ascii=False)
                    self.assertIn("HOLD-LIMIT", excerpts)
                    self.assertIn("REMOTE-STORE", excerpts)
                    return reply("WINDOW-DAYS")
                return reply("DONE")
            refs = supplied.get("source_pages") or supplied.get("page_summaries") or []
            for page in supplied.get("source_pages", []):
                source_paths.add(page["relative_path"])
            citations = " ".join(f"[{page['evidence_id']}]" for page in refs if page.get("evidence_id"))
            return reply("领取安排先取得记录，返回成功且保留天数未超过上限时设为已预留；上限为14天。" + citations)

        report, runner = self.run_question("延后领取的安排由哪些条件决定？", transport)
        self.assertEqual(runner["runner_status"], "COMPLETED", report)
        self.assertIsNone(report["selected_entry"])
        self.assertEqual(report["scope"]["mode"], "repository_question")
        answer = runner["agent_result"]
        self.assertEqual(len(search_payloads), 3)
        self.assertEqual(source_paths, {"entry.cbl", "allocation.cbl", "configuration.cbl", "SHARED-DATA.cpy"})
        self.assertIn("14天", answer["answer"])
        self.assertEqual(answer["investigation"]["repository_file_count"], 5)
        self.assertEqual(answer["investigation"]["selected_file_count"], 4)
        self.assertTrue(answer["reading_coverage"]["model_reading_completed"])
        self.assertEqual(answer["reading_coverage"]["omitted_repository_files"], 1)
        self.assertTrue(answer["framework_context"]["external_calls"])
        self.assertIn("requests a record", json.dumps(requests))
        self.assertNotIn(self.config.api_key, json.dumps(runner))

    def test_planning_failure_keeps_local_search_and_stops_model_requests(self):
        self.fixture()
        requests = []

        def transport(request):
            supplied = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(supplied)
            if supplied.get("stage") == "repository_search":
                return TransportResponse(503, '{"error":"temporarily unavailable"}')
            page = (supplied.get("source_pages") or [{}])[0]
            return reply("保留天数上限为14。" + (f"[{page['evidence_id']}]" if page.get("evidence_id") else ""))

        report, runner = self.run_question("HOLD-LIMIT 如何决定预留？", transport)
        self.assertEqual(runner["runner_status"], "NOT_READY", report)
        self.assertIsNone(runner["agent_result"])
        investigation = runner["investigation"]
        self.assertEqual(investigation["search_stop_reason"], "planning_unavailable")
        self.assertEqual(investigation["planning_diagnostics"][0]["code"], "HTTP_ERROR")
        self.assertEqual(len(requests), 1)
        self.assertTrue(report["source_manifest_verified"])
        self.assertIn("allocation.cbl", investigation["selected_paths"])
        self.assertEqual(runner["diagnostic"]["category"], "http_5xx_unknown")

    def test_unknown_concept_does_not_require_entry_or_a_predefined_topic(self):
        self.write("new-feature.cbl", "ROSTER", "IF WAIT-SLOTS > 0\nMOVE 'QUEUED' TO RESERVATION-STATE\nEND-IF.",
                   "01 WAIT-SLOTS PIC 9.\n01 RESERVATION-STATE PIC X(12).")
        self.write("other.cbl", "OTHER", "CONTINUE.")
        seen = []

        def transport(request):
            supplied = json.loads(json.loads(request.body)["messages"][-1]["content"])
            if supplied.get("stage") == "repository_search":
                return reply("DONE")
            seen.extend(page["relative_path"] for page in supplied.get("source_pages", []))
            return reply("系统在可等待名额大于零时设置排队状态。")

        _, runner = self.run_question("新业务的候补安排是什么？", transport)
        self.assertEqual(runner["runner_status"], "COMPLETED")
        self.assertTrue(runner["agent_result"]["investigation"]["fallback_all"])
        self.assertEqual(set(seen), {"new-feature.cbl", "other.cbl"})

    def test_invalid_planner_envelope_keeps_local_index_without_another_model_call(self):
        self.fixture()
        calls = []

        def transport(request):
            calls.append(request)
            supplied = json.loads(json.loads(request.body)["messages"][-1]["content"])
            if supplied.get("stage") == "repository_search":
                return TransportResponse(200, "temporary non-json gateway reply")
            return reply("保留天数在源码中定义为14；已有规则可以正常解释。")

        report, runner = self.run_question("HOLD-LIMIT 如何使用？", transport)
        self.assertEqual(len(calls), 1)
        self.assertEqual(runner["runner_status"], "NOT_READY")
        self.assertIsNone(runner["agent_result"])
        self.assertTrue(report["source_manifest_verified"])
        self.assertEqual(runner["investigation"]["planning_diagnostics"][0]["code"], "INVALID_JSON_RESPONSE")
        self.assertEqual(runner["diagnostic"]["category"], "invalid_response")

    def test_explicit_overview_request_reads_all_indexed_files(self):
        self.fixture()

        def transport(request):
            supplied = json.loads(json.loads(request.body)["messages"][-1]["content"])
            return reply("*" if supplied.get("stage") == "repository_search" else "按已读资料整理业务功能。")

        _, runner = self.run_question("这个代码库支持哪些业务操作？", transport)
        answer = runner["agent_result"]
        self.assertEqual(answer["investigation"]["selected_file_count"], 5)
        self.assertEqual(answer["reading_coverage"]["sent_files"], 5)

    def test_authorization_error_stops_without_repeating_every_source_page(self):
        self.fixture()
        requests = []

        def transport(request):
            requests.append(request)
            return TransportResponse(403, '{"error":"access denied"}')

        report, runner = self.run_question("HOLD-LIMIT 如何使用？", transport)
        self.assertEqual(runner["runner_status"], "NOT_READY")
        self.assertEqual(runner["reason_code"], "HTTP_ERROR")
        self.assertEqual(len(requests), 1)
        self.assertTrue(report["source_manifest_verified"])
        self.assertEqual(runner["api_diagnostics"]["request_count"], 1)

    def test_source_edit_during_planning_is_not_presented_as_current_evidence(self):
        self.fixture()
        called = []

        def transport(request):
            supplied = json.loads(json.loads(request.body)["messages"][-1]["content"])
            called.append(supplied)
            if supplied.get("stage") == "repository_search":
                path = self.source / "allocation.cbl"
                path.write_text(path.read_text(encoding="utf-8") + "*> changed during search\n", encoding="utf-8")
                return reply("DONE")
            self.assertNotIn("allocation.cbl", {page["relative_path"] for page in supplied.get("source_pages", [])})
            return reply("已读取其他仍一致的资料。")

        report, runner = self.run_question("HOLD-LIMIT 如何使用？", transport)
        self.assertFalse(report["source_manifest_verified"])
        self.assertIsNone(runner["agent_result"])
        self.assertEqual(sum(item.get("stage") == "repository_search" for item in called), 1)

    def test_single_matched_program_retains_its_framework_and_callable_page_reference(self):
        self.write("single.cbl", "RESERVATION", "MOVE 'FETCHROW' TO ACTION-CODE.\n"
                   "CALL 'REMOTE-STORE' USING ACTION-CODE RETURN-STATUS.\n"
                   "IF RETURN-STATUS = ZERO\nMOVE 'READY' TO ITEM-STATE\nEND-IF.",
                   "01 ACTION-CODE PIC X(12).\n01 RETURN-STATUS PIC 9.\n01 ITEM-STATE PIC X(12).")
        self.write("other.cbl", "OTHER", "CONTINUE.")
        referenced = []

        def transport(request):
            supplied = json.loads(json.loads(request.body)["messages"][-1]["content"])
            if supplied.get("stage") == "repository_search":
                return reply("DONE")
            self.assertTrue(supplied["framework_references"])
            self.assertTrue(supplied["external_calls"])
            call = supplied["external_calls"][0]
            self.assertNotIn("nearby_marker_evidence_ids", call)
            self.assertTrue(call["evidence_id"].startswith("ev_page_"))
            referenced.append(call["evidence_id"])
            return reply(f"按框架约定先请求取回记录，成功返回后设为就绪。[{call['evidence_id']}]")

        _, runner = self.run_question("RESERVATION 如何准备记录？", transport)
        result = runner["agent_result"]
        self.assertEqual(result["framework_context"]["status"], "MATCHED")
        self.assertNotIn("【未确认来源引用】", result["answer"])
        self.assertTrue(referenced)

    def test_search_format_variations_do_not_require_an_action_contract(self):
        for response, expected in (("- RESERVE-CODE\n- WAIT-DAYS", ["RESERVE-CODE", "WAIT-DAYS"]),
                                   ('```json\n{"search_terms":["WAIT-DAYS"]}\n```', ["WAIT-DAYS"]),
                                   ('["WAIT-DAYS"]', ["WAIT-DAYS"]),
                                   ("DONE", []), ("*", ["*"])):
            with self.subTest(response=response):
                self.assertEqual(search_suggestions(response), expected)


if __name__ == "__main__":
    unittest.main()
