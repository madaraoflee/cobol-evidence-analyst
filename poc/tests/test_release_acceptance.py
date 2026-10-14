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
                                review_template, _fingerprint)
from release_acceptance import evaluate_release, file_sha256, main


class ReleaseAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = {key: self.root / (key + ".json") for key in
                      ("compiled_report", "manifest", "policy", "live_report", "live_cases", "reviews")}
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
        source_hash = "d" * 64
        item = {"id": "flow", "question": "How is the result computed?", "gold": {
            "required_findings": [{"id": "formula", "supporting_ranges": [
                {"path": "entry.cbl", "start_line": 3, "end_line": 5, "source_sha256": source_hash}]}],
            "forbidden_claims": [{"id": "unsupported"}]}}
        self.write("live_cases", {"cases": [item]})
        runtime = {key: compiled[key] for key in ("source_commit", "source_dirty", "runtime_platform",
            "execution_surface", "archive_sha256", "binary_path", "binary_sha256", "application_sha256")}
        runtime["transport_kind"] = "remote_http"
        row = {"id": "flow", "turn_id": "flow", "run_id": "live-run:flow", "question": item["question"],
               "answer": "The result is twice the input under the stated condition.", "elapsed_seconds": 2,
               "model_requests": 1,
               "remote_result_unknown": False, "reason_code": None, "stop_reason": None,
               "history": [], "policy": {"max_model_requests": 2}, "quality_status": "not_reviewed"}
        evidence = {"sources": [{"evidence_id": "ev-1", "path": "entry.cbl", "source_sha256": source_hash,
                                "start_line": 3, "end_line": 5, "excerpt_sha256": "e" * 64}], "framework": []}
        row["supplied_evidence"] = _evidence_metadata(evidence)
        row["measurement"] = _measurement(row, runtime, file_sha256(self.paths["live_cases"]))
        row["review_identity"] = _review_identity(run_id=row["run_id"], case_id="flow", turn_id="flow",
            branch="automatic", answer=row["answer"], model="test-model",
            prompt={"version": "business-v1", "system_sha256": "f" * 64, "request_sha256": ["1" * 64]},
            policy=row["policy"], gold=item["gold"], context=evidence, source_snapshot_id="snapshot-1",
            question=item["question"], history=[], runtime_provenance=runtime, measurement=row["measurement"])
        live = {"schema_version": "live-business-eval/v3", "evaluation_run_id": "evaluation-1",
                "errors": [], "status": "completed_not_reviewed",
                "runtime_provenance": runtime, "cases_sha256": file_sha256(self.paths["live_cases"]),
                "plan": {"mode": "automatic"}, "cases": [row]}
        self.write("live_report", live)
        review = review_template([item], live)["reviews"][0]
        review.update(reviewer="local-reviewer", reviewed_at="2026-10-14T10:00:00Z",
                      required_findings=[{"id": "formula", "result": "met"}],
                      forbidden_claims=[{"id": "unsupported", "present": False}],
                      usefulness="usable", followup_continuity="not_applicable")
        office = {key: compiled[key] for key in ("source_commit", "archive_sha256", "binary_sha256")}
        office.update(evaluation_run_id="evaluation-1", live_report_sha256=file_sha256(self.paths["live_report"]),
                      reviewer="local-reviewer", reviewed_at="2026-10-14T11:00:00Z", checks={"desktop_start": True})
        self.write("reviews", {"reviews": [review], "office_acceptance": office})
        self.write("policy", {"schema_version": "release-acceptance-policy/v1", "source_commit": self.commit,
                              "manifest_sha256": file_sha256(self.paths["manifest"]),
                              "live_cases_sha256": file_sha256(self.paths["live_cases"]),
                              "live_limits": {"question_seconds": 10, "p95_question_seconds": 10,
                                              "min_evidence_recall": 1},
                              "office_required_checks": ["desktop_start"]})

    def read(self, key):
        return json.loads(self.paths[key].read_text(encoding="utf-8"))

    def write(self, key, document):
        self.paths[key].write_text(json.dumps(document), encoding="utf-8")

    def run_gate(self, live=True):
        return evaluate_release(**{key: value for key, value in self.paths.items()
            if live or key not in {"live_report", "live_cases", "reviews"}})

    def refresh_live_review(self, live):
        """A new human attestation must still fail independent machine checks."""
        row = live["cases"][0]
        identity = row["review_identity"]
        row["measurement"] = _measurement(row, live["runtime_provenance"], live["cases_sha256"])
        identity["measurement_sha256"] = _fingerprint(row["measurement"])
        identity["runtime_sha256"] = _fingerprint(live["runtime_provenance"])
        identity["evidence_sha256"] = _fingerprint(_evidence_metadata(row["supplied_evidence"]))
        identity["identity_sha256"] = _fingerprint({key: value for key, value in identity.items()
                                                    if key != "identity_sha256"})
        self.write("live_report", live)
        reviews = self.read("reviews")
        reviews["reviews"][0]["identity"] = copy.deepcopy(identity)
        reviews["office_acceptance"]["live_report_sha256"] = file_sha256(self.paths["live_report"])
        self.write("reviews", reviews)

    def test_joint_contract_accepts_exact_native_artifact_and_reviews(self):
        result = self.run_gate()
        self.assertTrue(result["candidate_allowed"], result)
        self.assertTrue(result["publish_allowed"], result)
        self.assertEqual(result["live_cases"][0]["supplied_evidence_recall"], 1)
        serialized = json.dumps(result)
        self.assertNotIn("The result is twice", serialized)
        self.assertNotIn("local-reviewer", serialized)

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
        item = self.read("live_cases")["cases"][0]
        trace = {"run_id": old["run_id"], "analysis_revision": "snapshot-1", "prompt_version": "business-v1",
                 "configuration": {"policy": old["policy"],
                                   "model_fingerprint": hashlib.sha256(b"test-model").hexdigest()},
                 "final": {"final_answer_round_ids": ["final"]}, "rounds": [{"round_id": "final",
                     "system_prompt_sha256": "f" * 64, "request_body_sha256": "1" * 64,
                     "context": {"history": []}, **old["supplied_evidence"]}]}
        trace_path = self.root / "trace.json"
        trace_path.write_text(json.dumps(trace), encoding="utf-8")
        observation = {"job": {"status": "COMPLETED"}, "elapsed_seconds": 2,
            "state": {"conversation": {"messages": [{"content": old["answer"]}]}},
            "result": {"runner_status": "READY", "agent_result": {"answer": old["answer"],
                "model_answer_recorded": True, "metrics": {"quality_trace_path": str(trace_path), "model_requests": 1}}}}
        journal = {"requests": [{"run_id": old["run_id"], "state": "completed", "request_hash": "1" * 64}]}
        with mock.patch("desktop_live_acceptance.read_journal", return_value=journal) as read_journal:
            live["cases"] = [live_row(observation, {**item, "turn_id": "flow", "conversation": "flow"},
                                       live["runtime_provenance"], live["cases_sha256"], "test-model")]
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
