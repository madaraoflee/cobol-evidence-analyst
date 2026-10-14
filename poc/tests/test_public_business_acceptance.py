"""Optional external corpus acceptance; no network and no copied source fixtures."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from public_business_acceptance import DEFAULT_MANIFEST, run_acceptance


class PublicBusinessAcceptanceTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("PUBLIC_BUSINESS_SOURCE_ROOT"), "external fixed corpus is not configured")
    def test_default_pipeline_uses_real_versioned_source_and_repairs_wrong_claims(self):
        report = run_acceptance(os.environ["PUBLIC_BUSINESS_SOURCE_ROOT"])
        self.assertTrue(report["mechanisms_passed"])
        self.assertEqual(len(report["cases"]), 7)
        self.assertFalse(report["model_accuracy_measured"])
        self.assertTrue(all(row["model_requests"] == 1 for row in report["cases"]))
        incorrect = [row for row in report["cases"] if row["branch"] == "incorrect"]
        self.assertEqual(len(incorrect), 2)
        self.assertTrue(all(row["incorrect_claim_revised"] for row in incorrect))
        versions = {row["path"]: row["sha256"] for row in report["source_files"]}
        self.assertTrue(all(ref["source_sha256"] == versions[ref["relative_path"]]
                            for row in report["cases"] for ref in row["supplied_evidence"]))

    def test_wrong_source_hash_stops_before_indexing_or_model_construction(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
            for row in manifest["files"]:
                path = root / row["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("changed version", encoding="utf-8")
            with mock.patch("public_business_acceptance.build_business_index", side_effect=AssertionError("index")), \
                    mock.patch("public_business_acceptance.CompanyAPIConfig", side_effect=AssertionError("model")), \
                    self.assertRaisesRegex(ValueError, "PUBLIC_CORPUS_SOURCE_VERSION_MISMATCH"):
                run_acceptance(root)


if __name__ == "__main__":
    unittest.main()
