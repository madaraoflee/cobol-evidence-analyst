"""Offline file/field impact regressions with neutral public source fixtures."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from file_impact import collect_file_impact


def page(path, text, first=1, **options):
    return {"relative_path": path, "source_text": text, "start_line": first,
            "end_line": first + len(text.splitlines()) - 1,
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "evidence_id": "ev_" + path.replace("/", "_") + "_" + str(first), **options}


SOURCE = """IDENTIFICATION DIVISION.
PROGRAM-ID. DEBITFLOW.
ENVIRONMENT DIVISION.
INPUT-OUTPUT SECTION.
FILE-CONTROL.
SELECT ACCOUNT-FILE ASSIGN TO 'ACCOUNTLF'.
SELECT RATE-FILE ASSIGN TO 'RATELF'.
DATA DIVISION.
FILE SECTION.
FD ACCOUNT-FILE.
01 ACCTREC.
 05 ACCTBAL PIC 9(7)V99.
 05 ACCTSTAT PIC X.
FD RATE-FILE.
01 RATEREC.
 05 RATEPCT PIC 9V99.
WORKING-STORAGE SECTION.
01 DEBIT-AMOUNT PIC 9(7)V99.
PROCEDURE DIVISION.
MAIN.
READ RATE-FILE.
SUBTRACT DEBIT-AMOUNT FROM ACCTBAL.
MOVE 'D' TO ACCTSTAT.
REWRITE ACCTREC.
CALL 'EXTERNALPOST'.
CALL POST-TARGET.
"""


class FileImpactTests(unittest.TestCase):
    def observations(self, result, kind):
        return [item for item in result["observations"] if item["kind"] == kind]

    def test_reports_known_direct_write_and_read_only_file_with_physical_provenance(self):
        supplied = page("debit.cbl", SOURCE)
        with mock.patch("builtins.open", side_effect=AssertionError("Extraction must use supplied pages only")):
            result = collect_file_impact([supplied])
        io = {item["operation"]: item for item in self.observations(result, "io_operation")}
        self.assertEqual(io["REWRITE"]["operand"], "ACCTREC")
        self.assertEqual(io["REWRITE"]["file_name"], "ACCOUNT-FILE")
        self.assertEqual(io["REWRITE"]["assigned_name"], "ACCOUNTLF")
        self.assertEqual(io["REWRITE"]["access_role"], "direct_write")
        self.assertEqual(io["READ"]["access_role"], "read_only_dependency")
        self.assertEqual(io["READ"]["fields"], ["RATEPCT"])
        self.assertFalse(io["REWRITE"]["external_identity_verified"])
        changes = self.observations(result, "field_assignment")
        self.assertEqual([item["written_fields"] for item in changes], [["ACCTBAL"], ["ACCTSTAT"]])
        self.assertEqual(changes[0]["record_memberships"][0]["file_name"], "ACCOUNT-FILE")
        self.assertEqual(changes[0]["source_locations"][0]["start_line"], 22)
        self.assertEqual({ref for item in result["observations"] for ref in item["evidence_ids"]}, {supplied["evidence_id"]})
        dependencies = self.observations(result, "dependency_reference")
        self.assertEqual([item["target_source_status"] for item in dependencies],
                         ["target_source_not_supplied", "dynamic_target_unresolved"])
        self.assertFalse(result["scope"]["runtime_paths_verified"])

    def test_public_fixture_copybook_indirect_call_and_dds_relationship_remain_scoped(self):
        root = Path(__file__).resolve().parents[1] / "fixtures" / "file-impact-v1"
        supplied = [page(path.relative_to(root).as_posix(), path.read_text(encoding="utf8"))
                    for path in sorted(root.rglob("*")) if path.is_file() and path.suffix != ".md"]
        result = collect_file_impact(supplied)
        io = self.observations(result, "io_operation")
        rewrite = next(item for item in io if item["operation"] == "REWRITE")
        self.assertEqual((rewrite["file_name"], rewrite["assigned_name"]), ("ACCOUNT-FILE", "ACCOUNTLF"))
        self.assertIn("ev_copybooks_ACCOUNTREC.cpy_1", rewrite["evidence_ids"])
        self.assertIn({"relative_path": "copybooks/ACCOUNTREC.cpy", "start_line": 3, "end_line": 3}, rewrite["source_locations"])
        audit_write = next(item for item in io if item["program_name"] == "AUDITPOST")
        self.assertEqual(audit_write["file_name"], "AUDIT-FILE")
        call = next(item for item in self.observations(result, "dependency_reference") if item["target_name"] == "AUDITPOST")
        self.assertEqual(call["target_source_status"], "supplied_static_candidate")
        self.assertEqual(call["effect_status"], "candidate_not_execution_proof")
        dds = next(item for item in self.observations(result, "dds_pfile") if item["record_name"] == "ACCTREC")
        self.assertEqual(dds["physical_file_names"], ["ACCOUNTPF"])
        self.assertEqual(dds["file_binding_candidates"][0]["status"], "candidate")
        self.assertFalse(result["scope"]["lf_pf_identity_verified"])

    def test_missing_declaration_still_preserves_assignment_and_record_write_operand(self):
        result = collect_file_impact([page("fragment.cbl", "SUBTRACT DEBIT-AMOUNT FROM ACCTBAL.\nREWRITE ACCTREC.", 300)])
        self.assertEqual(self.observations(result, "field_assignment")[0]["written_fields"], ["ACCTBAL"])
        io = self.observations(result, "io_operation")[0]
        self.assertEqual(io["operand"], "ACCTREC")
        self.assertIsNone(io["file_name"])
        self.assertIn("record_file_binding_not_supplied_or_ambiguous", [item["reason"] for item in result["boundaries"]])

    def test_simple_copy_requires_unique_supplied_complete_declarations(self):
        host = SOURCE.replace("01 ACCTREC.\n 05 ACCTBAL PIC 9(7)V99.\n 05 ACCTSTAT PIC X.", "COPY ACCOUNTREC.")
        member = "01 ACCTREC.\n 05 ACCTBAL PIC 9(7)V99.\n 05 ACCTSTAT PIC X."
        supplied = [page("host.cbl", host), page("first/ACCOUNTREC.cpy", member)]
        unique = collect_file_impact(supplied)
        self.assertEqual(next(item for item in self.observations(unique, "io_operation") if item["operation"] == "REWRITE")["file_name"], "ACCOUNT-FILE")
        for extra in ([page("second/ACCOUNTREC.cpy", member)], []):
            pages = supplied + extra if extra else [supplied[0], page("first/ACCOUNTREC.cpy", member.rstrip("."))]
            result = collect_file_impact(pages)
            self.assertIsNone(next(item for item in self.observations(result, "io_operation") if item["operation"] == "REWRITE")["file_name"])
            self.assertIn("copy_record_membership_unresolved", [item["reason"] for item in result["boundaries"]])
        replacing = collect_file_impact([page("host.cbl", host.replace("COPY ACCOUNTREC.", "COPY ACCOUNTREC REPLACING ACCTBAL BY NEWBAL.")), supplied[1]])
        self.assertIn("copy_form_not_supported", [item["reason"] for item in replacing["boundaries"]])

    def test_gaps_and_conflicts_do_not_create_membership(self):
        whole = page("debit.cbl", SOURCE)
        lines = SOURCE.splitlines()
        prefix = page("debit.cbl", "\n".join(lines[:13]), source_sha256=whole["source_sha256"])
        suffix = page("debit.cbl", "\n".join(lines[20:]), 21, source_sha256=whole["source_sha256"])
        result = collect_file_impact([prefix, suffix])
        self.assertIsNone(next(item for item in self.observations(result, "io_operation") if item["operation"] == "REWRITE")["file_name"])
        self.assertEqual(self.observations(result, "field_assignment")[0]["record_memberships"], [])
        conflict = page("debit.cbl", SOURCE.replace("ACCTREC", "OTHERREC"), source_sha256=whole["source_sha256"])
        self.assertEqual(collect_file_impact([whole, conflict])["observations"], [])
        self.assertEqual(collect_file_impact([whole, {**whole, "span_truncated": True}])["observations"],
                         collect_file_impact([whole])["observations"])

    def test_literal_comment_marker_giving_and_fixed_format(self):
        source = SOURCE.replace("MOVE 'D' TO ACCTSTAT.", "MOVE '*>D' TO ACCTSTAT. *> REWRITE OTHERREC")
        source = source.replace("SUBTRACT DEBIT-AMOUNT FROM ACCTBAL.", "SUBTRACT DEBIT-AMOUNT FROM ACCTBAL GIVING NET-AMOUNT.")
        free = collect_file_impact([page("debit.cbl", source)])
        self.assertEqual(self.observations(free, "field_assignment")[0]["written_fields"], ["NET-AMOUNT"])
        self.assertEqual(self.observations(free, "field_assignment")[1]["written_fields"], ["ACCTSTAT"])
        fixed = "\n".join(f"{number:06} {line}" for number, line in enumerate(SOURCE.splitlines(), 1))
        result = collect_file_impact([page("debit.cbl", fixed)])
        self.assertEqual(next(item for item in self.observations(result, "io_operation") if item["operation"] == "REWRITE")["file_name"], "ACCOUNT-FILE")

    def test_inline_unsupported_forms_and_dds_literals_are_not_invented_facts(self):
        supplied = [page("fragment.cbl", "MOVE A TO B REWRITE OTHERREC.\nSUBTRACT FEE FROM BALANCE OF REC."),
                    page("test.dds", "     A*         R FALSE PFILE(FALSEPF)\n     A          R REC TEXT('PFILE(FALSEPF)')")]
        result = collect_file_impact(supplied)
        self.assertEqual(self.observations(result, "field_assignment"), [])
        self.assertEqual(self.observations(result, "dds_pfile"), [])
        self.assertEqual([item["reason"] for item in result["boundaries"]], ["assignment_form_not_supported"] * 2)

    def test_limits_are_explicit_and_incomplete_pages_are_excluded(self):
        result = collect_file_impact([page("debit.cbl", SOURCE)], max_observations=2)
        self.assertEqual(len(result["observations"]), 2)
        self.assertTrue(result["truncated"])
        self.assertIn("file_impact_budget_reached", [item["reason"] for item in result["boundaries"]])
        self.assertEqual(collect_file_impact([page("debit.cbl", SOURCE, end_line=500)])["observations"], [])
        self.assertTrue(collect_file_impact([page("debit.cbl", SOURCE)], max_source_chars=5)["truncated"])

    def test_missing_select_does_not_drop_fd_and_record_mapping_provenance(self):
        source = SOURCE.replace("SELECT ACCOUNT-FILE ASSIGN TO 'ACCOUNTLF'.\n", "")
        result = collect_file_impact([page("debit.cbl", source)])
        rewrite = next(item for item in self.observations(result, "io_operation") if item["operation"] == "REWRITE")
        self.assertEqual(rewrite["file_name"], "ACCOUNT-FILE")
        locations = rewrite["source_locations"]
        physical = source.splitlines()
        for text in ("FD ACCOUNT-FILE.", "01 ACCTREC.", " 05 ACCTBAL PIC 9(7)V99."):
            self.assertIn(physical.index(text) + 1, [location["start_line"] for location in locations])
        self.assertNotIn("assigned_name", rewrite)

    def test_duplicate_working_storage_name_makes_membership_candidate(self):
        source = SOURCE.replace("01 DEBIT-AMOUNT PIC 9(7)V99.", "01 DEBIT-AMOUNT PIC 9(7)V99.\n01 ACCTBAL PIC 9(7)V99.")
        result = collect_file_impact([page("debit.cbl", source)])
        change = next(item for item in self.observations(result, "field_assignment") if item["written_fields"] == ["ACCTBAL"])
        self.assertEqual(change["status"], "confirmed_source_syntax")
        self.assertEqual(change["record_memberships"][0]["status"], "candidate")
        self.assertEqual(change["record_memberships"][0]["declaration_candidate_count"], 2)

    def test_assignment_modifiers_are_never_treated_as_external_file_names(self):
        for clause, assigned in (("DATABASE 'ACCOUNTLF'", "ACCOUNTLF"), ("DISK ACCOUNTLF", "ACCOUNTLF"),
                                 ("DYNAMIC WS-NAME", None)):
            with self.subTest(clause=clause):
                source = SOURCE.replace("ASSIGN TO 'ACCOUNTLF'", "ASSIGN TO " + clause)
                result = collect_file_impact([page("debit.cbl", source)])
                declaration = next(item for item in self.observations(result, "file_declaration") if item["file_name"] == "ACCOUNT-FILE")
                self.assertEqual(declaration["assigned_name"], assigned)
                if assigned is None:
                    self.assertEqual(declaration["dynamic_assignment_symbol"], "WS-NAME")
                    self.assertIn("dynamic_or_incomplete_file_assignment", [item["reason"] for item in result["boundaries"]])

    def test_assignment_eof_and_fact_budget_need_a_statement_boundary(self):
        incomplete = collect_file_impact([page("fragment.cbl", "SUBTRACT DEBIT-AMOUNT FROM ACCTBAL", 200)])
        self.assertEqual(self.observations(incomplete, "field_assignment"), [])
        self.assertEqual(self.observations(incomplete, "assignment_candidate")[0]["status"], "candidate")
        self.assertIn("statement_continuation_not_supplied", [item["reason"] for item in incomplete["boundaries"]])
        complete = collect_file_impact([page("fragment.cbl", "SUBTRACT DEBIT-AMOUNT FROM ACCTBAL\nGOBACK.", 200)])
        self.assertEqual(self.observations(complete, "field_assignment")[0]["written_fields"], ["ACCTBAL"])
        continued = collect_file_impact([page("fragment.cbl", "SUBTRACT DEBIT-AMOUNT FROM ACCTBAL\nGIVING NET-AMOUNT.", 200)])
        self.assertEqual(self.observations(continued, "field_assignment")[0]["written_fields"], ["NET-AMOUNT"])
        oversized = collect_file_impact([page("fragment.cbl", "SUBTRACT DEBIT-AMOUNT FROM ACCTBAL\n" + " ROUNDED\n" * 63)])
        self.assertEqual(self.observations(oversized, "field_assignment"), [])

    def test_dds_unclosed_literal_sequence_columns_and_record_provenance(self):
        incomplete = collect_file_impact([page("test.dds", "     A          R REC TEXT('PFILE(FAKEPF)\n     A PFILE(ALSOFAKE)")])
        self.assertEqual(self.observations(incomplete, "dds_pfile"), [])
        self.assertIn("dds_literal_continuation_not_supported", [item["reason"] for item in incomplete["boundaries"]])
        result = collect_file_impact([page("test.dds", "00010A          R VIEWREC\n00020A                                     PFILE(REALPF)")])
        dds = self.observations(result, "dds_pfile")[0]
        self.assertEqual((dds["record_name"], dds["physical_file_names"]), ("VIEWREC", ["REALPF"]))
        self.assertEqual({location["start_line"] for location in dds["source_locations"]}, {1, 2})

    def test_dds_comment_apostrophe_does_not_hide_later_pfile(self):
        result = collect_file_impact([page("test.dds", "     A* Author's neutral note\n"
                                          "     A          R VIEWREC\n"
                                          "     A                                      PFILE(REALPF)")])
        dds = self.observations(result, "dds_pfile")[0]
        self.assertEqual((dds["record_name"], dds["physical_file_names"]), ("VIEWREC", ["REALPF"]))
        self.assertEqual({location["start_line"] for location in dds["source_locations"]}, {2, 3})
        self.assertNotIn("dds_literal_continuation_not_supported", [item["reason"] for item in result["boundaries"]])

    def test_repeated_fields_and_large_records_have_global_metadata_budgets(self):
        records = "\n".join(f"FD FILE-{i}.\n01 REC-{i}.\n 05 BAL PIC 9(7)V99." for i in range(1200))
        source = "IDENTIFICATION DIVISION.\nPROGRAM-ID. REPEATED.\nDATA DIVISION.\nFILE SECTION.\n" + records
        source += "\nPROCEDURE DIVISION.\n" + "MOVE 1 TO BAL.\n" * 120
        result = collect_file_impact([page("repeated.cbl", source)])
        changes = self.observations(result, "field_assignment")
        details = [detail for item in changes for detail in item["record_memberships"]]
        self.assertLessEqual(len(details), 160)
        self.assertTrue(all(len(item["record_memberships"]) <= 8 for item in changes))
        self.assertTrue(all(item["status"] == "candidate" for item in details))
        refs = sum(len(item["evidence_ids"]) + len(item["source_locations"]) for item in result["observations"] + details)
        self.assertLessEqual(refs, 640)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode("utf8")), 160000)
        self.assertTrue(result["truncated"])
        self.assertIn("field_membership_limit", [item["reason"] for item in result["boundaries"]])
        fields = "\n".join(f" 05 FIELD-{i} PIC 9." for i in range(1200))
        source = "FD ACCOUNT-FILE.\n01 ACCTREC.\n" + fields + "\nREWRITE ACCTREC.\n" * 100
        result = collect_file_impact([page("large-record.cbl", source)])
        self.assertTrue(all(len(item.get("fields", [])) <= 8 for item in result["observations"]))
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode("utf8")), 160000)
        self.assertTrue(result["truncated"])

    def test_metadata_byte_limit_is_independent_of_fact_count(self):
        result = collect_file_impact([page("debit.cbl", SOURCE)], max_metadata_bytes=2000)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode("utf8")), 2000)
        self.assertTrue(result["truncated"])
        self.assertIn("file_impact_metadata_byte_limit", [item["reason"] for item in result["boundaries"]])


if __name__ == "__main__":
    unittest.main()
