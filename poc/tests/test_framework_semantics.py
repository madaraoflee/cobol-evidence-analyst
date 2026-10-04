from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
from framework_semantics import build_framework_facts, visible_framework_facts


MANUAL = """# Record processing R1.0

## Database I/O

CALL XXXXIO USING XXXX-PARAMS

The operation is selected by XXXX-FUNCTION.

### Operations

| Function | Meaning | Caution |
| --- | --- | --- |
| ADVANCE | Request the following record. | Inspect the returned status. |
| STORE | Request an update. | A request is not a committed result. |

### Status

| Status | Meaning |
| --- | --- |
| DONE | End of input. |
"""

SOURCE = """IDENTIFICATION DIVISION.
PROGRAM-ID. ENTRYJOB.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 ROWS-PARAMS.
  05 ROWS-FUNCTION PIC X(8).
  05 ROWS-STATUS PIC X(4).
PROCEDURE DIVISION.
READ-STAGE SECTION.
MOVE 'ADVANCE' TO ROWS-FUNCTION.
CALL 'ROWSIO' USING ROWS-PARAMS.
IF ROWS-STATUS = 'DONE' CONTINUE END-IF.
GOBACK.
"""


class OfflineFrameworkSemanticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.path = self.source / "entry.cbl"
        self.path.write_text(SOURCE)
        self.manual = self.root / "reference.md"
        self.manual.write_text(MANUAL)
        self.database = self.root / "index.sqlite"

    def build(self, **kwargs):
        return build_business_index(self.source, self.database, source_format="free", quiet=True,
            framework_reference_path=kwargs.pop("framework_reference_path", self.manual), **kwargs)

    def pages(self, text=None):
        text = self.path.read_text() if text is None else text
        return [{"evidence_id": "ev-test", "relative_path": "entry.cbl",
                 "program_name": "ENTRYJOB", "start_line": 1,
                 "end_line": len(text.splitlines()), "source_text": text,
                 "source_sha256": hashlib.sha256(text.encode()).hexdigest(), "format_hint": "free"}]

    def facts(self, pages=None, **kwargs):
        return build_framework_facts(self.database, self.pages() if pages is None else pages,
                                    reference_path=kwargs.pop("reference_path", self.manual), **kwargs)

    def test_import_persists_bound_operation_before_any_question(self):
        report = self.build()
        summary = report["framework_semantics"]
        self.assertEqual(summary["status"], "COMPILED")
        self.assertGreater(summary["rule_count"], 0)
        self.assertEqual(summary["fact_count"], 1)
        self.assertFalse(summary["network_calls"])
        with closing(sqlite3.connect(self.database)) as db:
            stored = json.loads(db.execute("SELECT facts_json FROM framework_file_semantics").fetchone()[0])
            self.assertEqual(stored[0]["operation"]["value"], "ADVANCE")
        result = self.facts()
        self.assertTrue(result["facts"][0]["dependency_covered"])
        self.assertFalse(result["facts"][0]["runtime_verified"])
        self.assertEqual(result["facts"][0]["source_evidence_ids"], ["ev-test"])

    def test_same_source_and_manual_reuses_offline_bindings(self):
        self.build()
        with patch("framework_binding.bind_framework_source", side_effect=AssertionError("unneeded rescan")):
            second = self.build()
            result = self.facts()
        self.assertEqual(second["framework_semantics"]["files_reused"], 1)
        self.assertEqual(len(result["facts"]), 1)

    def test_document_change_invalidates_cache_without_source_rebuild(self):
        self.build()
        previous = self.facts()
        self.manual.write_text(MANUAL.replace("following record", "following eligible record"))
        rebound = self.facts()
        self.assertNotEqual(previous["knowledge_digest"], rebound["knowledge_digest"])
        self.assertIn("eligible", rebound["facts"][0]["operation"]["meaning"])
        rebuilt = self.build()
        self.assertEqual(rebuilt["files"]["indexed_or_updated"], 0)
        self.assertEqual(rebuilt["framework_semantics"]["files_rebuilt"], 1)

    def test_source_change_cannot_reuse_old_operation_or_old_pages(self):
        self.build()
        old = self.pages()
        self.path.write_text(SOURCE.replace("'ADVANCE'", "'STORE'"))
        self.build()
        self.assertFalse(self.facts(old)["facts"])
        self.assertEqual(self.facts()["facts"][0]["operation"]["value"], "STORE")

    def test_disabled_or_unreadable_manual_does_not_keep_prior_facts(self):
        self.build()
        self.assertFalse(self.facts(reference_path="")["facts"])
        self.assertFalse(self.facts(reference_path=self.root / "missing.md")["facts"])
        report = self.build(framework_reference_path="")
        self.assertEqual(report["framework_semantics"]["fact_count"], 0)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM framework_file_semantics").fetchone()[0], 0)

    def test_internal_source_implementation_takes_precedence_even_on_cache_hit(self):
        self.build()
        (self.source / "service.cbl").write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. ROWSIO.\n"
            "PROCEDURE DIVISION.\nGOBACK.\n")
        self.build()
        row = self.facts()["facts"][0]
        self.assertFalse(row["dependency_covered"])
        self.assertTrue(row["target_source_available"])
        self.assertEqual(row["reason"], "source_implementation_takes_precedence")

    def test_copy_preprocessing_withdraws_cached_operation_in_data_and_procedure(self):
        declarations = "01 ROWS-PARAMS.\n  05 ROWS-FUNCTION PIC X(8).\n  05 ROWS-STATUS PIC X(4).\n"
        copy_path = self.source / "ROW-LAYOUT.cpy"
        for position in ("data", "procedure"):
            with self.subTest(position=position):
                caller = (SOURCE.replace(declarations, "COPY ROW-LAYOUT.\n") if position == "data"
                          else SOURCE.replace("MOVE 'ADVANCE'", "COPY ROW-LAYOUT.\nMOVE 'ADVANCE'"))
                contents = declarations if position == "data" else "CONTINUE.\n"
                self.path.write_text(caller)
                copy_path.write_text(contents)
                self.assertEqual(self.build()["framework_semantics"]["covered_external_calls"], 1)
                contents = "REPLACE =='ADVANCE'== BY =='STORE'==.\n" + contents
                copy_path.write_text(contents)
                report = self.build()
                self.assertEqual(report["framework_semantics"]["covered_external_calls"], 0)
                self.assertEqual(report["framework_semantics"]["files_reused"], 1)
                with closing(sqlite3.connect(self.database)) as db:
                    directive = db.execute("SELECT normalized_text FROM code_units WHERE "
                                           "unit_type='PreprocessorDirective'").fetchone()
                    self.assertIsNotNone(directive)
                pages = self.pages() + [{"evidence_id": "ev-copy", "relative_path": "ROW-LAYOUT.cpy",
                    "program_name": "ROW-LAYOUT", "start_line": 1, "end_line": len(contents.splitlines()),
                    "source_text": contents, "source_sha256": hashlib.sha256(contents.encode()).hexdigest(),
                    "format_hint": "free"}]
                facts = self.facts(pages)["facts"]
                self.assertTrue(facts)
                self.assertFalse(any(row["dependency_covered"] for row in facts))
                self.assertIsNone(facts[0]["operation"])

    def test_request_cannot_keep_fact_after_its_support_is_trimmed(self):
        self.build()
        result = self.facts()
        self.assertFalse(visible_framework_facts(result["facts"], self.pages(), []))
        page = self.pages()[0]
        page.update(start_line=11, source_text="\n".join(SOURCE.splitlines()[10:]))
        self.assertFalse(visible_framework_facts(result["facts"], [page], result["references"]))
        references = [{**r, "text_truncated": True} for r in result["references"]]
        self.assertFalse(visible_framework_facts(result["facts"], self.pages(), references))

    def test_visible_call_requests_missing_layout_without_premature_coverage(self):
        self.build()
        page = self.pages()[0]
        page.update(start_line=10, source_text="\n".join(SOURCE.splitlines()[9:]))
        result = self.facts([page])
        self.assertFalse(result["facts"])
        self.assertEqual({row["start_line"] for row in result["source_requests"]}, {5, 6})
        self.assertTrue(all(row["relative_path"] == "entry.cbl" for row in result["source_requests"]))
        declaration = {**self.pages()[0], "end_line": 7, "source_text": "\n".join(SOURCE.splitlines()[:7])}
        completed = self.facts([page, declaration])
        self.assertTrue(completed["facts"][0]["dependency_covered"])
        self.assertFalse(completed["source_requests"])

    def test_older_index_rebinds_only_contiguous_visible_text(self):
        self.build()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DROP TABLE framework_file_semantics")
        self.assertTrue(self.facts()["facts"])
        first = {**self.pages()[0], "end_line": 10,
                 "source_text": "\n".join(SOURCE.splitlines()[:10])}
        last = {**self.pages()[0], "start_line": 12,
                "source_text": "\n".join(SOURCE.splitlines()[11:])}
        self.assertFalse(self.facts([first, last])["facts"])

    def test_unrelated_files_do_not_trigger_additional_source_reads(self):
        self.path.write_text(SOURCE.replace("'ROWSIO'", "'LOCALWORK'"))
        with patch("framework_binding.bind_framework_source", side_effect=AssertionError("unrelated source read")):
            report = self.build()
        self.assertEqual(report["framework_semantics"]["files_rebuilt"], 0)

    def test_transient_source_failure_is_not_cached_as_an_empty_success(self):
        with patch("source_reading._verified_lines", side_effect=ValueError("SOURCE_PATH_INVALID")):
            failed = self.build()
        self.assertTrue(failed["framework_semantics"]["boundaries"])
        recovered = self.build()
        self.assertEqual(recovered["framework_semantics"]["files_rebuilt"], 1)
        self.assertTrue(self.facts()["facts"])

    def test_truncated_preprocessing_line_never_covers_a_later_call(self):
        from source_session import QuestionSourceSession
        directive = "REPLACE " + " " * 66_000 + "=='ADVANCE'== BY =='STORE'==."
        self.path.write_text(SOURCE.replace("MOVE 'ADVANCE'", directive + "\nMOVE 'ADVANCE'"))
        report = self.build()
        self.assertIn("source_line_exceeds_fact_budget", {
            row["status"] for row in report["scope"]["boundaries"]})
        self.assertEqual(report["framework_semantics"]["covered_external_calls"], 0)
        with closing(sqlite3.connect(self.database)) as db:
            stored, = json.loads(db.execute("SELECT facts_json FROM framework_file_semantics").fetchone()[0])
        self.assertFalse(stored["dependency_covered"])
        self.assertEqual(stored["reason"], "source_line_unavailable")
        self.assertIsNone(stored["operation"])
        self.assertEqual(stored["documented_operation_candidate"]["value"], "ADVANCE")
        self.assertTrue(stored["reference_ids"])
        self.assertEqual(self.build()["framework_semantics"]["files_reused"], 1)
        cached, = self.facts()["facts"]
        self.assertFalse(cached["dependency_covered"])
        self.assertIsNone(cached["operation"])

        # A changed manual triggers the request-time complete-source pass.
        self.manual.write_text(MANUAL.replace("following record", "following eligible record"))
        with QuestionSourceSession(self.database, self.source) as session:
            rebound, = self.facts(source_session=session)["facts"]
        self.assertFalse(rebound["dependency_covered"])
        self.assertEqual(rebound["reason"], "source_line_unavailable")
        self.assertIsNone(rebound["operation"])

    def test_changed_manual_partial_page_cannot_forget_earlier_replacement(self):
        from source_session import QuestionSourceSession
        self.path.write_text(SOURCE.replace("MOVE 'ADVANCE'", "REPLACE =='ADVANCE'== BY =='STORE'==.\nMOVE 'ADVANCE'"))
        self.build()
        self.manual.write_text(MANUAL.replace("following record", "following eligible record"))
        page = self.pages()[0]
        lines = page["source_text"].splitlines()
        first = next(i + 1 for i, line in enumerate(lines) if line.startswith("MOVE 'ADVANCE'"))
        partial = {**page, "start_line": first, "end_line": first + 1,
                   "source_text": "\n".join(lines[first - 1:first + 1])}
        with QuestionSourceSession(self.database, self.source) as session:
            result = self.facts([partial], source_session=session)
        self.assertFalse(any(row["dependency_covered"] for row in result["facts"]))

    def test_capped_offline_cache_can_rebind_a_later_call_from_complete_capture(self):
        from source_session import QuestionSourceSession
        pair = "MOVE 'ADVANCE' TO ROWS-FUNCTION.\nCALL 'ROWSIO' USING ROWS-PARAMS.\n"
        self.path.write_text(SOURCE.replace(pair, pair * 3))
        with patch("framework_binding._MAX_FACTS", 2), patch("framework_semantics.MAX_FILE_FACTS", 2):
            report = self.build()
            self.assertIn("framework_file_fact_limit", {row["reason"] for row in report["framework_semantics"]["boundaries"]})
            whole = self.pages()[0]
            lines = whole["source_text"].splitlines()
            first = max(i + 1 for i, line in enumerate(lines) if line.startswith("MOVE 'ADVANCE'"))
            partial = {**whole, "evidence_id": "ev-tail", "start_line": first, "end_line": first + 1,
                       "source_text": "\n".join(lines[first - 1:first + 1])}
            declarations = {**whole, "evidence_id": "ev-data", "start_line": 1, "end_line": 8,
                            "source_text": "\n".join(lines[:8])}
            with QuestionSourceSession(self.database, self.source) as session:
                result = self.facts([declarations, partial], source_session=session)
        self.assertEqual(len(result["facts"]), 1)
        self.assertEqual(result["facts"][0]["start_line"], first + 1)
        self.assertTrue(result["facts"][0]["dependency_covered"])


if __name__ == "__main__":
    unittest.main()
