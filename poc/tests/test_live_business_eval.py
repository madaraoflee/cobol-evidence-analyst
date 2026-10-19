from __future__ import annotations

import json
import hashlib
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from company_api import APIClientError, CompanyAPIConfig
from agent_policy import AgentPolicy
from live_business_eval import (evaluate, main, review_template, _review_status, _review_summary,
                                SYNTHESIS_PROMPT_VERSION, _review_identity, apply_reviews,
                                _fingerprint, _measurement, _runtime_provenance, _cases)
from company_api import TransportResponse


class LiveBusinessEvalTests(unittest.TestCase):
    def test_turn_identifiers_are_nonempty_and_unique_within_each_case(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cases.json"
            for identifiers in (["first", "first"], [""], ["   "]):
                with self.subTest(identifiers=identifiers):
                    path.write_text(json.dumps({"cases": [{"id": "flow", "turns": [
                        {"id": value, "question": "Why?"} for value in identifiers]}]}), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "EVALUATION_CASES_INVALID"):
                        _cases(path)
            path.write_text(json.dumps({"cases": [{"id": "flow", "turns": [
                {"id": value, "question": "Why?"} for value in ("first", "second")]}]}), encoding="utf-8")
            self.assertEqual(len(_cases(path)[0]["turns"]), 2)

    def test_source_runner_provenance_never_claims_native_execution(self):
        fake = _runtime_provenance(lambda request: None)
        remote = _runtime_provenance(None)
        self.assertEqual(fake["execution_surface"], "source_python")
        self.assertEqual(remote["execution_surface"], "source_python")
        self.assertEqual(fake["transport_kind"], "synthetic")
        self.assertEqual(remote["transport_kind"], "remote_http")
        self.assertEqual(fake["implementation_sha256"], remote["implementation_sha256"])
        self.assertEqual(len(fake["implementation_sha256"]), 64)
        self.assertIn("source_commit", fake)
        self.assertIn("source_dirty", fake)

    def test_runtime_and_same_answer_timing_extend_identity_without_rebinding_history(self):
        item, report, review = self._archived_answer()
        row = report["cases"][0]
        runtime = {"execution_surface": "source_python", "transport_kind": "synthetic"}
        report["runtime_provenance"] = runtime
        row["elapsed_seconds"] = 3
        row["measurement"] = _measurement(row, runtime, "a" * 64)
        row["review_identity"].update(runtime_sha256=_fingerprint(runtime),
                                      measurement_sha256=_fingerprint(row["measurement"]))
        row["review_identity"]["identity_sha256"] = _fingerprint({key: value for key, value
            in row["review_identity"].items() if key != "identity_sha256"})
        review["identity"] = copy.deepcopy(row["review_identity"])
        self.assertEqual(apply_reviews(copy.deepcopy(report), [item], [review])["correctness_counts"]["correct"], 1)
        for mutation in (lambda doc: doc["runtime_provenance"].update(execution_surface="native_windows_http"),
                         lambda doc: doc["cases"][0].update(elapsed_seconds=1),
                         lambda doc: doc["cases"][0]["measurement"].update(elapsed_seconds=1)):
            changed = copy.deepcopy(report)
            mutation(changed)
            self.assertEqual(apply_reviews(changed, [item], [review])["correctness_counts"]["correct"], 0)

    def _archived_answer(self, *, run_id="run-a", branch="automatic", answer="The result doubles."):
        item = {"id": "flow", "question": "How is the result computed?", "gold": {
            "required_findings": [{"id": "formula"}], "forbidden_claims": [{"id": "extra"}]}}
        identity = _review_identity(run_id=run_id, case_id="flow", turn_id="flow", branch=branch,
            answer=answer, model="test-model", prompt={"version": "prompt-a", "system_sha256": "prompt-hash"},
            policy={"max_model_requests": 2}, gold=item["gold"], source_snapshot_id="snapshot-a",
            question=item["question"],
            framework_revision="manual-a", context={"sources": [{"evidence_id": "ev-a",
                "path": "entry.cbl", "source_sha256": "source-a", "start_line": 4, "end_line": 4,
                "excerpt": "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2."}],
                "framework": [{"reference_id": "ref-a", "document_sha256": "manual-sha",
                    "document_name": "manual.md", "start_line": 1, "end_line": 2, "excerpt": "Manual."}]})
        row = {"id": "flow", "turn_id": "flow", "run_id": run_id,
               "answer": answer, "question": item["question"], "quality_status": "not_reviewed", "review_identity": identity}
        if branch != "automatic":
            row["name"] = branch
        report = {"cases": [row]}
        review = review_template([item], report)["reviews"][0]
        review.update(required_findings=[{"id": "formula", "result": "met"}],
                      forbidden_claims=[{"id": "extra", "present": False}],
                      usefulness="usable", followup_continuity="not_applicable")
        return item, report, review

    def test_identity_binds_all_versions_and_never_reuses_old_case_scores(self):
        item, report, review = self._archived_answer()
        accepted = apply_reviews(copy.deepcopy(report), [item], [review])
        self.assertEqual(accepted["correctness_counts"]["correct"], 1)
        self.assertEqual(accepted["review_completion"], "complete")
        for field in ("run_id", "case_id", "turn_id", "branch", "answer_sha256", "model", "prompt",
                      "policy_sha256", "gold_sha256", "source_snapshot_id", "source_versions",
                      "framework_revision", "framework_versions", "evidence_sha256", "question_sha256", "history_sha256"):
            with self.subTest(field=field):
                changed = copy.deepcopy(review)
                changed["identity"][field] = "changed"
                stale = apply_reviews(copy.deepcopy(report), [item], [changed])["cases"][0]
                self.assertIsNone(stale["human_review"])
                self.assertEqual(stale["review_binding_status"], "stale")
                self.assertEqual(stale["correctness_status"], "not_reviewed")
        legacy = {key: value for key, value in review.items() if key != "identity"}
        rejected = apply_reviews(copy.deepcopy(report), [item], [legacy])["cases"][0]
        self.assertEqual(rejected["rejected_reviews"][0]["reason"], "missing_identity")

    def test_changed_answer_or_gold_invalidates_review_but_live_source_is_irrelevant(self):
        item, report, review = self._archived_answer()
        changed_answer = copy.deepcopy(report)
        changed_answer["cases"][0]["answer"] = "The result is zero."
        self.assertEqual(apply_reviews(changed_answer, [item], [review])["correctness_counts"]["correct"], 0)
        changed_gold = {**item, "gold": {"required_findings": [{"id": "new-condition"}]}}
        self.assertIsNone(apply_reviews(copy.deepcopy(report), [changed_gold], [review])["cases"][0]["human_review"])
        # Saved evidence identities are checked locally; current filesystem state does not replace them.
        with mock.patch("live_business_eval._safe_file", side_effect=AssertionError("live source must not be read")):
            self.assertEqual(apply_reviews(report, [item], [review])["correctness_counts"]["correct"], 1)

    def test_complete_review_form_and_correct_answer_are_separate_metrics(self):
        item, report, review = self._archived_answer()
        review["required_findings"][0]["result"] = "omitted"
        reviewed = apply_reviews(report, [item], [review])
        self.assertEqual(reviewed["review_completion"], "complete")
        self.assertEqual(reviewed["correctness_counts"]["incomplete"], 1)
        self.assertEqual(reviewed["reviewed_findings"]["omitted"], 1)
        review["forbidden_claims"][0]["present"] = True
        reviewed = apply_reviews(report, [item], [review])
        self.assertEqual(reviewed["correctness_counts"]["incorrect"], 1)
        self.assertEqual(reviewed["unsupported_claims_present"], 1)

    def test_empty_gold_does_not_turn_complete_form_into_correctness_pass(self):
        item, report, review = self._archived_answer()
        item["gold"] = {}
        row = report["cases"][0]
        row["review_identity"] = _review_identity(run_id=row["run_id"], case_id="flow", turn_id="flow",
            branch="automatic", answer=row["answer"], model="test-model", prompt={}, policy={}, gold={},
            context={}, question=item["question"])
        review["identity"] = row["review_identity"]
        review["required_findings"], review["forbidden_claims"] = [], []
        result = apply_reviews(report, [item], [review])
        self.assertEqual(result["review_completion"], "complete")
        self.assertEqual(result["correctness_counts"]["correct"], 0)
        self.assertEqual(result["correctness_counts"]["pending"], 1)

    def test_paired_branch_reviews_do_not_cross_attach(self):
        item, first, review = self._archived_answer(branch="reviewed")
        _, second, _ = self._archived_answer(branch="automatic_context")
        report = {"cases": [{"id": "flow", "turn_id": "flow", "question": item["question"], "quality_status": "not_reviewed",
            "branches": [first["cases"][0], second["cases"][0]]}]}
        forms = review_template([item], report)["reviews"]
        self.assertEqual([row["branch"] for row in forms], ["reviewed", "automatic_context"])
        reviewed = apply_reviews(report, [item], [review])
        self.assertEqual(reviewed["review_counts"], {"not_reviewed": 1, "partial": 0, "complete": 1})
        self.assertEqual(reviewed["correctness_counts"]["correct"], 1)
        self.assertIsNone(reviewed["cases"][0]["branches"][1]["human_review"])

    def test_automatic_identity_uses_final_supplied_trace_without_capture_option(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            cases = root / "cases.json"
            item = {"id": "flow", "question": "How is the result computed?", "expected_source_paths": ["final.cbl"],
                    "gold": {"required_findings": [{"id": "formula"}]}}
            cases.write_text(json.dumps({"cases": [item]}), encoding="utf-8")
            trace = root / "trace.json"
            trace.write_text(json.dumps({"run_id": "trace-run", "prompt_version": "prompt-b",
                "analysis_revision": "actually-read-snapshot", "final": {"final_answer_round_id": "round-2"},
                "rounds": [{"round_id": "round-1", "sources": [{"path": "earlier.cbl", "source_sha256": "old"}]},
                    {"round_id": "round-2", "sources": [{"evidence_id": "ev-final", "path": "final.cbl",
                        "source_sha256": "final-hash", "start_line": 3, "end_line": 3,
                        "excerpt_sha256": "excerpt-hash"}], "framework": []}]}), encoding="utf-8")
            old_review = root / "old-review.json"
            old_review.write_text(json.dumps({"reviews": [{"case_id": "flow", "turn_id": "flow", "run_id": "old",
                "required_findings": [{"id": "formula", "result": "met"}], "forbidden_claims": [],
                "usefulness": "usable", "followup_continuity": "not_applicable"}]}), encoding="utf-8")
            output = {"runner_status": "COMPLETED", "agent_result": {"answer": "The result doubles.",
                "metrics": {"quality_trace_path": str(trace)}, "evidence_refs": [], "investigation": {}}}
            config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="test-key")
            with mock.patch("live_business_eval.build_business_index"), \
                    mock.patch("live_business_eval.ensure_repository_search", return_value={"snapshot_id": "indexed", "indexed_files": 2}), \
                    mock.patch("live_business_eval.run_business_chat", return_value=output):
                report = evaluate(source, root / "evaluation", cases, allow_network=True, preflight=False,
                                  config=config, framework="", reviews_file=old_review)
            row = report["cases"][0]
            self.assertEqual(row["run_id"], "trace-run")
            self.assertEqual(row["review_identity"]["source_versions"], ["final-hash"])
            self.assertEqual(row["review_identity"]["source_snapshot_id"], "actually-read-snapshot")
            self.assertEqual(row["review_identity"]["prompt"]["version"], "prompt-b")
            self.assertEqual(row["expected_paths_supplied"], ["final.cbl"])
            self.assertEqual(row["supplied_source_paths"], ["final.cbl"])
            self.assertIsNone(row["human_review"])
            self.assertEqual(row["review_binding_status"], "stale")
            self.assertEqual(report["stale_review_count"], 1)

    def test_archived_review_cli_never_constructs_model_client(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            item, report, review = self._archived_answer()
            report.update(model_preflight={"status": "responded"}, index_seconds=0, latency_seconds={},
                          case_count=1, plan={})
            saved = root / "saved.json"
            saved.write_text(json.dumps(report), encoding="utf-8")
            cases = root / "cases.json"
            cases.write_text(json.dumps({"cases": [item]}), encoding="utf-8")
            reviews = root / "reviews.json"
            reviews.write_text(json.dumps({"reviews": [review]}), encoding="utf-8")
            with mock.patch("live_business_eval.OpenAICompatibleChatClient", side_effect=AssertionError("model")), \
                    mock.patch("builtins.print"):
                code = main(["--source", str(root / "absent-live-source"), "--evaluation-dir", str(root / "evaluation"),
                             "--cases", str(cases), "--review-report", str(saved), "--reviews", str(reviews)])
            self.assertEqual(code, 0)
            reviewed = json.loads((root / "evaluation" / "live-business-eval.json").read_text(encoding="utf-8"))
            self.assertEqual(reviewed["correctness_counts"]["correct"], 1)

    def test_human_review_requires_each_finding_and_exports_unscored_form(self):
        turn = {"id": "next", "question": "Why?", "gold": {
            "required_findings": [{"id": "input"}, {"id": "result"}],
            "forbidden_claims": [{"id": "unsupported"}]}}
        item = {"id": "flow", "turns": [turn]}
        form = review_template([item], {"cases": []})["reviews"][0]
        self.assertIsNone(form["run_id"])
        self.assertEqual([row["result"] for row in form["required_findings"]], [None, None])
        self.assertEqual(_review_status(turn, None), "not_reviewed")
        partial = {**form, "required_findings": [{"id": "input", "result": "met"}]}
        self.assertEqual(_review_status(turn, partial), "partial")
        complete = {**partial, "required_findings": [{"id": "input", "result": "met"},
                    {"id": "result", "result": "omitted"}],
                    "forbidden_claims": [{"id": "unsupported", "present": False}],
                    "usefulness": "needs_work", "followup_continuity": "not_applicable"}
        self.assertEqual(_review_status(turn, complete), "complete")
        status, counts = _review_summary([{"quality_status": "complete"}, {"quality_status": "not_reviewed"}])
        self.assertEqual((status, counts), ("partial", {"not_reviewed": 1, "partial": 0, "complete": 1}))

    def test_paired_requests_change_only_evidence_and_never_include_gold(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            text = "PROGRAM-ID. ENTRY.\nMOVE INPUT-VALUE TO RESULT-VALUE.\n"
            (source / "entry.cbl").write_text(text, encoding="utf-8")
            digest = hashlib.sha256((source / "entry.cbl").read_bytes()).hexdigest()
            cases = root / "cases.json"
            cases.write_text(json.dumps({"cases": [{"id": "entry", "question": "How is RESULT-VALUE set?",
                "source_snapshot_id": "snapshot-a", "history": [{"role": "user", "content": "Earlier question"}],
                "reviewed_context": {"source_ranges": [{"path": "entry.cbl", "sha256": digest,
                    "start_line": 1, "end_line": 2, "role": "result"}]},
                "gold": {"required_findings": [{"id": "secret-grade", "description": "Do not send this"}]}}]}),
                encoding="utf-8")
            evaluation = root / "evaluation"
            capture = evaluation / "captures" / "entry-entry.json"
            capture.parent.mkdir(parents=True)
            excerpt = "MOVE INPUT-VALUE TO RESULT-VALUE."
            capture.write_text(json.dumps({"prompt_version": SYNTHESIS_PROMPT_VERSION,
                "question": "How is RESULT-VALUE set?", "history": [{"role": "user", "content": "Earlier question"}],
                "source_snapshot_id": "snapshot-a", "framework_revision": None,
                "context": {"sources": [{"path": "entry.cbl", "source_sha256": digest,
                    "start_line": 2, "end_line": 2, "excerpt": excerpt,
                    "excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest()}], "framework": []}}), encoding="utf-8")
            bodies = []
            def transport(request):
                bodies.append(json.loads(request.body))
                return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": "The value is copied."}, "finish_reason": "stop"}]}))
            config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="local-test-secret")
            report = evaluate(source, evaluation, cases, mode="paired-synthesis", allow_network=True,
                              preflight=False, config=config, transport=transport)
            self.assertEqual(len(bodies), 2)
            self.assertEqual(report["cases"][0]["quality_status"], "not_reviewed")
            prompts = [json.loads(body["messages"][-1]["content"]) for body in bodies]
            self.assertEqual(prompts[0]["question"], prompts[1]["question"])
            self.assertEqual(bodies[0]["messages"][:-1], bodies[1]["messages"][:-1])
            self.assertNotEqual(prompts[0]["source_evidence"], prompts[1]["source_evidence"])
            self.assertNotIn("secret-grade", json.dumps(bodies))

    def test_paired_plan_lists_missing_frozen_context_without_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            cases = root / "cases.json"
            cases.write_text(json.dumps({"cases": [{"id": "entry", "question": "Explain the rule"}]}), encoding="utf-8")
            config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="local-test-secret")
            with mock.patch("live_business_eval.OpenAICompatibleChatClient", side_effect=AssertionError("network")):
                report = evaluate(source, root / "evaluation", cases, mode="paired-synthesis",
                                  plan=True, config=config)
            self.assertEqual(report["plan"]["missing_frozen_captures"], ["entry/entry"])
            self.assertEqual(report["quality_status"], "not_reviewed")

    def test_authentication_failure_stops_before_indexing_or_sending_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            (source / "request.cbl").write_text("PROGRAM-ID. REQUEST.\n", encoding="utf-8")
            cases = root / "cases.json"
            cases.write_text(json.dumps({"cases": [{"id": "request", "question": "How is a request handled?"}]}),
                             encoding="utf-8")
            config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="local-test-secret")
            with mock.patch("live_business_eval.CompanyAPIConfig.from_env", return_value=config), \
                    mock.patch("live_business_eval.OpenAICompatibleChatClient.complete",
                               side_effect=APIClientError("HTTP_ERROR", http_status=401)), \
                    mock.patch("live_business_eval.build_business_index",
                               side_effect=AssertionError("source index should not start")):
                report = evaluate(source, root / "evaluation", cases, allow_network=True)
            self.assertEqual(report["model_preflight"]["http_status"], 401)
            self.assertEqual(report["cases"], [])
            self.assertFalse((root / "evaluation").exists())

    def test_answer_report_keeps_full_latency_and_followup_context_for_review(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            (source / "request.cbl").write_text(
                "IDENTIFICATION DIVISION.\nPROGRAM-ID. REQUEST.\nPROCEDURE DIVISION.\n"
                "MAIN.\nIF REQUEST-VALUE > 4\nMOVE 'REVIEW' TO REQUEST-STATE\nEND-IF.\nGOBACK.\n",
                encoding="utf-8")
            cases = root / "cases.json"
            cases.write_text(json.dumps({"cases": [
                {"id": "initial", "conversation": "flow", "question": "When is review needed?",
                 "expected_source_paths": ["request.cbl"], "review_checks": ["Explain the threshold"]},
                {"id": "followup", "conversation": "flow", "question": "What happens next?",
                 "expected_source_paths": ["request.cbl"]}]}), encoding="utf-8")
            config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="local-test-secret")
            history_sizes = []
            received_policies = []
            policy = AgentPolicy(max_model_requests=3)

            def answer(question, _database, _source, _config, *, history, **_options):
                history_sizes.append(len(history))
                received_policies.append(_options["policy"])
                return {"runner_status": "COMPLETED", "agent_result": {
                    "answer": "Requests above four are reviewed.",
                    "narrative": {"citations": [{"evidence_id": "ev-test", "relative_path": "request.cbl"}]},
                    "evidence_refs": [{"evidence_id": "ev-test", "relative_path": "request.cbl"}],
                    "investigation": {"selected_paths": ["request.cbl"],
                                      "business_map": {"selected_paths": ["request.cbl", "candidate.cbl"]}},
                    "metrics": {"model_requests": 1, "policy": policy.to_dict(),
                                "usage": {"status": "complete", "total_tokens": 240},
                                "tool_calls": {"search": 1, "read": 2, "framework_search": 1},
                                "request_bytes": [1200]}}}

            with mock.patch("live_business_eval.CompanyAPIConfig.from_env", return_value=config), \
                    mock.patch("live_business_eval.OpenAICompatibleChatClient.complete",
                               return_value={"choices": [{"message": {"role": "assistant", "content": "OK"}}]}), \
                    mock.patch("live_business_eval.run_business_chat", side_effect=answer):
                report = evaluate(source, root / "evaluation", cases, allow_network=True,
                                  source_format="free", policy=policy)
            self.assertEqual(report["model_preflight"]["status"], "responded")
            self.assertEqual(history_sizes, [0, 2])
            self.assertEqual(received_policies, [policy, policy])
            self.assertEqual(len(report["cases"]), 2)
            self.assertEqual(report["cases"][0]["expected_paths_cited"], ["request.cbl"])
            self.assertEqual(report["cases"][0]["expected_paths_retrieved"], ["request.cbl"])
            self.assertEqual(report["cases"][0]["navigation_source_paths"], ["candidate.cbl", "request.cbl"])
            self.assertEqual(report["cases"][0]["retrieved_source_paths"], ["request.cbl"])
            self.assertEqual(report["cases"][0]["usage"]["total_tokens"], 240)
            self.assertEqual(report["cases"][0]["tool_calls"]["framework_search"], 1)
            self.assertEqual(report["cases"][0]["request_bytes"], [1200])
            self.assertEqual(report["cases"][0]["review_checks"], ["Explain the threshold"])
            self.assertEqual(report["quality_status"], "not_reviewed")
            self.assertIsNotNone(report["latency_seconds"]["p95"])

    def test_navigation_candidates_do_not_count_as_source_supplied_to_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            cases = root / "cases.json"
            cases.write_text(json.dumps({"cases": [{"id": "candidate", "question": "Explain the rule.",
                               "expected_source_paths": ["candidate.cbl"]}]}), encoding="utf-8")
            config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="test-key")
            result = {"runner_status": "COMPLETED", "agent_result": {
                "answer": "A response for review.", "metrics": {"model_requests": 1},
                "investigation": {"business_map": {"selected_paths": ["candidate.cbl"]},
                                  "selected_paths": ["supplied.cbl"]}}}
            with mock.patch("live_business_eval.CompanyAPIConfig.from_env", return_value=config), \
                    mock.patch("live_business_eval.OpenAICompatibleChatClient.complete",
                               return_value={"choices": [{"message": {"content": "OK"}}]}), \
                    mock.patch("live_business_eval.build_business_index"), \
                    mock.patch("live_business_eval.ensure_repository_search",
                               return_value={"snapshot_id": "test-snapshot", "indexed_files": 2}), \
                    mock.patch("live_business_eval.run_business_chat", return_value=result):
                report = evaluate(source, root / "evaluation", cases, allow_network=True, framework="")
            case = report["cases"][0]
            self.assertEqual(case["expected_paths_navigated"], ["candidate.cbl"])
            self.assertEqual(case["retrieved_source_paths"], ["supplied.cbl"])
            self.assertEqual(case["expected_paths_retrieved"], [])
            self.assertEqual(case["expected_paths_cited"], [])
            self.assertEqual(report["quality_status"], "not_reviewed")

    def test_plan_reports_bounds_without_any_model_calls_or_index_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            cases = root / "cases.json"
            cases.write_text(json.dumps({"cases": [
                {"id": "one", "question": "Initial question"},
                {"id": "two", "question": "Follow-up question"}]}), encoding="utf-8")
            config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="test-key",
                                      max_output_tokens=512)
            with mock.patch("live_business_eval.CompanyAPIConfig.from_env", return_value=config), \
                    mock.patch("live_business_eval.OpenAICompatibleChatClient",
                               side_effect=AssertionError("model client must not be constructed")), \
                    mock.patch("live_business_eval.build_business_index",
                               side_effect=AssertionError("source must not be indexed")):
                report = evaluate(source, root / "evaluation", cases, allow_network=True, plan=True,
                                  policy={"max_model_requests": 2, "max_request_bytes": 48000})
            self.assertEqual(report["model_preflight"]["status"], "planned")
            self.assertEqual(report["plan"]["case_count"], 2)
            self.assertEqual(report["plan"]["max_model_requests"], 5)
            self.assertEqual(report["plan"]["max_request_bytes"], 48000)
            self.assertEqual(report["plan"]["max_output_tokens_per_request"], 512)
            self.assertEqual(report["plan"]["timeout_seconds_per_request"], config.timeout_seconds)
            self.assertFalse(report["plan"]["matches_workbench_defaults"])
            self.assertEqual(report["plan"]["configuration_scope"], "evaluation_runner")
            self.assertEqual(report["plan"]["workbench_defaults"], {"timeout_seconds": 60.0, "max_output_tokens": 8192})
            self.assertEqual(report["plan"]["preflight_max_output_tokens"], 32)
            self.assertIsNone(report["plan"]["currency_estimate"])
            self.assertEqual(report["plan"]["spend_authorization"], "not_granted_by_request_budget")
            self.assertEqual(report["cases"], [])
            self.assertFalse((root / "evaluation").exists())

    def test_network_is_disabled_by_default_and_cli_dry_run_uses_policy_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            cases = root / "cases.json"
            cases.write_text(json.dumps({"cases": [{"id": "one", "question": "Explain the flow"}]}), encoding="utf-8")
            settings = root / "settings.json"
            settings.write_text(json.dumps({"version": 1, "agent": {"max_model_requests": 2}}), encoding="utf-8")
            config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="test-key")
            with mock.patch("live_business_eval.CompanyAPIConfig.from_env", return_value=config), \
                    mock.patch("live_business_eval.OpenAICompatibleChatClient",
                               side_effect=AssertionError("network must stay disabled")), mock.patch("builtins.print"):
                report = evaluate(source, root / "evaluation", cases)
                code = main(["--source", str(source), "--evaluation-dir", str(root / "evaluation"),
                             "--cases", str(cases), "--dry-run", "--agent-settings", str(settings)])
            self.assertEqual(report["model_preflight"]["status"], "network_not_enabled")
            self.assertEqual(code, 0)
            saved = json.loads((root / "evaluation" / "live-business-eval.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["plan"]["max_model_requests"], 3)
            self.assertEqual(saved["plan"]["max_model_requests_per_case"], 2)

    def test_adaptive_workbench_policy_gets_recorded_batch_evaluation_limits(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            cases = root / "cases.json"
            cases.write_text(json.dumps({"cases": [{"id": "one", "question": "Explain the flow"}]}),
                             encoding="utf-8")
            config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="test-key")
            adaptive = AgentPolicy()
            with mock.patch("live_business_eval.load_agent_policy", return_value=adaptive), \
                    mock.patch("live_business_eval.OpenAICompatibleChatClient",
                               side_effect=AssertionError("network must stay disabled")):
                report = evaluate(source, root / "evaluation", cases, config=config, plan=True)
            self.assertEqual(adaptive.max_model_requests, 6)
            self.assertEqual(adaptive.max_question_seconds, 180)
            self.assertTrue(report["plan"]["bounded_evaluation_defaults_applied"])
            self.assertEqual(report["plan"]["max_model_requests"], 6)
            self.assertEqual(report["plan"]["policy"]["max_model_requests"], 5)
            self.assertEqual(report["plan"]["policy"]["max_question_seconds"], 120)
            self.assertEqual(report["plan"]["policy"]["hard_question_seconds"], 120)

    def test_batch_default_never_expands_stricter_deployment_allowance(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            cases = root / "cases.json"
            cases.write_text(json.dumps({"cases": [{"id": "one", "question": "Explain the flow"}]}))
            config = CompanyAPIConfig("https://example.invalid/v1", "test-model", api_key="test-key")
            strict = AgentPolicy(max_model_requests=2, max_question_seconds=30, hard_question_seconds=30)
            with mock.patch("live_business_eval.load_agent_policy", return_value=strict):
                report = evaluate(source, root / "evaluation", cases, config=config, plan=True)
            self.assertEqual(report["plan"]["policy"], strict.to_dict())
            self.assertFalse(report["plan"]["bounded_evaluation_defaults_applied"])
            self.assertEqual(report["plan"]["max_model_requests"], 3)
            explicit = AgentPolicy(max_model_requests=6, max_question_seconds=180)
            report = evaluate(source, root / "evaluation", cases, config=config, plan=True, policy=explicit)
            self.assertEqual(report["plan"]["policy"], explicit.to_dict())
            self.assertFalse(report["plan"]["bounded_evaluation_defaults_applied"])


if __name__ == "__main__":
    unittest.main()
