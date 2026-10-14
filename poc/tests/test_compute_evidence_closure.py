"""A COMPUTE citation retains its own explicit closer without hiding its node."""

from contextlib import closing
from dataclasses import asdict
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claim_support import check_claim_support
from structural_index import (PARSER_VERSION, SourceDocument, build_structural_index,
                              normalize_cobol_lines, parse_document)


def source(body):
    return ("IDENTIFICATION DIVISION.\nPROGRAM-ID. CALCULATION.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 BASIS PIC 9(5).\n01 FACTOR PIC 9(5).\n"
            "01 RESULT-AMOUNT PIC 9(7).\nPROCEDURE DIVISION.\nMAIN.\n" + body + "\nGOBACK.\n")


class ComputeEvidenceClosureTests(unittest.TestCase):
    def parsed(self, body):
        text = source(body)
        lines, hint = normalize_cobol_lines(text, "free")
        document = SourceDocument("calculation.cbl", hashlib.sha256(text.encode()).hexdigest(),
                                  "utf-8", False, hint, "COBOL", tuple(text.splitlines()), lines)
        return text, parse_document(document)

    def verify(self, parsed, compute):
        span = next(span for span in parsed.evidence_spans if span.evidence_id == compute.evidence_id)
        evidence = {**asdict(span), "source_text": span.text, "integrity": "VALID",
                    "span_truncated": False, "content_type": "UNTRUSTED_SOURCE_TEXT"}
        assertion = {"predicate": "compute_statement", "target": "RESULT-AMOUNT",
                     "expression": ["BASIS", "*", "FACTOR"], "rounded": False}
        return span, check_claim_support(assertion, [span.evidence_id], {span.evidence_id: evidence})

    def test_formula_citation_includes_closer_and_closer_keeps_independent_node(self):
        for body in ("COMPUTE RESULT-AMOUNT = BASIS * FACTOR\nEND-COMPUTE",
                     "COMPUTE RESULT-AMOUNT = BASIS *\nFACTOR\nEND-COMPUTE."):
            with self.subTest(body=body):
                text, parsed = self.parsed(body)
                compute = next(unit for unit in parsed.code_units if unit.name == "COMPUTE")
                closer = next(unit for unit in parsed.code_units if unit.name == "END_COMPUTE")
                span, verdict = self.verify(parsed, compute)
                self.assertEqual(verdict["support_status"], "supported")
                self.assertEqual(span.end_line, closer.start_line)
                self.assertNotEqual(compute.evidence_id, closer.evidence_id)
                self.assertEqual(span.text, "\n".join(text.splitlines()[span.start_line - 1:span.end_line]))
                self.assertNotIn("GOBACK", span.text)

    def test_unrelated_statement_or_wrong_closer_is_not_absorbed(self):
        for body in ("COMPUTE RESULT-AMOUNT = BASIS * FACTOR\nMOVE ZERO TO RESULT-AMOUNT\nEND-COMPUTE",
                     "COMPUTE RESULT-AMOUNT = BASIS * FACTOR\nEND-ADD",
                     "COMPUTE RESULT-AMOUNT = BASIS * FACTOR\nEND-COMPUTE MOVE ZERO TO RESULT-AMOUNT"):
            with self.subTest(body=body):
                _, parsed = self.parsed(body)
                compute = next(unit for unit in parsed.code_units if unit.name == "COMPUTE")
                span, verdict = self.verify(parsed, compute)
                self.assertEqual(verdict["support_status"], "unsupported")
                self.assertNotIn("END-COMPUTE", span.text)
                self.assertNotIn("MOVE ZERO", span.text)

    def test_existing_v07_index_is_rebuilt_for_new_statement_evidence(self):
        with tempfile.TemporaryDirectory(prefix="compute-evidence-cache-") as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            root.joinpath("calculation.cbl").write_text(
                source("COMPUTE RESULT-AMOUNT = BASIS * FACTOR\nEND-COMPUTE"), encoding="utf-8")
            database = Path(temporary) / "index.sqlite"
            build_structural_index(root, database, source_format="free", quiet=True)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("UPDATE metadata SET value='source-facts-v0.7' WHERE key='parser_version'")
            result = build_structural_index(root, database, source_format="free", quiet=True)
            self.assertTrue(result["parser_rebuild_required"])
            self.assertEqual(result["files"]["indexed_or_updated"], 1)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key='parser_version'").fetchone()[0],
                                 PARSER_VERSION)
                text = connection.execute("SELECT e.text FROM code_units u JOIN evidence_spans e USING(evidence_id) "
                                          "WHERE u.unit_type='Statement' AND u.name='COMPUTE'").fetchone()[0]
                self.assertIn("END-COMPUTE", text)


if __name__ == "__main__":
    unittest.main()
