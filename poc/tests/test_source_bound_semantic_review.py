"""Offline production-path checks for source-bound independent answer review.

Scripted judgments exercise delivery and validation, not model accuracy.
"""

import hashlib
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

import test_completion_driven_chat as fixtures
from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import OpenAICompatibleChatClient, TransportResponse, UrllibTransport
from quality_trace import QualityTrace
from repository_discovery import ensure_repository_search
from run_store import RunStore
from web_app import WorkbenchState


class UserCancelled(RuntimeError):
    pass


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class SourceBoundSemanticReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CompletionDrivenChatTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.requests, self.events, self.answers = [], [], []
        self.database_number = 0

    def prepare(self):
        self.database_number += 1
        self.fixture.database = self.fixture.root / f"case-{self.database_number}.sqlite"
        build_business_index(self.fixture.source, self.fixture.database, source_format="free",
                             verify_content=True, framework_reference_path="", quiet=True)
        ensure_repository_search(self.fixture.database, self.fixture.source)

    def answer(self, payload, *, revised=False, short=False):
        text = "基础金额初值为10，金额按基础金额乘以二得到20。"
        if not short:
            text += ("本段的计算语句把基础金额乘以二，再把计算结果放入总金额字段。"
                     "这里说明的是源码所写的算式，声明中的初值和计算时读取的字段属于两个不同位置。") * 10
        if revised:
            text += "计算目标是总金额，乘数为二。"
        result = self.fixture.cited_answer(payload, text)
        self.answers.append(result)
        return result

    @staticmethod
    def is_review(payload):
        return payload.get("schema") == "semantic-answer-review/v1"

    def review(self, payload, *, changes=False, mutate=None):
        source = next(row for row in payload["evidence"] if row["path"] == "root.cbl")
        quote = "COMPUTE TOTAL-AMOUNT = BASE-AMOUNT * 2."
        self.assertIn(quote, source["text"])
        value = {"answer_sha256": payload["answer_sha256"],
                 "evidence_sha256": payload["evidence_sha256"],
                 "reviews": [{"unit_id": unit["unit_id"], "verdict": "supported",
                     "reason": "The visible calculation is the stated multiplication.",
                     "evidence": [{"reference_id": source["reference_id"], "quote": quote}]}
                     for unit in payload["units"]], "omissions": []}
        if changes:
            value["reviews"][0].update(verdict="insufficient",
                reason="Explain the calculation target alongside the multiplier.", evidence=[])
        if mutate:
            mutate(value)
        return json.dumps(value, ensure_ascii=False)

    def ask(self, respond, *, policy=None, resume=False, progress=None, native_chunks=None,
            answer_instructions="", answer_detail="detailed"):
        if not resume:
            self.prepare()

        def record(body):
            request = json.loads(body)
            payload = json.loads(request["messages"][-1]["content"])
            self.requests.append({"wire": request, "payload": payload})
            self.assertLessEqual(len(self.requests), 6, "Unexpected extra provider attempt")
            return payload, respond(payload, len(self.requests))

        def transport(request):
            _, text = record(request.body)
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "content": text}, "finish_reason": "stop"}]}, ensure_ascii=False))

        def on_progress(event):
            self.events.append(event)
            if progress:
                progress(event)

        options = dict(framework_reference_path="", transport=transport, progress=on_progress,
                       policy=policy or AgentPolicy(), allow_network=False,
                       answer_instructions=answer_instructions, answer_detail=answer_detail)
        if native_chunks:
            native = UrllibTransport()
            options["transport"] = native

            def open_response(request, **_):
                payload, text = record(request.data)
                return native_chunks(payload, text)

            with patch.object(native._opener, "open", side_effect=open_response):
                return run_business_chat(self.fixture.question, self.fixture.database,
                    self.fixture.source, self.fixture.config, **options)["agent_result"]
        return run_business_chat(self.fixture.question, self.fixture.database,
            self.fixture.source, self.fixture.config, **options)["agent_result"]

    def saved(self):
        store = RunStore(self.fixture.database.parent / "investigation-runs.sqlite")
        with store.connect() as database:
            run_id = database.execute("SELECT run_id FROM investigation_runs ORDER BY created_at DESC").fetchone()[0]
        return store.load(run_id)

    @staticmethod
    def report(result):
        return result["answer_completion"]["semantic_review"]

    def test_review_receives_exact_answer_and_actual_source_once_without_history(self):
        result = self.ask(lambda p, n: self.answer(p) if n == 1 else self.review(p))
        self.assertEqual(len(self.requests), 2)
        initial = self.requests[0]["payload"]
        review = self.requests[1]["payload"]
        self.assertTrue(self.is_review(review))
        self.assertNotIn("source_context", review)
        self.assertEqual(len(self.requests[1]["wire"]["messages"]), 2)
        self.assertEqual("".join(unit["text"] for unit in review["units"]), self.answers[0])
        self.assertEqual(review["answer_sha256"], digest(self.answers[0]))
        pages = {p["evidence_id"]: p for bundle in initial["source_context"] for p in bundle["pages"]}
        self.assertEqual({row["reference_id"] for row in review["evidence"]}, set(pages))
        for row in review["evidence"]:
            page = pages[row["reference_id"]]
            self.assertEqual((row["path"], row["sha256"], row["start_line"], row["end_line"], row["text"]),
                (page["relative_path"], page["source_sha256"], page["start_line"], page["end_line"], page["source_text"]))
            self.assertEqual(row["text_sha256"], digest(page["source_text"]))
        self.assertEqual(result["answer"], self.answers[0])
        self.assertEqual(self.report(result)["status"], "model_reviewed")
        self.assertFalse(result["claims_semantically_verified"])
        self.assertFalse(self.report(result)["semantic_verification"])
        trace_path = self.fixture.database.parent / "quality-traces" / (result["run_id"] + ".json")
        trace = json.loads(trace_path.read_text())
        self.assertEqual(len(trace["rounds"]), 2)
        self.assertIn(trace["rounds"][0]["stage"], {"answer", "investigate"})
        self.assertEqual(trace["rounds"][1]["stage"], "semantic_review")
        self.assertEqual(trace["rounds"][0]["sources"], trace["rounds"][1]["sources"])

    def test_short_correct_answer_keeps_one_request(self):
        result = self.ask(lambda p, n: self.answer(p, short=True))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["answer"], self.answers[0])
        self.assertNotIn("semantic_review", result["answer_completion"])

    def test_many_supplied_citations_do_not_turn_short_prose_into_long_review(self):
        def respond(payload, number):
            self.assertEqual(number, 1)
            text = self.answer(payload, short=True)
            pages = [page for bundle in payload["source_context"] for page in bundle["pages"]]
            text += "".join(f"[{pages[0]['evidence_id']}]" for _ in range(24))
            self.assertGreater(len(text), 600)
            self.answers[-1] = text
            return text
        result = self.ask(respond)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["answer"], self.answers[0])
        self.assertNotIn("semantic_review", result["answer_completion"])

    def test_bracketed_business_prose_still_requires_independent_review(self):
        def respond(payload, number):
            if self.is_review(payload):
                return self.review(payload)
            text = self.answer(payload, short=True)
            text += "[计算读取基础金额字段，再把乘以二的结果放入总金额字段。]" * 24
            self.answers[-1] = text
            return text
        result = self.ask(respond)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.report(result)["status"], "model_reviewed")

    def test_incomplete_review_coverage_is_not_approval(self):
        result = self.ask(lambda p, n: self.answer(p) if n == 1 else self.review(p,
            mutate=lambda value: value["reviews"].pop()))
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.report(result)["status"], "invalid")
        self.assertEqual(self.report(result)["reason"], "INCOMPLETE_UNIT_COVERAGE")
        self.assertEqual(result["answer"], self.answers[0])
        self.assertEqual(result["status"], "PARTIAL")

    def test_unknown_reference_cannot_make_model_support_trusted(self):
        def malicious(value):
            value["reviews"][0]["evidence"][0]["reference_id"] = "ev_page_fabricated"
        result = self.ask(lambda p, n: self.answer(p) if n == 1 else self.review(p, mutate=malicious))
        self.assertEqual(self.report(result)["reason"], "UNKNOWN_REFERENCE")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["answer"], self.answers[0])
        self.assertFalse(result["claims_semantically_verified"])

    def test_visible_id_with_invented_quote_is_rejected(self):
        def unsupported(value):
            value["reviews"][0]["evidence"][0]["quote"] = "MOVE 999 TO TOTAL-AMOUNT."
        result = self.ask(lambda p, n: self.answer(p) if n == 1 else self.review(p, mutate=unsupported))
        self.assertEqual(self.report(result)["reason"], "QUOTE_NOT_VISIBLE")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["answer"], self.answers[0])

    def test_stale_draft_hash_cannot_approve_current_answer(self):
        result = self.ask(lambda p, n: self.answer(p) if n == 1 else self.review(p,
            mutate=lambda value: value.update(answer_sha256="0" * 64)))
        self.assertEqual(self.report(result)["reason"], "REVIEW_BINDING_MISMATCH")
        self.assertEqual(result["status"], "PARTIAL")

    def test_fenced_review_is_not_cleaned_into_protocol_approval(self):
        result = self.ask(lambda p, n: self.answer(p) if n == 1 else
                          "```json\n" + self.review(p) + "\n```")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.report(result)["status"], "invalid")
        self.assertEqual(self.report(result)["reason"], "INVALID_REVIEW_JSON")
        self.assertEqual(result["answer"], self.answers[0])
        self.assertEqual(result["status"], "PARTIAL")

    def test_answer_wrapper_with_action_is_not_unwrapped_into_review(self):
        result = self.ask(lambda p, n: self.answer(p) if n == 1 else json.dumps({
            "answer": self.review(p), "unrecognized_action": {"name": "lookup"}}))
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.report(result)["status"], "invalid")
        self.assertEqual(self.report(result)["reason"], "INVALID_REVIEW_OBJECT")
        self.assertEqual(result["answer"], self.answers[0])
        self.assertEqual(result["status"], "PARTIAL")

    def test_api_tool_call_alongside_valid_review_is_rejected_without_execution(self):
        original = OpenAICompatibleChatClient.complete
        for variant in ("tool_calls_field", "tool_calls_finish", "legacy_function_call"):
            with self.subTest(variant=variant):
                self.requests.clear()
                def add_action(client, *args, **kwargs):
                    raw = original(client, *args, **kwargs)
                    payload = json.loads(kwargs["messages"][-1]["content"])
                    if self.is_review(payload):
                        choice = raw["choices"][0]
                        action = {"name": "read", "arguments": json.dumps({
                            "relative_path": "untrusted-input.cbl", "start_line": 1, "end_line": 10})}
                        if variant == "tool_calls_field":
                            choice["message"]["tool_calls"] = [{"id": "call_untrusted",
                                "type": "function", "function": action}]
                        elif variant == "tool_calls_finish":
                            choice["finish_reason"] = "tool_calls"
                        else:
                            choice["message"]["function_call"] = action
                            choice["finish_reason"] = "function_call"
                    return raw
                with patch.object(OpenAICompatibleChatClient, "complete", add_action):
                    result = self.ask(lambda p, n: self.answer(p) if n == 1 else self.review(p))
                self.assertEqual(len(self.requests), 2)
                self.assertEqual(self.report(result)["status"], "not_reviewed")
                self.assertEqual(self.report(result)["reason"], "MODEL_ACTION_RESPONSE")
                self.assertEqual(result["answer"], self.answers[-1])
                self.assertEqual(result["status"], "PARTIAL")
                self.assertEqual(result["metrics"]["tool_calls"]["read"], 0)
                self.assertEqual(result["metrics"]["provider_retries"], [])

    def test_fenced_review_remains_invalid_after_durable_receipt_and_resume(self):
        original = QualityTrace.record_remote_response
        review_responses = []
        def respond(payload, number):
            if number == 1:
                return self.answer(payload)
            text = "```json\n" + self.review(payload) + "\n```"
            review_responses.append(text)
            return text
        def cancel_after_review(trace, response):
            original(trace, response)
            if len(self.requests) == 2:
                raise UserCancelled("Cancel after durable fenced review receipt")
        with patch.object(QualityTrace, "record_remote_response", cancel_after_review):
            with self.assertRaises(UserCancelled):
                self.ask(respond)
        saved = self.saved()
        self.assertFalse(saved["uncertain_requests"])
        raw = json.loads(saved["requests"][-1]["response_json"])["remote_response"]
        self.assertEqual(raw["choices"][0]["message"]["content"], review_responses[0])
        result = self.ask(respond, resume=True)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.report(result)["status"], "invalid")
        self.assertEqual(self.report(result)["reason"], "INVALID_REVIEW_JSON")
        self.assertEqual(result["answer"], self.answers[0])
        self.assertEqual(result["status"], "PARTIAL")
        self.assertFalse(any(event.get("answer_preview", {}).get("text", "").lstrip().startswith(("{", "```"))
                             for event in self.events))

    def test_received_review_text_error_keeps_its_classification_after_resume(self):
        original = QualityTrace.record_remote_response
        def cancel_after_error(trace, response):
            original(trace, response)
            if response.get("business_text_error"):
                raise UserCancelled("Cancel after durable review error receipt")
        respond = lambda p, n: self.answer(p) if n == 1 else " "
        with patch.object(QualityTrace, "record_remote_response", cancel_after_error):
            with self.assertRaises(UserCancelled):
                self.ask(respond)
        result = self.ask(respond, resume=True)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.report(result)["reason"], "MODEL_TEXT_EMPTY")
        self.assertEqual(result["answer"], self.answers[0])

    def test_no_review_allowance_retains_substantive_unreviewed_answer(self):
        result = self.ask(lambda p, n: self.answer(p), policy=AgentPolicy(max_model_requests=1))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.report(result)["status"], "not_reviewed")
        self.assertEqual(self.report(result)["reason"], "request_budget")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["answer"], self.answers[0])

    def test_review_request_limit_never_discards_source_to_make_review_fit(self):
        def respond(payload, number):
            self.assertEqual(number, 1)
            text = self.answer(payload) + "计算关系见上述算式。" * 1200
            self.answers[-1] = text
            return text
        result = self.ask(respond, policy=AgentPolicy(max_request_bytes=32768))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.report(result)["status"], "not_reviewed")
        self.assertEqual(self.report(result)["reason"], "request_size")
        self.assertEqual(result["answer"], self.answers[0])
        self.assertEqual(result["status"], "PARTIAL")
        saved = self.saved()["checkpoint"]["accepted_draft"]
        self.assertTrue(saved["source_pages"])
        self.assertTrue(all(page["source_text"] for page in saved["source_pages"]))

    def changing_response(self, payload, number):
        if number in {1, 3}:
            return self.answer(payload, revised=number == 3)
        return self.review(payload, changes=number == 2)

    def test_revision_consumes_shared_budget_and_requires_fresh_review(self):
        result = self.ask(self.changing_response)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(result["answer"], self.answers[-1])
        self.assertNotEqual(self.answers[0], self.answers[-1])
        self.assertEqual(self.report(result)["status"], "model_reviewed")
        self.assertEqual(self.report(result)["answer_sha256"], digest(result["answer"]))
        saved = self.saved()["checkpoint"]
        self.assertEqual(saved["semantic_review_state"]["review_count"], 2)
        self.assertEqual(saved["semantic_review_state"]["revision_count"], 1)
        self.assertEqual(saved["analysis_continuation"]["revision_requests"], 1)

    def test_semantic_revision_keeps_current_saved_answer_requirements(self):
        instructions = "请用中文按业务目的、处理步骤、依据的顺序说明，保留必要细节；最终答案不得输出 JSON。"
        result = self.ask(self.changing_response, answer_instructions=instructions)
        self.assertEqual(len(self.requests), 4)
        for request in (self.requests[0], self.requests[2]):
            self.assertEqual(request["payload"]["answer_detail"], "detailed")
        for request in self.requests:
            self.assertIn(instructions, request["wire"]["messages"][0]["content"])
        for request in (self.requests[1], self.requests[3]):
            self.assertIn("不能覆盖本次审查的 JSON 协议", request["wire"]["messages"][0]["content"])
        self.assertEqual(self.requests[2]["payload"]["answer_instructions"], instructions)
        self.assertEqual(self.report(result)["status"], "model_reviewed")

    def test_revision_without_recheck_budget_is_explicitly_unreviewed(self):
        result = self.ask(self.changing_response, policy=AgentPolicy(max_model_requests=3))
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(result["answer"], self.answers[-1])
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(self.report(result)["status"], "revised_unreviewed")

    def test_disabled_rewriting_does_not_disable_independent_review(self):
        result = self.ask(lambda p, n: self.answer(p) if n == 1 else self.review(p, changes=True),
                          policy=AgentPolicy(max_answer_revisions=0))
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["answer"], self.answers[0])
        self.assertEqual(self.report(result)["status"], "changes_requested")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(self.saved()["checkpoint"]["semantic_review_state"]["revision_count"], 0)

    def test_received_review_resumes_as_review_without_resending_or_json_answer(self):
        original = QualityTrace.record_remote_response
        def cancel_after_review(trace, response):
            original(trace, response)
            if self.requests and self.is_review(self.requests[-1]["payload"]):
                raise UserCancelled("Cancel after durable review receipt")
        respond = lambda p, n: self.answer(p) if n == 1 else self.review(p)
        with patch.object(QualityTrace, "record_remote_response", cancel_after_review):
            with self.assertRaises(UserCancelled):
                self.ask(respond)
        saved = self.saved()
        self.assertFalse(saved["uncertain_requests"])
        self.assertEqual(saved["checkpoint"]["accepted_draft"]["answer"], self.answers[0])
        self.assertEqual(saved["checkpoint"]["semantic_review_state"]["phase"], "review")
        result = self.ask(respond, resume=True)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["answer"], self.answers[0])
        self.assertEqual(self.report(result)["status"], "model_reviewed")
        self.assertFalse(any(event.get("answer_preview", {}).get("text", "").lstrip().startswith("{")
                             for event in self.events))

    def test_received_revision_resumes_then_checks_once(self):
        original = QualityTrace.record_remote_response
        def cancel_after_revision(trace, response):
            original(trace, response)
            if len(self.requests) == 3:
                raise UserCancelled("Cancel after durable revision receipt")
        with patch.object(QualityTrace, "record_remote_response", cancel_after_revision):
            with self.assertRaises(UserCancelled):
                self.ask(self.changing_response)
        self.assertFalse(self.saved()["uncertain_requests"])
        result = self.ask(self.changing_response, resume=True)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(result["answer"], self.answers[-1])
        self.assertEqual(self.report(result)["status"], "model_reviewed")

    def test_cancel_before_review_send_preserves_stage_and_does_not_repeat_answer(self):
        def cancel_review_progress(event):
            if event.get("analysis_update", {}).get("summary", "").startswith("正在逐段核对"):
                raise UserCancelled("Cancel before review request")
        respond = lambda p, n: self.answer(p) if n == 1 else self.review(p)
        with self.assertRaises(UserCancelled):
            self.ask(respond, progress=cancel_review_progress)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.saved()["uncertain_requests"])
        self.assertEqual(self.saved()["checkpoint"]["semantic_review_state"]["phase"], "review")
        result = self.ask(respond, resume=True)
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(self.is_review(self.requests[-1]["payload"]))
        self.assertEqual(result["answer"], self.answers[0])

    def test_single_pass_durable_review_recovery_retains_the_answer(self):
        original = QualityTrace.record_remote_response
        def cancel_after_review(trace, response):
            original(trace, response)
            if len(self.requests) == 2:
                raise UserCancelled("Cancel after single-pass review receipt")
        policy = AgentPolicy(investigation_mode="single_pass")
        respond = lambda p, n: self.answer(p) if n == 1 else self.review(p)
        with patch.object(QualityTrace, "record_remote_response", cancel_after_review):
            with self.assertRaises(UserCancelled):
                self.ask(respond, policy=policy)
        self.assertFalse(self.saved()["uncertain_requests"])
        result = self.ask(respond, policy=policy, resume=True)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["answer"], self.answers[0])
        self.assertEqual(self.report(result)["status"], "model_reviewed")

    def test_completed_review_is_not_replayed_when_final_local_verification_is_cancelled(self):
        def cancel_verification(event):
            if event.get("phase") == "verifying_answer":
                raise UserCancelled("Cancel local verification after review")
        respond = lambda p, n: self.answer(p) if n == 1 else self.review(p)
        with self.assertRaises(UserCancelled):
            self.ask(respond, progress=cancel_verification)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.saved()["checkpoint"]["semantic_review_state"]["phase"], "done")
        result = self.ask(respond, resume=True)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["answer"], self.answers[0])
        self.assertEqual(self.report(result)["status"], "model_reviewed")

    def test_recorded_business_failure_reaches_general_review_without_regex_nomination(self):
        archive = Path(__file__).resolve().parents[2] / "docs/reports/2026-10-20-real-model-delivery"
        observation = json.loads((archive / "phase5/run/B2-observation.json").read_text())
        answer = observation["final_visible_answer"]
        self.assertEqual(digest(answer), "53e2209b37770f33125816627f8b6092c0be633ceb90f91adbb66b96db0f9e2e")
        case = next(case for case in json.loads((archive / "phase1/cases.json").read_text())["cases"]
                    if case["case_id"] == "B2")
        source = self.fixture.root / "recorded-source"
        source.mkdir()
        for path in (archive / "phase1/fixtures/B2/source").iterdir():
            (source / path.name).write_bytes(path.read_bytes())
        self.fixture.source, self.fixture.question = source, case["question"]
        reviewed_units = []
        def respond(payload, number):
            if number == 1:
                return answer
            self.assertTrue(self.is_review(payload))
            self.assertEqual("".join(u["text"] for u in payload["units"]), answer)
            self.assertNotIn("gold", json.dumps(payload).lower())
            self.assertEqual({e["path"] for e in payload["evidence"]}, {"settle.cbl", "nightjob.clle"})
            source_page = next(e for e in payload["evidence"] if e["path"] == "settle.cbl")
            reviews = []
            for unit in payload["units"]:
                reviewed_units.append(unit["text"])
                # An explicit scripted critique tests routing, not model detection accuracy.
                reviews.append({"unit_id": unit["unit_id"], "verdict": "insufficient",
                    "reason": "Visible operations do not establish every runtime or object-type conclusion.",
                    "evidence": [{"reference_id": source_page["reference_id"], "quote": "DISPLAY 'ORDER SKIPPED'"}]})
            return json.dumps({"answer_sha256": payload["answer_sha256"],
                "evidence_sha256": payload["evidence_sha256"], "reviews": reviews, "omissions": []})
        result = self.ask(respond, policy=AgentPolicy(max_model_requests=2))
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(reviewed_units)
        self.assertEqual(result["answer"], answer)
        self.assertEqual(result["answer_completion"].get("semantic_risk_candidates"), [])
        self.assertEqual(self.report(result)["status"], "changes_requested")
        self.assertEqual(result["status"], "PARTIAL")

    def test_unknown_review_transport_stops_and_never_automatically_retries(self):
        def respond(payload, number):
            if number == 1:
                return self.answer(payload)
            raise OSError("Offline simulated unknown transport result")
        result = self.ask(respond)
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(self.saved()["uncertain_requests"])
        self.assertEqual(result["answer"], self.answers[0])
        resumed = self.ask(respond, resume=True)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(resumed["status"], "PARTIAL")

    def test_native_answer_arrives_before_completion_and_review_json_stays_hidden(self):
        app = WorkbenchState(state_path=self.fixture.root / "settings.json")
        job = "a" * 32
        app.job = {"job_id": job, "kind": "question", "status": "RUNNING", "result": None,
                   "cancel_requested": False, "progress": {"phase": "waiting_for_model"}}
        app.started_at = app.updated_at = app.phase_started_at = time.monotonic()
        snapshots = []
        def progress(event):
            app._progress(job, event)
        def event(text="", finish=None):
            return ("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": text},
                "finish_reason": finish}]}, ensure_ascii=False) + "\n\n").encode()
        class Response:
            status = 200
            headers = {"Content-Type": "text/event-stream"}
            def __init__(self, chunks):
                self.chunks = iter(chunks)
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def read1(self, size):
                part = next(self.chunks, b"")
                return part() if callable(part) else part
        def native(payload, text):
            prefix = text[:100]
            def inspect_before_next_read():
                snapshot = app.wait_job(job, after=0, wait_ms=0)
                snapshots.append(snapshot)
                visible = snapshot["progress"]["answer_preview"]["text"]
                self.assertEqual(snapshot["status"], "RUNNING")
                self.assertIsNone(snapshot["result"])
                if self.is_review(payload):
                    self.assertEqual(visible, self.answers[0])
                    self.assertNotIn('"reviews"', visible)
                    self.assertEqual(snapshot["progress"]["answer_preview"]["stage"], "verifying")
                else:
                    self.assertEqual(visible, prefix)
                    self.assertLess(len(visible), len(text))
                return event(text[100:], "stop")
            return Response([event(prefix), inspect_before_next_read, b"data: [DONE]\n\n"])
        result = self.ask(lambda p, n: self.answer(p) if n == 1 else self.review(p),
                          native_chunks=native, progress=progress)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(result["answer"], self.answers[0])
        self.assertEqual(self.report(result)["status"], "model_reviewed")
        self.assertFalse(any(event.get("answer_preview", {}).get("text", "").lstrip().startswith("{")
                             for event in self.events))


if __name__ == "__main__":
    unittest.main()
