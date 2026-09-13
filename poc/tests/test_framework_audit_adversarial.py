from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework_audit import audit_framework, validate_profile


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "framework-flow-v1"


class FrameworkAuditAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.profile = json.loads((FIXTURE / "profile.json").read_text())

    def tearDown(self):
        self.temporary.cleanup()

    def source(self, body):
        text = (
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRYPG.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 WS-COUNT PIC 99 VALUE 2.\n"
            "PROCEDURE DIVISION.\nENTRY-POINT.\n" + body + "\n"
        )
        (self.root / "entry.cbl").write_text(text)
        return audit_framework(self.root, "ENTRYPG")

    def test_identifier_times_is_inline_perform_not_paragraph_target(self):
        result = self.source("PERFORM WS-COUNT TIMES\nCONTINUE\nEND-PERFORM.\nGOBACK.")
        self.assertEqual(result["observations"]["performs"], [])
        self.assertIn("inline_perform_not_analyzed", [b["reason"] for b in result["boundaries"]])

    def test_qualified_inline_count_is_not_a_paragraph_reference(self):
        (self.root / "entry.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRYPG.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 COUNTERS.\n05 WS-COUNT PIC 99 VALUE 2.\n"
            "PROCEDURE DIVISION.\nENTRY-POINT.\n"
            "PERFORM WS-COUNT OF COUNTERS TIMES\nCONTINUE\nEND-PERFORM.\nGOBACK.\n"
        )
        result = audit_framework(self.root, "ENTRYPG")
        self.assertEqual(result["observations"]["performs"], [])
        self.assertIn("inline_perform_not_analyzed", [b["reason"] for b in result["boundaries"]])

    def test_statement_terminators_are_not_paragraph_definitions(self):
        for terminator in ("END-ADD", "END-SUBTRACT", "END-MULTIPLY", "END-DIVIDE",
                           "END-SEARCH", "END-STRING", "END-UNSTRING", "END-RETURN",
                           "END-REWRITE", "END-START", "END-DELETE", "END-ACCEPT", "END-DISPLAY"):
            with self.subTest(terminator=terminator):
                result = self.source(f"{terminator}.\nGOBACK.")
                names = [item["name"] for item in result["observations"]["procedure_definitions"]]
                self.assertNotIn(terminator, names)
                self.assertIn("ENTRY-POINT", names)

    def test_comments_and_quoted_program_text_do_not_become_facts(self):
        result = self.source(
            'DISPLAY "CALL \'PHANTOM\' PERFORM 3000-FAKE READ DATA".\n'
            'DISPLAY "A doubled ""CALL"" literal".\n'
            "DISPLAY 'PERFORM 3000-FAKE. it''s text'. *> CALL 'PHANTOM'.\n"
            "*> PERFORM 3000-FAKE.\nGOBACK."
        )
        self.assertEqual(result["observations"]["calls"], [])
        self.assertEqual(result["observations"]["performs"], [])
        self.assertEqual(result["observations"]["native_io"], [])

    def test_embedded_language_call_is_not_reported_as_cobol_call(self):
        result = self.source("EXEC SQL\nCALL SERVICEPROC()\nEND-EXEC.\nGOBACK.")
        self.assertEqual(result["observations"]["calls"], [])
        self.assertIn("embedded_language_not_analyzed", [b["reason"] for b in result["boundaries"]])
        qualified = self.source("EXEC SQL\nSELECT REQUESTS.\nREQUEST_ID FROM REQUESTS\nEND-EXEC.\nGOBACK.")
        self.assertNotIn("REQUESTS", [d["name"] for d in qualified["observations"]["procedure_definitions"]])

    def test_unsupported_literal_continuation_does_not_invent_a_call(self):
        (self.root / "entry.cbl").write_text(
            "000100 IDENTIFICATION DIVISION.\n"
            "000200 PROGRAM-ID. ENTRYPG.\n"
            "000300 PROCEDURE DIVISION.\n"
            '000400     DISPLAY "CALL \'PHANTOM\'\n'
            '000500-    " remains literal text".\n'
            "000600     GOBACK.\n"
        )
        result = audit_framework(self.root, "ENTRYPG")
        self.assertFalse(result["scope"]["source_expansion_complete"])
        self.assertIn("source_continuation_not_supported", [b["reason"] for b in result["boundaries"]])
        self.assertEqual(result["observations"]["calls"], [])

    def test_malformed_enum_types_raise_validation_error(self):
        for value in ([], {}):
            for field in ("provenance", "entry"):
                with self.subTest(field=field, value=value):
                    profile = deepcopy(self.profile)
                    if field == "provenance":
                        profile["provenance"]["kind"] = value
                    else:
                        profile["entries"][0]["mode"] = value
                    with self.assertRaises(ValueError):
                        validate_profile(profile)

    def test_changed_control_source_rebinds_origin_hash_and_manifest(self):
        (self.root / "entry.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRYPG.\n"
            "PROCEDURE DIVISION.\nCOPY ENTRYCTL.\n"
            "2000-HANDLE.\nCONTINUE.\n"
        )
        controller = self.root / "entryctl.cpy"
        controller.write_text("PERFORM 2000-HANDLE.\nGOBACK.\n")
        before = audit_framework(self.root, "ENTRYPG")
        controller.write_text("*> Changed controller\nPERFORM 2000-HANDLE.\nGOBACK.\n")
        after = audit_framework(self.root, "ENTRYPG")
        previous = before["observations"]["performs"][0]["references"][0]
        current = after["observations"]["performs"][0]["references"][0]
        self.assertNotEqual(before["source_manifest_hash"], after["source_manifest_hash"])
        self.assertEqual(current["include_chain"], previous["include_chain"])
        self.assertNotEqual(current["origin"]["source_hash"], previous["origin"]["source_hash"])
        self.assertEqual(current["origin"]["line"], 2)
        self.assertEqual(current["origin"]["source_hash"], hashlib.sha256(controller.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
