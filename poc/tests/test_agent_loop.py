from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

import agent_loop as agent_loop_module  # noqa: E402
from agent_loop import (  # noqa: E402
    BoundedAgentLoop,
    MAX_TOOL_CALLS,
    ModelProtocolError,
    normalize_model_output,
)


def json_action(action: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {"action": action, "arguments": arguments},
                        ensure_ascii=False,
                    ),
                }
            }
        ]
    }


def native_action(
    action: str,
    arguments: dict[str, object],
    *,
    call_id: str = "call_native",
) -> dict[str, object]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": action,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                }
            }
        ]
    }


class FakeClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []

    def complete(self, **kwargs: object) -> dict[str, object]:
        self.requests.append(copy.deepcopy(kwargs))
        if not self.responses:
            raise AssertionError("FakeClient received an unexpected model turn")
        return self.responses.pop(0)


class FakeTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    @staticmethod
    def _base(tool: str) -> dict[str, object]:
        result: dict[str, object] = {
            "tool": tool,
            "contract_version": "0.1",
            "snapshot_id": "snapshot-test",
            "status": "OK",
            "truncated": False,
            "boundaries": [],
            "diagnostics": [],
        }
        if tool == "search_code":
            result.update({"hits": [], "evidence_refs": []})
        elif tool == "inspect_symbol":
            result["matches"] = []
        elif tool == "trace_relations":
            result.update({"edges": [], "visited_entities": []})
        elif tool == "read_evidence":
            result["spans"] = []
        return result

    @staticmethod
    def snapshot_coverage() -> dict[str, object]:
        return {
            "type": "snapshot_coverage",
            "snapshot_id": "snapshot-test",
            "indexed_artifact_kinds": ["cobol_program", "copybook"],
            "missing_artifacts": [],
            "runtime_state_indexed": False,
        }

    def search_code(self, query: str, *, limit: int = 10) -> dict[str, object]:
        arguments = {"query": query, "limit": limit}
        self.calls.append(("search_code", arguments))
        result = self._base("search_code")
        if query == "NONE":
            result.update({"status": "NOT_FOUND", "hits": [], "evidence_refs": []})
            return result
        suffix = query.replace(" ", "_")
        evidence_id = f"ev_{suffix}"
        result.update(
            {
                "hits": [
                    {
                        "unit_id": f"unit_{suffix}",
                        "symbol_id": f"symbol_{suffix}",
                        "name": query,
                        "evidence_ref": {"evidence_id": evidence_id},
                    }
                ],
                "evidence_refs": [{"evidence_id": evidence_id}],
            }
        )
        return result

    def inspect_symbol(
        self,
        name: str,
        *,
        program_name: str | None = None,
        symbol_type: str | None = None,
        max_relations: int = 40,
    ) -> dict[str, object]:
        arguments = {
            "name": name,
            "program_name": program_name,
            "symbol_type": symbol_type,
            "max_relations": max_relations,
        }
        self.calls.append(("inspect_symbol", arguments))
        return self._base("inspect_symbol")

    def trace_relations(
        self,
        start_name: str,
        *,
        program_name: str | None = None,
        symbol_type: str | None = None,
        relation_types: list[str] | None = None,
        direction: str = "outgoing",
        max_depth: int = 3,
        max_edges: int = 80,
    ) -> dict[str, object]:
        arguments = {
            "start_name": start_name,
            "program_name": program_name,
            "symbol_type": symbol_type,
            "relation_types": relation_types,
            "direction": direction,
            "max_depth": max_depth,
            "max_edges": max_edges,
        }
        self.calls.append(("trace_relations", arguments))
        return self._base("trace_relations")

    def read_evidence(
        self,
        evidence_ids: list[str],
        *,
        max_chars: int = 16_000,
    ) -> dict[str, object]:
        arguments = {"evidence_ids": evidence_ids, "max_chars": max_chars}
        self.calls.append(("read_evidence", arguments))
        result = self._base("read_evidence")
        result["spans"] = [
            {
                "evidence_id": evidence_id,
                "relative_path": "programs/TEST.cbl",
                "start_line": 10,
                "end_line": 12,
                "source_sha256": "0" * 64,
                "integrity": "VALID",
                "content_type": "UNTRUSTED_SOURCE_TEXT",
                "source_text": "COMPUTE OUT-AMOUNT = IN-AMOUNT",
                "span_truncated": False,
            }
            for evidence_id in evidence_ids
        ]
        return result


class AgentLoopTests(unittest.TestCase):
    def test_success_normalizes_native_and_json_actions_and_validates_citation(self) -> None:
        client = FakeClient(
            [
                native_action(
                    "search_code", {"query": "OUT-AMOUNT", "limit": 3}, call_id="call_1"
                ),
                json_action("read_evidence", {"evidence_ids": ["ev_OUT-AMOUNT"]}),
                json_action(
                    "final_answer",
                    {
                        "answer": "OUT-AMOUNT is computed from IN-AMOUNT.",
                        "claims": [
                            {
                                "claim": "OUT-AMOUNT is computed from IN-AMOUNT.",
                                "kind": "code_fact",
                                "code_anchors": ["OUT-AMOUNT", "IN-AMOUNT"],
                                "evidence_ids": ["ev_OUT-AMOUNT"],
                                "support_status": "supported",
                            }
                        ],
                        "evidence_ids": ["ev_OUT-AMOUNT"],
                        "boundaries": [],
                    },
                ),
            ]
        )
        tools = FakeTools()

        result = BoundedAgentLoop(client, tools).run("How is OUT-AMOUNT computed?")

        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["tool_calls_used"], 2)
        self.assertEqual(result["evidence_ids"], ["ev_OUT-AMOUNT"])
        self.assertEqual(result["claims"][0]["support_status"], "partial")
        self.assertIn("## 结论", result["answer"])
        self.assertIn("programs/TEST.cbl:10-12", result["answer"])
        self.assertEqual(
            [name for name, _ in tools.calls], ["search_code", "read_evidence"]
        )
        second_turn_messages = client.requests[1]["messages"]
        self.assertEqual(second_turn_messages[-2]["role"], "assistant")
        self.assertEqual(second_turn_messages[-2]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(second_turn_messages[-1]["role"], "tool")
        self.assertEqual(second_turn_messages[-1]["tool_call_id"], "call_1")
        third_turn_messages = client.requests[2]["messages"]
        self.assertEqual(third_turn_messages[-2]["role"], "assistant")
        self.assertEqual(third_turn_messages[-1]["role"], "tool")
        self.assertNotIn(
            "COMPUTE OUT-AMOUNT",
            json.dumps(result["tool_trace"], ensure_ascii=False),
        )

    def test_unauthorized_tool_and_extra_argument_are_never_executed(self) -> None:
        cases = [
            ("run_shell", {"command": "whoami"}, "unauthorized_tool"),
            (
                "search_code",
                {"query": "OUT-AMOUNT", "path": "/private/source.cbl"},
                "invalid_tool_arguments",
            ),
        ]
        for action, arguments, reason in cases:
            with self.subTest(action=action):
                tools = FakeTools()
                result = BoundedAgentLoop(
                    FakeClient([json_action(action, arguments)]), tools
                ).run("Find the amount")
                self.assertEqual(result["status"], "ABSTAINED")
                self.assertEqual(result["stop_reason"], reason)
                self.assertEqual(tools.calls, [])

    def test_six_call_budget_is_a_hard_limit(self) -> None:
        responses = [
            json_action("search_code", {"query": f"FIELD-{index}"})
            for index in range(1, MAX_TOOL_CALLS + 2)
        ]
        tools = FakeTools()

        result = BoundedAgentLoop(FakeClient(responses), tools).run("Trace fields")

        self.assertEqual(result["status"], "ABSTAINED")
        self.assertEqual(result["stop_reason"], "tool_budget_exhausted")
        self.assertEqual(result["tool_calls_used"], MAX_TOOL_CALLS)
        self.assertEqual(len(tools.calls), MAX_TOOL_CALLS)

    def test_two_consecutive_calls_without_new_ids_stop_the_loop(self) -> None:
        client = FakeClient(
            [
                json_action("search_code", {"query": "NONE"}),
                json_action("search_code", {"query": "NONE"}),
                json_action("search_code", {"query": "SHOULD-NOT-RUN"}),
            ]
        )
        tools = FakeTools()

        result = BoundedAgentLoop(client, tools).run("Find an absent symbol")

        self.assertEqual(result["stop_reason"], "no_progress")
        self.assertEqual(result["tool_calls_used"], 2)
        self.assertEqual(len(tools.calls), 2)
        self.assertEqual(len(client.requests), 2)

    def test_final_reference_must_be_discovered_and_pass_read_evidence(self) -> None:
        client = FakeClient(
            [
                json_action("search_code", {"query": "OUT-AMOUNT"}),
                json_action("read_evidence", {"evidence_ids": ["ev_OUT-AMOUNT"]}),
                json_action(
                    "final_answer",
                    {
                        "claims": [
                            {
                                "claim": "OUT-AMOUNT has an unsupported citation.",
                                "kind": "code_fact",
                                "code_anchors": ["OUT-AMOUNT"],
                                "evidence_ids": ["ev_OTHER"],
                                "support_status": "supported",
                            }
                        ],
                        "evidence_ids": ["ev_OTHER"],
                        "boundaries": [],
                    },
                ),
            ]
        )

        result = BoundedAgentLoop(client, FakeTools()).run("Explain the result")

        self.assertEqual(result["status"], "ABSTAINED")
        self.assertEqual(result["stop_reason"], "invalid_evidence_reference")
        self.assertEqual(result["claims"], [])
        self.assertIn("ev_OUT-AMOUNT", result["verified_evidence_ids"])

    def test_read_evidence_cannot_escape_current_investigation(self) -> None:
        tools = FakeTools()
        client = FakeClient(
            [json_action("read_evidence", {"evidence_ids": ["ev_secret"]})]
        )

        result = BoundedAgentLoop(client, tools).run("Read an arbitrary ID")

        self.assertEqual(result["stop_reason"], "invalid_tool_arguments")
        self.assertEqual(tools.calls, [])

    def test_explicit_refusal_is_structured_and_needs_no_tool(self) -> None:
        client = FakeClient(
            [
                json_action(
                    "abstain",
                    {
                        "answer": "The static snapshot cannot prove a production value.",
                        "claims": [],
                        "evidence_ids": [],
                        "boundaries": [
                            {
                                "reason": "Production runtime data is not indexed.",
                                "required_artifact": "approved runtime snapshot",
                            }
                        ],
                    },
                )
            ]
        )
        tools = FakeTools()

        result = BoundedAgentLoop(client, tools).run(
            "What is the current production premium?"
        )

        self.assertEqual(result["status"], "ABSTAINED")
        self.assertEqual(result["stop_reason"], "model_abstained")
        self.assertEqual(result["claims"], [])
        self.assertTrue(result["boundaries"])
        self.assertEqual(tools.calls, [])

    def test_model_prose_cannot_bypass_claim_citations(self) -> None:
        client = FakeClient(
            [
                json_action("search_code", {"query": "OUT-AMOUNT"}),
                json_action("read_evidence", {"evidence_ids": ["ev_OUT-AMOUNT"]}),
                json_action(
                    "final_answer",
                    {
                        "answer": "HALLUCINATED-RUNTIME-VALUE is 999.",
                        "claims": [
                            {
                                "claim": "OUT-AMOUNT is assigned in the indexed code.",
                                "kind": "code_fact",
                                "code_anchors": ["OUT-AMOUNT"],
                                "evidence_ids": ["ev_OUT-AMOUNT"],
                                "support_status": "supported",
                            }
                        ],
                        "evidence_ids": ["ev_OUT-AMOUNT"],
                        "boundaries": [],
                    },
                ),
            ]
        )

        result = BoundedAgentLoop(client, FakeTools()).run("Explain OUT-AMOUNT")

        self.assertEqual(result["status"], "PARTIAL")
        self.assertNotIn("HALLUCINATED-RUNTIME-VALUE", result["answer"])
        self.assertNotIn("model_answer", result)
        self.assertFalse(result["model_answer_recorded"])

    def test_runtime_determines_support_status_without_model_upgrade(self) -> None:
        def run_with(claim_status: str, boundaries: list[object]) -> dict[str, object]:
            return BoundedAgentLoop(
                FakeClient(
                    [
                        json_action("search_code", {"query": "OUT-AMOUNT"}),
                        json_action(
                            "read_evidence", {"evidence_ids": ["ev_OUT-AMOUNT"]}
                        ),
                        json_action(
                            "final_answer",
                            {
                                "status": "SUPPORTED",
                                "claims": [
                                    {
                                        "claim": "OUT-AMOUNT is checked.",
                                        "kind": "code_fact",
                                        "code_anchors": ["OUT-AMOUNT"],
                                        "evidence_ids": ["ev_OUT-AMOUNT"],
                                        "support_status": claim_status,
                                    }
                                ],
                                "evidence_ids": ["ev_OUT-AMOUNT"],
                                "boundaries": boundaries,
                            },
                        ),
                    ]
                ),
                FakeTools(),
            ).run("Check status")

        partial = run_with("partial", ["The complete value flow is unavailable."])
        bounded = run_with("supported", ["Runtime configuration is absent."])
        unsupported = run_with("unsupported", [])

        self.assertEqual(partial["status"], "PARTIAL")
        self.assertEqual(bounded["status"], "PARTIAL")
        self.assertEqual(unsupported["status"], "ABSTAINED")
        self.assertEqual(unsupported["stop_reason"], "unsupported_claim")

    def test_non_native_strict_json_mode_omits_tool_fields_from_request(self) -> None:
        client = FakeClient(
            [
                json_action("search_code", {"query": "OUT-AMOUNT"}),
                json_action(
                    "abstain",
                    {
                        "claims": [],
                        "evidence_ids": [],
                        "boundaries": ["More source is required."],
                    },
                ),
            ]
        )

        result = BoundedAgentLoop(
            client,
            FakeTools(),
            native_tool_calling=False,
            strict_json=True,
        ).run("Locate OUT-AMOUNT")

        self.assertEqual(result["status"], "ABSTAINED")
        self.assertNotIn("tools", client.requests[0])
        self.assertNotIn("tool_choice", client.requests[0])
        self.assertEqual(
            client.requests[0]["response_format"]["type"], "json_schema"
        )
        history = client.requests[1]["messages"]
        self.assertEqual(history[-2]["role"], "assistant")
        self.assertEqual(history[-1]["role"], "user")
        self.assertEqual(json.loads(history[-1]["content"])["type"], "TOOL_RESULT")
        self.assertFalse(any(message.get("role") == "tool" for message in history))
        self.assertFalse(any("tool_calls" in message for message in history))

    def test_multiple_native_tool_calls_are_rejected(self) -> None:
        response = native_action("search_code", {"query": "A"})
        second = native_action("search_code", {"query": "B"})
        response["choices"][0]["message"]["tool_calls"].append(
            second["choices"][0]["message"]["tool_calls"][0]
        )

        with self.assertRaises(ModelProtocolError):
            normalize_model_output(response)

    def test_semantically_contradictory_claim_is_never_fully_supported(self) -> None:
        result = BoundedAgentLoop(
            FakeClient(
                [
                    json_action("search_code", {"query": "OUT-AMOUNT"}),
                    json_action(
                        "read_evidence", {"evidence_ids": ["ev_OUT-AMOUNT"]}
                    ),
                    json_action(
                        "final_answer",
                        {
                            "claims": [
                                {
                                    "claim": (
                                        "OUT-AMOUNT is never computed from IN-AMOUNT."
                                    ),
                                    "kind": "code_fact",
                                    "code_anchors": ["OUT-AMOUNT", "IN-AMOUNT"],
                                    "evidence_ids": ["ev_OUT-AMOUNT"],
                                    "support_status": "supported",
                                }
                            ],
                            "evidence_ids": ["ev_OUT-AMOUNT"],
                            "boundaries": [],
                        },
                    ),
                ]
            ),
            FakeTools(),
        ).run("Is the amount computed?")

        self.assertEqual(result["status"], "PARTIAL")
        self.assertFalse(result["verification"]["semantic_claim_support_checked"])
        self.assertTrue(
            any(
                boundary.get("reason") == "semantic_claim_support_not_checked"
                for boundary in result["boundaries"]
                if isinstance(boundary, dict)
            )
        )

    def test_undeclared_program_identifier_is_rejected(self) -> None:
        result = BoundedAgentLoop(
            FakeClient(
                [
                    json_action("search_code", {"query": "OUT-AMOUNT"}),
                    json_action(
                        "read_evidence", {"evidence_ids": ["ev_OUT-AMOUNT"]}
                    ),
                    json_action(
                        "final_answer",
                        {
                            "claims": [
                                {
                                    "claim": "SYNP999 writes OUT-AMOUNT.",
                                    "kind": "code_fact",
                                    "code_anchors": ["OUT-AMOUNT"],
                                    "evidence_ids": ["ev_OUT-AMOUNT"],
                                    "support_status": "supported",
                                }
                            ],
                            "evidence_ids": ["ev_OUT-AMOUNT"],
                            "boundaries": [],
                        },
                    ),
                ]
            ),
            FakeTools(),
        ).run("Explain it")

        self.assertEqual(result["stop_reason"], "unsupported_claim_content")

    def test_tool_result_contract_version_is_required(self) -> None:
        class MissingContractTools(FakeTools):
            def search_code(
                self, query: str, *, limit: int = 10
            ) -> dict[str, object]:
                result = super().search_code(query, limit=limit)
                result.pop("contract_version")
                return result

        client = FakeClient([json_action("search_code", {"query": "OUT-AMOUNT"})])
        result = BoundedAgentLoop(client, MissingContractTools()).run("Find it")

        self.assertEqual(result["stop_reason"], "tool_contract_mismatch")
        self.assertEqual(result["tool_trace"][0]["outcome"], "POLICY_REJECTED")

    def test_non_read_tool_cannot_smuggle_source_to_model(self) -> None:
        secret = "SECRET SOURCE BODY"

        class SourceSmugglingTools(FakeTools):
            def search_code(
                self, query: str, *, limit: int = 10
            ) -> dict[str, object]:
                result = super().search_code(query, limit=limit)
                result["source_text"] = secret
                return result

        client = FakeClient([json_action("search_code", {"query": "OUT-AMOUNT"})])
        result = BoundedAgentLoop(client, SourceSmugglingTools()).run("Find it")

        self.assertEqual(result["stop_reason"], "tool_contract_mismatch")
        self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))
        self.assertEqual(len(client.requests), 1)

    def test_read_ok_requires_complete_well_formed_spans(self) -> None:
        class EmptyReadTools(FakeTools):
            def read_evidence(
                self, evidence_ids: list[str], *, max_chars: int = 16_000
            ) -> dict[str, object]:
                result = super().read_evidence(
                    evidence_ids, max_chars=max_chars
                )
                result["spans"] = []
                return result

        result = BoundedAgentLoop(
            FakeClient(
                [
                    json_action("search_code", {"query": "OUT-AMOUNT"}),
                    json_action(
                        "read_evidence", {"evidence_ids": ["ev_OUT-AMOUNT"]}
                    ),
                ]
            ),
            EmptyReadTools(),
        ).run("Explain it")

        self.assertEqual(result["stop_reason"], "tool_contract_mismatch")
        self.assertEqual(result["verified_evidence_ids"], [])

    def test_scope_violation_redacts_rejected_result_metadata(self) -> None:
        class ExtraEvidenceTools(FakeTools):
            def read_evidence(
                self, evidence_ids: list[str], *, max_chars: int = 16_000
            ) -> dict[str, object]:
                result = super().read_evidence(
                    evidence_ids, max_chars=max_chars
                )
                result["spans"].append(
                    {
                        "evidence_id": "ev_secret",
                        "relative_path": "secret/PAYROLL.cbl",
                        "start_line": 1,
                        "end_line": 1,
                        "source_sha256": "1" * 64,
                        "integrity": "VALID",
                        "content_type": "UNTRUSTED_SOURCE_TEXT",
                        "source_text": "SECRET",
                        "span_truncated": False,
                    }
                )
                return result

        result = BoundedAgentLoop(
            FakeClient(
                [
                    json_action("search_code", {"query": "OUT-AMOUNT"}),
                    json_action(
                        "read_evidence", {"evidence_ids": ["ev_OUT-AMOUNT"]}
                    ),
                ]
            ),
            ExtraEvidenceTools(),
        ).run("Explain it")
        rendered = json.dumps(result["tool_trace"], ensure_ascii=False)

        self.assertEqual(result["stop_reason"], "evidence_scope_violation")
        self.assertNotIn("ev_secret", rendered)
        self.assertNotIn("PAYROLL.cbl", rendered)
        self.assertNotIn("SECRET", rendered)

    def test_multiple_choices_and_mixed_protocols_are_rejected(self) -> None:
        multiple_choices = json_action("search_code", {"query": "A"})
        multiple_choices["choices"].append(
            json_action("search_code", {"query": "B"})["choices"][0]
        )
        mixed = {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "search_code",
                    "arguments": '{"query":"A"}',
                }
            ],
            "choices": json_action("search_code", {"query": "B"})["choices"],
        }

        for response in (multiple_choices, mixed):
            with self.subTest(response=response):
                with self.assertRaises(ModelProtocolError):
                    normalize_model_output(response)

    def test_empty_tool_calls_field_is_not_json_fallback(self) -> None:
        response = json_action("search_code", {"query": "A"})
        response["choices"][0]["message"]["tool_calls"] = []

        with self.assertRaises(ModelProtocolError):
            normalize_model_output(response)

    def test_duplicate_registry_definition_is_rejected(self) -> None:
        definitions = agent_loop_module.tool_definitions()
        with mock.patch.object(
            agent_loop_module,
            "tool_definitions",
            return_value=[*definitions, copy.deepcopy(definitions[0])],
        ):
            with self.assertRaises(RuntimeError):
                BoundedAgentLoop(FakeClient([]), FakeTools())


if __name__ == "__main__":
    unittest.main()
