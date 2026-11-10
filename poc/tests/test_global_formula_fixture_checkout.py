"""Frozen business originals must survive Windows Git checkout filters."""
import hashlib
import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path("poc/tests/fixtures/global-premium-inventory-v1")


class GlobalFormulaFixtureCheckoutTests(unittest.TestCase):
    def test_windows_autocrlf_filter_preserves_every_frozen_original_hash_and_range(self):
        gold = json.loads((ROOT / FIXTURE / "gold.json").read_text(encoding="utf-8"))
        self.assertTrue(gold["files"])
        self.assertIn("RATEPF.pf", {entry["path"] for entry in gold["files"]})
        for entry in gold["files"]:
            relative = (FIXTURE / "source" / entry["path"]).as_posix()
            with self.subTest(path=relative):
                # Execute Git's real working-copy conversion, even on a Unix
                # test host. A checkout-only line-ending change must fail the
                # independently authored SHA and original-range contract.
                filtered = subprocess.check_output(
                    ["git", "-c", "core.autocrlf=true", "cat-file", "--filters", "HEAD:" + relative],
                    cwd=ROOT)
                self.assertEqual(hashlib.sha256(filtered).hexdigest(), entry["sha256"])
                self.assertEqual(len(filtered.decode("utf-8").splitlines()), entry["lines"])
                self.assertNotIn(b"\r", filtered)


if __name__ == "__main__":
    unittest.main()
