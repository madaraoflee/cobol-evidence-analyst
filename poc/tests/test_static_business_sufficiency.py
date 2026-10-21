"""Static business source can be complete while runtime scheduling is unknown."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[2]),
               str(Path(__file__).resolve().parents[1])]

from activation_evidence import investigate_activation
from agent_policy import AgentPolicy
from business_chat import _answer_scope_gaps, run_business_chat
from business_index import build_business_index
from business_map import build_business_map
from company_api import CompanyAPIConfig, TransportResponse
from question_investigation import build_question_investigation
from repository_discovery import ensure_repository_search
from tools.business_onboarding_fixture import BATCH_QUESTION, scripted_answer, sources


class StaticBusinessSufficiencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.contents = sources()
        self.build()

    def build(self):
        for path, text in self.contents.items():
            # Captured pages and imported source must hash the same LF bytes.
            (self.source / path).write_bytes(text.encode("utf-8"))
        build_business_index(self.source, self.database, source_format="free",
                             framework_reference_path="", quiet=True, verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.mapping = build_business_map(self.database, self.source, BATCH_QUESTION)

    def page(self, path, first=1, last=None):
        raw = (self.source / path).read_bytes()
        lines = raw.decode("utf-8").splitlines()
        last = len(lines) if last is None else last
        return {"relative_path": path, "source_sha256": hashlib.sha256(raw).hexdigest(),
                "source_text": "\n".join(lines[first-1:last]), "start_line": first,
                "end_line": last, "evidence_id": f"ev_{path}_{first}_{last}", "format_hint": "free"}

    def investigate(self, pages=None):
        if pages is None:
            pages = [self.page("settle.cbl"), self.page("nightjob.clle")]
        return build_question_investigation(BATCH_QUESTION, self.mapping,
            database_path=self.database, source_pages=pages, max_actions=1)

    def item(self, result, kind):
        return next(item for item in result["required_items"] if item["kind"] == kind)

    def test_supplied_cl_call_and_file_input_satisfy_static_source_not_runtime(self):
        result = self.investigate()
        for kind in ("entry_point", "caller_invocation", "activation_conditions", "inputs"):
            self.assertEqual(self.item(result, kind)["status"], "SATISFIED")
        caller, = result["activation_evidence"]["callers"]
        self.assertEqual((caller["relative_path"], caller["line"], caller["target_path"]),
                         ("nightjob.clle", 4, "settle.cbl"))
        self.assertEqual(caller["relation_type"], "CL_CALL")
        self.assertTrue(caller["static_target_name_match"])
        self.assertFalse(caller["binding_confirmed"])
        self.assertFalse(caller["runtime_object_verified"])
        self.assertFalse(caller["execution_verified"])
        self.assertFalse(result["activation_evidence"]["execution_verified"])
        self.assertEqual(self.item(result, "scheduler_binding")["status"], "OPEN")
        self.assertFalse(result["planned_actions"])
        material, limits = _answer_scope_gaps(BATCH_QUESTION, result)
        self.assertEqual(material, [])
        self.assertEqual([item["kind"] for item in limits], ["scheduler_binding"])
        self.assertFalse(limits[0]["execution_verified"])
        # The original obligation stays unknown; delivery classification does
        # not change it into a runtime fact.
        self.assertEqual(result["open_gaps"][0]["status"], "OPEN")

    def test_missing_cl_page_still_requires_a_bounded_original_source_read(self):
        result = self.investigate([self.page("settle.cbl")])
        self.assertNotEqual(self.item(result, "caller_invocation")["status"], "SATISFIED")
        action, = result["planned_actions"]
        self.assertEqual(action["tool"], "read")
        self.assertEqual(action["arguments"],
                         {"relative_path": "nightjob.clle", "start_line": 1, "end_line": 6})
        self.assertEqual(_answer_scope_gaps(BATCH_QUESTION, result), (result["open_gaps"], []))

    def test_wrong_version_changed_text_and_truncated_cl_never_bind(self):
        original = self.page("nightjob.clle")
        for changed in (
            {**original, "source_sha256": "0" * 64},
            {**original, "source_text": original["source_text"].replace("PGM(SETTLE)", "PGM(OTHER)")},
            {**original, "span_truncated": True},
        ):
            with self.subTest(changed=changed):
                result = self.investigate([self.page("settle.cbl"), changed])
                self.assertNotEqual(self.item(result, "caller_invocation")["status"], "SATISFIED")

    def test_captured_cl_context_is_rechecked_after_request_pages_are_trimmed(self):
        complete = self.investigate()
        result = investigate_activation(self.mapping, [self.page("settle.cbl")],
            database_path=self.database, cl_context=complete["cl_context"], max_actions=1)
        self.assertNotEqual(self.item(result, "caller_invocation")["status"], "SATISFIED")
        self.assertFalse(result["activation_evidence"]["callers"])
        self.assertLessEqual(len(result["planned_actions"]), 1)

    def test_guarded_cl_requires_actual_guard_in_current_supplied_pages(self):
        self.contents["nightjob.clle"] = (
            "PGM\nDCL VAR(&READY) TYPE(*CHAR) LEN(1)\n"
            "IF COND(&READY *EQ 'Y') THEN(DO)\nCALL PGM(SETTLE)\nENDDO\nENDPGM\n")
        self.build()
        complete = self.investigate()
        self.assertEqual(self.item(complete, "activation_conditions")["status"], "SATISFIED")
        self.assertTrue(complete["activation_evidence"]["callers"][0]["conditions"])
        partial = investigate_activation(self.mapping,
            [self.page("settle.cbl"), self.page("nightjob.clle", 4, 4)],
            database_path=self.database, cl_context=complete["cl_context"], max_actions=1)
        self.assertEqual(self.item(partial, "caller_invocation")["status"], "SATISFIED")
        self.assertNotEqual(self.item(partial, "activation_conditions")["status"], "SATISFIED")
        self.assertEqual(partial["planned_actions"][0]["arguments"]["relative_path"], "nightjob.clle")

    def test_same_named_programs_prevent_cl_name_from_resolving_a_unique_source(self):
        self.contents["alternative.cbl"] = self.contents["settle.cbl"]
        self.build()
        mapping = {**self.mapping, "direct_paths": ["settle.cbl"]}
        # Even if a previous focus selected one path, a literal CL program
        # name cannot choose between two source definitions.
        from batch_context_evidence import investigate_batch_context
        pages = [self.page("settle.cbl"), self.page("nightjob.clle")]
        batch = investigate_batch_context(mapping, pages, database_path=self.database)
        result = investigate_activation(mapping, pages, database_path=self.database,
                                       cl_context=batch["cl_context"])
        self.assertNotEqual(self.item(result, "caller_invocation")["status"], "SATISFIED")
        self.assertFalse(result["activation_evidence"]["callers"])

    def test_dynamic_cl_target_does_not_become_a_confirmed_static_caller(self):
        self.contents["nightjob.clle"] = "PGM\nDCL VAR(&TARGET) TYPE(*CHAR) LEN(10)\nCALL PGM(&TARGET)\nENDPGM\n"
        self.build()
        result = self.investigate()
        self.assertNotEqual(self.item(result, "caller_invocation")["status"], "SATISFIED")
        self.assertFalse(result["activation_evidence"]["callers"])

    def test_explicit_runtime_questions_do_not_hide_scheduler_gap(self):
        result = self.investigate()
        for suffix in ("请确认实际调度时间。", "它实际上是否执行？", "它怎么触发？",
                       "什么时候执行这项批处理？", "谁启动这项批处理？", "多久运行一次？",
                       "When does the job run?", "Verify runtime scheduling.",
                       "At what time the batch runs?", "Who launches this batch?", "How often does it execute?"):
            with self.subTest(suffix=suffix):
                self.assertEqual(_answer_scope_gaps(BATCH_QUESTION + suffix, result),
                                 (result["open_gaps"], []))
        self.assertEqual(_answer_scope_gaps("SETTLE 批处理怎么 trigger？", result),
                         (result["open_gaps"], []))

    def test_unknown_source_and_new_runtime_gap_kinds_remain_material(self):
        result = self.investigate()
        for gap in ({"kind": "inputs", "reason": "input_source_not_located", "status": "UNRESOLVED"},
                    {"kind": "scheduler_binding", "reason": "missing_source_rule", "status": "OPEN"}):
            changed = deepcopy(result)
            changed["open_gaps"].append(gap)
            material, limits = _answer_scope_gaps(BATCH_QUESTION, changed)
            self.assertEqual(material, [gap])
            self.assertEqual(len(limits), 1)
        for kind in ("entry_point", "caller_invocation", "activation_conditions"):
            changed = deepcopy(result)
            self.item(changed, kind)["evidence_ids"] = []
            self.assertEqual(_answer_scope_gaps(BATCH_QUESTION, changed), (changed["open_gaps"], []))

    def test_overview_with_explicit_initiator_keeps_runtime_gap_material(self):
        result = self.investigate()
        for question in (
            "请介绍 SERVICE 这项批处理的业务用途、输入输出，并确认实际发起者是谁。",
            "Explain the business purpose of this batch and who initiates it.",
        ):
            with self.subTest(question=question):
                self.assertEqual(_answer_scope_gaps(question, result), (result["open_gaps"], []))

    def test_local_io_followup_does_not_ask_for_unmentioned_online_program_launch(self):
        question = ("继续核对业务边界：这批处理遇到空输入会怎样？ORDER SKIPPED 到哪里？"
                    "BALANCEQ 是否任何异常都能返回90？请说明源码结论和缺少的运行资料。")
        history = [{"role": "user", "content": BATCH_QUESTION},
                   {"role": "assistant", "content": "批次请求调用 SETTLE。",
                    "evidence_refs": [{"relative_path": "settle.cbl"}, {"relative_path": "nightjob.clle"}]}]
        mapping = build_business_map(self.database, self.source, question,
                                     prior_paths=["settle.cbl", "nightjob.clle"], history=history)
        result = build_question_investigation(question, mapping, database_path=self.database,
            source_pages=[self.page(path) for path in self.contents], history=history)
        self.assertEqual(mapping["direct_paths"], ["balance.cbl"])
        self.assertNotIn("activation", result["analysis_plan"]["intents"])
        self.assertFalse({"caller_invocation", "activation_conditions", "scheduler_binding"}.intersection(
            item["kind"] for item in result["required_items"]))
        self.assertFalse(result["planned_actions"])
        self.assertFalse(result["semantic_execution_verified"])

    def test_live_pipeline_with_offline_transport_delivers_static_answer_and_runtime_limit(self):
        requests = []
        config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline",
                                  api_key="unused")
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            for kind in ("caller_invocation", "activation_conditions", "inputs"):
                self.assertEqual(self.item(payload["question_investigation"], kind)["status"], "SATISFIED")
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": scripted_answer(payload)}, "finish_reason": "stop"}]},
                ensure_ascii=False))
        output = run_business_chat(BATCH_QUESTION, self.database, self.source, config,
            transport=transport, framework_reference_path="",
            policy=AgentPolicy(max_model_requests=1))
        result = output["agent_result"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["status"], "ANALYZED")
        self.assertEqual(result["stop_reason"], "sufficient_material")
        self.assertIsNone(result["material_gap"])
        self.assertFalse(result["claims_semantically_verified"])
        self.assertEqual(result["narrative"]["verification"], "unverified")
        self.assertEqual([row["kind"] for row in result["runtime_limitations"]], ["scheduler_binding"])
        self.assertIn("未证明", result["answer"])
        self.assertEqual(self.item(result["investigation_state"]["question_investigation"],
                                   "scheduler_binding")["status"], "OPEN")


if __name__ == "__main__":
    unittest.main()
