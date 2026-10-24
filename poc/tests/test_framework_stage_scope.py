"""Persist uncertain preprocessing before projecting documented stage meaning."""

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
from framework_binding import bind_framework_source
from framework_rules import compile_framework_rules
from framework_semantics import SEMANTICS_VERSION, build_framework_facts


MANUAL = """# Record processing R1.0

## Stages

| Section | Meaning | Caution |
| --- | --- | --- |
| 3000-READ | Prepare a request for review. | Execution depends on the caller. |

## Database I/O

CALL XXXXIO USING XXXX-PARAMS

The operation is selected by XXXX-FUNCTION.

### Operations

| Function | Meaning |
| --- | --- |
| ADVANCE | Request the next record. |
"""

SOURCE = """>>SOURCE FORMAT FREE
IDENTIFICATION DIVISION.
PROGRAM-ID. ENTRYJOB.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 ROWS-PARAMS.
05 ROWS-FUNCTION PIC X(8).
PROCEDURE DIVISION.
MAIN.
PERFORM 3000-READ.
GOBACK.
3000-READ SECTION.
READ-PARA.
MOVE 'ADVANCE' TO ROWS-FUNCTION.
CALL 'ROWSIO' USING ROWS-PARAMS.
"""

BOUNDARY = "framework_stage_scope_requires_expansion"


class FrameworkStageScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.path = self.source / "entry.cbl"
        self.manual = self.root / "manual.md"
        self.manual.write_text(MANUAL, encoding="utf-8")
        self.knowledge = compile_framework_rules(self.manual)
        self.database = self.root / "index.sqlite"

    def bind(self, source, **kwargs):
        return bind_framework_source(enumerate(source.splitlines(), 1), self.knowledge,
            relative_path="entry.cbl", source_sha256=hashlib.sha256(source.encode()).hexdigest(),
            source_format="free", **kwargs)

    def stage(self, facts):
        stages = [row for row in facts if row["kind"] == "framework_section"]
        self.assertEqual(len(stages), 1)
        return stages[0]

    def build(self, source):
        self.path.write_text(source, encoding="utf-8")
        return build_business_index(self.source, self.database, source_format="free", quiet=True,
                                    framework_reference_path=self.manual)

    def page(self, source, first=1, last=None):
        lines = source.splitlines()
        last = last or len(lines)
        return {"relative_path": "entry.cbl", "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "evidence_id": "ev-stage", "start_line": first, "end_line": last,
                "source_text": "\n".join(lines[first - 1:last]), "format_hint": "free"}

    def test_procedure_copy_invalidates_stage_before_or_after_observation(self):
        for source in (
            SOURCE.replace("3000-READ SECTION.", "COPY CONTROL-FRAGMENT.\n3000-READ SECTION."),
            SOURCE.replace("READ-PARA.", "COPY CONTROL-FRAGMENT.\nREAD-PARA."),
            SOURCE + "COPY CONTROL-FRAGMENT.\n",
        ):
            with self.subTest(source=source):
                facts = self.bind(source)
                stage = self.stage(facts)
                self.assertEqual(stage["reason"], BOUNDARY)
                self.assertEqual(stage["operation"]["meaning"], "Prepare a request for review.")
                self.assertTrue(stage["reference_ids"])
                self.assertFalse(stage["dependency_covered"])
                operation = next(row for row in facts if row["kind"] == "framework_operation")
                self.assertTrue(operation["dependency_covered"])

    def test_data_copy_preserves_existing_local_operation_binding_and_stage_observation(self):
        source = SOURCE.replace("01 ROWS-PARAMS.\n05 ROWS-FUNCTION PIC X(8).", "COPY RECORD-LAYOUT.")
        facts = self.bind(source)
        self.assertEqual(self.stage(facts)["reason"], "documented_section_observed")
        self.assertTrue(next(row for row in facts if row["kind"] == "framework_operation")["dependency_covered"])

    def test_replacement_and_compile_directives_are_persistent_stage_boundaries(self):
        for directive in (
            "REPLACE ==3000-READ== BY ==9000-OTHER==.\nREPLACE OFF.",
            "REPLACE OFF.", ">>IF BUILD-MODE", ">>ELSE", ">>END-IF", ">>DEFINE BUILD-MODE AS 1",
            "$SET CONDITIONAL", "CBL CONDITIONAL", "PROCESS CONDITIONAL",
        ):
            for anchor in ("IDENTIFICATION DIVISION.", "01 ROWS-PARAMS.", "MAIN."):
                with self.subTest(directive=directive, anchor=anchor):
                    source = SOURCE.replace(anchor, directive + "\n" + anchor)
                    self.assertEqual(self.stage(self.bind(source))["reason"], BOUNDARY)

    def test_comment_literal_embedded_text_and_source_format_do_not_create_boundary(self):
        source = SOURCE.replace("READ-PARA.", """*> COPY CONTROL-FRAGMENT. REPLACE OFF. >>IF UNKNOWN
READ-PARA.
DISPLAY "COPY CONTROL-FRAGMENT. REPLACE OFF. >>IF UNKNOWN".
EXEC SQL
COPY CONTROL-FRAGMENT.
REPLACE OFF.
END-EXEC.
""")
        self.assertEqual(self.stage(self.bind(source))["reason"], "documented_section_observed")

    def test_focus_does_not_hide_an_outside_preprocessing_boundary(self):
        source = SOURCE.replace("READ-PARA.", "COPY CONTROL-FRAGMENT.\nREAD-PARA.")
        stage_line = source.splitlines().index("3000-READ SECTION.") + 1
        stage = self.stage(self.bind(source, focus_ranges=[(stage_line, stage_line)]))
        self.assertEqual(stage["reason"], BOUNDARY)

    def test_indexed_boundary_survives_request_trimming_and_cache_reuse(self):
        for directive in ("COPY CONTROL-FRAGMENT.", "REPLACE ==MAIN== BY ==OTHER-MAIN==.\nREPLACE OFF.",
                          ">>IF BUILD-MODE\n>>END-IF"):
            with self.subTest(directive=directive):
                source = SOURCE.replace("MAIN.", directive + "\nMAIN.")
                self.build(source)
                with sqlite3.connect(self.database) as db:
                    version, stored = db.execute("SELECT version,bindings_json FROM framework_file_semantics").fetchone()
                self.assertEqual(version, SEMANTICS_VERSION)
                self.assertEqual(self.stage(json.loads(stored))["reason"], BOUNDARY)
                first = source.splitlines().index("3000-READ SECTION.") + 1
                page = self.page(source, first)
                self.assertNotIn(directive, page["source_text"])
                with patch("framework_binding.bind_framework_source", side_effect=AssertionError("Unexpected rebind")):
                    result = build_framework_facts(self.database, [page], reference_path=self.manual)
                stage = self.stage(result["facts"])
                self.assertEqual(stage["reason"], BOUNDARY)
                self.assertEqual(stage["source_evidence_ids"], ["ev-stage"])

    def test_v15_cache_is_rebound_even_when_source_and_manual_are_unchanged(self):
        source = SOURCE.replace("READ-PARA.", "COPY CONTROL-FRAGMENT.\nREAD-PARA.")
        self.build(source)
        with sqlite3.connect(self.database) as db:
            stored = json.loads(db.execute("SELECT bindings_json FROM framework_file_semantics").fetchone()[0])
            self.stage(stored)["reason"] = "documented_section_observed"
            db.execute("UPDATE framework_file_semantics SET version=?,bindings_json=?,facts_json=?",
                       ("framework-semantics/v1.5", json.dumps(stored), json.dumps(stored)))
        with patch("framework_binding.bind_framework_source", wraps=bind_framework_source) as bind:
            report = self.build(source)
        self.assertEqual(bind.call_count, 1)
        self.assertEqual(report["files"]["indexed_or_updated"], 0)
        self.assertEqual(report["framework_semantics"]["files_rebuilt"], 1)
        result = build_framework_facts(self.database, [self.page(source)], reference_path=self.manual)
        self.assertEqual(self.stage(result["facts"])["reason"], BOUNDARY)


if __name__ == "__main__":
    unittest.main()
