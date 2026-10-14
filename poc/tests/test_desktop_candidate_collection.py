from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from collect_desktop_candidate import collect
from desktop_acceptance import inspect_package, sample_failures
import test_release_acceptance as gate_fixture


class CandidateCollectionTests(unittest.TestCase):
    def setUp(self):
        fixture = gate_fixture.ReleaseAcceptanceTests(methodName="test_joint_contract_accepts_exact_native_artifact_and_reviews")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.paths = fixture.paths
        from release_acceptance import file_sha256
        self.paths["package"].with_suffix(".zip.sha256").write_text(
            f"{file_sha256(self.paths['package'])}  {self.paths['package'].name}\n", encoding="ascii")

    def collect(self, **options):
        return collect(package=self.paths["package"], report=self.paths["compiled_report"],
            manifest=self.paths["manifest"], policy=self.paths["policy"],
            destination=self.fixture.root / "delivery", **options)

    def test_only_white_listed_candidate_files_are_copied(self):
        (self.fixture.root / ".env").write_text("do-not-upload", encoding="utf-8")
        result = self.collect()
        self.assertTrue(result["candidate_allowed"])
        self.assertFalse(result["publish_allowed"])
        expected = {"desktop.zip", "desktop.zip.sha256", "BUILD-INFO.json", "COMPILED-ACCEPTANCE.json",
                    "ACCEPTANCE-MANIFEST.json", "CANDIDATE-POLICY.json", "CANDIDATE-GATE.json"}
        self.assertEqual({path.name for path in (self.fixture.root / "delivery").iterdir()}, expected)
        self.assertFalse((self.fixture.root / "delivery/.env").exists())

    def test_failed_or_incomplete_acceptance_never_collects_package(self):
        report = self.fixture.read("compiled_report")
        for change in ({"selection_complete": False}, {"status": "failed"}, {"errors": ["interrupted"]}):
            with self.subTest(change=change):
                self.fixture.write("compiled_report", {**report, **change})
                with self.assertRaisesRegex(ValueError, "ACCEPTANCE_FAILED"):
                    self.collect()
                self.assertFalse((self.fixture.root / "delivery").exists())

    def test_final_delivery_inspection_rejects_other_commit(self):
        identity = inspect_package(self.paths["package"], self.fixture.commit)
        self.assertEqual(identity["binary_sha256"], self.fixture.binary)
        with self.assertRaisesRegex(ValueError, "IDENTITY_MISMATCH"):
            inspect_package(self.paths["package"], "f" * 40)

    def test_sample_missing_speed_quality_or_completeness_is_not_green(self):
        manifest = self.fixture.read("manifest")
        rows = self.fixture.read("compiled_report")["cases"]
        self.assertEqual(sample_failures(manifest, rows), [])
        for invalid in (rows[:-1], [{**rows[0], "timings": {}}, rows[1]],
                        [{**rows[0], "checks": {"quality_formula": True}}, rows[1]]):
            self.assertTrue(sample_failures(manifest, invalid))

    def test_changed_bytes_during_copy_cannot_pass_collection(self):
        import shutil
        original = shutil.copyfile

        def replaced(source, target):
            result = original(source, target)
            if Path(target).name == "COMPILED-ACCEPTANCE.json":
                data = json.loads(Path(target).read_text(encoding="utf-8"))
                data["cases"][0]["checks"]["quality_formula"] = False
                Path(target).write_text(json.dumps(data), encoding="utf-8")
            return result

        with patch("collect_desktop_candidate.shutil.copyfile", side_effect=replaced), \
             self.assertRaisesRegex(ValueError, "CHANGED_DURING_COLLECTION"):
            self.collect()

if __name__ == "__main__":
    unittest.main()
