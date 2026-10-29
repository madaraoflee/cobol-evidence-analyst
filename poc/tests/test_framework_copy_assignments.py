"""Ordinary data COPY can prove separate storage without guessing execution."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from business_index import build_business_index
from framework_binding import bind_framework_source
from framework_rules import compile_framework_rules
from framework_semantics import build_framework_facts
from test_framework_semantics import MANUAL


LAYOUT = """01 ROWS-PARAMS.
  05 ROWS-FUNCTION PIC X(8).
  05 ROWS-STATUS PIC X(4).
  05 ROWS-KEY PIC X(8).
  05 ROWS-VIEW PIC X(8).
"""
BODY = """MOVE 'ADVANCE' TO ROWS-FUNCTION.
MOVE 'KEY' TO ROWS-KEY.
MOVE 'VIEW' TO ROWS-VIEW.
CALL 'ROWSIO' USING ROWS-PARAMS.
"""
SOURCE = """IDENTIFICATION DIVISION.
PROGRAM-ID. ENTRYJOB.
DATA DIVISION.
WORKING-STORAGE SECTION.
COPY "layout.cpy".
PROCEDURE DIVISION.
MAIN.
""" + BODY + "GOBACK.\n"


class FrameworkCopyAssignmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.entry = self.source / "entry.cbl"
        self.layout = self.source / "layout.cpy"
        self.entry.write_text(SOURCE)
        self.layout.write_text(LAYOUT)
        self.manual = self.root / "reference.md"
        self.manual.write_text(MANUAL)
        self.database = self.root / "index.sqlite"

    def build(self):
        return build_business_index(self.source, self.database, source_format="free",
                                    quiet=True, framework_reference_path=self.manual)

    def pages(self):
        result = []
        for path in sorted(self.source.rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text()
            relative = path.relative_to(self.source).as_posix()
            result.append({"evidence_id": "ev:" + relative, "relative_path": relative,
                "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "start_line": 1, "end_line": len(text.splitlines()), "source_text": text, "format_hint": "free"})
        return result

    def facts(self, pages=None):
        return build_framework_facts(self.database, self.pages() if pages is None else pages,
                                     reference_path=self.manual)

    def assert_uncovered(self):
        self.build()
        self.assertFalse(any(row["dependency_covered"] for row in self.facts()["facts"]))

    def test_full_copy_and_caller_bind_same_operation_as_inline_declarations(self):
        self.build()
        fact, = self.facts()["facts"]
        self.assertTrue(fact["dependency_covered"])
        self.assertEqual(fact["operation"]["value"], "ADVANCE")
        self.assertEqual(set(fact["source_evidence_ids"]), {"ev:entry.cbl", "ev:layout.cpy"})
        self.assertFalse(fact["runtime_verified"])
        self.assertFalse(fact["layout_validation"]["callee_abi_verified"])
        self.entry.write_text(SOURCE.replace('COPY "layout.cpy".', LAYOUT))
        self.layout.unlink()
        self.build()
        inline, = self.facts()["facts"]
        self.assertEqual(fact["operation"], inline["operation"])

    def test_binder_alone_does_not_promote_unchecked_copy_candidate(self):
        compiled = compile_framework_rules(self.manual)
        fact, = bind_framework_source(enumerate(SOURCE.splitlines(), 1), compiled,
            relative_path="entry.cbl", source_sha256=hashlib.sha256(SOURCE.encode()).hexdigest(), source_format="free")
        self.assertFalse(fact["dependency_covered"])
        self.assertIsNone(fact["operation"])
        self.assertEqual(fact["reason"], "assignment_layout_not_verified")
        self.assertEqual(fact["documented_operation_candidate"]["value"], "ADVANCE")

    def test_missing_copy_source_requests_original_declarations_before_coverage(self):
        self.build()
        caller = next(page for page in self.pages() if page["relative_path"] == "entry.cbl")
        result = self.facts([caller])
        self.assertFalse(result["facts"])
        required = {row["start_line"] for row in result["source_requests"] if row["relative_path"] == "layout.cpy"}
        self.assertTrue({1, 2, 4, 5} <= required)

    def test_missing_or_conflicting_intervening_write_never_covers_call(self):
        self.build()
        pages = self.pages()
        caller = next(page for page in pages if page["relative_path"] == "entry.cbl")
        missing = next(index for index, line in enumerate(caller["source_text"].splitlines(), 1)
                       if line.startswith("MOVE 'KEY'"))
        lines = caller["source_text"].splitlines()
        before = {**caller, "evidence_id": "ev:before", "end_line": missing - 1,
                  "source_text": "\n".join(lines[:missing - 1])}
        after = {**caller, "evidence_id": "ev:after", "start_line": missing + 1,
                 "source_text": "\n".join(lines[missing:])}
        layout = next(page for page in pages if page["relative_path"] == "layout.cpy")
        self.assertFalse(self.facts([before, after, layout])["facts"])
        conflict = {**caller, "evidence_id": "ev:conflict", "start_line": missing, "end_line": missing,
                    "source_text": "MOVE 'DIFFERENT' TO ROWS-KEY."}
        self.assertFalse(self.facts(pages + [conflict])["facts"])

    def test_stale_copy_page_cannot_support_current_binding(self):
        self.build()
        old_pages = self.pages()
        self.layout.write_text(LAYOUT.replace("PIC X(8)", "PIC X(9)"))
        self.build()
        self.assertFalse(self.facts(old_pages)["facts"])

    def test_copy_change_invalidates_binding_even_when_caller_bytes_are_unchanged(self):
        self.build()
        caller = self.entry.read_bytes()
        self.assertGreater(self.build()["framework_semantics"]["files_reused"], 0)
        self.layout.write_text(LAYOUT.replace("05 ROWS-KEY PIC X(8).", "05 ROWS-KEY REDEFINES ROWS-FUNCTION PIC X(8)."))
        report = self.build()
        self.assertEqual(self.entry.read_bytes(), caller)
        self.assertEqual(report["framework_semantics"]["files_reused"], 0)
        self.assertEqual(report["framework_semantics"]["files_rebuilt"], 1)
        self.assertFalse(any(row["dependency_covered"] for row in self.facts()["facts"]))

    def test_unsupported_layouts_and_overwrites_remain_uncovered(self):
        changes = [
            LAYOUT.replace("05 ROWS-KEY PIC X(8).", "05 ROWS-KEY REDEFINES ROWS-FUNCTION PIC X(8)."),
            LAYOUT.replace("05 ROWS-KEY PIC X(8).", "05 ROWS-KEY OCCURS 2 PIC X(8)."),
            LAYOUT + "01 ROWS-KEY PIC X(8).\n",
            LAYOUT.replace("05 ROWS-KEY PIC X(8).", "01 ROWS-KEY PIC X(8)."),
            LAYOUT.replace("05 ROWS-KEY PIC X(8).", "05 ROWS-KEY.\n  10 CHILD-FIELD PIC X(8)."),
            LAYOUT.replace("05 ROWS-FUNCTION PIC X(8).", "05 ROWS-FUNCTION PIC X(2)."),
        ]
        for layout in changes:
            with self.subTest(layout=layout):
                self.layout.write_text(layout)
                self.assert_uncovered()

    def test_resolved_nested_copy_requires_every_physical_source(self):
        self.layout.write_text('COPY "fields.cpy".\n')
        leaf = self.source / "fields.cpy"
        leaf.write_text(LAYOUT)
        self.build()
        fact, = self.facts()["facts"]
        self.assertTrue(fact["dependency_covered"])
        self.assertEqual(set(fact["source_evidence_ids"]),
                         {"ev:entry.cbl", "ev:layout.cpy", "ev:fields.cpy"})
        pages = [page for page in self.pages() if page["relative_path"] != "fields.cpy"]
        self.assertFalse(self.facts(pages)["facts"])
        leaf.unlink()
        self.assert_uncovered()

    def test_single_token_replacing_preserves_original_source_and_transformation(self):
        names = ("PARAMS", "FUNCTION", "STATUS", "KEY", "VIEW")
        replacements = " ".join(f"==TEMPLATE-{name}== BY ==ROWS-{name}==" for name in names)
        self.layout.write_text(LAYOUT.replace("ROWS-", "TEMPLATE-"))
        self.entry.write_text(SOURCE.replace('COPY "layout.cpy".',
            f'COPY "layout.cpy" REPLACING {replacements}.'))
        self.build()
        fact, = self.facts()["facts"]
        self.assertTrue(fact["dependency_covered"])
        self.assertEqual(fact["operation"]["value"], "ADVANCE")
        self.assertEqual(set(fact["source_evidence_ids"]), {"ev:entry.cbl", "ev:layout.cpy"})
        member_refs = [ref for ref in fact["binding_evidence_refs"] if ref["relative_path"] == "layout.cpy"]
        self.assertTrue(member_refs)
        self.assertTrue(all(ref.get("replacement_trace") for ref in member_refs))
        self.assertIn("TEMPLATE-FUNCTION", self.layout.read_text())

    def test_missing_and_ambiguous_copies_remain_uncovered(self):
        self.layout.unlink()
        self.assert_uncovered()
        for folder in ("one", "two"):
            (self.source / folder).mkdir()
            (self.source / folder / "layout.cpy").write_text(LAYOUT)
        self.assert_uncovered()

    def test_call_control_preprocessing_and_group_write_barriers_are_not_bypassed(self):
        variants = [
            BODY.replace("MOVE 'KEY'", "CALL 'OTHER-SERVICE' USING ROWS-PARAMS.\nMOVE 'KEY'"),
            BODY.replace("MOVE 'KEY'", "IF ROWS-STATUS = 'DONE'\nMOVE 'KEY'").replace(
                "CALL 'ROWSIO'", "END-IF\nCALL 'ROWSIO'"),
            BODY.replace("MOVE 'KEY'", "REPLACE ==PLACEHOLDER== BY ==OTHER==.\nMOVE 'KEY'"),
            BODY.replace("MOVE 'KEY' TO ROWS-KEY.", "MOVE SPACES TO ROWS-PARAMS."),
            BODY.replace("'ADVANCE'", "ACTION-NAME"),
            BODY.replace("USING ROWS-PARAMS", "USING OTHER-PARAMS"),
            BODY.replace("CALL 'ROWSIO'", "CALL TARGET-NAME"),
        ]
        for body in variants:
            with self.subTest(body=body):
                self.entry.write_text(SOURCE.replace(BODY, body))
                self.assert_uncovered()

    def test_procedure_copy_without_program_header_stays_outside_binding_scope(self):
        self.entry.write_text(SOURCE.replace(BODY, 'COPY "actions.cpy".\n'))
        (self.source / "actions.cpy").write_text(BODY)
        self.assert_uncovered()


if __name__ == "__main__":
    unittest.main()
