from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from answer_markdown import BUSINESS_ANSWER_POLICY
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


def reply(text):
    return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": text},
                                                           "finish_reason": "stop"}]}, ensure_ascii=False))


def source_pages(payload):
    return [page for context in payload["source_context"] for page in context.get("pages", [])]


class BusinessChatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="local-test-secret")
        self.requests = []
        self.request_bytes = []

    def write(self, path, name, body, data=""):
        contents = (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
                    f"WORKING-STORAGE SECTION.\n{data}\nPROCEDURE DIVISION.\nMAIN.\n{body}\nGOBACK.\n")
        destination = self.source / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(contents, encoding="utf-8")
        return destination

    def build(self):
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        return ensure_repository_search(self.database, self.source)

    def ask(self, question, respond, **options):
        def transport(request):
            envelope = json.loads(request.body)
            self.requests.append(envelope)
            self.request_bytes.append(len(request.body))
            self.assertNotIn("tools", envelope)
            self.assertNotIn("response_format", envelope)
            return respond(json.loads(envelope["messages"][-1]["content"]), envelope)
        return run_business_chat(question, self.database, self.source, self.config, transport=transport,
                                 framework_reference_path=options.pop("framework_reference_path", ""), **options)

    def write_window(self, limit=9, field="TIDE-WINDOW", path="window.cbl"):
        return self.write(path, "HOLDING-RULE", f"IF HOLD-DAYS <= {field}\n"
                          "MOVE 'HELD' TO REQUEST-STATE\nELSE\nMOVE 'REVIEW' TO REQUEST-STATE\nEND-IF.",
                          f"01 {field} PIC 99 VALUE {limit}.\n01 HOLD-DAYS PIC 99.\n01 REQUEST-STATE PIC X(12).")

    def grounded_window_reply(self, payload, _envelope):
        page = next(page for page in source_pages(payload) if "MOVE 'REVIEW'" in page["source_text"])
        code = page["source_text"]
        field, limit = re.search(r"01 ([A-Z-]+) PIC 99 VALUE (\d+)", code).groups()
        self.assertIn(f"IF HOLD-DAYS <= {field}", code)
        self.assertIn("ELSE\nMOVE 'REVIEW'", code)
        return reply(f"保留期不超过{limit}天时进入HELD；超过{limit}天进入REVIEW等待复核。[{page['evidence_id']}]")

    def test_markdown_answers_and_supported_wrappers_need_one_model_request(self):
        self.write_window()
        self.build()
        for wrapper in (lambda text: text, lambda text: f"```markdown\n{text}\n```",
                        lambda text: json.dumps({"answer": f"```md\n{text}\n```"}, ensure_ascii=False)):
            with self.subTest(wrapper=wrapper):
                self.requests.clear()
                expected = []

                def respond(payload, envelope):
                    self.assertIn("最终给用户的答案使用 Markdown 正文", envelope["messages"][0]["content"])
                    self.assertIn(BUSINESS_ANSWER_POLICY, envelope["messages"][0]["content"])
                    page = next(page for page in source_pages(payload) if "MOVE 'REVIEW'" in page["source_text"])
                    text = (f"**保留期以 9 天为界。** [{page['evidence_id']}]\n\n"
                            "| 条件 | 处理 |\n| --- | --- |\n| 不超过 9 天 | HELD |\n| 超过 9 天 | REVIEW |")
                    expected.append(text)
                    return reply(wrapper(text))

                result = self.ask("TIDE-WINDOW 如何处理？", respond)["agent_result"]
                self.assertEqual(result["answer"], expected[0])
                self.assertEqual(result["answer_format"], "markdown")
                self.assertEqual(result["narrative"]["format"], "markdown")
                self.assertEqual(len(result["narrative"]["citations"]), 1)
                self.assertEqual(result["metrics"]["model_requests"], 1)
                self.assertEqual(result["status"], "ANALYZED")

    def test_explicit_source_request_preserves_requested_local_code(self):
        self.write_window()
        self.build()

        def respond(payload, envelope):
            self.assertEqual(payload["question"], "请给我 TIDE-WINDOW 判断条件的具体源码。")
            self.assertIn("只有用户明确要求查看源码、具体语句或开发实现时", envelope["messages"][0]["content"])
            page = next(page for page in source_pages(payload) if "IF HOLD-DAYS" in page["source_text"])
            return reply(f"判断条件如下：\n\n```cobol\nIF HOLD-DAYS <= TIDE-WINDOW\n```\n\n[{page['evidence_id']}]")

        result = self.ask("请给我 TIDE-WINDOW 判断条件的具体源码。", respond)["agent_result"]
        self.assertIn("```cobol\nIF HOLD-DAYS <= TIDE-WINDOW\n```", result["answer"])
        self.assertEqual(result["metrics"]["model_requests"], 1)

    def test_arbitrary_rules_use_one_request_and_preserve_conditions_and_real_citations(self):
        for field, limit in (("TIDE-WINDOW", 9), ("MAPLE-WINDOW", 17)):
            with self.subTest(field=field, limit=limit):
                path = self.write_window(limit, field)
                self.build()
                self.requests.clear()
                output = self.ask(f"{field} 如何决定业务处理？", self.grounded_window_reply)
                result = output["agent_result"]
                self.assertEqual(output["runner_status"], "COMPLETED")
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(result["metrics"]["model_requests"], 1)
                self.assertIn(f"不超过{limit}天时进入HELD", result["answer"])
                self.assertIn(f"超过{limit}天进入REVIEW", result["answer"])
                self.assertFalse(result["claims_semantically_verified"])
                self.assertFalse(result["reading_coverage"]["complete"])
                citation = result["narrative"]["citations"][0]
                self.assertEqual(citation["source_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
                with closing(sqlite3.connect(self.database)) as connection:
                    actual = connection.execute("SELECT text FROM evidence_spans WHERE evidence_id=?", (citation["evidence_id"],)).fetchone()[0]
                self.assertEqual(actual, "\n".join(path.read_text().splitlines()[citation["start_line"] - 1:citation["end_line"]]))
                self.assertNotIn(self.config.api_key, json.dumps(output))

    def test_entry_question_reaches_a_distant_rule_without_page_by_page_model_calls(self):
        for index in range(31):
            body = (f'CALL "ENTRY-{index + 1:03d}".' if index < 30 else
                    "DISPLAY 'UNCHANGED'.\n" * 1600 +
                    "IF INPUT-POINTS > 5\nCOMPUTE FINAL-POINTS = INPUT-POINTS * 2\nEND-IF.")
            self.write(f"node-{index:03d}.cbl", f"ENTRY-{index:03d}", body,
                       "01 INPUT-POINTS PIC 9(4).\n01 FINAL-POINTS PIC 9(4).")
        self.build()

        def respond(payload, _envelope):
            programs = payload["business_map"]["programs"]
            self.assertEqual(len(programs), 31)
            self.assertEqual(programs[-1]["relative_path"], "node-030.cbl")
            rule = next(item for item in payload["business_map"]["rule_leads"]
                        if item["relative_path"] == "node-030.cbl" and item["rule_kind"] == "COMPUTE")
            page = next(page for page in source_pages(payload)
                        if page["relative_path"] == rule["relative_path"] and "FINAL-POINTS" in page["source_text"])
            self.assertIn("IF INPUT-POINTS > 5", page["source_text"])
            return reply(f"输入积分大于5时，结果按输入积分的两倍计算。[{page['evidence_id']}]")

        output = self.ask("ENTRY-000 最终怎样决定积分？", respond)
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertEqual(output["agent_result"]["metrics"]["model_requests"], 1)
        self.assertIn("node-030.cbl", output["agent_result"]["investigation"]["business_map"]["selected_paths"])

    def test_large_entry_keeps_its_deep_call_and_downstream_calculation_in_one_request(self):
        filler = "\n".join(f"ADD {index} TO WORK-COUNT." for index in range(1800))
        self.write("entry.cbl", "ENTRY-RULE", filler + '\nCALL "CALC-RULE".',
                   "01 WORK-COUNT PIC 9(5).")
        self.write("calc.cbl", "CALC-RULE", "IF REQUEST-COUNT > 4\n"
                   "COMPUTE RESULT-COUNT = REQUEST-COUNT * 2\nEND-IF.",
                   "01 REQUEST-COUNT PIC 9(5).\n01 RESULT-COUNT PIC 9(5).")
        self.build()

        def respond(payload, _envelope):
            pages = source_pages(payload)
            deep = next(page for page in pages if 'CALL "CALC-RULE"' in page["source_text"])
            leaf = next(page for page in pages if "RESULT-COUNT = REQUEST-COUNT * 2" in page["source_text"])
            self.assertGreater(deep["start_line"], 1000)
            self.assertIn("IF REQUEST-COUNT > 4", leaf["source_text"])
            self.assertEqual({item["relative_path"] for item in payload["business_map"]["programs"]},
                             {"entry.cbl", "calc.cbl"})
            return reply(f"请求数量大于4时，结果按两倍计算。[{leaf['evidence_id']}]")

        output = self.ask("ENTRY-RULE 的处理最终如何计算？", respond)
        self.assertEqual(output["agent_result"]["metrics"]["model_requests"], 1)
        self.assertIn("两倍计算", output["agent_result"]["answer"])

    def test_impact_question_expands_all_users_of_a_shared_copy(self):
        (self.source / "sharedset.cpy").write_text("01 RULE-SET-42 PIC 9 VALUE 7.\n", encoding="utf-8")
        for index in range(12):
            self.write(f"member-{index:02d}.cbl", f"MEMBER-{index:02d}",
                       "COPY SHAREDSET.\nIF REQUEST-VALUE > RULE-SET-42\nMOVE 'REVIEW' TO REQUEST-STATE\nEND-IF.")
        self.build()

        def respond(payload, _envelope):
            business_map = payload["business_map"]
            affected = [item for item in business_map["programs"] if item["relative_path"].startswith("member-")]
            self.assertEqual(len(affected), 12)
            page = next(page for page in source_pages(payload) if page["relative_path"] == "sharedset.cpy")
            return reply(f"这项设定被12个处理程序引用；它控制请求进入复核的阈值。[{page['evidence_id']}]")

        output = self.ask("如果改 RULE-SET-42，哪些程序会受到影响？", respond)
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertEqual(output["agent_result"]["investigation"]["business_map"]["intent"], "impact")
        self.assertEqual(len(self.requests), 1)

    def test_cross_language_search_refreshes_the_business_map_before_answering(self):
        for index in range(12):
            self.write(f"orientation-{index:02d}.cbl", f"ORIENTATION-{index:02d}", "CONTINUE.")
        self.write("z-score.cbl", "SCORE-RULE", "IF REQUEST-SCORE > POINTS-CAP\n"
                   "COMPUTE FINAL-SCORE = REQUEST-SCORE * 3\nEND-IF.",
                   "01 REQUEST-SCORE PIC 9(4).\n01 POINTS-CAP PIC 9(4) VALUE 11.\n"
                   "01 FINAL-SCORE PIC 9(4).")
        self.build()

        def respond(payload, _envelope):
            if len(self.requests) == 1:
                self.assertFalse(payload["business_map"]["direct_paths"])
                return reply('{"search":["POINTS-CAP"]}')
            self.assertIn("z-score.cbl", payload["business_map"]["direct_paths"])
            self.assertTrue(any(item["rule_kind"] == "COMPUTE"
                                for item in payload["business_map"]["rule_leads"]))
            page = next(page for page in source_pages(payload) if page["relative_path"] == "z-score.cbl")
            return reply(f"超过11分后，最终积分按请求积分的三倍计算。[{page['evidence_id']}]")

        output = self.ask("积分上限之后怎么算？", respond)
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertEqual(len(self.requests), 2)
        self.assertIn("超过11分", output["agent_result"]["answer"])

    def test_followup_carries_history_and_prior_source_even_with_competing_generic_matches(self):
        self.write_window(path="z-window.cbl")
        for index in range(14):
            self.write(f"noise-{index:02d}.cbl", f"PACKAGING-{index}", "*> 超过上限 什么情况\nDISPLAY 'PACKAGE'.")
        self.build()
        first = self.ask("TIDE-WINDOW", self.grounded_window_reply)["agent_result"]
        history = [{"role": "user", "content": "TIDE-WINDOW 如何处理？"},
                   {"role": "assistant", "content": first["answer"], "evidence_refs": first["evidence_refs"]}]
        def respond(payload, envelope):
            self.assertEqual(envelope["messages"][1:3], [{"role": item["role"], "content": item["content"]} for item in history])
            self.assertIn("z-window.cbl", {page["relative_path"] for page in source_pages(payload)})
            return self.grounded_window_reply(payload, envelope)
        output = self.ask("那超过上限会怎样？", respond, history=history)
        self.assertIn("超过9天进入REVIEW", output["agent_result"]["answer"])
        self.assertEqual(output["agent_result"]["metrics"]["history_messages"], 2)

    def test_new_topic_does_not_count_previous_citation_as_a_current_source_match(self):
        self.write_window(path="window.cbl")
        self.write("fee.cbl", "FEE-CALC", "COMPUTE TOTAL-FEE = BASE-FEE * FEE-RATE.",
                   "01 BASE-FEE PIC 9(4).\n01 FEE-RATE PIC 9V99.\n01 TOTAL-FEE PIC 9(5)V99.")
        self.build()
        first = self.ask("TIDE-WINDOW", self.grounded_window_reply)["agent_result"]
        history = [{"role": "user", "content": "TIDE-WINDOW 如何处理？"},
                   {"role": "assistant", "content": first["answer"], "evidence_refs": first["evidence_refs"]}]

        def respond(payload, _envelope):
            if len(self.requests) == 2:
                self.assertFalse(payload["business_map"]["direct_paths"])
                self.assertIn("当前问题尚未命中源码", payload["task"])
                return reply('{"search":["FEE-RATE"]}')
            self.assertIn("fee.cbl", payload["business_map"]["direct_paths"])
            page = next(page for page in source_pages(payload) if page["relative_path"] == "fee.cbl")
            return reply(f"总费用按基础费用乘以费率计算。[{page['evidence_id']}]")

        output = self.ask("这笔费用怎么算？", respond, history=history)
        self.assertEqual(output["agent_result"]["metrics"]["model_requests"], 2)
        self.assertIn("基础费用乘以费率", output["agent_result"]["answer"])

    def test_model_can_search_a_new_business_term_then_answer_from_new_evidence(self):
        for index in range(12):
            self.write(f"entry-{index:02d}.cbl", f"ROUTINE-{index}", "DISPLAY 'ROUTINE'.")
        self.write_window(23, "RETURN-WINDOW", "z-return.cbl")
        self.build()
        def respond(payload, envelope):
            if len(self.requests) == 1:
                self.assertNotIn("z-return.cbl", {page["relative_path"] for page in source_pages(payload)})
                return reply('{"search":["RETURN-WINDOW"]}')
            return self.grounded_window_reply(payload, envelope)
        output = self.ask("新业务退回请求的办理期限？", respond)
        self.assertEqual(len(self.requests), 2)
        self.assertIn("不超过23天", output["agent_result"]["answer"])
        self.assertEqual(output["agent_result"]["investigation"]["search_rounds"], 2)
        self.assertIn("RETURN-WINDOW", output["agent_result"]["investigation"]["searches"][1]["query"])

    def test_model_can_read_a_deep_specific_branch_instead_of_analyzing_every_page(self):
        path = self.write("long.cbl", "LONG-WORK", "*> REQUEST-GATE entry\n" + "DISPLAY 'UNCHANGED'.\n" * 1800
                          + "IF STATE-CODE = 'RETRY'\nMOVE 3 TO DEFER-DAYS\nEND-IF.")
        lines = path.read_text().splitlines()
        branch = lines.index("IF STATE-CODE = 'RETRY'") + 1
        self.build()
        def respond(payload, _envelope):
            if len(self.requests) == 1:
                self.assertFalse(any("MOVE 3 TO DEFER-DAYS" in page["source_text"] for page in source_pages(payload)))
                return reply(json.dumps({"read": [{"relative_path": "long.cbl", "start_line": branch, "end_line": branch + 2}]}))
            page = next(page for page in source_pages(payload) if "MOVE 3 TO DEFER-DAYS" in page["source_text"])
            self.assertIn("IF STATE-CODE = 'RETRY'", page["source_text"])
            return reply(f"进入RETRY状态时延期3天。[{page['evidence_id']}]")
        output = self.ask("REQUEST-GATE 的重试延期规则？", respond)
        self.assertEqual(len(self.requests), 2)
        self.assertIn("延期3天", output["agent_result"]["answer"])
        self.assertIn("read", [item["tool"] for item in output["agent_result"]["tool_trace"]])
        self.assertLess(output["agent_result"]["metrics"]["source_characters"], len(path.read_text()))

    def test_followup_restores_the_previous_deep_excerpt_not_only_file_header(self):
        self.write("large.cbl", "LONG-WORK", "DISPLAY 'INITIAL'.\n" * 1800
                   + "*> DEEP-RETRY processing\nIF LAST-RESULT = 'FAILED'\n"
                   "MOVE 'MANUAL' TO NEXT-STEP\nEND-IF.")
        self.build()
        def respond(payload, _envelope):
            page = next(page for page in source_pages(payload) if "MOVE 'MANUAL' TO NEXT-STEP" in page["source_text"])
            self.assertIn("IF LAST-RESULT = 'FAILED'", page["source_text"])
            return reply(f"上次结果为FAILED时转人工处理。[{page['evidence_id']}]")
        first = self.ask("DEEP-RETRY 如何处理？", respond)["agent_result"]
        self.assertTrue(any(ref["start_line"] > 1000 for ref in first["evidence_refs"]))
        history = [{"role": "user", "content": "DEEP-RETRY 如何处理？"},
                   {"role": "assistant", "content": first["answer"], "evidence_refs": first["evidence_refs"]}]
        second = self.ask("那失败之后呢？", respond, history=history)
        self.assertIn("转人工处理", second["agent_result"]["answer"])
        self.assertEqual(second["agent_result"]["metrics"]["model_requests"], 1)

    def test_missing_framework_does_not_block_answer_or_trigger_framework_source_rescan(self):
        self.write_window()
        self.build()
        with mock.patch("framework_knowledge._source_candidates", side_effect=AssertionError("framework rescanned source")), \
             mock.patch("framework_knowledge._verified_lines", side_effect=AssertionError("framework reopened source")):
            output = self.ask("TIDE-WINDOW", self.grounded_window_reply, framework_reference_path=self.root / "missing")
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertEqual(output["agent_result"]["framework_context"]["status"], "UNAVAILABLE")
        self.assertIn("超过9天进入REVIEW", output["agent_result"]["answer"])

    def test_unavailable_optional_read_is_reported_to_model_without_discarding_good_evidence(self):
        self.write_window()
        self.build()
        def respond(payload, envelope):
            if len(self.requests) == 1:
                return reply('{"read":[{"relative_path":"absent.cbl","start_line":1,"end_line":20}]}')
            return self.grounded_window_reply(payload, envelope)
        output = self.ask("TIDE-WINDOW", respond)
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertIn("超过9天进入REVIEW", output["agent_result"]["answer"])
        self.assertEqual(len(self.requests), 2)

    def test_directory_framework_can_explain_visible_closed_call_and_keep_document_citations(self):
        self.write("entry.cbl", "RECORD-REQUEST", "MOVE 'FETCH-ROW' TO ACTION-CODE.\n"
                   "CALL 'CLOSED-STORE' USING ACTION-CODE REQUEST-ROW RETURN-CODE.\n"
                   "IF RETURN-CODE = ZERO\nMOVE 'FOUND' TO REQUEST-STATE\nEND-IF.")
        manual = self.root / "manual"
        manual.mkdir()
        document = manual / "record-guide.md"
        document.write_text("# Record access\n\nFETCH-ROW requests a row by its supplied key; a zero return code means a row was found.\n", encoding="utf-8")
        self.build()
        def respond(payload, envelope):
            reference = next(item for item in payload["framework_references"] if "row by its supplied key" in item["text"])
            page = next(page for page in source_pages(payload) if "CLOSED-STORE" in page["source_text"])
            self.assertIn(BUSINESS_ANSWER_POLICY, envelope["messages"][0]["content"])
            self.assertIn("FETCH-ROW", reference["matched_terms"])
            self.assertEqual(reference["selection_reason"], "source_marker")
            self.assertTrue(any(location["relative_path"] == page["relative_path"] for location in reference["source_locations"]))
            links = [link for context in payload["source_context"] for link in context.get("call_chain", {}).get("links", [])]
            self.assertTrue(any(link["target_name"] == "CLOSED-STORE" and link["target_source_status"] == "unavailable" for link in links))
            return reply(f"请求会按指定标识查找记录。查到记录后，系统把这次请求标记为已找到，可以继续使用该记录；框架资料明确了读取成功的含义，源码决定了成功后的状态变化。[{page['evidence_id']}] [{reference['reference_id']}]")
        with mock.patch("framework_knowledge._source_candidates", side_effect=AssertionError("framework rescanned source")):
            output = self.ask("FETCH-ROW 如何处理请求？", respond, framework_reference_path=manual)
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertIn("查到记录后，系统把这次请求标记为已找到", output["agent_result"]["answer"])
        self.assertNotIn("```", output["agent_result"]["answer"])
        references = [item for item in output["agent_result"]["narrative"]["citations"] if item["kind"] == "framework_reference"]
        self.assertEqual(len(references), 1)
        reference = references[0]
        self.assertEqual(reference["document_name"], "record-guide.md")
        self.assertEqual(reference["document_sha256"], hashlib.sha256(document.read_bytes()).hexdigest())
        self.assertEqual(reference["start_line"], 3)

    def test_source_changed_while_model_answers_is_not_shown_as_current_business_answer(self):
        path = self.write_window()
        self.build()
        def respond(payload, envelope):
            response = self.grounded_window_reply(payload, envelope)
            path.write_text(path.read_text().replace("VALUE 9", "VALUE 2"), encoding="utf-8")
            return response
        output = self.ask("TIDE-WINDOW", respond)
        self.assertEqual(output["runner_status"], "NOT_READY")
        self.assertEqual(output["reason_code"], "SOURCE_CHANGED_DURING_ANSWER")
        self.assertNotIn("不超过9天", output["agent_result"]["answer"])
        self.assertFalse(output["agent_result"]["model_answer_recorded"])
        self.assertEqual(output["agent_result"]["evidence_refs"], [])

    def test_unknown_citations_are_removed_but_supported_answer_is_retained(self):
        self.write_window()
        self.build()
        def respond(payload, _envelope):
            page = source_pages(payload)[0]
            return reply(f"上限为9天。[{page['evidence_id']}] [ev_fabricated_source]")
        output = self.ask("TIDE-WINDOW", respond)
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertIn("上限为9天", output["agent_result"]["answer"])
        self.assertNotIn("ev_fabricated_source", output["agent_result"]["answer"])
        self.assertEqual(len(output["agent_result"]["narrative"]["citations"]), 1)

    def test_no_match_uses_bounded_orientation_and_never_calls_full_repository_analysis(self):
        for index in range(36):
            self.write(f"entry-{index:02d}.cbl", f"ROUTINE-{index}", "DISPLAY 'ROUTINE'.\n" * 500)
        overview = self.build()
        def respond(payload, _envelope):
            self.assertLessEqual(len(source_pages(payload)), 8)
            self.assertLess(sum(len(page["source_text"]) for page in source_pages(payload)), 33000)
            return reply("当前片段没有这项业务的实现；可以按具体规则名称继续查找。")
        with mock.patch("business_analysis.run_business_analysis", side_effect=AssertionError("full analysis invoked")), \
             mock.patch("repository_discovery.ensure_repository_search", side_effect=AssertionError("repository reindexed")), \
             mock.patch("business_index.build_business_index", side_effect=AssertionError("repository reparsed")):
            output = self.ask("UnknownNebulaFeature", respond)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(output["agent_result"]["metrics"]["repository_rebuilt"], False)
        self.assertLess(output["agent_result"]["metrics"]["retrieved_pages"], overview["indexed_pages"])
        self.assertFalse(output["agent_result"]["reading_coverage"]["complete"])

    def test_repeated_targeted_reads_do_not_duplicate_large_call_graph_or_exceed_request_budget(self):
        self.write("entry.cbl", "ENTRYPOINT", "\n".join(f'CALL "MEMBER{index:02d}".' for index in range(64))
                   + "\nIF ALL-RESULT = ZERO\nMOVE 'READY' TO REQUEST-STATE\nEND-IF.",
                   "01 ALL-RESULT PIC 9.\n01 REQUEST-STATE PIC X(10).")
        for index in range(64):
            self.write(f"member{index:02d}.cbl", f"MEMBER{index:02d}", "CONTINUE.")
        self.build()
        def respond(payload, _envelope):
            links = [link for context in payload["source_context"] for link in context.get("call_chain", {}).get("links", [])]
            identifiers = [link["relation_id"] for link in links]
            self.assertGreater(len(identifiers), 30)
            self.assertEqual(len(identifiers), len(set(identifiers)))
            self.assertLess(self.request_bytes[-1], 240000)
            turn = len(self.requests)
            if turn < 4:
                return reply(json.dumps({"read": [{"relative_path": "entry.cbl", "start_line": turn * 3 + offset,
                                                     "end_line": turn * 3 + offset} for offset in range(3)]}))
            page = next(page for page in source_pages(payload) if "MOVE 'READY' TO REQUEST-STATE" in page["source_text"])
            self.assertIn("IF ALL-RESULT = ZERO", page["source_text"])
            return reply(f"只有ALL-RESULT为零时，请求状态才设为READY。[{page['evidence_id']}]")
        output = self.ask("ENTRYPOINT 如何决定请求就绪？", respond)
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertNotEqual(output["reason_code"], "REQUEST_TOO_LARGE")
        self.assertEqual(len(self.requests), 4)
        self.assertIn("只有ALL-RESULT为零时", output["agent_result"]["answer"])
        self.assertTrue(all(size < 240000 for size in self.request_bytes))

    def test_framework_changed_during_answer_retains_explanation_as_supplied_document_version(self):
        self.write("entry.cbl", "RECORD-REQUEST", "MOVE 'FETCH-ROW' TO ACTION-CODE.\n"
                   "CALL 'CLOSED-STORE' USING ACTION-CODE RETURN-CODE.\n"
                   "IF RETURN-CODE = ZERO\nMOVE 'FOUND' TO REQUEST-STATE\nEND-IF.")
        manual = self.root / "guide.md"
        manual.write_text("# Record access\n\nFETCH-ROW requests a record by key; zero means the record was found.\n", encoding="utf-8")
        old_digest = hashlib.sha256(manual.read_bytes()).hexdigest()
        self.build()
        def respond(payload, _envelope):
            reference = next(item for item in payload["framework_references"] if "record was found" in item["text"])
            page = next(page for page in source_pages(payload) if "MOVE 'FOUND'" in page["source_text"])
            manual.write_text("# Record access\n\nFETCH-ROW requests the active record; zero means the current record was found.\n", encoding="utf-8")
            return reply(f"根据读取时的资料，请求按键取得记录；返回零时业务状态变为FOUND。[{page['evidence_id']}] [{reference['reference_id']}]")
        output = self.ask("FETCH-ROW 如何处理？", respond, framework_reference_path=manual)
        result = output["agent_result"]
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("返回零时业务状态变为FOUND", result["answer"])
        boundary = next(item for item in result["boundaries"] if item.get("reason") == "framework_reference_changed")
        self.assertEqual(boundary["supplied_sha256"], old_digest)
        self.assertEqual(boundary["current_sha256"], hashlib.sha256(manual.read_bytes()).hexdigest())
        self.assertEqual(result["framework_context"]["source_version_status"], "historical")
        reference = next(item for item in result["narrative"]["citations"] if item["kind"] == "framework_reference")
        self.assertEqual(reference["document_sha256"], old_digest)
        self.assertNotEqual(reference["document_sha256"], hashlib.sha256(manual.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
