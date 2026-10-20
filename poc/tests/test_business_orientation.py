from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_orientation import build_business_orientation, _cached_parse_run
from business_index import build_business_index
from batch_context_evidence import investigate_batch_context
from framework_semantics import build_framework_facts


SETTLEMENT = """*> Purpose: prepare settlement output for approved orders.
IDENTIFICATION DIVISION.
PROGRAM-ID. SETTLE.
ENVIRONMENT DIVISION.
INPUT-OUTPUT SECTION.
FILE-CONTROL.
SELECT ORDER-FILE ASSIGN TO 'ORDERLF'.
SELECT SETTLE-FILE ASSIGN TO 'SETTLEPF'.
DATA DIVISION.
FILE SECTION.
FD ORDER-FILE.
01 ORDER-REC.
 05 ORDER-AMOUNT PIC 9(5).
 05 APPROVAL-STATUS PIC X.
FD SETTLE-FILE.
01 SETTLE-REC.
 05 SETTLE-AMOUNT PIC 9(5).
PROCEDURE DIVISION.
READ ORDER-FILE.
IF APPROVAL-STATUS = 'Y'
 COMPUTE SETTLE-AMOUNT = ORDER-AMOUNT * 2 END-COMPUTE
 WRITE SETTLE-REC
END-IF.
CALL 'RECORD-SERVICE' USING SETTLE-REC.
GOBACK.
"""
BATCH = """PGM
OVRDBF FILE(ORDERLF) TOFILE(INPUTLIB/ORDERS)
SBMJOB CMD(CALL PGM(SETTLE)) JOB(NIGHTRUN)
ENDPGM
"""
QUERY = """*> Function: query account balance for an operator.
IDENTIFICATION DIVISION.
PROGRAM-ID. BALANCEQ.
DATA DIVISION.
LINKAGE SECTION.
01 ACCOUNT-ID PIC X(8).
01 RESULT-AMOUNT PIC 9(6).
PROCEDURE DIVISION USING ACCOUNT-ID RESULT-AMOUNT.
EXEC SQL
 SELECT BALANCE INTO :RESULT-AMOUNT
 FROM ACCOUNT_BALANCES WHERE ACCOUNT_ID = :ACCOUNT-ID
END-EXEC.
GOBACK.
"""


def page(path, text, *, eid=None, first=1, last=None, full_text=None):
    return {"relative_path": path, "source_text": text, "start_line": first,
            "end_line": last if last is not None else first + len(text.splitlines()) - 1,
            "evidence_id": eid or "ev-" + path, "format_hint": "free",
            "source_sha256": hashlib.sha256((full_text or text).encode()).hexdigest()}


class BusinessOrientationTests(unittest.TestCase):
    def project(self, pages, investigation=None, **kwargs):
        return build_business_orientation("请从业务目的说明整体流程", investigation or {}, pages, **kwargs)

    def test_batch_program_file_chain_retains_physical_evidence_and_limits(self):
        pages = [page("nightjob.cl", BATCH), page("settle.cbl", SETTLEMENT),
                 page("ORDERLF.dds", "     A          R ORDERREC                  PFILE(ORDERPF)\n")]
        result = self.project(pages)
        self.assertEqual(result["purpose_candidates"][0]["text"],
                         "Purpose: prepare settlement output for approved orders.")
        job, = [row for row in result["entries"] if row.get("command") == "SBMJOB"]
        call, = [row for row in result["relationships"] if row.get("command") == "CALL"]
        self.assertEqual((job["job_name"], call["target_name"]), ("NIGHTRUN", "SETTLE"))
        self.assertEqual(call["command_context"], "submitted_job_candidate")
        self.assertEqual(call["source_locations"][0]["start_line"], 3)
        self.assertFalse(job["inherited_overrides_verified"])
        self.assertFalse(call["runtime_execution_verified"])
        self.assertEqual([row["program_name"] for row in result["programs"]], ["SETTLE"])
        operations = {row["operation"]: row for row in result["data_roles"] if row["kind"] == "io_operation"}
        self.assertEqual((operations["READ"]["assigned_name"], operations["READ"]["access_role"]),
                         ("ORDERLF", "read_only_dependency"))
        self.assertEqual((operations["WRITE"]["file_name"], operations["WRITE"]["access_role"]),
                         ("SETTLE-FILE", "direct_write"))
        dds, = [row for row in result["data_roles"] if row["kind"] == "dds_pfile"]
        self.assertEqual(dds["physical_file_names"], ["ORDERPF"])
        self.assertNotIn("file_binding_candidates", dds)
        formula, = [row for row in result["visible_rules"] if row.get("statement_kind") == "COMPUTE"]
        self.assertEqual(formula["expression"], ["ORDER-AMOUNT", "*", "2"])
        self.assertEqual(formula["guards"][0]["condition"], "IF APPROVAL-STATUS = 'Y'")
        self.assertEqual({location["start_line"] for location in formula["source_locations"]}, {20, 21})
        self.assertTrue(any(row["reason"] == "called_implementation_not_in_supplied_projection"
                            and row["target_name"] == "RECORD-SERVICE" for row in result["unknowns"]))
        self.assertFalse(result["scope"]["semantic_execution_verified"])
        self.assertIn("visible_rules", result["available_facets"])
        for group in ("entries", "programs", "relationships", "data_roles", "visible_rules", "purpose_candidates"):
            for row in result[group]:
                self.assertTrue(row["supplied_reference_ids"])
                for location in row["source_locations"]:
                    source = next(p for p in pages if p["relative_path"] == location["relative_path"])
                    self.assertEqual(location["source_sha256"], source["source_sha256"])
                    self.assertLessEqual(location["end_line"], source["end_line"])

    def test_documented_query_entry_has_sql_read_role_without_online_deployment_claim(self):
        result = self.project([page("balance.cbl", QUERY)])
        entry, = result["entries"]
        self.assertIn("USING ACCOUNT-ID RESULT-AMOUNT", entry["statement"])
        self.assertEqual(entry["execution_mode"], "unknown")
        self.assertFalse(entry["online_or_batch_verified"])
        role, = [row for row in result["data_roles"] if row["kind"] == "sql_table_access"]
        self.assertEqual((role["table_name"], role["access_role"]), ("ACCOUNT_BALANCES", "read"))
        self.assertEqual((role["source_locations"][0]["start_line"], role["source_locations"][0]["end_line"]), (9, 12))
        self.assertEqual(result["purpose_candidates"][0]["interpretation_level"], "source_documentation")
        self.assertFalse(result["scope"]["business_purpose_verified"])

    def test_static_sql_update_delete_and_dynamic_sql_are_distinguished(self):
        text = QUERY.replace(" SELECT BALANCE INTO :RESULT-AMOUNT\n FROM ACCOUNT_BALANCES WHERE ACCOUNT_ID = :ACCOUNT-ID",
                             " DELETE FROM ACCOUNT_BALANCES WHERE ACCOUNT_ID = :ACCOUNT-ID")
        result = self.project([page("delete.cbl", text)])
        roles = [row for row in result["data_roles"] if row["kind"] == "sql_table_access"]
        self.assertEqual([(row["table_name"], row["access_role"]) for row in roles],
                         [("ACCOUNT_BALANCES", "write_request")])
        dynamic = text.replace("DELETE FROM ACCOUNT_BALANCES WHERE ACCOUNT_ID = :ACCOUNT-ID",
                               "EXECUTE IMMEDIATE :ACCOUNT-ID")
        self.assertFalse(self.project([page("dynamic.cbl", dynamic)])["data_roles"])

    def test_generic_comments_and_names_do_not_invent_business_purpose(self):
        text = QUERY.replace("*> Function: query account balance for an operator.",
                             "*> neutral padding line 12\n*> TODO\n*> IF FLAG = Y")
        result = self.project([page("online_balance_business.cbl", text)])
        self.assertFalse(result["purpose_candidates"])
        self.assertNotIn("business_purpose", result["available_facets"])
        self.assertEqual(result["entries"][0]["execution_mode"], "unknown")

    def test_clipping_rebuild_drops_removed_purpose_entry_and_call_evidence(self):
        complete = [page("nightjob.cl", BATCH), page("settle.cbl", SETTLEMENT)]
        original = self.project(complete)
        self.assertTrue(original["purpose_candidates"])
        lines = SETTLEMENT.splitlines()
        trimmed = page("settle.cbl", "\n".join(lines[19:23]), first=20, full_text=SETTLEMENT, eid="ev-rule")
        result = self.project([trimmed])
        self.assertFalse(result["purpose_candidates"])
        self.assertFalse(result["entries"])
        self.assertFalse(result["relationships"])
        self.assertTrue(result["visible_rules"])
        self.assertTrue(all(row["supplied_reference_ids"] == ["ev-rule"] for row in result["visible_rules"]))

    def test_version_conflict_changed_version_and_incomplete_pages_supply_no_facts(self):
        original = page("balance.cbl", QUERY)
        for pages, options in (
            ([original, {**original, "source_sha256": "0" * 64}], {}),
            ([original], {"business_map": {"source_versions": {"balance.cbl": "0" * 64}}}),
            ([{**original, "span_truncated": True}], {}),
            ([{**original, "end_line": original["end_line"] + 1}], {}),
            ([original, {**original, "source_text": QUERY.replace("BALANCE INTO", "BALANCE + 1 INTO")}], {}),
        ):
            with self.subTest(pages=pages, options=options):
                result = self.project(pages, **options)
                self.assertFalse(result["available_facets"])

    def test_adjacent_pages_preserve_sql_span_but_missing_middle_line_cannot(self):
        lines = QUERY.splitlines()
        first = page("balance.cbl", "\n".join(lines[:10]), eid="ev-head", full_text=QUERY)
        tail = page("balance.cbl", "\n".join(lines[10:]), first=11, eid="ev-tail", full_text=QUERY)
        result = self.project([first, tail])
        sql, = [row for row in result["data_roles"] if row["kind"] == "sql_table_access"]
        self.assertEqual(sql["supplied_reference_ids"], ["ev-head", "ev-tail"])
        missing = {**tail, "source_text": "\n".join(lines[11:]), "start_line": 12}
        self.assertFalse([row for row in self.project([first, missing])["data_roles"]
                          if row["kind"] == "sql_table_access"])

    def test_exclusive_scope_does_not_restore_outside_material_or_wrong_version(self):
        source = page("balance.cbl", QUERY)
        scope = {"relative_path": "balance.cbl", "source_sha256": source["source_sha256"],
                 "start_line": 9, "end_line": 12, "exclusive": True}
        result = self.project([source], {"source_scope": {"ranges": [scope]}})
        self.assertFalse(result["purpose_candidates"])
        self.assertFalse(result["entries"])
        self.assertEqual(len(result["data_roles"]), 1)
        bad = {**scope, "source_sha256": "0" * 64}
        self.assertFalse(self.project([source], {"source_scope": {"ranges": [bad]}})["available_facets"])

    def test_indexed_cl_context_requires_same_captured_statement_and_current_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "nightjob.cl").write_text(BATCH)
            (source / "settle.cbl").write_text(SETTLEMENT)
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", quiet=True)
            cl = page("nightjob.cl", "\n".join(BATCH.splitlines()[1:3]), first=2, full_text=BATCH)
            mapped = {"direct_paths": ["settle.cbl"], "selected_paths": ["settle.cbl"],
                      "programs": [{"program_names": ["SETTLE"], "relative_path": "settle.cbl"}]}
            batch = investigate_batch_context(mapped, [cl], database_path=database)
            result = self.project([cl], batch)
            self.assertTrue([row for row in result["relationships"] if row.get("target_name") == "SETTLE"])
            wrong = {**cl, "source_text": cl["source_text"].replace("PGM(SETTLE)", "PGM(OTHERJOB)")}
            self.assertFalse(self.project([wrong], batch)["relationships"])
            with closing(sqlite3.connect(database)) as db:
                self.assertGreater(db.execute("SELECT COUNT(*) FROM cl_command_facts").fetchone()[0], 0)

    def test_projection_is_bounded_and_does_not_scan_files_or_open_network(self):
        sources = [page("balance.cbl", QUERY), page("settle.cbl", SETTLEMENT), page("nightjob.cl", BATCH)]
        with patch("builtins.open", side_effect=AssertionError("unexpected file access")), \
             patch("sqlite3.connect", side_effect=AssertionError("unexpected index lookup")), \
             patch("socket.socket.connect", side_effect=AssertionError("unexpected network")):
            result = self.project(sources, max_items=2, max_metadata_bytes=2400)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 2400)
        self.assertTrue(result["truncated"])
        for group in ("entries", "programs", "relationships", "data_roles", "visible_rules", "purpose_candidates"):
            self.assertLessEqual(len(result[group]), 2)
            for row in result[group]:
                self.assertTrue(row["evidence_ids"])
                self.assertTrue(row["source_locations"])
        empty = self.project(sources, max_source_chars=1)
        self.assertFalse(empty["available_facets"])
        self.assertTrue(empty["truncated"])

    def test_parse_cache_is_bounded_and_matches_content_version_path_range_and_format(self):
        import business_orientation
        source = page("balance.cbl", QUERY)
        _cached_parse_run.cache_clear()
        self.addCleanup(_cached_parse_run.cache_clear)
        with patch("business_orientation.parse_document", wraps=business_orientation.parse_document) as parser:
            first = self.project([source])
            self.assertEqual(self.project([source]), first)
            self.assertEqual(parser.call_count, 1)
            for changed in ({**source, "source_sha256": "1" * 64},
                            {**source, "relative_path": "other.cbl"},
                            {**source, "start_line": 11, "end_line": source["end_line"] + 10},
                            {**source, "format_hint": "auto"},
                            {**source, "source_text": QUERY.replace("account balance", "current balance")}):
                before = parser.call_count
                self.project([changed])
                self.assertEqual(parser.call_count, before + 1)
            for offset in range(10):
                self.project([{**source, "relative_path": f"query{offset}.cbl"}])
            self.assertEqual(_cached_parse_run.cache_info().currsize, 8)
        large = QUERY + ("*> neutral padding\n" * 7500)
        with patch("business_orientation._cached_parse_run", side_effect=AssertionError("oversized cached source")):
            result = self.project([page("large.cbl", large)])
        self.assertTrue(result["programs"])
        captured = page("settle.cbl", SETTLEMENT)
        with patch("business_orientation.supplied_statement_facts",
                   wraps=business_orientation.supplied_statement_facts) as project_statements:
            initial = self.project([captured])
            formula = next(row for row in initial["visible_rules"] if row.get("statement_kind") == "COMPUTE")
            formula["expression"].append("CHANGED")
            repeated = self.project([captured])
            self.assertEqual(project_statements.call_count, 1)
            repeated_formula = next(row for row in repeated["visible_rules"] if row.get("statement_kind") == "COMPUTE")
            self.assertNotIn("CHANGED", repeated_formula["expression"])

    def test_framework_convention_requires_visible_source_and_manual_together(self):
        from test_framework_semantics import MANUAL, SOURCE
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "entry.cbl").write_text(SOURCE)
            manual = root / "manual.md"
            manual.write_text(MANUAL)
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", quiet=True, framework_reference_path=manual)
            pages = [page("entry.cbl", SOURCE)]
            framework = build_framework_facts(database, pages, reference_path=manual)
            refs = framework["references"]
            facts = framework["facts"]
            self.assertTrue(facts)
            result = self.project(pages, framework_facts=facts, framework_references=refs)
            conventions = [row for row in result["visible_rules"] if row["interpretation_level"] == "framework_convention"]
            self.assertEqual(conventions[0]["operation"]["value"], "ADVANCE")
            self.assertFalse(conventions[0]["runtime_verified"])
            self.assertTrue(set(facts[0]["reference_ids"]) <= set(conventions[0]["supplied_reference_ids"]))
            for supplied, references in (([], refs), (pages, []), (pages, [{**ref, "text_truncated": True} for ref in refs])):
                clipped = self.project(supplied, framework_facts=facts, framework_references=references)
                self.assertFalse([row for row in clipped["visible_rules"] if row["interpretation_level"] == "framework_convention"])


if __name__ == "__main__":
    unittest.main()
