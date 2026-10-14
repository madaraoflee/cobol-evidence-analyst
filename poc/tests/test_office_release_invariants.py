"""Exact office data paths and actual diagnostics must survive helper assembly."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "poc"))

import office_acceptance_checks as checks
import office_acceptance_app as office
import collect_office_acceptance_kit as collector
from tools import desktop_acceptance_cases as cases
from live_business_eval import _fingerprint
from poc.tests import test_release_acceptance as release_fixture


class OfficeRunnerNamespaceTests(unittest.TestCase):
    def test_office_runner_uses_actual_kit_contracts_and_frozen_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            kit_root = root / "kit"
            source = kit_root / "sources"
            frozen = source / "synthetic"
            contracts = kit_root / "contracts"
            for path in (frozen, contracts, kit_root / "fixtures"):
                path.mkdir(parents=True, exist_ok=True)
            neutral = b"COMPUTE RESULT = INPUT * 2.\n"
            (frozen / "frontend.cbl").write_bytes(neutral)
            (source / "public.cbl").write_bytes(neutral)
            version = hashlib.sha256(neutral).hexdigest()
            public_manifest = contracts / "public-manifest.json"
            public_manifest.write_text(json.dumps({"files": [{"path": "public.cbl", "sha256": version}]}))
            public_cases = contracts / "public-cases.json"
            public_cases.write_text("{}")
            manifest = {"schema_version": "desktop-joint-acceptance/v1",
                "source_files": {"frontend.cbl": {"path": "frontend.cbl", "sha256": version}},
                "public_corpus": {"manifest_sha256": cases.digest(public_manifest),
                                  "cases_sha256": cases.digest(public_cases)},
                "cases": [{"id": "frozen", "owner": "cases", "corpus": "frozen", "files": ["frontend.cbl"]},
                          {"id": "public", "owner": "cases", "corpus": "public"}]}
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest))
            archive, binary = root / "archive.zip", root / "binary.exe"
            archive.write_bytes(b"isolated runner contract fixture")
            binary.write_bytes(b"isolated runner binary fixture")
            artifact = {"archive_sha256": cases.digest(archive), "binary_sha256": cases.digest(binary),
                "application_sha256": "b" * 64, "source_commit": "a" * 40, "source_dirty": False,
                "binary_path": "COBOLWorkbench/COBOLWorkbench/COBOLWorkbench.exe",
                "execution_surface": "native_windows_http", "runtime_platform": "Windows"}
            kit = {"inputs": {"archive": archive, "manifest": manifest_path}, "root": kit_root,
                   "document": {"source_commit": "a" * 40}, "source": source}
            globals_ = checks.run_native_suite.__globals__
            measured = []
            def collect(command, destination, specs, public_source):
                self.assertEqual(cases.PUBLIC_MANIFEST, public_manifest)
                self.assertEqual(cases.PUBLIC_CASES, public_cases)
                from tools import desktop_acceptance_runtime as runtime
                self.assertEqual(runtime.FIXTURES, kit_root / "fixtures")
                for case in specs["cases"]:
                    target = destination / case["id"]
                    result = cases.prepare_source(case, specs, target, public_source)
                    self.assertEqual(result["files"], 1)
                    name = "frontend.cbl" if case["corpus"] == "frozen" else "public.cbl"
                    self.assertEqual((target / name).read_bytes(), neutral)
                    measured.append(case["id"])
                return []
            # Restore mutable module data globals so later tests cannot reuse
            # a temporary kit path. This test makes no socket or model calls.
            from tools import desktop_acceptance_runtime as runtime
            with mock.patch.object(office, "load_kit", return_value=kit), \
                 mock.patch.object(checks.sys, "platform", "win32"), \
                 mock.patch.dict(globals_, {"inspect_package": mock.Mock(return_value=artifact),
                     "extract_package": mock.Mock(return_value=binary), "sample_failures": mock.Mock(return_value=[])}), \
                 mock.patch.object(cases, "run_cases", side_effect=collect), \
                 mock.patch.object(cases, "PUBLIC_MANIFEST", cases.PUBLIC_MANIFEST), \
                 mock.patch.object(cases, "PUBLIC_CASES", cases.PUBLIC_CASES), \
                 mock.patch.object(cases, "FROZEN_SOURCE_ROOT", getattr(cases, "FROZEN_SOURCE_ROOT", None), create=True), \
                 mock.patch.object(runtime, "FIXTURES", runtime.FIXTURES):
                result = checks.run_office_native(kit_root, root / "measured")
            self.assertEqual(result["status"], "passed")
            self.assertEqual(measured, ["frozen", "public"])
            self.assertEqual(result["office_source"], "actual_target_computer_full_suite")


class OfficeGateIndependentEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = release_fixture.ReleaseAcceptanceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def rebind_live(self, document, row_index=0):
        row = document["cases"][row_index]
        diagnostics = row["acceptance_diagnostics"]
        diagnostics["final_sha256"] = _fingerprint(diagnostics["final"])
        row["review_identity"]["acceptance_diagnostics_sha256"] = _fingerprint(diagnostics)
        self.fixture.refresh_live_review(document, row_index)

    def test_office_cannot_reuse_ci_case_runs_with_new_summary_id_and_fresh_human_hash(self):
        office_report = copy.deepcopy(self.fixture.read("compiled_report"))
        office_report.update(report_execution_id="new-office-summary",
            office_environment=self.fixture.read("live_report")["office_environment"],
            office_source="actual_target_computer_full_suite")
        self.fixture.write("office_report", office_report)
        reviews = self.fixture.read("reviews")
        reviews["office_acceptance"]["office_report_sha256"] = hashlib.sha256(
            self.fixture.paths["office_report"].read_bytes()).hexdigest()
        self.fixture.write("reviews", reviews)
        result = self.fixture.run_gate()
        self.assertFalse(result["publish_allowed"])
        self.assertTrue(any("REUSED" in row["code"] for row in result["publish_errors"]))

    def test_live_status_missing_does_not_bypass_partial_explanation(self):
        live = self.fixture.read("live_report")
        row = live["cases"][0]
        row["agent_status"] = None
        row["acceptance_diagnostics"]["agent_status"] = None
        self.rebind_live(live)
        result = self.fixture.run_gate()
        self.assertFalse(result["publish_allowed"])
        self.assertTrue(any("DIAGNOSTICS" in part["code"] for part in result["publish_errors"]))

    def test_trace_final_cannot_contradict_recorded_stop_after_fresh_review(self):
        live = self.fixture.read("live_report")
        row = live["cases"][0]
        row.update(agent_status="ANALYZED", stop_reason="sufficient_material")
        diagnostics = row["acceptance_diagnostics"]
        diagnostics.update(agent_status="ANALYZED", stop_reason="answer_incomplete")
        diagnostics["final"] = {"stop_reason": "answer_incomplete",
            "answer_completion": {"status": "incomplete", "missing_aspects": ["conditions"]},
            "question_investigation": {"open_gaps": []}}
        self.rebind_live(live)
        result = self.fixture.run_gate()
        self.assertFalse(result["publish_allowed"])
        self.assertTrue(any("DIAGNOSTICS" in part["code"] for part in result["publish_errors"]))

    def test_empty_trace_final_does_not_count_as_completed_diagnostics(self):
        live = self.fixture.read("live_report")
        row = live["cases"][0]
        row.update(agent_status="ANALYZED", stop_reason="sufficient_material")
        row["acceptance_diagnostics"].update(agent_status="ANALYZED", stop_reason="sufficient_material", final={})
        self.rebind_live(live)
        result = self.fixture.run_gate()
        self.assertFalse(result["publish_allowed"])
        self.assertTrue(any("DIAGNOSTICS" in part["code"] for part in result["publish_errors"]))

    def test_empty_completion_and_investigation_objects_cannot_restore_missing_diagnostics(self):
        live = self.fixture.read("live_report")
        row = live["cases"][0]
        row.update(agent_status="ANALYZED", stop_reason="sufficient_material")
        row["acceptance_diagnostics"].update(agent_status="ANALYZED", stop_reason="sufficient_material",
            final={"question_investigation": {}, "answer_completion": {}, "stop_reason": "sufficient_material"})
        self.rebind_live(live)
        result = self.fixture.run_gate()
        self.assertFalse(result["publish_allowed"])
        self.assertTrue(any("DIAGNOSTICS" in part["code"] for part in result["publish_errors"]))

    def test_office_answer_body_must_match_its_native_diagnostic_hash(self):
        report = self.fixture.read("office_report")
        report["cases"][0]["answer"] = "Altered office answer with a different formula."
        self.fixture.write("office_report", report)
        reviews = self.fixture.read("reviews")
        reviews["office_acceptance"]["office_report_sha256"] = hashlib.sha256(
            self.fixture.paths["office_report"].read_bytes()).hexdigest()
        self.fixture.write("reviews", reviews)
        result = self.fixture.run_gate()
        self.assertFalse(result["publish_allowed"])
        self.assertTrue(any("DIAGNOSTICS" in part["code"] for part in result["publish_errors"]))

    def test_more_requests_than_per_turn_policy_is_not_accepted_as_cost_verified(self):
        live = self.fixture.read("live_report")
        live["cases"][0]["model_requests"] = live["cases"][0]["policy"]["max_model_requests"] + 1
        self.rebind_live(live)
        result = self.fixture.run_gate()
        self.assertFalse(result["publish_allowed"])
        self.assertTrue(any("REQUEST" in part["code"] for part in result["publish_errors"]))


class OfficeHelperArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.candidate = self.root / "candidate"
        self.candidate.mkdir()
        for name in collector.CANDIDATE_FILES:
            (self.candidate / name).write_bytes(b"Neutral candidate contract fixture.\n")
        self.helper = self.root / "helper.zip"
        self.binary = bytearray(128)
        self.binary[:2] = b"MZ"
        struct.pack_into("<I", self.binary, 0x3C, 64)
        self.binary[64:68] = b"PE\0\0"
        struct.pack_into("<H", self.binary, 68, 0x8664)
        self.metadata = {"schema_version": "office-helper-build/v1", "source_commit": "f" * 40,
            "helper_source_sha256": "c" * 64, "target": "windows-x64", "source_dirty": False,
            "workbench_rebuilt": False, "self_test": {"ok": True, "gui_started": True,
                "native_platform": "Windows", "model_calls": 0}}
        self.artifact = {"archive_sha256": "b" * 64, "binary_sha256": "c" * 64, "source_commit": "a" * 40}

    def write_helper(self, extra=None):
        with zipfile.ZipFile(self.helper, "w") as archive:
            archive.writestr("OfficeAcceptance/OfficeAcceptance.exe", self.binary)
            archive.writestr("OfficeAcceptance/HELPER-BUILD-INFO.json", json.dumps(self.metadata))
            if extra:
                archive.writestr(extra[0], extra[1])

    def generate(self, destination, public_source, include_scale):
        destination.mkdir()
        source = destination / "source"
        source.mkdir()
        (source / "neutral.cbl").write_bytes(b"MOVE 1 TO RESULT.\n")
        for name in ("LIVE-CASES.json", "SOURCE-LOCK.json", "WINDOWS-MEASUREMENT-PLAN.json"):
            (destination / name).write_text("{}")
        (destination / "PUBLIC-SOURCE-LICENSE.txt").write_text("Neutral public license fixture.")
        return {"source": source, "source_files": 1}

    def assemble(self):
        with mock.patch.object(collector, "evaluate_release", return_value={"candidate_allowed": True}), \
             mock.patch.object(collector, "inspect_package", return_value=self.artifact), \
             mock.patch.object(collector, "generate_suite", side_effect=self.generate), \
             mock.patch.object(collector, "load_kit", return_value={"artifact": self.artifact,
                 "turns": 1, "request_budget": 3}):
            return collector.assemble(candidate=self.candidate, helper_archive=self.helper,
                public_source=self.root, destination=self.root / "delivery", source_commit="a" * 40,
                helper_commit="f" * 40)

    def test_mz_prefix_without_x64_pe_cannot_become_a_verified_helper(self):
        struct.pack_into("<H", self.binary, 68, 0xAA64)
        self.write_helper()
        with self.assertRaisesRegex(ValueError, "EXE_NOT_X64"):
            self.assemble()
        self.assertFalse((self.root / "delivery").exists())

    def test_unix_symlink_member_is_rejected_before_extraction(self):
        entry = zipfile.ZipInfo("OfficeAcceptance/extra.dll")
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.write_helper((entry, b"../../outside"))
        with self.assertRaisesRegex(ValueError, "ARCHIVE_UNSAFE"):
            self.assemble()

    def test_posix_absolute_archive_member_is_rejected_even_for_windows_packaging(self):
        self.write_helper(("/OfficeAcceptance/extra.dll", b"extra"))
        with self.assertRaisesRegex(ValueError, "ARCHIVE_UNSAFE"):
            self.assemble()

    def test_helper_commit_is_distinct_and_bound_to_the_expected_helper_build(self):
        self.metadata["source_commit"] = "a" * 40
        self.write_helper()
        with self.assertRaisesRegex(ValueError, "NOT_NATIVE_TESTED"):
            self.assemble()

    def test_clean_helper_has_frozen_manifest_and_never_rebuilds_workbench(self):
        self.write_helper()
        delivery = self.assemble()
        self.assertTrue(delivery.is_file())
        with zipfile.ZipFile(delivery) as archive:
            contract = json.loads(archive.read("OfficeAcceptanceKit/OFFICE-KIT.json"))
            self.assertEqual(contract["helper_source_commit"], "f" * 40)
            self.assertEqual(contract["source_commit"], "a" * 40)
            self.assertEqual(contract["artifact"], self.artifact)
            self.assertEqual(contract["model_calls_during_assembly"], 0)
            self.assertFalse(contract["publication_performed"])


if __name__ == "__main__":
    unittest.main()
