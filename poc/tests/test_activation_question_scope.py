"""A program category does not add an unasked launch investigation."""
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from activation_evidence import investigate_activation
from batch_context_evidence import investigate_batch_context
from business_index import build_business_index
from question_planner import QuestionPlanner, runtime_activation_requested
from question_investigation import build_question_investigation


class ActivationQuestionScopeTests(unittest.TestCase):
    def plan(self, question):
        return QuestionPlanner().plan(question, {
            "direct_paths": ["service.cbl"], "selected_paths": ["service.cbl"],
            "programs": [{"program_names": ["SERVICE"]}],
            "rule_leads": [{"reads": ["INPUT-AMOUNT"], "writes": ["OUTPUT-AMOUNT"]}]})

    def test_batch_noun_in_local_boundary_or_calculation_question_adds_no_launch_obligations(self):
        for question in (
            "这批处理遇到空输入会怎样？",
            "批处理的输出文件写失败时怎样处理？",
            "批处理中的 INPUT-AMOUNT 大于零是否等于非零？",
            "这个 batch 的 OUTPUT-AMOUNT 怎么计算？",
            "继续核对业务边界：这批处理遇空输入会怎样？SERVICE 任何异常都返回90吗？",
            "What happens when this batch receives empty input?",
            "Can this batch return an error after a failed file write?",
            "How does the batch calculate OUTPUT-AMOUNT?",
        ):
            with self.subTest(question=question):
                plan = self.plan(question)
                self.assertNotIn("activation", plan.intents)
                self.assertFalse({"entry_point", "caller_invocation", "activation_conditions",
                                  "scheduler_binding"}.intersection(plan.required_obligations))

    def test_business_overview_does_not_require_runtime_scheduling(self):
        for question in (
            "请从业务目的解释 SERVICE 的整体流程，包括 Batch job、输入输出和异常。",
            "Explain the business purpose and workflow of this batch.",
        ):
            with self.subTest(question=question):
                plan = self.plan(question)
                self.assertNotIn("activation", plan.intents)
                self.assertNotIn("scheduler_binding", plan.required_obligations)

    def test_explicit_entry_still_requests_static_activation_evidence(self):
        for question in (
            "批处理入口和 OUTPUT-AMOUNT 公式是什么？",
            "Explain the batch entry and OUTPUT-AMOUNT formula.",
            "批处理入口 COPY 的完整计算公式是什么？",
        ):
            with self.subTest(question=question):
                plan = self.plan(question)
                self.assertIn("activation", plan.intents)
                self.assertIn("caller_invocation", plan.required_obligations)
                self.assertIn("scheduler_binding", plan.required_obligations)

    def test_explicit_activation_survives_combined_local_questions(self):
        for question in (
            "SERVICE 批处理怎么trigger的，OUTPUT-AMOUNT怎么计算？",
            "SERVICE如何启动，空输入如何处理？",
            "谁启动这批处理，OUTPUT-AMOUNT 是什么？",
            "这个batch如何调度，写失败如何处理？",
            "How is the batch triggered and what happens on empty input?",
            "How is SERVICE triggered and how is OUTPUT-AMOUNT computed?",
            "Who launches this batch?",
            "At what time does this batch run?",
            "什么时候执行这项批处理？",
            "谁启动这项批处理？",
            "这批处理多久运行一次？",
            "How often does this batch run?",
            "Who launches SERVICE?",
            "什么时候执行 SERVICE？",
        ):
            with self.subTest(question=question):
                self.assertIn("activation", self.plan(question).intents)

    def test_rule_local_trigger_and_incoming_program_relation_keep_existing_scope(self):
        local = self.plan("SERVICE 出账触发条件和金额如何计算？")
        self.assertNotIn("activation", local.intents)
        self.assertIn("control_conditions", local.required_obligations)
        incoming = self.plan("哪些程序调用 SERVICE？")
        self.assertEqual(incoming.relation_direction, "incoming")
        self.assertIn("call_relationships", incoming.intents)
        self.assertNotIn("scheduler_binding", incoming.required_obligations)

    def test_explicit_initiator_question_is_shared_with_delivery_scope(self):
        for question in (
            "请介绍 SERVICE 这项批处理的业务用途、输入输出，并确认实际发起者是谁。",
            "Explain the business purpose of this batch and who initiates it.",
        ):
            with self.subTest(question=question):
                self.assertTrue(runtime_activation_requested(question))
                plan = self.plan(question)
                self.assertIn("activation", plan.intents)
                self.assertIn("scheduler_binding", plan.required_obligations)
        self.assertFalse(runtime_activation_requested("批处理输入记录里的发起者字段是什么意思？"))


class SuppliedCLScopeReasonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        (self.source / "service.cbl").write_bytes(
            b"IDENTIFICATION DIVISION.\nPROGRAM-ID. SERVICE.\nPROCEDURE DIVISION.\nGOBACK.\n")

    def fixture(self, *, environment=False):
        lines = ["PGM", "DCL VAR(&READY) TYPE(*CHAR) LEN(1)",
                 "IF COND(&READY *EQ 'Y') THEN(DO)"]
        if environment:
            lines.insert(2, "OVRDBF FILE(INPUTLF) TOFILE(DATALIB/INPUTPF)")
        lines += ["/* neutral source context */"] * 300
        lines += ["CALL SERVICE PARM(&READY)", "ENDDO", "ENDPGM"]
        (self.source / "launch.clle").write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
        build_business_index(self.source, self.database, source_format="free",
                             framework_reference_path="", quiet=True, verify_content=True)
        return lines.index("IF COND(&READY *EQ 'Y') THEN(DO)") + 1, len(lines) - 2

    def page(self, path, first=1, last=None):
        raw = (self.source / path).read_bytes()
        lines = raw.decode("utf-8").splitlines()
        last = len(lines) if last is None else last
        return {"relative_path": path, "source_sha256": hashlib.sha256(raw).hexdigest(),
                "source_text": "\n".join(lines[first-1:last]), "start_line": first,
                "end_line": last, "evidence_id": f"ev_{path}_{first}_{last}", "span_truncated": False}

    def investigate(self, pages):
        mapping = {"direct_paths": ["service.cbl"], "programs": [
            {"relative_path": "service.cbl", "program_names": ["SERVICE"]}]}
        context = investigate_batch_context(mapping, pages, database_path=self.database)["cl_context"]
        result = investigate_activation(mapping, pages, database_path=self.database, cl_context=context)
        for caller in result["activation_evidence"]["callers"]:
            self.assertFalse(caller["execution_verified"])
            self.assertFalse(caller["runtime_object_verified"])
            self.assertFalse(caller["binding_confirmed"])
        self.assertFalse(result["activation_evidence"]["scheduler_binding_verified"])
        self.assertFalse(result["activation_evidence"]["execution_verified"])
        return result

    def test_exact_separate_guard_page_clears_only_static_scope_unknown_reason(self):
        guard_line, call_line = self.fixture()
        self.assertGreater(call_line, 300)
        pages = [self.page("service.cbl"), self.page("launch.clle", guard_line, guard_line),
                 self.page("launch.clle", call_line, call_line)]
        result = self.investigate(pages)
        caller, = result["activation_evidence"]["callers"]
        self.assertTrue(caller["scope_known"])
        self.assertIsNone(caller["scope_unknown_reason"])
        self.assertTrue(caller["conditions_supplied"])
        self.assertEqual(caller["condition_evidence_ids"], [pages[1]["evidence_id"]])
        self.assertEqual(caller["conditions"][0]["condition"], "&READY *EQ 'Y'")
        self.assertFalse(caller["conditions"][0]["outcome_evaluated"])
        statuses = {item["kind"]: item["status"] for item in result["required_items"]}
        self.assertEqual(statuses["activation_conditions"], "SATISFIED")
        self.assertEqual(statuses["scheduler_binding"], "OPEN")

    def test_overview_keeps_source_batch_entry_and_local_scheduler_limitation(self):
        self.fixture(environment=True)
        mapping = {"direct_paths": ["service.cbl"], "selected_paths": ["service.cbl"],
                   "programs": [{"relative_path": "service.cbl", "program_names": ["SERVICE"]}]}
        result = build_question_investigation("解释一下 SERVICE 这项批处理的业务用途", mapping,
            source_pages=[self.page("service.cbl"), self.page("launch.clle")],
            database_path=self.database)
        self.assertNotIn("activation", result["analysis_plan"]["intents"])
        self.assertGreater(result["cl_context"]["candidate_count"], 0)
        from business_chat import _answer_scope_gaps
        material, limitations = _answer_scope_gaps("解释一下 SERVICE 这项批处理的业务用途", result)
        self.assertNotIn("scheduler_binding", {item["kind"] for item in material})
        self.assertIn("scheduler_binding", {item["kind"] for item in limitations})
        self.assertTrue(all(item["blocks_static_source_explanation"] is False for item in limitations))

    def test_missing_or_invalid_guard_keeps_unknown_reason_and_runtime_unverified(self):
        guard_line, call_line = self.fixture()
        guard = self.page("launch.clle", guard_line, guard_line)
        for changed in (None, {**guard, "source_sha256": "0" * 64},
                        {**guard, "source_text": "IF COND(&READY *EQ 'N') THEN(DO)"},
                        {**guard, "span_truncated": True}):
            with self.subTest(guard=changed):
                pages = [self.page("service.cbl"), self.page("launch.clle", call_line, call_line)]
                if changed is not None:
                    pages.append(changed)
                result = self.investigate(pages)
                caller, = result["activation_evidence"]["callers"]
                self.assertFalse(caller["scope_known"])
                self.assertEqual(caller["scope_unknown_reason"],
                                 "cl_control_or_environment_source_not_supplied")
                self.assertFalse(caller["conditions_supplied"])
                self.assertNotEqual(next(item["status"] for item in result["required_items"]
                    if item["kind"] == "activation_conditions"), "SATISFIED")

    def test_supplied_guard_without_environment_predecessor_cannot_claim_known_scope(self):
        guard_line, call_line = self.fixture(environment=True)
        pages = [self.page("service.cbl"), self.page("launch.clle", guard_line, guard_line),
                 self.page("launch.clle", call_line, call_line)]
        partial = self.investigate(pages)
        self.assertFalse(partial["activation_evidence"]["callers"])
        self.assertNotEqual(next(item["status"] for item in partial["required_items"]
            if item["kind"] == "activation_conditions"), "SATISFIED")
        result = self.investigate([*pages, self.page("launch.clle", 3, 3)])
        complete, = result["activation_evidence"]["callers"]
        self.assertTrue(complete["scope_known"])
        self.assertIsNone(complete["scope_unknown_reason"])
        self.assertEqual(next(item["status"] for item in result["required_items"]
            if item["kind"] == "activation_conditions"), "SATISFIED")


if __name__ == "__main__":
    unittest.main()
