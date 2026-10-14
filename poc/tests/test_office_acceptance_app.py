from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import office_acceptance_app as office
import build_office_acceptance as builder


class OfficeAssistantTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.kit_root = self.root / "kit"
        self.kit_root.mkdir()
        self.document = {"schema_version": "office-acceptance-kit/v1", "source_commit": "a" * 40,
            "archive": "candidate/delivery.zip", "cases": "cases/LIVE-CASES.json", "policy": "cases/LIVE-POLICY.json",
            "compiled_report": "candidate/COMPILED-ACCEPTANCE.json", "manifest": "candidate/ACCEPTANCE-MANIFEST.json",
            "source_root": "sources", "helper_entry": "helper/OfficeAcceptance/OfficeAcceptance.exe", "files": {}}
        data = {"candidate/delivery.zip": b"fixture archive", "sources/entry.cbl": b"COMPUTE RESULT = INPUT * 2.\n",
                "helper/OfficeAcceptance/OfficeAcceptance.exe": b"MZ helper fixture",
                "candidate/COMPILED-ACCEPTANCE.json": b"{}", "candidate/ACCEPTANCE-MANIFEST.json": b"{}"}
        sha = hashlib.sha256(data["sources/entry.cbl"]).hexdigest()
        item = {"id": "formula", "question": "How is RESULT computed?", "split": "holdout",
            "coverage": list(office.OFFICE_REGRESSION_BASELINE),
            "gold": {"required_findings": [{"id": "formula", "supporting_ranges": [
                {"path": "entry.cbl", "source_sha256": sha, "start_line": 1, "end_line": 1}]}],
                "forbidden_claims": [{"id": "invented"}]}, "reviewed_context": {"source_ranges": [
                {"path": "entry.cbl", "sha256": sha, "start_line": 1, "end_line": 1}]}}
        multi = {key: value for key, value in item.items() if key != "question"}
        multi.update(id="multi", split="regression", turns=[{"id": str(index), "question": f"Question {index}?"} for index in range(1, 4)])
        data["cases/LIVE-CASES.json"] = json.dumps({"cases": [multi, item]}).encode()
        policy = {"source_commit": "a" * 40, "manifest_sha256": hashlib.sha256(data["candidate/ACCEPTANCE-MANIFEST.json"]).hexdigest(),
            "live_cases_sha256": hashlib.sha256(data["cases/LIVE-CASES.json"]).hexdigest(),
            "live_execution": {"max_model_requests": 3, "max_question_seconds": 120}}
        data["cases/LIVE-POLICY.json"] = json.dumps(policy).encode()
        for name, content in data.items():
            path = self.kit_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            self.document["files"][name] = hashlib.sha256(content).hexdigest()
        self.path = self.kit_root / "OFFICE-KIT.json"
        self.write_manifest()
        self.artifact = {"archive_sha256": "b" * 64, "binary_sha256": "c" * 64,
                         "source_commit": "a" * 40, "runtime_platform": "Windows"}
        package = patch.object(office, "inspect_package", return_value=self.artifact)
        package.start()
        self.addCleanup(package.stop)

    def write_manifest(self):
        self.path.write_text(json.dumps(self.document), encoding="utf-8")

    def test_all_inputs_source_and_budget_are_frozen(self):
        kit = office.load_kit(self.path)
        self.assertEqual(kit["turns"], 4)
        self.assertEqual(kit["request_budget"], 12)
        self.assertEqual(kit["source"], (self.kit_root / "sources").resolve())
        self.assertEqual(kit["artifact"], self.artifact)

    def test_changed_file_or_missing_source_binding_rejected(self):
        (self.kit_root / "sources/entry.cbl").write_bytes(b"COMPUTE RESULT = INPUT * 999.")
        with self.assertRaisesRegex(ValueError, "FILE_CHANGED"):
            office.load_kit(self.path)
        self.document["files"].pop("sources/entry.cbl")
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "SOURCE_NOT_FROZEN"):
            office.load_kit(self.path)

    def test_new_unlisted_source_file_rejected(self):
        (self.kit_root / "sources/extra.cbl").write_text("DISPLAY 'extra'.", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SOURCE_NOT_FROZEN"):
            office.load_kit(self.path)

    def test_unlisted_helper_dependency_cannot_be_loaded(self):
        (self.kit_root / "helper/OfficeAcceptance/extra.dll").write_bytes(b"extra dependency")
        with self.assertRaisesRegex(ValueError, "HELPER_NOT_FROZEN"):
            office.load_kit(self.path)

    def test_scope_regression_coverage_and_gold_are_checked_before_runner(self):
        for invalid, reason in (("scope", "SCOPE"), ("coverage", "REGRESSIONS"), ("gold", "SOURCE")):
            with self.subTest(invalid=invalid):
                path = self.kit_root / self.document["cases"]
                original = path.read_bytes()
                items = json.loads(original)["cases"]
                if invalid == "scope":
                    items = [items[1]]
                elif invalid == "coverage":
                    for item in items:
                        item["coverage"] = []
                else:
                    items[0]["reviewed_context"]["source_ranges"][0]["sha256"] = "0" * 64
                path.write_text(json.dumps({"cases": items}), encoding="utf-8")
                self.document["files"][self.document["cases"]] = office.digest(path)
                policy_path = self.kit_root / self.document["policy"]
                policy = office.read_object(policy_path)
                policy["live_cases_sha256"] = office.digest(path)
                policy_path.write_text(json.dumps(policy), encoding="utf-8")
                self.document["files"][self.document["policy"]] = office.digest(policy_path)
                self.write_manifest()
                with patch.object(office, "evaluate_native") as runner, self.assertRaisesRegex(ValueError, reason):
                    office.run_kit(kit_path=self.path, output=self.root / "invalid", allow_network=True,
                                   request_budget=12, profile_config=self.root / "profile.env")
                runner.assert_not_called()
                path.write_bytes(original)
                self.document["files"][self.document["cases"]] = office.digest(path)
                policy["live_cases_sha256"] = office.digest(path)
                policy_path.write_text(json.dumps(policy), encoding="utf-8")
                self.document["files"][self.document["policy"]] = office.digest(policy_path)
                self.write_manifest()

    def test_paths_cannot_escape_bundle(self):
        for value in ("../outside", "/absolute", "C:/drive", "candidate\\delivery.zip"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "PATH_INVALID"):
                office.kit_file(self.kit_root, value)

    def test_directory_symlink_cannot_escape_bundle(self):
        target = self.root / "outside"
        target.mkdir()
        link = self.kit_root / "outside-link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks requires local privilege on this platform")
        with self.assertRaisesRegex(ValueError, "ESCAPES_ROOT"):
            office.kit_file(self.kit_root, "outside-link")

    def test_windows_case_collisions_rejected(self):
        self.document["files"]["SOURCES/ENTRY.CBL"] = "b" * 64
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "DUPLICATE"):
            office.load_kit(self.path)

    def test_required_input_cannot_be_unlisted(self):
        self.document["files"].pop(self.document["compiled_report"])
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "INPUT_NOT_FROZEN"):
            office.load_kit(self.path)

    def test_policy_cases_binding_cannot_be_replaced_even_when_file_hash_updated(self):
        path = self.kit_root / self.document["cases"]
        path.write_text(json.dumps({"cases": [{"id": "other", "question": "Other?"}]}), encoding="utf-8")
        self.document["files"][self.document["cases"]] = office.digest(path)
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "POLICY_BINDING_INVALID"):
            office.load_kit(self.path)

    def test_plan_uses_final_zip_and_never_reads_profile_or_starts_workbench(self):
        import desktop_live_acceptance as native
        with patch.object(native, "inspect_package", return_value=self.artifact), \
             patch.object(native, "NativeWorkbench") as workbench, \
             patch.object(native, "_read_local_env") as profile:
            result = office.run_kit(kit_path=self.path, output=self.root / "plan", profile_config=self.root / "absent.env")
        workbench.assert_not_called()
        profile.assert_not_called()
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["runtime_provenance"]["binary_sha256"], "c" * 64)
        self.assertEqual(result["plan"]["max_model_requests"], 12)
        self.assertFalse((self.root / "plan/private-profile").exists())
        receipt = office.read_object(self.root / "plan/KIT-RUN.json")
        self.assertEqual(receipt["declared_request_budget"], 0)
        self.assertFalse(receipt["publication_performed"])

    def test_live_requires_explicit_matching_budget_and_profile_before_runner(self):
        for options, reason in (({}, "BUDGET"), ({"request_budget": 11}, "BUDGET"), ({"request_budget": 12}, "PROFILE")):
            with self.subTest(options=options), patch.object(office, "evaluate_native") as runner:
                with self.assertRaisesRegex(ValueError, reason):
                    office.run_kit(kit_path=self.path, output=self.root / "live", allow_network=True, **options)
                runner.assert_not_called()

    def test_output_inside_bundle_or_existing_cannot_overwrite_evidence(self):
        for output in (self.kit_root / "result", self.root, self.kit_root):
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, "OUTPUT"):
                office.run_kit(kit_path=self.path, output=output)

    def test_exception_records_uncertainty_without_retry(self):
        target = self.root / "interrupted"
        def failed(**options):
            target.mkdir()
            raise RuntimeError("secret value that must stay private")
        with patch.object(office, "evaluate_native", side_effect=failed) as runner:
            with self.assertRaises(RuntimeError):
                office.run_kit(kit_path=self.path, output=target, allow_network=True,
                               request_budget=12, profile_config=self.root / "local.env")
        self.assertEqual(runner.call_count, 1)
        receipt = office.read_object(target / "ATTENTION-REQUIRED.json")
        self.assertTrue(receipt["remote_outcome_may_be_unknown"])
        self.assertFalse(receipt["automatic_retry_allowed"])
        self.assertNotIn("secret value", json.dumps(receipt))

    def test_changed_kit_during_evaluation_rejected(self):
        def changed(**options):
            options["output"].mkdir()
            self.document["extra"] = "changed"
            self.write_manifest()
            return {"evaluation_run_id": "run", "status": "planned"}
        with patch.object(office, "evaluate_native", side_effect=changed), self.assertRaisesRegex(ValueError, "CHANGED_DURING"):
            office.run_kit(kit_path=self.path, output=self.root / "changed")

    def test_cli_live_cannot_infer_authorization_from_existing_profile(self):
        with patch.object(office, "run_kit") as runner:
            self.assertEqual(office.main(["--run", "--kit", str(self.path), "--output", str(self.root / "live"),
                                         "--profile-config", str(self.root / "local.env"), "--request-budget", "12"]), 2)
            runner.assert_not_called()

    def test_error_display_never_prints_arbitrary_remote_or_credential_text(self):
        self.assertEqual(office.safe_error(RuntimeError("authorization=private-value")), "RuntimeError")
        self.assertEqual(office.safe_error(ValueError("OFFICE_PROFILE_INVALID")), "OFFICE_PROFILE_INVALID")

    def template(self):
        return {"schema_version": "business-human-review/v2", "reviews": [{"case_id": "formula", "turn_id": "1",
            "identity": {"answer_sha256": "f" * 64}, "required_findings": [{"id": "formula", "result": None}],
            "forbidden_claims": [{"id": "invented", "present": None}],
            "partial_review": {"classification": None, "reason": "", "affected_findings": [], "resolved": None}}],
            "office_acceptance": {"archive_sha256": "b" * 64, "live_report_sha256": "c" * 64,
                                  "checks": {key: None for key in office.OFFICE_BASELINE_CHECKS}}}

    def test_empty_human_scores_remain_empty_and_identity_unchanged(self):
        template = self.template()
        frozen = copy.deepcopy(template)
        saved = office.save_review(self.root / "reviews.json", template, {}, reviewer="local-reviewer", office_checks={})
        self.assertEqual(saved["reviews"][0]["identity"], template["reviews"][0]["identity"])
        self.assertIsNone(saved["reviews"][0]["required_findings"][0]["result"])
        self.assertIsNone(saved["reviews"][0]["forbidden_claims"][0]["present"])
        self.assertIsNone(saved["reviews"][0]["partial_review"]["resolved"])
        self.assertTrue(all(value is None for value in saved["office_acceptance"]["checks"].values()))
        self.assertEqual(template, frozen)

    def test_explicit_human_error_and_partial_omission_are_preserved(self):
        scores = {("formula", "1"): {"required_findings": {"formula": "omitted"},
            "forbidden_claims": {"invented": True}, "usefulness": "unusable", "followup_continuity": "broken",
            "partial_review": {"classification": "answer_omission", "reason": "The formula is missing.",
                               "affected_findings": ["formula"], "resolved": False}}}
        saved = office.save_review(self.root / "reviews.json", self.template(), scores, reviewer="local-reviewer",
                                   office_checks={"company_api_ca": False})
        review = saved["reviews"][0]
        self.assertEqual(review["required_findings"][0]["result"], "omitted")
        self.assertTrue(review["forbidden_claims"][0]["present"])
        self.assertFalse(review["partial_review"]["resolved"])
        self.assertFalse(saved["office_acceptance"]["checks"]["company_api_ca"])

    def test_review_cannot_use_bool_like_strings_or_unrecognized_scores(self):
        for score in ({"required_findings": {"formula": "looks-ok"}},
                      {"forbidden_claims": {"invented": "False"}},
                      {"partial_review": {"classification": "all-good"}},
                      {"partial_review": {"resolved": "yes"}}):
            with self.subTest(score=score), self.assertRaisesRegex(ValueError, "INVALID"):
                office.save_review(self.root / "reviews.json", self.template(), {("formula", "1"): score},
                                   reviewer="local-reviewer", office_checks={})

    def test_gate_receives_actual_office_report_and_never_publishes(self):
        evaluation = self.root / "evaluation"
        evaluation.mkdir()
        with patch.object(office, "evaluate_release", return_value={"publish_allowed": False}) as gate:
            result = office.check_review(kit_path=self.path, evaluation_dir=evaluation,
                reviews=self.root / "reviews.json", office_report=self.root / "office.json")
        self.assertEqual(gate.call_args.kwargs["office_report"], self.root / "office.json")
        self.assertFalse(result["publication_performed"])
        self.assertTrue((evaluation / "LOCAL-RELEASE-GATE.json").is_file())

    def test_office_review_hash_binds_explicit_local_full_regression(self):
        report = self.root / "COMPILED-ACCEPTANCE.json"
        report.write_text("local native report", encoding="utf-8")
        saved = office.save_review(self.root / "reviews.json", self.template(), {}, reviewer="local-reviewer",
                                   office_checks={}, office_report=report)
        self.assertEqual(saved["office_acceptance"]["office_report_sha256"], office.digest(report))

    def test_resume_reuses_scores_only_for_same_answer_identity(self):
        template = self.template()
        path = self.root / "reviews.json"
        office.save_review(path, template, {}, reviewer="local-reviewer", office_checks={})
        self.assertEqual(office.restore_review(template, path)["reviews"][0]["reviewer"], "local-reviewer")
        changed = copy.deepcopy(template)
        changed["reviews"][0]["identity"]["answer_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "RESUME_STALE"):
            office.restore_review(changed, path)

    def test_resume_rejects_duplicate_review_or_different_report(self):
        template = self.template()
        path = self.root / "reviews.json"
        saved = office.save_review(path, template, {}, reviewer="local-reviewer", office_checks={})
        saved["office_acceptance"]["live_report_sha256"] = "0" * 64
        path.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "RESUME_STALE"):
            office.restore_review(template, path)

    def test_open_measured_gui_reuses_exact_profile_and_explicit_ca_without_ping(self):
        evaluation = self.root / "live"
        profile = evaluation / "private-profile"
        profile.mkdir(parents=True)
        (profile / ".env").write_text("private-value", encoding="utf-8")
        (profile / "agent-settings.json").write_text("{}", encoding="utf-8")
        binary = evaluation / "extracted" / office.BINARY_MEMBER
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"MZ measured fixture")
        artifact = {**self.artifact, "binary_sha256": office.digest(binary)}
        (evaluation / "LIVE-ACCEPTANCE.json").write_text(json.dumps({"runtime_provenance": artifact}), encoding="utf-8")
        ca = self.root / "company-ca.crt"
        ca.write_text("public-certificate-fixture", encoding="utf-8")
        with patch.object(office.sys, "platform", "win32"), patch.object(office, "inspect_package", return_value=artifact), \
             patch.object(office.subprocess, "Popen") as process:
            office.open_workbench(kit_path=self.path, evaluation_dir=evaluation, ca_file=ca)
        command = process.call_args.args[0]
        self.assertEqual(command, [str(binary.resolve()), "--data-dir", str(profile.resolve())])
        self.assertEqual(process.call_args.kwargs["env"]["SSL_CERT_FILE"], str(ca.resolve()))
        self.assertNotIn("private-value", str(process.call_args))
        self.assertNotIn("--acceptance-server-info", command)

    def test_gui_cannot_start_a_changed_or_unmeasured_binary(self):
        evaluation = self.root / "live"
        evaluation.mkdir()
        (evaluation / "LIVE-ACCEPTANCE.json").write_text(json.dumps({"runtime_provenance": {**self.artifact,
            "binary_sha256": "0" * 64}}), encoding="utf-8")
        with patch.object(office.sys, "platform", "win32"), patch.object(office.subprocess, "Popen") as process, \
             self.assertRaisesRegex(ValueError, "ARTIFACT_MISMATCH"):
            office.open_workbench(kit_path=self.path, evaluation_dir=evaluation)
        process.assert_not_called()


class OfficeHelperBuildTests(unittest.TestCase):
    def test_compiler_bundles_tk_and_uses_separate_console_executable(self):
        command = builder.compiler_command(Path("C:/test with spaces/stage"), Path("C:/test with spaces/output"), executable="python.exe")
        self.assertIn("--enable-plugin=tk-inter", command)
        self.assertIn("--mode=standalone", command)
        self.assertIn("--output-filename=OfficeAcceptance.exe", command)
        self.assertIn("--windows-console-mode=force", command)
        self.assertEqual(Path(command[-1]).parts[-2:], ("tools", "office_acceptance_app.py"))
        self.assertFalse(any("desktop_app.py" in value for value in command))

    def test_stage_has_only_selected_public_python_inputs_and_no_local_env(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary) / "stage"
            fingerprint = builder.stage_sources(stage)
            self.assertEqual(len(fingerprint), 64)
            self.assertTrue((stage / "tools/office_acceptance_checks.py").is_file())
            self.assertTrue((stage / "tools/acceptance_diagnostics.py").is_file())
            self.assertTrue((stage / "poc/release_acceptance.py").is_file())
            self.assertFalse(any(path.name == ".env" or path.suffix == ".key" for path in stage.rglob("*")))
            self.assertEqual(fingerprint, builder.stage_sources(stage))

    def test_native_builder_does_not_cross_compile_or_rebuild_application(self):
        with patch.object(builder.sys, "platform", "darwin"), self.assertRaisesRegex(ValueError, "WINDOWS_X64"):
            builder.build(output=Path("unused"))


class OfficeEvidenceDisplayTests(unittest.TestCase):
    def row(self):
        excerpt = "COMPUTE RESULT = INPUT * 2."
        framework = "A successful call returns the modified output record."
        context = {"sources": [{"evidence_id": "ev-actual", "relative_path": "entry.cbl",
            "source_sha256": "a" * 64, "start_line": 21, "end_line": 21,
            "excerpt": excerpt, "excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
            "profile_config": "credential-must-not-be-displayed"}],
            "framework": [{"reference_id": "fw-actual", "document_name": "effects.txt",
                "document_sha256": "b" * 64, "range": {"start_line": 7, "end_line": 7},
                "excerpt": framework, "excerpt_sha256": hashlib.sha256(framework.encode()).hexdigest()}]}
        return {"actual_evidence_context": context, "supplied_evidence": office._evidence_metadata(context),
                "private_profile": "credential-must-not-be-displayed"}

    def test_displays_archived_source_and_framework_with_identity_without_config_fields(self):
        row = self.row()
        frozen = copy.deepcopy(row)
        with patch.object(office, "_source_excerpt") as read_current, \
             patch.object(office, "evaluate_native") as runner:
            shown = office.review_actual_evidence_text(row)
        self.assertIn("实际源码证据 ev-actual", shown)
        self.assertIn("entry.cbl:21-21", shown)
        self.assertIn("源文件 SHA256：" + "a" * 64, shown)
        self.assertIn("COMPUTE RESULT = INPUT * 2.", shown)
        self.assertIn("实际框架证据 fw-actual", shown)
        self.assertIn("文档 SHA256：" + "b" * 64, shown)
        self.assertIn("modified output record", shown)
        self.assertNotIn("credential-must-not-be-displayed", shown)
        self.assertEqual(row, frozen)
        read_current.assert_not_called()
        runner.assert_not_called()

    def test_missing_or_changed_supply_cannot_be_displayed_as_gold_source(self):
        for mutation, code in (("missing", "EXCERPTS_MISSING"), ("identity", "IDENTITY_MISMATCH"),
                               ("excerpt", "EXCERPT_CHANGED")):
            with self.subTest(mutation=mutation):
                row = self.row()
                if mutation == "missing":
                    row.pop("actual_evidence_context")
                elif mutation == "identity":
                    row["actual_evidence_context"]["sources"][0]["source_sha256"] = "c" * 64
                else:
                    row["actual_evidence_context"]["sources"][0]["excerpt"] = "COMPUTE RESULT = INPUT * 999."
                with self.assertRaisesRegex(ValueError, code):
                    office.review_actual_evidence_text(row)

    def test_review_details_keep_actual_archived_supply_distinct_from_gold(self):
        class Widget:
            def __init__(self, *args, **kwargs):
                self.text = ""
            def configure(self, **kwargs):
                pass
            def grid(self, **kwargs):
                pass
            def delete(self, *args):
                self.text = ""
            def insert(self, _position, text):
                self.text += text
            def winfo_children(self):
                return []
        fake_ttk = types.SimpleNamespace(Label=Widget, Combobox=Widget, Entry=Widget)
        fake_tk = types.SimpleNamespace(ttk=fake_ttk)
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary).resolve()
            content = "MOVE 0 TO RESULT.\n"
            (source / "gold.cbl").write_text(content, encoding="utf-8")
            span = {"path": "gold.cbl", "sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "start_line": 1, "end_line": 1}
            key, row = ("formula", "turn"), self.row()
            row.update(question="How is RESULT computed?", answer="The archived rule multiplies by 2.")
            window = office.ReviewWindow.__new__(office.ReviewWindow)
            window.owner = types.SimpleNamespace(tk=types.SimpleNamespace(
                StringVar=lambda **kwargs: types.SimpleNamespace(get=lambda: kwargs.get("value", ""))))
            window.source, window.current, window.keys = source, None, [key]
            window.selector, window.details, window.form = types.SimpleNamespace(current=lambda: 0), Widget(), Widget()
            window.rows, window.scores = {key: row}, {}
            window.actual_evidence_text = {key: office.review_actual_evidence_text(row)}
            window.items = {key: {"gold": {}, "reviewed_context": {"source_ranges": [span]}}}
            window.template = {"reviews": [{"case_id": key[0], "turn_id": key[1]}]}
            with patch.dict(sys.modules, {"tkinter": fake_tk}):
                window.select()
            self.assertIn("实际源码证据 ev-actual", window.details.text)
            self.assertIn("COMPUTE RESULT = INPUT * 2.", window.details.text)
            self.assertIn("冻结金标准源码 gold.cbl:1-1", window.details.text)
            self.assertIn("MOVE 0 TO RESULT.", window.details.text)
            self.assertLess(window.details.text.index("实际源码证据"),
                            window.details.text.index("冻结金标准源码"))


if __name__ == "__main__":
    unittest.main()
