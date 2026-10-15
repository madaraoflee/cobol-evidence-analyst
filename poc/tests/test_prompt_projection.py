"""Provider navigation sharing preserves source authority and distinct occurrences."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
import business_chat
from business_index import build_business_index
from business_synthesis import _analysis_brief_task, project_analysis_brief
from company_api import CompanyAPIConfig, TransportResponse
from prompt_projection import project_prompt_payload
from repository_discovery import ensure_repository_search


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def expand_projection(wire):
    """Independent consumer of inheritance and exact-record references."""
    output = deepcopy(wire)
    if "prompt_projection" not in output:
        return output
    marker = output.pop("prompt_projection")
    if marker["format"] != "shared-navigation/v1":
        raise AssertionError("Unsupported projection contract")
    facts = output.pop("shared_navigation_facts", {})

    def expand(value):
        if isinstance(value, list):
            return [expand(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "navigation_fact_ref" in value:
            reference = value["navigation_fact_ref"]
            value = {**deepcopy(facts[reference]), **{key: item for key, item in value.items()
                                                      if key != "navigation_fact_ref"}}
        result = {key: item for key, item in value.items() if not key.endswith("_shared_fields")}
        for key in list(result):
            shared = value.get(key + "_shared_fields")
            if shared is not None:
                if not isinstance(result[key], list):
                    raise AssertionError("Shared fields need occurrence records")
                result[key] = [{**deepcopy(shared), **row} for row in result[key]]
            result[key] = expand(result[key])
        return result

    for key in ("business_map", "evidence_groups", "business_analysis_brief", "question_investigation"):
        if key in output:
            wrapper = {key: output[key]}
            shared_key = key + "_shared_fields"
            if shared_key in output:
                wrapper[shared_key] = output.pop(shared_key)
            output[key] = expand(wrapper)[key]
    for context in output.get("source_context", []):
        for key in list(context):
            if key not in {"pages", "working_set"}:
                context[key] = expand(context[key])
    return output


class PromptProjectionTests(unittest.TestCase):
    @staticmethod
    def payload():
        text = 'IF IN-AMOUNT > 7\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2\nEND-IF.\nDISPLAY "CALL *>".'
        page = {"evidence_id": "ev:raw", "relative_path": "rule.cbl", "source_text": text,
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(), "start_line": 11, "end_line": 14,
            "include_chain": [{"relative_path": "caller.cbl", "line": 7}], "replacement_trace": ["SRC BY DST"],
            "span_truncated": False, "selection_reasons": ["complete_working_set"]}
        guard = {"kind": "IF", "condition": "IN-AMOUNT > 7", "outcome": True,
            "source_sha256": page["source_sha256"], "start_line": 11, "end_line": 11,
            "execution_verified": False, "interpretation": "source_syntax_not_execution"}
        statements = [{"occurrence_id": f"write:{index}", "relative_path": "rule.cbl",
            "start_line": index + 12, "end_line": index + 12, "kind": "COMPUTE",
            "expression": "IN-AMOUNT * 2", "writes": ["OUT-AMOUNT"], "reads": ["IN-AMOUNT"],
            "guards": [deepcopy(guard)], "evidence_ids": ["ev:raw"], "effects_unknown": False,
            "execution_verified": False, "source_sha256": page["source_sha256"]}
            for index in range(18)]
        return {"question": "为什么最后变成零？", "history": [{"role": "user", "content": "刚才第二种情况呢？"}],
            "source_context": [{"pages": [page], "working_set": {"physical_complete": True,
                "supplied_complete_paths": ["rule.cbl"], "frontier": [{"reason": "runtime_input"}],
                "source_manifest": [{"evidence_id": "ev:raw", "sha256": page["source_sha256"]}]},
                "call_chain": {"links": [], "omitted_links": 9}, "outline": []}],
            "framework_references": [{"reference_id": "fw:1", "text": "调用后状态依赖实际返回。",
                "document_sha256": "f" * 64, "extra": {"pages": [deepcopy(page)]}}],
            "framework_facts": [{"fact_id": "fw:fact", "effect": "unknown", "source_text": text}],
            "business_map": {"relations": [], "candidate_count": 101, "omitted_rules": 19},
            "evidence_groups": [{"group_id": f"group:{index}", "anchor": {"line": index + 1},
                "semantic_analysis": {"slice_statements": deepcopy(statements),
                    "unknown_effects": [{"reason": "unknown_call", "affected_storage": "all_visible_storage"}]},
                "required_evidence_ids": ["ev:raw"], "core_evidence_ids": ["ev:raw"],
                "open_frontier": [{"reason": "unknown_call", "runtime_value_known": False}]}
                for index in range(4)],
            "question_investigation": {"required_items": [{"kind": "formula", "status": "SATISFIED",
                "evidence_ids": ["ev:raw"]}], "candidate_count": 32, "planned_actions": [],
                "open_gaps": [{"reason": "external_scheduler_not_supplied"}]},
            "business_analysis_brief": {"detail_requested": True, "syntax_guide": {"facts": deepcopy(statements)},
                "behavior_guide": {"observations": [deepcopy(guard)]}, "required_answer_aspects": ["formula", "later_writes"],
                "call_effect_boundaries": {"required": True, "reasons": [{"kind": "unknown_call"}]},
                "task": "Custom guidance is retained."}}

    def test_sharing_round_trips_every_fact_without_mutating_canonical(self):
        payload = self.payload()
        original = deepcopy(payload)
        projected = project_prompt_payload(payload)
        self.assertIn("prompt_projection", projected)
        self.assertEqual(encoded(expand_projection(projected)), encoded(payload))
        self.assertEqual(payload, original)
        self.assertLess(len(encoded(projected)), len(encoded(payload)) * .75)
        self.assertEqual(projected["source_context"][0]["pages"], payload["source_context"][0]["pages"])
        self.assertEqual(projected["source_context"][0]["working_set"], payload["source_context"][0]["working_set"])
        for key in ("question", "history", "framework_references", "framework_facts"):
            self.assertEqual(projected[key], payload[key])
        # A consumer changing the projected evidence cannot alter the canonical ledger.
        projected["source_context"][0]["pages"][0]["source_text"] = "changed"
        self.assertEqual(payload, original)

    def test_different_occurrences_guards_versions_and_copy_instances_remain_distinct(self):
        payload = self.payload()
        facts = payload["business_analysis_brief"]["syntax_guide"]["facts"]
        facts[1]["guards"][0]["outcome"] = False
        facts[2]["source_sha256"] = "b" * 64
        facts[3]["include_chain"] = [{"relative_path": "different.cbl", "line": 19}]
        restored = expand_projection(project_prompt_payload(payload))
        self.assertEqual(restored, payload)
        self.assertEqual(len({fact["occurrence_id"] for fact in restored["business_analysis_brief"]["syntax_guide"]["facts"]}), 18)
        for group in restored["evidence_groups"]:
            self.assertEqual(group["required_evidence_ids"], ["ev:raw"])
            self.assertEqual(group["core_evidence_ids"], ["ev:raw"])
            self.assertEqual(group["open_frontier"], payload["evidence_groups"][0]["open_frontier"])

    def test_missing_null_false_and_zero_are_never_equal_defaults(self):
        payload = self.payload()
        rows = payload["business_analysis_brief"]["syntax_guide"]["facts"]
        for row, optional in zip(rows, [None, False, 0]):
            row["optional"] = optional
        result = expand_projection(project_prompt_payload(payload))
        self.assertEqual(result, payload)
        self.assertIs(result["business_analysis_brief"]["syntax_guide"]["facts"][0]["optional"], None)
        self.assertIs(result["business_analysis_brief"]["syntax_guide"]["facts"][1]["optional"], False)
        self.assertNotIn("optional", result["business_analysis_brief"]["syntax_guide"]["facts"][3])

    def test_projection_rebuilds_after_source_and_candidate_trim(self):
        payload = self.payload()
        projected = project_prompt_payload(payload)
        payload["source_context"][0]["pages"].clear()
        payload["evidence_groups"] = payload["evidence_groups"][:1]
        payload["business_analysis_brief"]["syntax_guide"]["facts"] = []
        payload["question_investigation"]["required_items"][0].update(status="OPEN", evidence_ids=[])
        result = expand_projection(project_prompt_payload(payload))
        self.assertEqual(result, payload)
        self.assertTrue(projected["source_context"][0]["pages"])
        self.assertEqual(result["source_context"][0]["pages"], [])

    def test_small_input_and_reserved_names_are_preserved(self):
        small = {"question": "简短回答", "source_context": [{"pages": []}]}
        self.assertIs(project_prompt_payload(small), small)
        for key in ("navigation_fact_ref", "facts_shared_fields", "prompt_projection"):
            payload = self.payload()
            payload["business_analysis_brief"][key] = {"original": True}
            self.assertIs(project_prompt_payload(payload), payload)

    def test_projection_is_idempotent_and_keeps_unknown_instruction(self):
        payload = self.payload()
        once = project_prompt_payload(payload)
        self.assertIs(project_prompt_payload(once), once)
        self.assertEqual(expand_projection(once)["business_analysis_brief"]["task"], "Custom guidance is retained.")

    def test_exact_fact_is_shared_across_navigation_layers_with_provenance(self):
        payload = self.payload()
        fact = {"kind": "possible_storage_effect", "relative_path": "worker.cbl",
            "source_sha256": "c" * 64, "start_line": 19, "end_line": 21,
            "source_refs": [{"relative_path": "worker.cbl", "source_sha256": "c" * 64,
                "start_line": 19, "end_line": 21, "evidence_id": "ev:worker"}],
            "expression": "OUT-AMOUNT = IN-AMOUNT * 2", "guards": [{"condition": "IN-AMOUNT > 7"}],
            "execution_verified": False, "unknown_effects": ["runtime_input"],
            "support_basis": "possible_source_flow_not_runtime_execution"}
        payload["business_map"]["effect_candidates"] = [deepcopy(fact)]
        payload["question_investigation"]["observations"] = [deepcopy(fact)]
        payload["business_analysis_brief"]["effect_candidates"] = [deepcopy(fact)]
        projected = project_prompt_payload(payload)
        self.assertIn("shared_navigation_facts", projected)
        self.assertEqual(expand_projection(projected), payload)
        refs = [projected[key][field][0]["navigation_fact_ref"] for key, field in (
            ("business_map", "effect_candidates"), ("question_investigation", "observations"),
            ("business_analysis_brief", "effect_candidates"))]
        self.assertEqual(len(set(refs)), 1)
        self.assertEqual(projected["shared_navigation_facts"][refs[0]], fact)

    def test_nested_source_and_framework_text_never_move_to_shared_facts(self):
        payload = self.payload()
        page = payload["source_context"][0]["pages"][0]
        payload["business_map"]["diagnostics"] = [{"kind": "source_detail", "pages": [deepcopy(page)]}
                                                    for _ in range(4)]
        projected = project_prompt_payload(payload)
        for diagnostic in projected["business_map"]["diagnostics"]:
            self.assertEqual(diagnostic["pages"], [page])
        self.assertEqual(expand_projection(projected), payload)

    def test_repeated_parameter_sets_keep_each_call_and_its_local_conditions(self):
        payload = self.payload()
        parameters = [{"position": 1, "name": "IO-PARAMETERS", "mode": "REFERENCE"},
                      {"position": 2, "name": "REQUEST-RECORD", "mode": "REFERENCE"}]
        layout = [{"argument": "REQUEST-RECORD", "relative_path": "request.cpy",
                   "source_sha256": "d" * 64, "start_line": 1, "end_line": 18,
                   "members": ["REQUEST-ID", "REQUEST-AMOUNT", "REQUEST-STATUS"],
                   "evidence_ids": ["ev:layout"], "execution_verified": False}]
        reasons = []
        for index in range(9):
            reasons.append({"occurrence_id": "call:" + str(index), "target": "RECORDSTORE",
                "start_line": 20 + index, "end_line": 20 + index,
                "actual_parameters": deepcopy(parameters), "mutable_output_groups": deepcopy(layout),
                "guards": [{"condition": "READY-FLAG = " + str(index % 2), "outcome": True}],
                "supplied_reference_ids": ["ev:call:" + str(index)],
                "source_sha256": ("a" if index < 7 else "b") * 64,
                "unknown_effects": ["target_source_not_supplied"], "execution_verified": False})
        for reason in reasons[-2:]:
            reason["target"] = "CHECKPOINTSTORE"
            reason["actual_parameters"][1]["name"] = "RESTART-STATE"
            reason["mutable_output_groups"][0]["argument"] = "RESTART-STATE"
        # The same field spelling in a different COPY instance remains distinct.
        reasons[-1]["mutable_output_groups"][0]["include_chain"] = [{"path": "other.cbl", "line": 5}]
        payload["business_analysis_brief"]["call_effect_boundaries"]["reasons"] = reasons
        projected = project_prompt_payload(payload)
        rows = projected["business_analysis_brief"]["call_effect_boundaries"]["reasons"]
        self.assertTrue(any("navigation_fact_ref" in row for row in rows))
        self.assertTrue(any("actual_parameters" in fact for fact in projected["shared_navigation_facts"].values()))
        self.assertEqual([row["occurrence_id"] for row in rows], [row["occurrence_id"] for row in reasons])
        self.assertEqual([row["supplied_reference_ids"] for row in rows],
                         [row["supplied_reference_ids"] for row in reasons])
        self.assertEqual(expand_projection(projected), payload)
        self.assertEqual(payload["business_analysis_brief"]["call_effect_boundaries"]["reasons"], reasons)

    def test_observation_subsets_keep_exact_scope_versions_and_unknowns(self):
        payload = self.payload()
        observations = []
        for program, digest, first in (("BATCH-A", "a" * 64, 20), ("BATCH-B", "b" * 64, 80)):
            for index in range(12):
                observations.append({"occurrence_id": f"{program}:{index}", "kind": "field_assignment",
                    "program_name": program, "relative_path": "programs/" + program + ".cbl",
                    "source_sha256": digest, "evidence_ids": ["ev:" + program],
                    "source_locations": [{"start_line": first + index, "end_line": first + index}],
                    "expression": "IN-AMOUNT * 2" if index % 2 else "0",
                    "condition": "IN-AMOUNT > 7" if index % 2 else "FLAG = 1",
                    "execution_verified": False, "effect_status": "candidate_not_execution_proof",
                    "unknown_effects": ["runtime_input_unknown"]})
        observations[0]["optional"] = None
        observations[1]["optional"] = False
        observations[2]["optional"] = 0
        payload["question_investigation"]["file_impact"] = {"observations": observations, "truncated": False}
        projected = project_prompt_payload(payload)
        rows = projected["question_investigation"]["file_impact"]["observations"]
        self.assertTrue(all("navigation_fact_ref" in row for row in rows))
        self.assertEqual([row["evidence_ids"] for row in rows], [row["evidence_ids"] for row in observations])
        self.assertEqual(expand_projection(projected), payload)
        # Consumer support is shared with the packaged Windows acceptance tool.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from tools.desktop_acceptance_runtime import decoded_navigation
        self.assertEqual(decoded_navigation(business_chat._compact_prompt_payload(projected)), payload)

    def test_subsets_do_not_treat_arrays_as_execution_or_drop_authority(self):
        payload = self.payload()
        page = payload["source_context"][0]["pages"][0]
        rows = [{"occurrence_id": str(index), "source_text": page["source_text"],
                 "source_sha256": page["source_sha256"], "pages": [deepcopy(page)],
                 "parameters": [{"name": "IN-AMOUNT", "mode": "REFERENCE"}],
                 "unknown": "target_source_not_supplied", "execution_verified": False}
                for index in range(8)]
        payload["question_investigation"]["diagnostics"] = rows
        projected = project_prompt_payload(payload)
        for row in projected["question_investigation"]["diagnostics"]:
            self.assertEqual(row["source_text"], page["source_text"])
            self.assertEqual(row["pages"], [page])
        self.assertEqual(expand_projection(projected), payload)

    def test_only_known_generic_brief_guidance_changes(self):
        brief = self.payload()["business_analysis_brief"]
        for detail in (False, True):
            for syntax in (False, True):
                with self.subTest(detail=detail, syntax=syntax):
                    current = deepcopy(brief)
                    current["detail_requested"] = detail
                    current["syntax_guide"]["facts"] = brief["syntax_guide"]["facts"] if syntax else []
                    current["task"] = _analysis_brief_task(detail, syntax)
                    compact = project_analysis_brief(current)
                    self.assertLess(len(compact["task"].encode()), len(current["task"].encode()))
                    self.assertEqual({key: value for key, value in compact.items() if key != "task"},
                                     {key: value for key, value in current.items() if key != "task"})
                    self.assertEqual(current["task"], _analysis_brief_task(detail, syntax))
                    current["task"] += " Preserve an additional custom rule."
                    self.assertIs(project_analysis_brief(current), current)

    def test_canonical_guidance_matches_pre_projection_captured_requests(self):
        # Frozen from the existing public fixture's actual requests before
        # extraction of the guidance helper, rather than another implementation.
        expected = {
            True: "ac1e5ee61ea7dfb7345f80ca4e0ad54b4e01c1b96336552242bbfcee322f5f9e",
            False: "a7bb1cc93605eb47e4d7d245fa329e4d218a18375506b4c4e8d0f87365658bf0",
        }
        for syntax, digest in expected.items():
            self.assertEqual(hashlib.sha256(_analysis_brief_task(True, syntax).encode()).hexdigest(), digest)

    def test_real_fixture_keeps_actual_evidence_and_single_model_request(self):
        source = Path(__file__).resolve().parents[1] / "fixtures/framework-workbench/source"
        config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only")
        questions = ("When does the service request move to review, and what happens before saving it?",
                     "说说服务请求的batch操作，是怎么trigger的，好像是batch trigger的。",
                     "NIGHTBATCH 的批次流程如何执行，哪些记录会跳过，什么时候增加成功数量和提交进度？")
        original_prompt = business_chat._prompt_payload
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "index.sqlite"
            build_business_index(source, database, verify_content=True)
            ensure_repository_search(database, source)
            for question in questions:
                captured, sent = [], []

                def prompt(payload):
                    captured.append(deepcopy(payload))
                    return original_prompt(payload)

                def transport(request):
                    body = json.loads(request.body)
                    sent.append(body)
                    wire = json.loads(body["messages"][-1]["content"])
                    page = next(page for context in wire["source_context"] for page in context["pages"])
                    answer = f"当前程序根据输入和分支处理请求。[{page['evidence_id']}]"
                    return TransportResponse(200, json.dumps({"choices": [{"message": {"content": answer},
                                                                             "finish_reason": "stop"}]}))

                with mock.patch.object(business_chat, "_prompt_payload", side_effect=prompt):
                    output = business_chat.run_business_chat(question, database, source, config,
                        transport=transport, framework_reference_path="", capture_context=True,
                        policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))
                self.assertEqual(output["agent_result"]["metrics"]["model_requests"], 1)
                self.assertEqual(len(sent), 1)
                canonical = captured[-1]
                projected = project_prompt_payload(canonical)
                wire = json.loads(sent[0]["messages"][-1]["content"])
                self.assertEqual(encoded(wire), encoded(original_prompt(canonical)))
                self.assertIn("prompt_projection", wire)
                restored = expand_projection(projected)
                expected = deepcopy(canonical)
                expected["business_analysis_brief"] = project_analysis_brief(canonical["business_analysis_brief"])
                self.assertEqual(encoded(restored), encoded(expected))
                for before, after in zip(canonical["source_context"], projected["source_context"]):
                    self.assertEqual(before["pages"], after["pages"])
                    self.assertEqual(before.get("working_set"), after.get("working_set"))
                    for page in after["pages"]:
                        self.assertEqual(hashlib.sha256(page["source_text"].encode()).hexdigest(),
                            hashlib.sha256(next(p["source_text"] for p in before["pages"]
                                                if p["evidence_id"] == page["evidence_id"]).encode()).hexdigest())
                # Compare the same facts with the existing key-column codec;
                # cold/warm SQL counters cannot manufacture a size improvement.
                plain = business_chat._compact_prompt_payload(canonical)
                shared = business_chat._compact_prompt_payload(projected)
                self.assertLess(len(encoded(shared).encode()), len(encoded(plain).encode()) * .95)


if __name__ == "__main__":
    unittest.main()
