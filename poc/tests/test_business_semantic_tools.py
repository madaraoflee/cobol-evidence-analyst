"""Named semantic tools through ordinary Chat JSON and the default workbench."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import (_LocalAnalysisBudgetExceeded, _SEMANTIC_TOOLS, _SYSTEM,
                           _actions, _validated_semantic_anchor, run_business_chat)
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


def response(text):
    return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": text},
                                                         "finish_reason": "stop"}]}, ensure_ascii=False))


class SemanticToolContractTests(unittest.TestCase):
    def test_plain_chat_json_accepts_every_named_tool(self):
        for tool in _SEMANTIC_TOOLS:
            with self.subTest(tool=tool):
                action = _actions(json.dumps({tool: {"relative_path": "root.cbl", "line": 12}}))
                self.assertEqual(action[tool], [{"relative_path": "root.cbl", "line": 12}])
                self.assertIn('"' + tool + '"', _SYSTEM)

    def test_tools_share_existing_inspection_allowance(self):
        arguments = {"relative_path": "root.cbl", "line": 12}
        payload = {"inspect_business_context": [arguments], **{tool: [arguments] for tool in _SEMANTIC_TOOLS}}
        action = _actions(json.dumps(payload), AgentPolicy(max_business_context_actions_per_turn=2))
        self.assertEqual(sum(len(action.get(tool, [])) for tool in ("inspect_business_context", *_SEMANTIC_TOOLS)), 2)


class DefaultSemanticToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.code = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. ROOT-RULE.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 IN-AMOUNT PIC 9(4).\n01 OUT-AMOUNT PIC 9(4).\n"
            "PROCEDURE DIVISION.\nMAIN.\nMOVE 3 TO IN-AMOUNT.\n"
            "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nCALL 'LEAF-RULE' USING OUT-AMOUNT.\nGOBACK.\n")
        (self.source / "root.cbl").write_text(self.code)
        (self.source / "leaf.cbl").write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. LEAF-RULE.\nDATA DIVISION.\n"
            "LINKAGE SECTION.\n01 INPUT-AMOUNT PIC 9(4).\nPROCEDURE DIVISION USING INPUT-AMOUNT.\n"
            "MAIN.\nCONTINUE.\nGOBACK.\n")
        self.database = self.root / "index.sqlite"
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="local-secret")

    def test_local_entity_and_location_checks_reject_untrusted_plan_parameters(self):
        valid = _validated_semantic_anchor({"relative_path": "root.cbl", "line": 10, "fields": ["OUT-AMOUNT"]}, self.database)
        self.assertEqual(valid["fields"], ["OUT-AMOUNT"])
        for arguments in ({"relative_path": "../root.cbl", "line": 10},
                          {"relative_path": "missing.cbl", "line": 10},
                          {"relative_path": "root.cbl", "line": True},
                          {"relative_path": "root.cbl", "line": 9999},
                          {"relative_path": "root.cbl", "line": 10, "fields": ["FABRICATED-FIELD"]}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                _validated_semantic_anchor(arguments, self.database)

    def test_flow_budget_fallback_keeps_version_bound_indexed_syntax_roles(self):
        path = self.source / "root.cbl"
        path.write_text(self.code.replace("GOBACK.", "\n".join("DISPLAY 'UNCHANGED'." for _ in range(700)) + "\nGOBACK."))
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        requests = []
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            supplied = [page for context in payload["source_context"] for page in context.get("pages", [])]
            formula = next(page for page in supplied if "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2" in page["source_text"])
            self.assertIn("result", formula["semantic_roles"])
            self.assertEqual(formula["source_statement_role_scope"], "indexed_statement_syntax_candidate")
            self.assertFalse(formula["flow_execution_verified"])
            self.assertFalse(any(context.get("evidence_groups") for context in payload["source_context"]))
            return response(f"OUT-AMOUNT = IN-AMOUNT * 2。[{formula['evidence_id']}]")
        with mock.patch("semantic_scope.prepare_semantic_scope",
                        side_effect=_LocalAnalysisBudgetExceeded("initial_context")):
            result = run_business_chat("ROOT-RULE OUT-AMOUNT 怎样计算？", self.database, self.source,
                self.config, transport=transport, framework_reference_path="",
                policy=AgentPolicy(max_source_characters=4096, max_complete_source_characters=4096,
                                   max_model_requests=1, max_answer_revisions=0))["agent_result"]
        self.assertEqual(len(requests), 1)
        self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", result["answer"])
        self.assertEqual(result["claim_ledger"]["entries"][0]["support_status"], "supported")

    def test_default_path_executes_each_tool_and_supplies_its_result(self):
        for tool in _SEMANTIC_TOOLS:
            with self.subTest(tool=tool):
                requests = []
                line = 11 if tool in {"trace_parameter", "explain_external_effect"} else 10
                def transport(request):
                    envelope = json.loads(request.body)
                    self.assertNotIn("tools", envelope)
                    self.assertNotIn("response_format", envelope)
                    payload = json.loads(envelope["messages"][-1]["content"])
                    requests.append(payload)
                    if len(requests) == 1:
                        return response(json.dumps({tool: {"relative_path": "root.cbl", "line": line}}))
                    groups = [group for group in payload["evidence_groups"]
                              if group.get("semantic_analysis", {}).get("requested_tool") == tool]
                    self.assertTrue(groups, tool)
                    results = groups[-1]["semantic_analysis"]["tool_results"]
                    self.assertTrue(results, tool)
                    self.assertTrue(all(result["execution_verified"] is False for result in results))
                    self.assertTrue(all(result["flow_status"] == "possible_source_flow" for result in results))
                    supplied = [page for context in payload["source_context"] for page in context.get("pages", [])]
                    page = next(page for page in supplied if "OUT-AMOUNT = IN-AMOUNT * 2" in page["source_text"])
                    return response(f"OUT-AMOUNT = IN-AMOUNT * 2。[{page['evidence_id']}]")
                result = run_business_chat("ROOT-RULE 的 OUT-AMOUNT 怎样计算？", self.database, self.source, self.config,
                    transport=transport, framework_reference_path="",
                    policy=AgentPolicy(max_model_requests=2, max_answer_revisions=0,
                                       max_business_context_actions_per_turn=2))["agent_result"]
                self.assertEqual(result["metrics"]["model_requests"], 2)
                self.assertEqual(result["metrics"]["tool_calls"][tool], 1)
                self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", result["answer"])


if __name__ == "__main__":
    unittest.main()
