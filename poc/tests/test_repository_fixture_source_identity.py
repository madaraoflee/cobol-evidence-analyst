"""A normalized source view must retain the original file-byte identity."""
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
from repository_inventory import inventory_batch, accept_inventory_response


class RepositoryFixtureSourceIdentityTests(unittest.TestCase):
    def make_fixture(self, newline):
        from test_repository_formula_inventory import RepositoryFormulaInventoryTests, program
        fixture = RepositoryFormulaInventoryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        original_write = Path.write_text
        def write_source(path, text, *args, **kwargs):
            if path.parent == fixture.source:
                kwargs["newline"] = newline
            return original_write(path, text, *args, **kwargs)
        with patch.object(Path, "write_text", write_source):
            fixture.prepare({"charge.cbl": program("CHARGE-ENTRY", "COMPUTE PREMIUM-AMT = BASIS * FACTOR.")})
        return fixture

    def test_lf_and_crlf_sources_use_their_actual_bytes_for_index_and_evidence_identity(self):
        hashes = []
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                fixture = self.make_fixture(newline)
                raw = (fixture.source / "charge.cbl").read_bytes()
                page = fixture.pages[0]
                digest = hashlib.sha256(raw).hexdigest()
                hashes.append(digest)
                self.assertEqual(b"\r\n" in raw, newline == "\r\n")
                self.assertEqual(page["source_sha256"], digest)
                self.assertNotIn("\r", page["source_text"])
                self.assertIn("COMPUTE PREMIUM-AMT", page["source_text"])
                items = inventory_batch(fixture.state)
                self.assertTrue(all(item["source_sha256"] == digest for item in items))
                response = json.dumps({"inventory_findings": fixture.findings(items, fixture.pages)})
                self.assertEqual(accept_inventory_response(fixture.state, response, fixture.pages,
                    requested_items=items), len(items))
                self.assertEqual(fixture.state["reviewed_count"], len(items))
        self.assertNotEqual(hashes[0], hashes[1], "Normalized views must not collapse different original bytes")

    def test_wrong_original_hash_is_still_rejected_for_both_newline_formats(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                fixture = self.make_fixture(newline)
                items = inventory_batch(fixture.state)
                response = json.dumps({"inventory_findings": fixture.findings(items, fixture.pages)})
                changed = [{**page, "source_sha256": hashlib.sha256(b"different original").hexdigest()}
                    for page in fixture.pages]
                self.assertEqual(accept_inventory_response(fixture.state, response, changed,
                    requested_items=items), 0)
                self.assertEqual(fixture.state["reviewed_count"], 0)
                self.assertEqual(fixture.state["findings"], {})
