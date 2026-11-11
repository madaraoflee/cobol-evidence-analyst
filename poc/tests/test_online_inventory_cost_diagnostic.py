"""The performance observer must preserve data and report failures honestly."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "poc"))

from tools import diagnose_online_inventory_cost as diagnostic
import large_repository_online_driver as driver
from large_repository_fixture import create_large_fixture
import online_inventory_store as inventory
import online_investigation as investigation
from business_index import build_business_index
from online_material_export import export_material
from repository_discovery import ensure_repository_search


class OnlineInventoryCostDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def invoke(self, output):
        arguments = ["diagnose_online_inventory_cost.py", "--repo-root", str(ROOT),
                     "--output", str(output), "--files", "200"]
        with mock.patch.object(sys, "argv", arguments), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return diagnostic.main()

    @staticmethod
    def small_fixture(output, count):
        return create_large_fixture(output, 3)

    def test_all_full_durability_modes_preserve_complete_delivery_and_rollback(self):
        fixture = create_large_fixture(self.root / "fixture", 3)
        source, database = Path(fixture["source"]), self.root / "index.sqlite"
        built = build_business_index(source, database, source_format="free", verify_content=True,
                                     quiet=True, framework_reference_path="")
        ensure_repository_search(database, source)
        material = self.root / "material"
        export_material(database, source, material, repository_id="example/neutral-source",
                        commit="d" * 40, build_report=built)
        expected = None
        with redirect_stdout(io.StringIO()):
            for ordinal, mode in enumerate(("DELETE", "PERSIST", "TRUNCATE", "DELETE")):
                output = self.root / ("case-" + str(ordinal))
                output.mkdir()
                observer = diagnostic.Observer(output, "unit-production-pipeline", journal_mode=mode)
                with diagnostic.instrument(observer, driver, inventory, investigation):
                    value = driver.exercise_online_store(material, source, output / "inventory")
                    checked = diagnostic.verify_delivery(value, fixture["gold"], 3)
                self.assertTrue(checked["page_delivery"]["complete"])
                self.assertTrue(checked["export_delivery"]["complete"])
                self.assertTrue(checked["rollback_kept_publication"])
                self.assertEqual(3, checked["source_reviews"])
                self.assertGreater(observer.counts["commit_row_changes_completed"], 0)
                self.assertEqual(observer.counts["connect"], observer.counts["connections_verified_FULL"])
                self.assertTrue(observer.config)
                for configuration in observer.config.values():
                    self.assertEqual(2, configuration["synchronous"])
                    self.assertEqual(mode, configuration["journal_mode"].upper())
                if expected is None:
                    expected = checked
                else:
                    self.assertEqual(expected, checked)

    def test_incorrect_delivery_size_fails_without_overwriting_workflow_evidence(self):
        output = self.root / "output"
        output.mkdir()
        evidence = {"WORKFLOW-EXECUTION.json": b'{"status":"incomplete"}', "diagnostic.log": b"previous log\n"}
        for name, content in evidence.items():
            (output / name).write_bytes(content)
        with mock.patch("large_repository_fixture.create_large_fixture", side_effect=self.small_fixture):
            with self.assertRaises(AssertionError):
                self.invoke(output)
        report = json.loads((output / "DIAGNOSTIC-REPORT.json").read_text())
        self.assertEqual("failed", report["status"])
        self.assertEqual("failed", report["cases"][-1]["status"])
        self.assertFalse(report["source_regression_acceptance"])
        self.assertFalse(report["executable_acceptance"])
        for name, content in evidence.items():
            self.assertEqual(content, (output / name).read_bytes())
        observations = json.loads((output / "case-01-delete/observations.json").read_text())
        self.assertEqual("failed", observations["status"])

    def test_keyboard_interrupt_keeps_report_and_case_explicitly_interrupted(self):
        output = self.root / "interrupted"
        with mock.patch("large_repository_fixture.create_large_fixture", side_effect=self.small_fixture), \
             mock.patch.object(driver, "exercise_online_store", side_effect=KeyboardInterrupt):
            self.assertEqual(130, self.invoke(output))
        report = json.loads((output / "DIAGNOSTIC-REPORT.json").read_text())
        self.assertEqual("interrupted", report["status"])
        self.assertEqual("interrupted", report["cases"][-1]["status"])
        self.assertFalse(report["source_regression_acceptance"])
        self.assertFalse(report["executable_acceptance"])
        observations = json.loads((output / "case-01-delete/observations.json").read_text())
        self.assertEqual("interrupted", observations["status"])

    def test_existing_result_is_rejected_without_modifying_its_bytes(self):
        output = self.root / "existing"
        output.mkdir()
        path = output / "DIAGNOSTIC-REPORT.json"
        content = b'{"status":"incomplete","preserve":true}\n'
        path.write_bytes(content)
        with self.assertRaises(SystemExit) as error:
            self.invoke(output)
        self.assertEqual(2, error.exception.code)
        self.assertEqual(content, path.read_bytes())
        self.assertEqual([path], list(output.iterdir()))

    def test_final_source_observation_change_blocks_completed_diagnostic(self):
        original = diagnostic.binding(ROOT)
        changed = {**original, "poc/online_inventory_store.py": "0" * 64}
        for last_observation, expected_code, expected_status in (
            (original, 0, "completed_diagnostic"), (changed, 1, "failed")
        ):
            with self.subTest(expected_status=expected_status):
                output = self.root / expected_status
                arguments = diagnostic.argparse.Namespace(repo_root=ROOT, output=output, files=3)
                with mock.patch.object(diagnostic.argparse.ArgumentParser, "parse_args", return_value=arguments), \
                     mock.patch.object(diagnostic, "binding", side_effect=[original, original, last_observation]) as observed, \
                     redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(expected_code, diagnostic.main())
                report = json.loads((output / "DIAGNOSTIC-REPORT.json").read_text())
                self.assertEqual(3, observed.call_count)
                self.assertEqual(expected_status, report["status"])
                self.assertEqual(expected_code == 0, report["source_unchanged_in_declared_scope"])
                self.assertEqual(4, len(report["cases"]))
                self.assertTrue(all(case["status"] == "passed" for case in report["cases"]))
                if expected_code:
                    self.assertEqual("SourceBindingChanged", report["error_type"])

    def test_cleanup_error_fails_a_successful_body_and_keeps_an_original_failure(self):
        for primary_failure in (False, True):
            with self.subTest(primary_failure=primary_failure):
                output = self.root / ("primary" if primary_failure else "cleanup")
                output.mkdir()
                observer = diagnostic.Observer(output, "cleanup-boundary", journal_mode="DELETE")
                expected = ValueError if primary_failure else RuntimeError
                with mock.patch.object(inventory.OnlineInventoryStore, "close", create=True,
                                       side_effect=RuntimeError("injected cleanup failure")), \
                     redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    with self.assertRaises(expected) as caught:
                        with diagnostic.instrument(observer, driver, inventory, investigation):
                            inventory.OnlineInventoryStore(output / "inventory")
                            if primary_failure:
                                raise ValueError("preserve the original failure")
                self.assertIn("resource_cleanup", observer.tests)
                self.assertTrue(observer.tests["resource_cleanup"].startswith("failed:"))
                if primary_failure:
                    self.assertEqual("preserve the original failure", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
