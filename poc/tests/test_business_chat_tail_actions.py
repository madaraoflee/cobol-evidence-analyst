"""Terminal action objects must trigger bounded investigation, not leak as answers."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import _actions, _action_reply_invalid, run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


ACTION = {"read": [{"relative_path": "programs/ROUTESEL.cbl", "start_line": 1, "end_line": 42}],
          "search": ["ROUTE_CONFIG", "ROUTE-OUTPUT", "SELECT PROGRAM_NAME"]}
PROSE = ("初步已查明：TXNCORE 先调 ROUTESEL，再以返回的字段作为程序名调用。"
         "[ev_page_source]\n\n要说明查不到和重复配置各怎样处理，还缺 ROUTESEL 的分支原文。")


class TerminalActionParsingTests(unittest.TestCase):
    def test_terminal_bare_and_fenced_actions_with_prose_or_citations(self):
        command = json.dumps(ACTION)
        expected = _actions(command)
        for text in (command, "```json\n" + command + "\n```",
                     PROSE + "\n\n" + command,
                     PROSE + "\n\n```json\n" + command + "\n```",
                     "[ev_page_source] 还需核对路由分支。\n" + command):
            with self.subTest(text=text):
                self.assertEqual(_actions(text), expected)
                self.assertFalse(_action_reply_invalid(text, _actions(text)))

    def test_embedded_examples_multiple_objects_and_trailing_prose_do_not_execute(self):
        command = json.dumps(ACTION)
        for text in ("输出样例：\n" + command,
                     "For example:\n```json\n" + command + "\n```",
                     "这是正文中的例子 " + command,
                     PROSE + "\n" + command + "\n以上只是调用示例。",
                     PROSE + "\n```json\n" + command + "\n```\n这是说明。",
                     PROSE + "\n" + command + "\n" + command,
                     PROSE + "\n```json\n" + command + "\n```\n```json\n" + command + "\n```",
                     '{"search":["FIRST"]}\n' + PROSE + "\n" + command,
                     PROSE + '\n{"result":{"search":["EXAMPLE"]}}',
                     PROSE + '\n{"search":["EXAMPLE"],"unsupported":true}',
                     PROSE + '\n> ' + command):
            with self.subTest(text=text):
                self.assertIsNone(_actions(text))

    def test_budget_limits_and_malformed_protocol_detection_are_preserved(self):
        command = json.dumps({**ACTION, "framework_search": ["a", "b"]})
        policy = AgentPolicy(max_reads_per_turn=0, max_searches_per_turn=1,
                             max_framework_searches_per_turn=1)
        self.assertEqual(_actions(PROSE + "\n" + command, policy),
                         {"read": [], "search": ["ROUTE_CONFIG"], "framework_search": ["a"]})
        for tail in ('{"search": [', '{"read": []}', '{"read": "bad"}'):
            text = PROSE + "\n" + tail
            self.assertTrue(_action_reply_invalid(text, _actions(text)))


class TerminalActionChatTests(unittest.TestCase):
    def test_route_gap_executes_read_before_final_answer_without_protocol_leak(self):
        source = Path(__file__).resolve().parents[1] / "fixtures/complex-business-v2/main"
        config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "index.sqlite"
            build_business_index(source, database, verify_content=True)
            ensure_repository_search(database, source)
            for fenced in (False, True):
                with self.subTest(fenced=fenced):
                    requests = []

                    def transport(request):
                        payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
                        requests.append(payload)
                        if len(requests) == 1:
                            command = json.dumps(ACTION)
                            if fenced:
                                command = "```json\n" + command + "\n```"
                            text = PROSE + "\n\n" + command
                        else:
                            self.assertTrue(any(action.get("read", {}).get("relative_path") ==
                                "programs/ROUTESEL.cbl" for action in payload["completed_actions"]))
                            page = next(page for bundle in payload["source_context"] for page in bundle["pages"]
                                if page["relative_path"] == "programs/ROUTESEL.cbl" and "WHEN -811" in page["source_text"])
                            text = ("不能断言一定调用 CALCSTD。目标名来自按产品与生效日期查询 ROUTE_CONFIG 的 PROGRAM_NAME。"
                                "查不到置状态 71，重复配置置 72，其他数据库错误置 73；状态仍为零而名称空白时置 74。"
                                "这不证明当前数据库配置或实际运行目标。" + f"[{page['evidence_id']}]")
                        return TransportResponse(200, json.dumps({"choices": [{"message": {
                            "role": "assistant", "content": text}, "finish_reason": "stop"}]}))

                    output = run_business_chat("TXNCORE 的动态目标从哪里来，查不到或重复配置怎样处理？",
                        database, source, config, framework_reference_path="", transport=transport,
                        policy=AgentPolicy(max_model_requests=3, max_answer_revisions=0,
                            max_reads_per_turn=4))
                    result = output["agent_result"]
                    self.assertEqual(len(requests), 2)
                    self.assertGreater(result["metrics"]["tool_calls"]["read"], 0)
                    self.assertIn("重复配置置 72", result["answer"])
                    self.assertNotIn('"read"', result["answer"])
                    self.assertNotIn("还缺", result["answer"])
                    self.assertTrue(result["model_answer_recorded"])

    def test_terminal_read_keeps_existing_path_and_range_validation(self):
        source = Path(__file__).resolve().parents[1] / "fixtures/complex-business-v2/main"
        config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "index.sqlite"
            build_business_index(source, database, verify_content=True)
            ensure_repository_search(database, source)
            requests = []

            def transport(request):
                payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
                requests.append(payload)
                if len(requests) == 1:
                    text = "还需核对原文。\n" + json.dumps({"read": [
                        {"relative_path": "../outside.cbl", "start_line": 1, "end_line": 2},
                        {"relative_path": "programs/ROUTESEL.cbl", "start_line": -1, "end_line": 2}]})
                else:
                    failed = [item for item in payload["completed_actions"] if item.get("outcome") == "unavailable"]
                    self.assertEqual(len(failed), 2)
                    text = "现有源码通过变量选择目标程序，实际目标取决于数据库配置。"
                return TransportResponse(200, json.dumps({"choices": [{"message": {
                    "content": text}, "finish_reason": "stop"}]}))

            result = run_business_chat("TXNCORE 如何调用目标？", database, source, config,
                framework_reference_path="", transport=transport,
                policy=AgentPolicy(max_model_requests=2, max_answer_revisions=0,
                    max_reads_per_turn=4))["agent_result"]
            self.assertEqual(len(requests), 2)
            self.assertNotIn('"read"', result["answer"])


if __name__ == "__main__":
    unittest.main()
