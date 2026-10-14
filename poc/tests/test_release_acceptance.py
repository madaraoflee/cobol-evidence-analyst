from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import struct
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live_business_eval import (_evidence_metadata, _measurement, _review_identity,
                                review_template, _fingerprint, _turns)
from release_acceptance import (evaluate_release, file_sha256, main,
                                OFFICE_BASELINE_CHECKS, OFFICE_REGRESSION_BASELINE, _live_case_scope)


class ReleaseAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = {key: self.root / (key + ".json") for key in
                      ("compiled_report", "manifest", "policy", "live_report", "live_cases", "reviews", "office_report")}
        self.paths["package"] = self.root / "desktop.zip"
        self.commit, self.application = "a" * 40, "b" * 64
        binary = bytearray(128)
        binary[:2] = b"MZ"
        struct.pack_into("<I", binary, 0x3C, 64)
        binary[64:68] = b"PE\0\0"
        struct.pack_into("<H", binary, 68, 0x8664)
        self.binary = hashlib.sha256(binary).hexdigest()
        self.binary_path = "COBOLWorkbench/COBOLWorkbench/COBOLWorkbench.exe"
        with zipfile.ZipFile(self.paths["package"], "w") as archive:
            archive.writestr(self.binary_path, binary)
            archive.writestr("COBOLWorkbench/BUILD-INFO.json", json.dumps({
                "target": "windows-x64", "source_fingerprint": self.application,
                "source_commit": self.commit, "source_dirty": False,
                "self_test": {"ok": True, "model_calls": 0, "desktop_dependencies": True,
                              "json_accelerated": True}}))
        manifest = {"schema_version": "desktop-joint-acceptance/v1", "cases": [{
            "id": "formula", "kind": "answer", "repeats": 2,
            "assertions": ["quality_formula", "completeness_condition", "evidence_supplied"],
            "assertion_groups": {"quality": ["quality_formula"], "completeness": ["completeness_condition"],
                                 "evidence": ["evidence_supplied"]},
            "limits": {"cold_index_seconds": 20, "first_provider_seconds": 5, "question_seconds": 10}}]}
        self.write("manifest", manifest)
        compiled = {"schema_version": "compiled-workbench-acceptance/v1",
            "source_commit": self.commit, "source_dirty": False, "runtime_platform": "Windows",
            "execution_surface": "native_windows_http", "transport_kind": "synthetic",
            "report_execution_id": "compiled-run", "archive_sha256": file_sha256(self.paths["package"]),
            "selection_complete": True, "errors": [], "status": "passed",
            "binary_path": self.binary_path, "binary_sha256": self.binary, "application_sha256": self.application,
            "manifest_sha256": file_sha256(self.paths["manifest"]), "cases": [
                {"id": "formula", "turn_id": "1", "sample_index": sample, "run_id": f"run-{sample}",
                 "answer_sha256": "c" * 64, "elapsed_seconds": 2, "index_reused": sample > 1,
                 "question_index_reused": True,
                 "timings": {"cold_index_seconds": 3 if sample == 1 else None,
                             "first_provider_seconds": 1, "question_seconds": 2},
                 "checks": {key: True for key in manifest["cases"][0]["assertions"]}}
                for sample in (1, 2)]}
        self.write("compiled_report", compiled)
        environment = {"schema_version": "office-execution-environment/v1", "machine_sha256": "9" * 64,
                       "system": "Windows"}
        office_rows = copy.deepcopy(compiled["cases"])
        for row in office_rows:
            row.update(run_id="office-" + row["run_id"], kind="answer", agent_status="ANALYZED", stop_reason="sufficient_material")
            row["answer"] = "The result is twice the input under the stated condition."
            row["answer_sha256"] = hashlib.sha256(row["answer"].encode()).hexdigest()
            final = {"question_investigation": {"required_items": [], "open_gaps": []},
                     "answer_completion": {"status": "not_assessed", "missing_aspects": []}, "stop_reason": "sufficient_material"}
            row["acceptance_diagnostics"] = {"schema_version": "executable-answer-diagnostics/v1",
                "run_id": row["run_id"], "answer_sha256": row["answer_sha256"], "trace_sha256": "8" * 64,
                "agent_status": "ANALYZED", "stop_reason": "sufficient_material", "final": final,
                "final_sha256": _fingerprint(final)}
        self.write("office_report", {**compiled, "cases": office_rows, "report_execution_id": "office-native-run",
                                     "office_environment": environment})
        source_hash = "d" * 64
        gold = {
            "required_findings": [{"id": "formula", "supporting_ranges": [
                {"path": "entry.cbl", "start_line": 3, "end_line": 5, "source_sha256": source_hash}]}],
            "forbidden_claims": [{"id": "unsupported"}]}
        items = [{"id": "flow", "split": "regression", "gold": gold, "turns": [
            {"id": "flow", "question": "How is the result computed?"},
            {"id": "condition", "question": "When is that calculation applied?"},
            {"id": "overwrite", "question": "Can that result be overwritten later?"}]},
            {"id": "reserved", "split": "holdout", "gold": copy.deepcopy(gold),
             "question": "How does the input determine the final amount?"}]
        items[0]["coverage"] = list(OFFICE_REGRESSION_BASELINE)
        self.write("live_cases", {"cases": items})
        runtime = {key: compiled[key] for key in ("source_commit", "source_dirty", "runtime_platform",
            "execution_surface", "archive_sha256", "binary_path", "binary_sha256", "application_sha256")}
        runtime["transport_kind"] = "remote_http"
        rows = []
        for item in items:
            history = []
            for turn in _turns(item):
                row = self.make_live_row(item, turn, runtime, history)
                rows.append(row)
                history.extend([{"role": "user", "content": row["question"]},
                                {"role": "assistant", "content": row["answer"]}])
        live = {"schema_version": "live-business-eval/v3", "evaluation_run_id": "evaluation-1",
                "errors": [], "status": "completed_not_reviewed",
                "runtime_provenance": runtime, "cases_sha256": file_sha256(self.paths["live_cases"]),
                "plan": {"mode": "automatic"}, "cases": rows, "office_environment": environment}
        self.write("live_report", live)
        reviews = review_template(items, live)["reviews"]
        for row, review in zip(rows, reviews):
            review.update(reviewer="local-reviewer", reviewed_at="2026-10-14T10:00:00Z",
                          required_findings=[{"id": "formula", "result": "met"}],
                          forbidden_claims=[{"id": "unsupported", "present": False}],
                          usefulness="usable", followup_continuity="preserved" if row["history"] else "not_applicable")
        office = {key: compiled[key] for key in ("source_commit", "archive_sha256", "binary_sha256")}
        office.update(evaluation_run_id="evaluation-1", live_report_sha256=file_sha256(self.paths["live_report"]),
                      reviewer="local-reviewer", reviewed_at="2026-10-14T11:00:00Z",
                      office_report_sha256=file_sha256(self.paths["office_report"]),
                      checks={name: True for name in OFFICE_BASELINE_CHECKS})
        self.write("reviews", {"reviews": reviews, "office_acceptance": office})
        self.write("policy", {"schema_version": "release-acceptance-policy/v1", "source_commit": self.commit,
                              "manifest_sha256": file_sha256(self.paths["manifest"]),
                              "live_cases_sha256": file_sha256(self.paths["live_cases"]),
                              "live_limits": {"question_seconds": 10, "p95_question_seconds": 10,
                                              "min_evidence_recall": 1},
                              "office_required_checks": list(OFFICE_BASELINE_CHECKS)})

    def make_live_row(self, item, turn, runtime, history):
        merged = {**item, **turn}
        row = {"id": item["id"], "turn_id": turn["id"], "run_id": f"live-run:{item['id']}:{turn['id']}",
               "question": turn["question"], "conversation": item["id"],
               "answer": "The result is twice the input under the stated condition.", "elapsed_seconds": 2,
               "model_requests": 1, "remote_result_unknown": False, "reason_code": None, "stop_reason": "sufficient_material",
               "agent_status": "ANALYZED",
               "history": copy.deepcopy(history), "policy": {"max_model_requests": 2}, "quality_status": "not_reviewed"}
        final = {"question_investigation": {"required_items": [], "open_gaps": []},
                 "answer_completion": {"status": "not_assessed", "missing_aspects": []}, "stop_reason": "sufficient_material"}
        row["acceptance_diagnostics"] = {"schema_version": "executable-answer-diagnostics/v1",
            "run_id": row["run_id"], "trace_sha256": "8" * 64,
            "answer_sha256": hashlib.sha256(row["answer"].encode()).hexdigest(),
            "agent_status": row.get("agent_status"), "stop_reason": "sufficient_material",
            "final": final, "final_sha256": _fingerprint(final)}
        evidence = {"sources": [{"evidence_id": "ev-1", "path": "entry.cbl", "source_sha256": "d" * 64,
                                "start_line": 3, "end_line": 5, "excerpt_sha256": "e" * 64}], "framework": []}
        row["supplied_evidence"] = _evidence_metadata(evidence)
        row["measurement"] = _measurement(row, runtime, file_sha256(self.paths["live_cases"]))
        row["review_identity"] = _review_identity(run_id=row["run_id"], case_id=item["id"], turn_id=turn["id"],
            branch="automatic", answer=row["answer"], model="test-model",
            prompt={"version": "business-v1", "system_sha256": "f" * 64, "request_sha256": ["1" * 64]},
            policy=row["policy"], gold=merged["gold"], context=evidence, source_snapshot_id="snapshot-1",
            question=turn["question"], history=row["history"], runtime_provenance=runtime, measurement=row["measurement"])
        row["review_identity"]["acceptance_diagnostics_sha256"] = _fingerprint(row["acceptance_diagnostics"])
        row["review_identity"]["identity_sha256"] = _fingerprint({key: value for key, value in
            row["review_identity"].items() if key != "identity_sha256"})
        return row

    def read(self, key):
        return json.loads(self.paths[key].read_text(encoding="utf-8"))

    def write(self, key, document):
        self.paths[key].write_text(json.dumps(document), encoding="utf-8")

    def run_gate(self, live=True):
        return evaluate_release(**{key: value for key, value in self.paths.items()
            if live or key not in {"live_report", "live_cases", "reviews"}})

    def refresh_live_review(self, live, row_index=0):
        """A new human attestation must still fail independent machine checks."""
        row = live["cases"][row_index]
        identity = row["review_identity"]
        row["measurement"] = _measurement(row, live["runtime_provenance"], live["cases_sha256"])
        identity["measurement_sha256"] = _fingerprint(row["measurement"])
        identity["runtime_sha256"] = _fingerprint(live["runtime_provenance"])
        identity["evidence_sha256"] = _fingerprint(_evidence_metadata(row["supplied_evidence"]))
        identity["history_sha256"] = _fingerprint(row["history"])
        identity["identity_sha256"] = _fingerprint({key: value for key, value in identity.items()
                                                    if key != "identity_sha256"})
        self.write("live_report", live)
        reviews = self.read("reviews")
        reviews["reviews"][row_index]["identity"] = copy.deepcopy(identity)
        reviews["office_acceptance"]["live_report_sha256"] = file_sha256(self.paths["live_report"])
        self.write("reviews", reviews)

    def replace_live_suite(self, items):
        """Create internally valid reviews for a deliberately undersized suite."""
        self.write("live_cases", {"cases": items})
        live = self.read("live_report")
        live["cases_sha256"] = file_sha256(self.paths["live_cases"])
        live["cases"] = []
        for item in items:
            history = []
            for turn in _turns(item):
                row = self.make_live_row(item, turn, live["runtime_provenance"], history)
                live["cases"].append(row)
                history.extend([{"role": "user", "content": row["question"]},
                                {"role": "assistant", "content": row["answer"]}])
        self.write("live_report", live)
        document = self.read("reviews")
        document["reviews"] = review_template(items, live)["reviews"]
        for row, review in zip(live["cases"], document["reviews"]):
            review.update(reviewer="local-reviewer", reviewed_at="2026-10-14T10:00:00Z",
                          required_findings=[{"id": "formula", "result": "met"}],
                          forbidden_claims=[{"id": "unsupported", "present": False}],
                          usefulness="usable", followup_continuity="preserved" if row["history"] else "not_applicable")
        document["office_acceptance"]["live_report_sha256"] = file_sha256(self.paths["live_report"])
        self.write("reviews", document)
        policy = self.read("policy")
        policy["live_cases_sha256"] = live["cases_sha256"]
        self.write("policy", policy)

    def test_joint_contract_accepts_exact_native_artifact_and_reviews(self):
        result = self.run_gate()
        self.assertTrue(result["candidate_allowed"], result)
        self.assertTrue(result["publish_allowed"], result)
        self.assertEqual(result["live_cases"][0]["supplied_evidence_recall"], 1)
        serialized = json.dumps(result)
        self.assertNotIn("The result is twice", serialized)
        self.assertNotIn("local-reviewer", serialized)

    def test_actual_office_report_cannot_be_missing_reused_or_from_another_machine(self):
        report = self.read("office_report")
        for defect in ("missing", "reuse", "machine", "assertion", "hash"):
            with self.subTest(defect=defect):
                self.write("office_report", copy.deepcopy(report))
                if defect == "missing":
                    paths = {key: value for key, value in self.paths.items() if key != "office_report"}
                    result = evaluate_release(**paths)
                else:
                    changed = copy.deepcopy(report)
                    if defect == "reuse":
                        changed["report_execution_id"] = "compiled-run"
                    elif defect == "machine":
                        changed["office_environment"]["machine_sha256"] = "0" * 64
                    elif defect == "assertion":
                        changed["cases"][0]["checks"]["quality_formula"] = False
                    else:
                        changed["archive_sha256"] = "0" * 64
                    self.write("office_report", changed)
                    result = self.run_gate()
                self.assertFalse(result["publish_allowed"], result)
                self.assertTrue(result["candidate_allowed"], result)

    def test_prior_problem_coverage_cannot_be_replaced_by_easy_live_suite(self):
        originals = self.read("live_cases")["cases"]
        for missing in ("qualified_identity", "bare_group_field", "qualified_in_alias", "ambiguous_group_field"):
            with self.subTest(missing=missing):
                items = copy.deepcopy(originals)
                items[0]["coverage"].remove(missing)
                self.replace_live_suite(items)
                result = self.run_gate()
                self.assertFalse(result["publish_allowed"])
                self.assertIn("LIVE_PRIOR_REGRESSIONS_MISSING", {row["code"] for row in result["publish_errors"]})

    def test_partial_requires_bound_diagnostics_and_specific_human_resolution(self):
        live = self.read("live_report")
        row = live["cases"][0]
        row["agent_status"] = row["acceptance_diagnostics"]["agent_status"] = "PARTIAL"
        identity = row["review_identity"]
        identity["acceptance_diagnostics_sha256"] = _fingerprint(row["acceptance_diagnostics"])
        self.refresh_live_review(live)
        result = self.run_gate()
        self.assertFalse(result["publish_allowed"])
        self.assertIn("LIVE_PARTIAL_NOT_EXPLAINED", {entry["code"] for entry in result["publish_errors"]})
        reviews = self.read("reviews")
        reviews["reviews"][0]["partial_review"] = {"classification": "explicit_unknown",
            "reason": "The source does not initialize the runtime input, but the rule is independently supported.",
            "affected_findings": ["formula"], "resolved": True}
        self.write("reviews", reviews)
        self.assertTrue(self.run_gate()["publish_allowed"])
        live["cases"][0]["acceptance_diagnostics"]["final"]["new_gap"] = "unreviewed"
        self.write("live_report", live)
        result = self.run_gate()
        self.assertFalse(result["publish_allowed"])
        self.assertIn("LIVE_DIAGNOSTICS_MISSING_OR_STALE", {entry["code"] for entry in result["publish_errors"]})

    def test_office_baseline_cannot_be_reduced_by_policy_or_completed_form(self):
        original_policy, original_reviews = self.read("policy"), self.read("reviews")
        for missing in OFFICE_BASELINE_CHECKS:
            for location in ("policy", "review"):
                with self.subTest(missing=missing, location=location):
                    policy, reviews = copy.deepcopy(original_policy), copy.deepcopy(original_reviews)
                    if location == "policy":
                        policy["office_required_checks"].remove(missing)
                    else:
                        reviews["office_acceptance"]["checks"].pop(missing)
                    self.write("policy", policy)
                    self.write("reviews", reviews)
                    result = self.run_gate()
                    self.assertTrue(result["candidate_allowed"])
                    self.assertFalse(result["publish_allowed"])
                    self.assertIn("OFFICE_POLICY_MISSING" if location == "policy" else "OFFICE_ACCEPTANCE_MISSING_OR_STALE",
                                  {entry["code"] for entry in result["publish_errors"]})
        original_policy["office_required_checks"].append("target_environment")
        original_reviews["office_acceptance"]["checks"]["target_environment"] = True
        self.write("policy", original_policy)
        self.write("reviews", original_reviews)
        self.assertTrue(self.run_gate()["publish_allowed"])

    def test_scope_preflight_and_gate_reject_smaller_suites_with_fresh_valid_reviews(self):
        original = self.read("live_cases")["cases"]
        mutations = [
            (lambda items: items[0].update(turns=items[0]["turns"][:2]), "LIVE_THREE_TURN_CASE_REQUIRED"),
            (lambda items: items[1].update(split="development"), "LIVE_HOLDOUT_CASE_REQUIRED"),
            (lambda items: items[0].update(split="development"), "LIVE_REGRESSION_CASE_REQUIRED"),
            (lambda items: items.pop(0), "LIVE_THREE_TURN_CASE_REQUIRED")]
        for mutate, expected in mutations:
            with self.subTest(expected=expected):
                items = copy.deepcopy(original)
                mutate(items)
                errors = []
                self.assertFalse(_live_case_scope(items, errors))
                self.assertIn(expected, {entry["code"] for entry in errors})
                self.replace_live_suite(items)
                result = self.run_gate()
                self.assertTrue(result["candidate_allowed"])
                self.assertFalse(result["publish_allowed"])
                self.assertIn(expected, {entry["code"] for entry in result["publish_errors"]})
                self.assertNotIn("LIVE_IDENTITY_INVALID", {entry["code"] for entry in result["publish_errors"]})
        original[0]["split"] = "public-development"
        self.replace_live_suite(original)
        self.assertTrue(self.run_gate()["publish_allowed"])

    def test_followup_rows_require_real_history_even_with_rebound_reviews(self):
        original = self.read("live_report")
        for row_index in (1, 2):
            for history in ([], [{"role": "user", "content": "Only a question"}]):
                with self.subTest(row=row_index, history=history):
                    live = copy.deepcopy(original)
                    live["cases"][row_index]["history"] = history
                    self.refresh_live_review(live, row_index)
                    reviews = self.read("reviews")
                    reviews["reviews"][row_index]["followup_continuity"] = "preserved" if history else "not_applicable"
                    self.write("reviews", reviews)
                    result = self.run_gate()
                    self.assertFalse(result["publish_allowed"])
                    self.assertIn("LIVE_FOLLOWUP_HISTORY_MISSING", {entry["code"] for entry in result["publish_errors"]})

    def test_scope_preflight_does_not_count_invalid_three_turn_structures(self):
        for invalid in ([{}, {}, {}], [{"id": "same", "question": "Why?"}] * 3):
            items = self.read("live_cases")["cases"]
            items[0]["turns"] = invalid
            errors = []
            self.assertFalse(_live_case_scope(items, errors))
            self.assertIn("LIVE_THREE_TURN_CASE_REQUIRED", {entry["code"] for entry in errors})

    def test_offline_success_only_allows_candidate_even_with_forged_green_summary(self):
        compiled = self.read("compiled_report")
        compiled.update(publish_allowed=True, quality_status="correct", model_accuracy=1)
        self.write("compiled_report", compiled)
        result = self.run_gate(live=False)
        self.assertTrue(result["candidate_allowed"])
        self.assertFalse(result["publish_allowed"])

    def test_archive_binary_application_commit_and_manifest_must_match(self):
        original = self.read("compiled_report")
        for field in ("archive_sha256", "binary_sha256", "application_sha256", "source_commit", "manifest_sha256"):
            with self.subTest(field=field):
                document = copy.deepcopy(original)
                document[field] = "0" * (40 if field == "source_commit" else 64)
                self.write("compiled_report", document)
                self.assertFalse(self.run_gate()["candidate_allowed"])
        self.write("compiled_report", original)

    def test_package_identity_is_recomputed_even_if_report_hashes_are_updated(self):
        raw = self.paths["package"].read_bytes()
        original = self.read("compiled_report")
        for mutation in ("casefold_duplicate", "unsafe_path", "wrong_machine", "dirty", "missing_commit", "other_exe"):
            with self.subTest(mutation=mutation):
                self.paths["package"].write_bytes(raw)
                with zipfile.ZipFile(self.paths["package"]) as archive:
                    members = {name: archive.read(name) for name in archive.namelist()}
                compiled = copy.deepcopy(original)
                if mutation == "casefold_duplicate":
                    members["COBOLWorkbench/build-info.json"] = b"{}"
                elif mutation == "unsafe_path":
                    members["../escape.txt"] = b"unsafe"
                elif mutation == "wrong_machine":
                    binary = bytearray(members[self.binary_path])
                    struct.pack_into("<H", binary, 68, 0x14C)
                    members[self.binary_path] = binary
                    compiled["binary_sha256"] = hashlib.sha256(binary).hexdigest()
                elif mutation == "other_exe":
                    members["COBOLWorkbench/other.exe"] = members[self.binary_path]
                    compiled["binary_path"] = "COBOLWorkbench/other.exe"
                else:
                    metadata = json.loads(members["COBOLWorkbench/BUILD-INFO.json"])
                    if mutation == "dirty":
                        metadata["source_dirty"] = True
                    else:
                        metadata.pop("source_commit")
                    members["COBOLWorkbench/BUILD-INFO.json"] = json.dumps(metadata).encode()
                with zipfile.ZipFile(self.paths["package"], "w") as archive:
                    for name, value in members.items():
                        archive.writestr(name, value)
                compiled["archive_sha256"] = file_sha256(self.paths["package"])
                self.write("compiled_report", compiled)
                self.assertFalse(self.run_gate()["candidate_allowed"])

    def test_exact_samples_checks_and_bounded_real_timings_are_required(self):
        original = self.read("compiled_report")
        changes = [lambda doc: doc["cases"].pop(),
                   lambda doc: doc["cases"].append(copy.deepcopy(doc["cases"][0])),
                   lambda doc: doc["cases"][0]["checks"].update(quality_formula=False),
                   lambda doc: doc["cases"][0]["checks"].pop("completeness_condition"),
                   lambda doc: doc["cases"][0].update(answer_sha256=None),
                   lambda doc: doc["cases"][0].update(index_reused=True),
                   lambda doc: doc["cases"][0]["timings"].update(cold_index_seconds=None),
                   lambda doc: doc["cases"][0]["timings"].update(first_provider_seconds=True),
                   lambda doc: doc["cases"][0]["timings"].update(question_seconds=float("nan")),
                   lambda doc: doc["cases"][0]["timings"].update(first_provider_seconds=6),
                   lambda doc: doc["cases"][0].update(elapsed_seconds=1),
                   lambda doc: doc["cases"][0].update(question_index_reused=False),
                   lambda doc: doc["cases"][1].update(run_id=doc["cases"][0]["run_id"]),
                   lambda doc: doc.update(selection_complete=False),
                   lambda doc: doc.update(errors=["failed"]),
                   lambda doc: doc.update(status="failed"),
                   lambda doc: doc.update(execution_surface="source_python"),
                   lambda doc: doc.update(source_dirty=True)]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                document = copy.deepcopy(original)
                change(document)
                self.write("compiled_report", document)
                self.assertFalse(self.run_gate()["candidate_allowed"])

    def test_live_fake_source_python_dirty_or_other_artifact_never_passes(self):
        original = self.read("live_report")
        for field, value in (("transport_kind", "synthetic"), ("execution_surface", "source_python"),
                             ("runtime_platform", "Darwin"), ("source_dirty", True),
                             ("archive_sha256", "0" * 64)):
            with self.subTest(field=field):
                document = copy.deepcopy(original)
                document["runtime_provenance"][field] = value
                self.write("live_report", document)
                self.assertFalse(self.run_gate()["publish_allowed"])

    def test_summary_green_does_not_cover_missing_case_review_evidence_or_time(self):
        original = self.read("live_report")
        changes = [lambda doc: doc["cases"].clear(),
                   lambda doc: doc["cases"][0].update(answer="Changed answer"),
                   lambda doc: doc["cases"][0].update(elapsed_seconds=999),
                   lambda doc: doc["cases"][0].pop("measurement"),
                   lambda doc: doc["cases"][0].pop("supplied_evidence"),
                   lambda doc: doc["cases"][0]["supplied_evidence"]["sources"][0].update(end_line=3),
                   lambda doc: doc["cases"][0].update(policy={"max_model_requests": 9}),
                   lambda doc: doc["cases"][0].update(history=[{"role": "user", "content": "different"}]),
                   lambda doc: doc["cases"][0].update(run_id="different"),
                   lambda doc: doc["cases"][0].update(truncated=True),
                   lambda doc: doc["cases"][0].update(remote_result_unknown=True),
                   lambda doc: doc["cases"][0].update(reason_code="QUESTION_TIME_BUDGET_EXHAUSTED"),
                   lambda doc: doc["cases"][0].update(stop_reason="REMOTE_REQUEST_OUTCOME_UNKNOWN"),
                   lambda doc: doc["cases"][0].update(reason_code="MODEL_OUTPUT_TRUNCATED"),
                   lambda doc: doc["cases"][0].update(stop_reason="cancelled"),
                   lambda doc: doc["cases"][0].update(model_requests=0),
                   lambda doc: doc.update(errors=[{"error_type": "TimeoutError"}]),
                   lambda doc: doc.update(status="interrupted_attention_required"),
                   lambda doc: doc.update(plan={"mode": "reviewed-context"})]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                document = copy.deepcopy(original)
                document.update(quality_status="correct", correctness_counts={"correct": 100},
                                latency_seconds={"p95": 0}, publish_allowed=True)
                change(document)
                self.write("live_report", document)
                self.assertFalse(self.run_gate()["publish_allowed"])

    def test_complete_form_with_omission_wrong_rule_or_unusable_answer_fails(self):
        original = self.read("reviews")
        changes = [lambda doc: doc["reviews"][0]["required_findings"][0].update(result="omitted"),
                   lambda doc: doc["reviews"][0]["required_findings"][0].update(result="incorrect"),
                   lambda doc: doc["reviews"][0]["forbidden_claims"][0].update(present=True),
                   lambda doc: doc["reviews"][0].update(usefulness="needs_work"),
                   lambda doc: doc["reviews"][0].update(reviewer=""),
                   lambda doc: doc["reviews"].append(copy.deepcopy(doc["reviews"][0])),
                   lambda doc: doc["reviews"][0]["identity"].update(run_id="older-run"),
                   lambda doc: doc["office_acceptance"].update(live_report_sha256="0" * 64),
                   lambda doc: doc["office_acceptance"]["checks"].update(desktop_start=False),
                   lambda doc: doc.pop("office_acceptance")]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                document = copy.deepcopy(original)
                change(document)
                self.write("reviews", document)
                self.assertFalse(self.run_gate()["publish_allowed"])

    def test_current_reviews_cannot_override_recomputed_latency_and_evidence_recall(self):
        original = self.read("live_report")
        cases = [(lambda row: row.update(elapsed_seconds=11), "LIVE_LATENCY_EXCEEDED"),
                 (lambda row: row["supplied_evidence"]["sources"][0].update(end_line=3),
                  "LIVE_SUPPLIED_EVIDENCE_INCOMPLETE")]
        for mutate, expected in cases:
            with self.subTest(expected=expected):
                live = copy.deepcopy(original)
                mutate(live["cases"][0])
                live.update(quality_status="correct", latency_seconds={"p95": 0}, supplied_evidence_recall=1)
                self.refresh_live_review(live)
                decision = self.run_gate()
                self.assertFalse(decision["publish_allowed"])
                self.assertIn(expected, {entry["code"] for entry in decision["publish_errors"]})

    def test_native_report_adapter_contract_round_trip(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        from desktop_live_acceptance import live_row
        live = self.read("live_report")
        old = live["cases"][0]
        case = self.read("live_cases")["cases"][0]
        item = {**case, **case["turns"][0]}
        trace = {"run_id": old["run_id"], "analysis_revision": "snapshot-1", "prompt_version": "business-v1",
                 "configuration": {"policy": old["policy"],
                                   "model_fingerprint": hashlib.sha256(b"test-model").hexdigest()},
                 "final": {"final_answer_round_ids": ["final"], "question_investigation": {"required_items": [], "open_gaps": []},
                           "answer_completion": {"status": "not_assessed", "missing_aspects": []}, "stop_reason": "sufficient_material"}, "rounds": [{"round_id": "final",
                     "system_prompt_sha256": "f" * 64, "request_body_sha256": "1" * 64,
                     "context": {"history": []}, **old["supplied_evidence"]}]}
        trace_path = self.root / "trace.json"
        trace_path.write_text(json.dumps(trace), encoding="utf-8")
        observation = {"job": {"status": "COMPLETED"}, "elapsed_seconds": 2,
            "state": {"conversation": {"messages": [{"content": old["answer"]}]}},
            "result": {"runner_status": "READY", "agent_result": {"answer": old["answer"],
                "model_answer_recorded": True, "status": "ANALYZED", "stop_reason": "sufficient_material",
                "metrics": {"quality_trace_path": str(trace_path), "model_requests": 1}}}}
        journal = {"requests": [{"run_id": old["run_id"], "state": "completed", "request_hash": "1" * 64}]}
        with mock.patch("desktop_live_acceptance.read_journal", return_value=journal) as read_journal:
            live["cases"][0] = live_row(observation, {**item, "turn_id": "flow", "conversation": "flow"},
                                       live["runtime_provenance"], live["cases_sha256"], "test-model")
        read_journal.assert_called_once_with(trace_path.parent.parent)
        self.refresh_live_review(live)
        result = self.run_gate()
        self.assertTrue(result["publish_allowed"], result)

    def test_policy_and_gold_are_frozen_and_required(self):
        cases = self.read("live_cases")
        cases["cases"][0]["gold"]["required_findings"].clear()
        self.write("live_cases", cases)
        self.assertFalse(self.run_gate()["publish_allowed"])
        config = self.read("policy")
        config.pop("manifest_sha256")
        self.write("policy", config)
        self.assertFalse(self.run_gate()["candidate_allowed"])

    def test_malformed_nested_documents_fail_closed_with_audit(self):
        original = self.read("live_report")
        for bad in (None, [], "green", True):
            with self.subTest(bad=bad):
                document = copy.deepcopy(original)
                document["cases"][0]["review_identity"] = bad
                self.write("live_report", document)
                result = self.run_gate()
                self.assertFalse(result["publish_allowed"])
                self.assertTrue(result["publish_errors"])

    def test_cli_publish_is_nonzero_without_real_reviews_candidate_is_explicit(self):
        output = self.root / "decision.json"
        args = [argument for key in ("package", "compiled_report", "manifest", "policy")
                for argument in ("--" + key.replace("_", "-"), str(self.paths[key]))]
        args += ["--output", str(output)]
        with mock.patch("builtins.print"):
            self.assertEqual(main([*args, "--require-publish"]), 1)
            self.assertFalse(json.loads(output.read_text())["publish_allowed"])
            self.assertEqual(main([*args, "--candidate"]), 0)
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as raised:
            main(args)
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
