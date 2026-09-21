from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
import framework_knowledge as knowledge


REFERENCE = """# Processing reference

## Record access

| Operation | Meaning |
| --- | --- |
| FETCH-LOCK | Request a record and a lock; inspect the returned status. |
| SAVE-ROW | Request an update; completion is described by the returned status. |

## Shared records

HOLD-ROW retains a shared record for another program to read with TAKE-ROW.
"""


def program(name, operation, target="RECORDSERVICE"):
    return (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 ACCESS-FUNCTION PIC X(12).\n01 ACCESS-STATUS PIC X(4).\n"
            "PROCEDURE DIVISION.\n"
            f"MOVE '{operation}' TO ACCESS-FUNCTION.\n"
            f"CALL '{target}' USING ACCESS-FUNCTION ACCESS-STATUS.\n"
            "IF ACCESS-STATUS = 'OK' CONTINUE END-IF.\nGOBACK.\n")


class RepositoryFrameworkContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source_root = self.root / "source"
        self.source_root.mkdir()
        self.database = self.root / "index.sqlite"
        self.reference = self.root / "reference.md"
        self.reference.write_text(REFERENCE, encoding="utf-8")
        knowledge._CACHE.clear()

    def build(self, files):
        for relative, text in files.items():
            path = self.source_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        build_business_index(self.source_root, self.database, source_format="free", quiet=True)

    def context(self, **kwargs):
        return knowledge.build_framework_context(self.database, reference_path=self.reference,
            source_root=self.source_root, **kwargs)

    def test_arbitrary_business_question_retrieves_different_operations_across_repository(self):
        self.build({"first.cbl": program("FIRSTENTRY", "FETCH-LOCK"),
                    "second.cbl": program("SECONDENTRY", "HOLD-ROW")})
        result = self.context(question="为什么上一步取消之后，后面的处理仍然会拿到这笔申请？")
        self.assertEqual(result["status"], "MATCHED")
        self.assertEqual(result["coverage"]["files_scanned"], 2)
        self.assertTrue(result["coverage"]["source_hash_verified"])
        self.assertEqual({item["relative_path"] for item in result["source_matches"]},
                         {"first.cbl", "second.cbl"})
        terms = {term for item in result["source_matches"] for term in item["matched_terms"]}
        self.assertTrue({"FETCH-LOCK", "HOLD-ROW"}.issubset(terms))

    def test_selected_paths_include_copybook_data_and_override_entry(self):
        self.build({"first.cbl": program("FIRSTENTRY", "FETCH-LOCK"),
                    "shared.cpy": "01 SAVED-ACTION PIC X(12) VALUE 'HOLD-ROW'.\n",
                    "second.cbl": program("SECONDENTRY", "SAVE-ROW")})
        result = self.context(entry_program="FIRSTENTRY", source_paths=["./shared.cpy", "second.cbl"])
        self.assertEqual(result["coverage"]["source_scope"], "selected_files")
        self.assertEqual(result["coverage"]["files_scanned"], 2)
        self.assertEqual({item["relative_path"] for item in result["source_matches"]},
                         {"shared.cpy", "second.cbl"})
        self.assertTrue(any("HOLD-ROW" in item["matched_terms"] for item in result["source_matches"]))
        empty = self.context(source_paths=[])
        self.assertEqual(empty["coverage"]["files_scanned"], 0)
        self.assertFalse(empty["source_matches"])

    def test_multiple_programs_in_one_file_keep_actual_program_at_each_line(self):
        self.build({"combined.cbl": program("FIRSTENTRY", "FETCH-LOCK") + program("SECONDENTRY", "SAVE-ROW")})
        result = self.context()
        owners = {term: item["program_name"] for item in result["source_matches"] for term in item["matched_terms"]}
        self.assertEqual(owners["FETCH-LOCK"], "FIRSTENTRY")
        self.assertEqual(owners["SAVE-ROW"], "SECONDENTRY")

    def test_missing_callee_keeps_caller_and_manual_evidence_without_claiming_runtime(self):
        text = program("FIRSTENTRY", "FETCH-LOCK")
        self.build({"first.cbl": text})
        result = self.context()
        self.assertEqual(result["status"], "MATCHED")
        self.assertEqual(len(result["external_calls"]), 1)
        call = result["external_calls"][0]
        self.assertEqual(call["target_name"], "RECORDSERVICE")
        self.assertFalse(call["target_source_available"])
        self.assertFalse(call["runtime_verified"])
        self.assertFalse(call["parameter_binding_verified"])
        self.assertEqual(call["source_sha256"], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(call["source_text"], text.splitlines()[call["start_line"] - 1])
        references = {item["reference_id"]: item for item in result["references"]}
        self.assertTrue(any("FETCH-LOCK" in references[key]["matched_terms"] for key in call["reference_ids"]))
        with sqlite3.connect(self.database) as connection:
            span = connection.execute("SELECT relative_path,start_line,end_line,source_sha256,text "
                "FROM evidence_spans WHERE evidence_id=?", (call["evidence_id"],)).fetchone()
        self.assertEqual(span, ("first.cbl", call["start_line"], call["end_line"], call["source_sha256"], call["source_text"]))

    def test_dynamic_target_is_not_mislabeled_as_known_missing_program(self):
        text = program("FIRSTENTRY", "SAVE-ROW").replace("CALL 'RECORDSERVICE'", "CALL TARGET-PROGRAM")
        self.build({"first.cbl": text})
        result = self.context()
        call = result["external_calls"][0]
        self.assertEqual(call["relation_type"], "CALL_TARGET_FROM")
        self.assertEqual(call["target_resolution"], "dynamic_target")
        self.assertIsNone(call["target_source_available"])

    def test_present_target_is_not_reported_as_external(self):
        self.build({"first.cbl": program("FIRSTENTRY", "FETCH-LOCK"),
                    "service.cbl": "IDENTIFICATION DIVISION.\nPROGRAM-ID. RECORDSERVICE.\nPROCEDURE DIVISION.\nGOBACK.\n"})
        self.assertFalse(self.context()["external_calls"])

    def test_reference_and_evidence_selection_do_not_let_first_file_consume_other_file(self):
        operations = [f"ACTION-{index:03d}" for index in range(20)]
        self.reference.write_text("# Reference\n\n" + "\n\n".join(
            f"## Rule {index}\n\n{operation} requests action {index}." for index, operation in enumerate(operations))
            + "\n\n## Release\n\nRELEASE-ROW releases the selected record.\n", encoding="utf-8")
        self.build({"a.cbl": program("FIRSTENTRY", operations[0]).replace("GOBACK.",
                        "\n".join(f"MOVE '{operation}' TO ACCESS-FUNCTION." for operation in operations) + "\nGOBACK."),
                    "z.cbl": program("SECONDENTRY", "RELEASE-ROW")})
        with mock.patch.object(knowledge, "MAX_REFERENCES", 2), mock.patch.object(knowledge, "MAX_SOURCE_MATCHES", 2), \
             mock.patch.object(knowledge, "MAX_SOURCE_CANDIDATES", 4):
            result = self.context()
        self.assertEqual({item["relative_path"] for item in result["source_matches"]}, {"a.cbl", "z.cbl"})
        self.assertTrue(result["coverage"]["candidates_truncated"])
        self.assertTrue(result["coverage"]["references_truncated"])
        self.assertEqual(result["coverage"]["files_scanned"], 2)

    def test_changed_file_does_not_discard_verified_matches_from_other_files(self):
        self.build({"first.cbl": program("FIRSTENTRY", "FETCH-LOCK"),
                    "second.cbl": program("SECONDENTRY", "SAVE-ROW")})
        with (self.source_root / "first.cbl").open("a") as handle:
            handle.write("*> changed\n")
        result = self.context()
        self.assertEqual(result["status"], "MATCHED")
        self.assertEqual(result["coverage"]["files_scanned"], 1)
        self.assertFalse(result["coverage"]["source_hash_verified"])
        self.assertEqual(result["coverage"]["source_scan_errors"][0]["relative_path"], "first.cbl")
        self.assertEqual({item["relative_path"] for item in result["source_matches"]}, {"second.cbl"})
        self.assertEqual({item["relative_path"] for item in result["external_calls"]}, {"second.cbl"})

    def test_unavailable_reference_remains_optional(self):
        self.build({"first.cbl": program("FIRSTENTRY", "FETCH-LOCK")})
        for reference, status in (("", "NOT_CONFIGURED"), (self.root / "missing.md", "UNAVAILABLE")):
            with self.subTest(status=status):
                result = knowledge.build_framework_context(self.database, source_root=self.source_root,
                    reference_path=reference, question="请解释这项业务处理的完整规则。")
                self.assertEqual(result["status"], status)
                self.assertFalse(result["references"])
                self.assertFalse(result["external_calls"])

    def test_cancellation_rolls_back_new_evidence_across_files(self):
        self.build({"first.cbl": program("FIRSTENTRY", "FETCH-LOCK"),
                    "second.cbl": program("SECONDENTRY", "SAVE-ROW")})
        with sqlite3.connect(self.database) as connection:
            before = connection.execute("SELECT COUNT(*) FROM evidence_spans").fetchone()[0]
        original = knowledge._verified_lines
        def interrupted(root, item, check, limit):
            if item["relative_path"] == "second.cbl":
                raise RuntimeError("cancelled")
            yield from original(root, item, check, limit)
        with mock.patch.object(knowledge, "_verified_lines", interrupted):
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                self.context()
        with sqlite3.connect(self.database) as connection:
            after = connection.execute("SELECT COUNT(*) FROM evidence_spans").fetchone()[0]
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
