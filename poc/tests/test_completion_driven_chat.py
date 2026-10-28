"""Offline integration coverage for completion-driven business investigations."""

import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from business_synthesis import assess_business_answer
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search
from run_store import RunStore
from question_budget import QuestionBudget


class CompletionDrivenChatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="completion-driven-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.requests = []
        self.events = []
        self.files = {}
        self.question = "root.cbl 的金额如何计算？"
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
                                       api_key="offline-only")
        for owner, attribute in ((socket, "create_connection"), (socket.socket, "connect"),
                                 (socket.socket, "connect_ex")):
            blocker = patch.object(owner, attribute, side_effect=AssertionError("Network forbidden"))
            blocker.start()
            self.addCleanup(blocker.stop)
        self.write("root.cbl", "TOTAL-RULE",
                   "COMPUTE TOTAL-AMOUNT = BASE-AMOUNT * 2.",
                   "01 BASE-AMOUNT PIC 9(5) VALUE 10.\n01 TOTAL-AMOUNT PIC 9(6).")
        # Independent files prevent initial dependency expansion from supplying
        # all of the later tool results before the scripted investigation starts.
        for index in range(1, 8):
            self.write(f"helper-{index}.cbl", f"HELPER-{index}",
                       f'MOVE {index} TO HELPER-RESULT.\nDISPLAY "HELPER-{index}-MARKER".',
                       "01 HELPER-RESULT PIC 9.")

    def write(self, path, name, body, data=""):
        text = (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
                f"WORKING-STORAGE SECTION.\n{data}\nPROCEDURE DIVISION.\nMAIN.\n{body}\nGOBACK.\n")
        (self.source / path).write_text(text, encoding="utf-8")
        self.files[path] = text

    @staticmethod
    def pages(payload):
        return [page for bundle in payload["source_context"] for page in bundle["pages"]]

    def cited_answer(self, payload, text="基础金额初值为10，金额按基础金额乘以二得到20。"):
        root = next(page for page in self.pages(payload) if page["relative_path"] == "root.cbl")
        return f"{text}[{root['evidence_id']}]"

    def read_helper(self, number):
        path = f"helper-{number}.cbl"
        return json.dumps({"read": [{"relative_path": path, "start_line": 1,
                                     "end_line": len(self.files[path].splitlines())}]})

    def ask(self, respond, *, request_guard=12, **options):
        build_business_index(self.source, self.database, source_format="free", verify_content=True,
                             framework_reference_path="", quiet=True)
        ensure_repository_search(self.database, self.source)

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            self.requests.append(payload)
            self.assertLessEqual(len(self.requests), request_guard, "Investigation failed to converge")
            self.assertEqual(payload["question"], self.question)
            reply = respond(payload, len(self.requests))
            text, finish_reason = reply if isinstance(reply, tuple) else (reply, "stop")
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text},
                "finish_reason": finish_reason}]}, ensure_ascii=False))

        result = run_business_chat(self.question, self.database, self.source, self.config,
            framework_reference_path="", transport=transport, progress=self.events.append,
            **options)["agent_result"]
        # The adapter converts transport exceptions into a diagnostic result;
        # assert outside that boundary as well so a guard trip cannot pass.
        self.assertLessEqual(len(self.requests), request_guard, "Investigation failed to converge")
        return result

    def test_explicit_unbounded_policy_completes_more_than_six_useful_tool_rounds(self):
        def respond(payload, number):
            self.assertIsNone(payload["investigation_budget"]["remaining_model_requests"])
            if number > 1:
                previous = f"helper-{number - 1}.cbl"
                page = next(page for page in self.pages(payload) if page["relative_path"] == previous)
                self.assertEqual(page["source_text"], self.files[previous].rstrip("\n"))
            if number <= 7:
                self.assertNotIn(f"helper-{number}.cbl", {page["relative_path"] for page in self.pages(payload)})
                return self.read_helper(number)
            return self.cited_answer(payload)

        result = self.ask(respond, policy=AgentPolicy(max_model_requests=None,
            max_investigation_rounds=None))
        self.assertEqual(len(self.requests), 8)
        self.assertEqual(result["metrics"]["model_requests"], 8)
        self.assertIn("金额按基础金额乘以二得到20", result["answer"])
        self.assertTrue(result["narrative"]["citations"])
        self.assertFalse(result["claims_semantically_verified"])
        self.assertTrue(any(event.get("analysis_update", {}).get("round", 0) >= 8 for event in self.events))

    def test_default_six_investigations_reserve_a_separate_final_answer(self):
        def respond(payload, number):
            if number <= 6:
                self.assertGreater(payload["investigation_budget"]["reads_per_turn"], 0)
                self.assertEqual(payload["investigation_budget"]["remaining_model_requests"],
                                 9 - number)
                return self.read_helper(number)
            self.assertEqual(payload["investigation_budget"]["reads_per_turn"], 0)
            self.assertIn("helper-6.cbl", {page["relative_path"] for page in self.pages(payload)})
            return self.cited_answer(payload)

        result = self.ask(respond, request_guard=7)
        self.assertEqual(len(self.requests), 7)
        self.assertEqual(result["metrics"]["model_requests"], 7)
        self.assertEqual(result["metrics"]["convergence_stop_reason"], "investigation_round_budget")
        self.assertTrue(result["answer_available"])
        self.assertIn("金额按基础金额乘以二得到20", result["answer"])
        saved = RunStore(self.root / "investigation-runs.sqlite").load(result["run_id"])
        stages = [row["stage"] for row in saved["checkpoint"]["quality"]["rounds"]]
        self.assertEqual(stages.count("investigate"), 6)
        self.assertEqual(stages[-1], "answer")

    def test_explicit_continue_after_six_rounds_keeps_answer_and_gets_fresh_bounded_allowance(self):
        prior = self.ask(lambda payload, number: self.read_helper(number)
            if number <= 6 else self.cited_answer(payload), request_guard=7)
        self.assertEqual(prior["metrics"]["convergence_stop_reason"], "investigation_round_budget")
        store = RunStore(self.root / "investigation-runs.sqlite")
        before = store.load(prior["run_id"])

        def supplement(payload, number):
            self.assertEqual(payload["question"], self.question)
            self.assertEqual(payload["draft_answer"], prior["answer"])
            self.assertTrue(payload["analysis_memory"]["continuation_requested"])
            self.assertIn("不从头重复定位或重查已解决事项", payload["task"])
            self.assertIn("段落原样保留", payload["task"])
            self.assertGreater(payload["investigation_budget"]["reads_per_turn"], 0)
            if number == 8:
                self.assertEqual(payload["investigation_budget"]["remaining_model_requests"], 8)
                return self.read_helper(7)
            self.assertEqual(payload["investigation_budget"]["remaining_model_requests"], 7)
            helper = next(page for page in self.pages(payload)
                          if page["relative_path"] == "helper-7.cbl")
            return self.cited_answer(payload) + f" 补充：HELPER-RESULT 被赋值为7。[{helper['evidence_id']}]"

        continued = self.ask(supplement, request_guard=9,
            resume_run_id=prior["run_id"], continue_investigation=True)
        self.assertEqual(len(self.requests), 9)
        self.assertNotEqual(continued["run_id"], prior["run_id"])
        self.assertEqual(continued["metrics"]["model_requests"], 2)
        self.assertEqual(continued["metrics"]["policy"]["max_investigation_rounds"], 6)
        self.assertEqual(continued["metrics"]["policy"]["max_model_requests"], 8)
        self.assertIn("金额按基础金额乘以二得到20", continued["answer"])
        self.assertIn("HELPER-RESULT 被赋值为7", continued["answer"])
        self.assertEqual(store.load(prior["run_id"]), before)
        saved = store.load(continued["run_id"])
        self.assertEqual(saved["checkpoint"]["quality"]["continued_from_run_id"], prior["run_id"])
        self.assertEqual(len(saved["requests"]), 2)

    def test_continuation_final_delivery_preserves_previous_answer_and_continuation_task(self):
        prior = self.ask(lambda payload, number: self.cited_answer(payload), request_guard=1)
        self.assertNotIn("continuation_requested", self.requests[0].get("analysis_memory", {}))

        def supplement(payload, number):
            self.assertEqual(payload["question"], self.question)
            self.assertEqual(payload["draft_answer"], prior["answer"])
            self.assertTrue(payload["analysis_memory"]["continuation_requested"])
            self.assertEqual(payload["investigation_budget"]["reads_per_turn"], 0)
            self.assertIn("本组现在收尾", payload["task"])
            self.assertIn("不从头重复定位或重查已解决事项", payload["task"])
            self.assertIn("需要更正时说明更正内容和证据", payload["task"])
            return payload["draft_answer"]

        continued = self.ask(supplement, policy=AgentPolicy(max_model_requests=1), request_guard=2,
            resume_run_id=prior["run_id"], continue_investigation=True)
        self.assertEqual(continued["answer"], prior["answer"])
        saved = RunStore(self.root / "investigation-runs.sqlite").load(continued["run_id"])
        self.assertTrue(saved["checkpoint"]["continuation_requested"])

    def test_continuation_tool_recovery_does_not_replace_the_continuation_task(self):
        prior = self.ask(lambda payload, number: self.cited_answer(payload), request_guard=1)

        def recover(payload, number):
            self.assertEqual(payload["draft_answer"], prior["answer"])
            self.assertTrue(payload["analysis_memory"]["continuation_requested"])
            self.assertIn("不从头重复定位或重查已解决事项", payload["task"])
            if number == 2:
                return '{"read":[{"relative_path":"helper-1.cbl","start_line":1'
            self.assertEqual(payload["investigation_budget"]["reads_per_turn"], 0)
            self.assertIn("补查动作未能执行", payload["task"])
            return payload["draft_answer"]

        continued = self.ask(recover, request_guard=3,
            resume_run_id=prior["run_id"], continue_investigation=True)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(continued["answer"], prior["answer"])

    def test_actions_ignoring_explicit_answer_target_stop_at_default_request_limit(self):
        now = [0.0]

        def budget(*args, **kwargs):
            return QuestionBudget(*args, **{**kwargs, "clock": lambda: now[0],
                                          "wall_clock": lambda: 1000 + now[0]})

        def respond(payload, number):
            if number > 1:
                self.assertEqual(payload["investigation_budget"]["reads_per_turn"], 0)
            now[0] = 60
            return self.read_helper(1)

        with patch("business_chat.QuestionBudget", side_effect=budget):
            result = self.ask(respond, policy=AgentPolicy(delivery_target_seconds=60), request_guard=8)
        self.assertEqual(len(self.requests), 8)
        self.assertEqual(result["metrics"]["model_requests"], 8)
        saved = RunStore(self.root / "investigation-runs.sqlite").load(result["run_id"])
        self.assertTrue(any(item.get("arguments", {}).get("relative_path") == "helper-1.cbl"
                            for item in saved["checkpoint"]["frontier"]))

    def test_unique_truncated_output_stops_at_request_limit_and_retains_all_received_text(self):
        fragments = []

        def respond(payload, number):
            if number == 1:
                text = self.cited_answer(payload)
            else:
                self.assertTrue(payload["draft_continuation"])
                self.assertIn(fragments[0], payload["draft_answer"])
                text = f"\n\n说明段落 {number}：本段补充已有业务解释。"
            fragments.append(text)
            return text, "length"

        result = self.ask(respond, request_guard=8)
        self.assertEqual(len(self.requests), 8)
        self.assertEqual(result["metrics"]["model_requests"], 8)
        self.assertTrue(result["answer_available"])
        self.assertTrue(result["answer_truncated"])
        self.assertIn("金额按基础金额乘以二得到20", result["answer"])
        for number in range(2, 9):
            self.assertIn(f"说明段落 {number}", result["answer"])

    def test_soft_delivery_target_answers_known_source_and_retains_more_pending_work(self):
        now = [0.0]

        def budget(*args, **kwargs):
            return QuestionBudget(*args, **{**kwargs, "clock": lambda: now[0],
                                          "wall_clock": lambda: 1000 + now[0]})

        def respond(payload, number):
            if number <= 2:
                if number == 2:
                    self.assertIn("helper-1.cbl", {page["relative_path"] for page in self.pages(payload)})
                now[0] += 10
                return self.read_helper(number)
            self.assertEqual(payload["investigation_budget"]["reads_per_turn"], 0)
            self.assertIn("helper-1.cbl", {page["relative_path"] for page in self.pages(payload)})
            self.assertNotIn("helper-2.cbl", {page["relative_path"] for page in self.pages(payload)})
            # The first answer finishes after its soft target. Receiving its
            # full text is allowed; the target only changes the next action.
            now[0] = 75
            return self.cited_answer(payload)

        with patch("business_chat.QuestionBudget", side_effect=budget):
            result = self.ask(respond, policy=AgentPolicy(delivery_target_seconds=60), request_guard=3)
        self.assertEqual(len(self.requests), 3)
        self.assertIn("金额按基础金额乘以二得到20", result["answer"])
        self.assertTrue(result["model_answer_recorded"])
        self.assertFalse(result["answer_truncated"])
        self.assertEqual(result["metrics"]["delivery_target"], {"seconds": 60, "reached": True,
            "inflight_request_cancelled_by_target": False, "hard_limit_seconds": None})
        saved = RunStore(self.root / "investigation-runs.sqlite").load(result["run_id"])
        self.assertTrue(any(item.get("arguments", {}).get("relative_path") == "helper-2.cbl"
                            for item in saved["checkpoint"]["frontier"]))

    def test_explicit_null_soft_target_can_keep_useful_work_past_sixty_seconds(self):
        now = [0.0]

        def budget(*args, **kwargs):
            return QuestionBudget(*args, **{**kwargs, "clock": lambda: now[0],
                                          "wall_clock": lambda: 1000 + now[0]})

        def respond(payload, number):
            if number <= 3:
                self.assertGreater(payload["investigation_budget"]["reads_per_turn"], 0)
                now[0] += 30
                return self.read_helper(number)
            self.assertIn("helper-3.cbl", {page["relative_path"] for page in self.pages(payload)})
            return self.cited_answer(payload)

        with patch("business_chat.QuestionBudget", side_effect=budget):
            result = self.ask(respond, policy=AgentPolicy(delivery_target_seconds=None), request_guard=4)
        self.assertEqual(len(self.requests), 4)
        self.assertIn("金额按基础金额乘以二得到20", result["answer"])
        self.assertIsNone(result["metrics"]["delivery_target"]["seconds"])
        self.assertIsNone(result["metrics"]["delivery_target"]["hard_limit_seconds"])

    def test_draft_review_retains_original_question_and_can_read_before_rewriting(self):
        self.question = "root.cbl 的金额如何计算，输入初值来自哪里？"
        draft = []

        def respond(payload, number):
            if number == 1:
                draft.append(self.cited_answer(payload, "金额按基础金额乘以二得到。"))
                return draft[0]
            self.assertEqual(payload["draft_answer"], draft[0])
            self.assertFalse(payload["analysis_memory"]["draft_is_evidence"])
            self.assertIn("inputs", payload["answer_review"]["missing_aspects"])
            if number == 2:
                self.assertGreater(payload["investigation_budget"]["reads_per_turn"], 0)
                return self.read_helper(1)
            self.assertIn("helper-1.cbl", {page["relative_path"] for page in self.pages(payload)})
            return self.cited_answer(payload)

        result = self.ask(respond, request_guard=4)
        self.assertEqual(len(self.requests), 3)
        self.assertIn("初值为10", result["answer"])
        self.assertFalse(result["business_review"]["answer_completion"]["missing_aspects"])

    def test_adaptive_revision_preserves_previously_explained_aspects(self):
        self.question = "root.cbl 的金额如何计算，输入初值来自哪里？"

        def respond(payload, number):
            # The first answer correctly explains the calculation but omits
            # inputs. The revision adds inputs while dropping the calculation.
            return self.cited_answer(payload, "金额按基础金额乘以二得到。" if number == 1 else
                                     "基础金额初值来自声明中的VALUE 10。")

        result = self.ask(respond, request_guard=3)
        self.assertIn("乘以二", result["answer"])
        self.assertNotIn("formula", result["business_review"]["answer_completion"]["missing_aspects"])

    def test_tool_only_progress_is_checkpointed_before_any_answer_exists(self):
        class UserCancelled(RuntimeError):
            pass

        def check_cancel():
            if len(self.requests) >= 2:
                raise UserCancelled("User cancelled after one completed source read")

        with self.assertRaises(UserCancelled):
            self.ask(lambda payload, number: self.read_helper(number), request_guard=2,
                     check_cancel=check_cancel)
        store = RunStore(self.database.parent / "investigation-runs.sqlite")
        with store.connect() as db:
            run_id = db.execute("SELECT run_id FROM investigation_runs").fetchone()[0]
        checkpoint = store.load(run_id)["checkpoint"]
        self.assertTrue(checkpoint["completed_actions"])
        self.assertTrue("analysis_continuation" in checkpoint, "Tool-only continuation state was not persisted")
        self.assertTrue(checkpoint["analysis_continuation"]["known_material"])

    def test_repeated_denial_does_not_create_new_progress_by_duplicating_the_same_gap(self):
        def respond(payload, number):
            return self.cited_answer(payload) + " root.cbl 的完整源码本轮没有提供。" * number

        result = self.ask(respond, request_guard=3)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("没有提供", result["answer"])
        self.assertFalse(result["answer_completion"]["source_availability_contradiction"])

    def test_permuted_source_denials_still_represent_the_same_gap(self):
        def respond(payload, number):
            if number == 1:
                return self.read_helper(1)
            paths = ["root.cbl", "helper-1.cbl"] if number == 2 else ["helper-1.cbl", "root.cbl"]
            return self.cited_answer(payload) + " ".join(f"{path} 源码本轮未提供。" for path in paths)

        result = self.ask(respond, request_guard=4)
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn("未提供", result["answer"])
        self.assertFalse(result["answer_completion"]["source_availability_contradiction"])

    def test_repeated_answer_gap_converges_even_when_wording_changes(self):
        def respond(payload, number):
            wording = "金额按基础金额乘以二得到。" if number == 1 else "计算规则是将基础金额翻倍。"
            return self.cited_answer(payload, wording) + " root.cbl 的完整源码本轮没有提供。"

        result = self.ask(respond, request_guard=3)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("没有提供", result["answer"])
        self.assertIn("乘以二", result["answer"])
        self.assertFalse(result["business_review"]["answer_completion"]["source_availability_contradiction"])
        trace = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text())
        self.assertTrue(trace["source_availability_repairs"])

    def test_repeated_tool_request_converges_to_an_answer(self):
        def respond(payload, number):
            if number <= 2:
                return self.read_helper(1)
            self.assertEqual(payload["investigation_budget"]["reads_per_turn"], 0)
            return self.cited_answer(payload)

        result = self.ask(respond, request_guard=3)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(result["metrics"]["convergence_stop_reason"], "no_evidence_progress")
        self.assertIn("金额按基础金额乘以二得到20", result["answer"])

    def test_user_cancel_interrupts_an_active_default_investigation(self):
        class UserCancelled(RuntimeError):
            pass

        def check_cancel():
            if len(self.requests) >= 2:
                raise UserCancelled("User cancelled the investigation")

        with self.assertRaises(UserCancelled):
            self.ask(lambda payload, number: self.read_helper(number), request_guard=2,
                     check_cancel=check_cancel)
        self.assertEqual(len(self.requests), 2)

    def test_explicit_two_request_limit_still_forces_delivery_on_second_request(self):
        def respond(payload, number):
            if number == 1:
                self.assertEqual(payload["investigation_budget"]["remaining_model_requests"], 2)
                return self.read_helper(1)
            self.assertEqual(payload["investigation_budget"]["remaining_model_requests"], 1)
            self.assertEqual(payload["investigation_budget"]["reads_per_turn"], 0)
            return self.cited_answer(payload)

        result = self.ask(respond, policy=AgentPolicy(max_model_requests=2), request_guard=2)
        self.assertEqual(result["metrics"]["model_requests"], 2)
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(result["answer_available"])

    def test_explicit_investigation_round_limit_still_reserves_final_delivery(self):
        def respond(payload, number):
            if number <= 2:
                self.assertGreater(payload["investigation_budget"]["reads_per_turn"], 0)
                return self.read_helper(number)
            self.assertEqual(payload["investigation_budget"]["reads_per_turn"], 0)
            return self.cited_answer(payload)

        result = self.ask(respond, policy=AgentPolicy(max_investigation_rounds=2), request_guard=3)
        self.assertEqual(result["metrics"]["model_requests"], 3)
        self.assertEqual(result["metrics"]["convergence_stop_reason"], "investigation_round_budget")
        self.assertTrue(result["answer_available"])

    def test_new_evidence_keeps_pending_revisions_moving_past_two_total_revisions(self):
        answers = []

        def assess(question, answer, investigation, pages, **options):
            assessment = assess_business_answer(question, answer, investigation, pages, **options)
            stage = next((index for index in range(4, 0, -1) if f"说明步骤{index}" in answer), 0)
            missing = [f"supporting-rule-{stage + 1}"] if stage < 4 else []
            assessment.update(missing_aspects=missing, required_aspects=missing,
                              status="incomplete" if missing else "not_assessed",
                              reason="supplied_business_aspects_unexplained" if missing else None)
            return assessment

        def respond(payload, number):
            if number % 2 == 0:
                return self.read_helper(number // 2)
            stage = (number + 1) // 2
            if stage > 1:
                self.assertIn(f"helper-{stage - 1}.cbl", {
                    page["relative_path"] for page in self.pages(payload)})
                self.assertEqual(payload["draft_answer"], answers[-1])
            answer = self.cited_answer(payload) + f" 说明步骤{stage}。"
            answers.append(answer)
            return answer

        with patch("business_chat.assess_business_answer", side_effect=assess):
            result = self.ask(respond, request_guard=7)
        self.assertEqual(len(self.requests), 7)
        self.assertEqual(result["answer"], answers[-1])
        self.assertFalse(result["answer_completion"]["missing_aspects"])
        self.assertNotEqual(result["metrics"]["convergence_stop_reason"], "answer_revision_budget")

    def test_changed_wording_cannot_bypass_counted_revision_budget(self):
        self.question = "请完整解释 root.cbl 的七项金额计算规则。"
        fields = [f"RESULT-{index:02d}" for index in range(1, 8)]
        self.write("root.cbl", "TOTAL-RULE",
                   "\n".join(f"COMPUTE {field} = BASE-AMOUNT * {index}."
                             for index, field in enumerate(fields, 1)),
                   "01 BASE-AMOUNT PIC 9(5) VALUE 10.\n" +
                   "\n".join(f"01 {field} PIC 9(6)." for field in fields))
        answers = []

        def assess(question, answer, investigation, pages, **options):
            assessment = assess_business_answer(question, answer, investigation, pages, **options)
            missing = [field for field in fields if field not in answer]
            # A deterministic reviewer stands in for per-rule completeness;
            # retrieval, transport, answer retention and loop control stay real.
            assessment.update(missing_aspects=missing, required_aspects=fields,
                              status="incomplete" if missing else "not_assessed",
                              reason="supplied_business_aspects_unexplained" if missing else None)
            return assessment

        def respond(payload, number):
            if number > 1:
                self.assertEqual(payload["draft_answer"], answers[-1])
                self.assertEqual(payload["analysis_memory"]["unresolved_aspects"], fields[number - 1:])
            text = "基础金额初值为10。" + " ".join(
                f"{field} 等于基础金额乘以{index}。" for index, field in enumerate(fields[:number], 1))
            answers.append(self.cited_answer(payload, text))
            return answers[-1]

        with patch("business_chat.assess_business_answer", side_effect=assess):
            result = self.ask(respond, request_guard=8)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(result["answer"], answers[-1])
        self.assertTrue(result["business_review"]["answer_completion"]["missing_aspects"])
        self.assertEqual(result["metrics"]["convergence_stop_reason"], "answer_revision_budget")


if __name__ == "__main__":
    unittest.main()
