"""Offline contracts for source-bound business relationships, not model scores."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_handoffs import FACT_GROUPS, retained_handoffs
from business_orientation import build_business_orientation, project_business_orientation
from business_index import build_business_index
from framework_semantics import build_framework_facts
from prompt_projection import project_prompt_payload
from test_business_orientation import BATCH, QUERY, SETTLEMENT, page


DDS = "     A          R ORDERREC                  PFILE(ORDERPF)\n"


class BusinessHandoffTests(unittest.TestCase):
    @staticmethod
    def provider_bytes(orientation):
        payload = project_prompt_payload({"business_analysis_brief": {
            "business_orientation": project_business_orientation(orientation)}})
        return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def project(self, pages, investigation=None, **kwargs):
        return build_business_orientation("从业务角度说明入口、程序交接和输入输出", investigation or {}, pages, **kwargs)

    def facts(self, orientation):
        return {row["orientation_id"]: row for group in FACT_GROUPS for row in orientation[group]}

    def assert_grounded(self, result):
        facts = self.facts(result)
        for edge in result["handoffs"]:
            self.assertTrue(edge["source_fact_ids"])
            self.assertLessEqual(set(edge["source_fact_ids"]), facts.keys())
            expected = {ref for key in edge["source_fact_ids"] for ref in facts[key]["supplied_reference_ids"]}
            self.assertEqual(set(edge["supplied_reference_ids"]), expected)
            for key in edge["source_fact_ids"]:
                self.assertTrue(facts[key]["source_locations"])
            self.assertLessEqual(set(edge["conditions_from_fact_ids"]), set(edge["source_fact_ids"]))
            self.assertFalse(edge["runtime_verified"])
            self.assertTrue(edge["limits"])

    def test_batch_program_record_and_dds_handoffs_have_scoped_meaning(self):
        result = self.project([page("nightjob.cl", BATCH), page("settle.cbl", SETTLEMENT), page("ORDERLF.dds", DDS)])
        self.assertFalse(result["truncated"])
        self.assert_grounded(result)
        edges = result["handoffs"]
        call, = [edge for edge in edges if edge["relation"] == "requests_program_call" and edge["to"]["name"] == "SETTLE"]
        self.assertEqual(call["from"]["source_path"], "nightjob.cl")
        self.assertEqual(call["implementation_candidate"]["source_path"], "settle.cbl")
        self.assertEqual(call["target_resolution"], "unique_supplied_program_name_candidate")
        job, = [edge for edge in edges if edge["relation"] == "submits_call"]
        self.assertEqual((job["from"]["name"], job["to"]["name"]), ("NIGHTRUN", "SETTLE"))
        self.assertIn("submitter_overrides_not_inherited_by_this_projection", job["limits"])
        read, = [edge for edge in edges if edge["relation"] == "reads_record_from"]
        self.assertEqual((read["from"]["name"], read["to"]["name"], read["to"]["assigned_name"]),
                         ("SETTLE", "ORDER-FILE", "ORDERLF"))
        self.assertEqual(read["record_names"], ["ORDER-REC"])
        write, = [edge for edge in edges if edge["relation"] == "writes_record_to"]
        self.assertEqual((write["to"]["name"], write["record_names"]), ("SETTLE-FILE", ["SETTLE-REC"]))
        guards = [guard for key in write["conditions_from_fact_ids"] for guard in self.facts(result)[key]["guards"]]
        self.assertEqual(guards[0]["condition"], "IF APPROVAL-STATUS = 'Y'")
        dds, = [edge for edge in edges if edge["relation"] == "dds_record_declares_pfile"]
        self.assertEqual((dds["from"]["name"], dds["to"]["name"]), ("ORDERREC", "ORDERPF"))
        self.assertEqual(dds["from"]["kind"], "dds_record")
        # A matching ORDERLF path never proves that the READ resolves this DDS.
        self.assertFalse(any(edge["from"].get("name") == "ORDERLF" and edge["to"].get("name") == "ORDERPF" for edge in edges))
        self.assertFalse(any("transform" in edge["relation"] or "derived_from" in edge["relation"] for edge in edges))

    def test_sql_handoff_keeps_query_inputs_and_results_in_the_origin(self):
        result = self.project([page("balance.cbl", QUERY)])
        edge, = [row for row in result["handoffs"] if row["relation"] == "reads_sql_table"]
        self.assertEqual((edge["from"]["name"], edge["to"]["name"]), ("BALANCEQ", "ACCOUNT_BALANCES"))
        statement = self.facts(result)[edge["statement_fact_id"]]["statement"]
        self.assertIn("BALANCE INTO :RESULT-AMOUNT", statement)
        self.assertIn("ACCOUNT_ID = :ACCOUNT-ID", statement)
        self.assertIn("deployment_mode_database_binding_and_sql_result_not_established", edge["limits"])
        self.assertNotIn("online", json.dumps(edge))
        self.assert_grounded(result)

    def test_sql_update_has_write_request_direction_not_success(self):
        query = QUERY.replace(" SELECT BALANCE INTO :RESULT-AMOUNT\n FROM ACCOUNT_BALANCES WHERE ACCOUNT_ID = :ACCOUNT-ID",
                              " UPDATE ACCOUNT_BALANCES SET BALANCE = :RESULT-AMOUNT WHERE ACCOUNT_ID = :ACCOUNT-ID")
        result = self.project([page("update.cbl", query)])
        edges = [row for row in result["handoffs"] if "sql_table" in row["relation"]]
        self.assertEqual([row["relation"] for row in edges], ["requests_sql_table_change"])
        self.assertFalse(edges[0]["runtime_verified"])

    def test_different_or_duplicate_program_names_do_not_become_resolved_targets(self):
        duplicated = self.project([page("nightjob.cl", BATCH), page("one.cbl", SETTLEMENT), page("two.cbl", SETTLEMENT)])
        call = next(row for row in duplicated["handoffs"] if row["relation"] == "requests_program_call" and row["to"]["name"] == "SETTLE")
        self.assertEqual(call["target_resolution"], "ambiguous_supplied_program_definitions")
        self.assertNotIn("implementation_candidate", call)
        absent = self.project([page("nightjob.cl", BATCH), page("SETTLE.cbl", QUERY)])
        call = next(row for row in absent["handoffs"] if row["relation"] == "requests_program_call")
        self.assertEqual(call["target_resolution"], "implementation_not_in_supplied_facts")
        self.assertNotIn("implementation_candidate", call)

    def test_dynamic_calls_overrides_and_sql_do_not_guess_objects(self):
        source = SETTLEMENT.replace("CALL 'RECORD-SERVICE'", "CALL TARGET-PROGRAM")
        batch = BATCH.replace("PGM(SETTLE)", "PGM(&TARGET)").replace("TOFILE(INPUTLIB/ORDERS)", "TOFILE(&INPUT)")
        query = QUERY.replace("SELECT BALANCE INTO :RESULT-AMOUNT\n FROM ACCOUNT_BALANCES WHERE ACCOUNT_ID = :ACCOUNT-ID",
                              "EXECUTE IMMEDIATE :ACCOUNT-ID")
        result = self.project([page("settle.cbl", source), page("nightjob.cl", batch), page("balance.cbl", query)])
        self.assertFalse([row for row in result["handoffs"] if row["relation"] in
                          {"requests_program_call", "submits_call", "requests_file_override", "reads_sql_table"}])

    def test_conditional_call_keeps_the_branch_with_its_edge(self):
        batch = "PGM\nIF COND(&MODE *EQ 'B') THEN(CALL PGM(SETTLE))\nENDPGM\n"
        result = self.project([page("entry.cl", batch), page("settle.cbl", SETTLEMENT)])
        edge = next(row for row in result["handoffs"] if row["relation"] == "requests_program_call" and row["to"]["name"] == "SETTLE")
        guard = self.facts(result)[edge["conditions_from_fact_ids"][0]]["guards"][0]
        self.assertEqual(guard["condition"], "&MODE *EQ 'B'")
        self.assertFalse(guard["outcome_evaluated"])

    def test_compile_conditional_call_does_not_promote_candidate_to_known_call(self):
        text = "IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRY.\nPROCEDURE DIVISION.\n>>IF FEATURE DEFINED\nCALL 'SERVICE'.\n>>END-IF\nGOBACK.\n"
        result = self.project([page("entry.cbl", text)])
        self.assertFalse(result["handoffs"])

    def test_compile_conditional_io_and_declaration_keep_their_directive(self):
        source = SETTLEMENT.replace(" WRITE SETTLE-REC", ">>IF FEATURE DEFINED\n WRITE SETTLE-REC.\n>>END-IF")
        result = self.project([page("settle.cbl", source)])
        edge, = [row for row in result["handoffs"] if row["relation"] == "writes_record_to"]
        self.assertIn("compilation_branch_not_resolved", edge["limits"])
        origin = next(self.facts(result)[key] for key in edge["conditions_from_fact_ids"]
                      if self.facts(result)[key].get("compilation_guards"))
        self.assertEqual(origin["compilation_guards"][0]["expression"], "FEATURE DEFINED")
        self.assertIn(22, [span["start_line"] for span in origin["source_locations"]])
        source = SETTLEMENT.replace("SELECT SETTLE-FILE ASSIGN TO 'SETTLEPF'.",
                                    ">>IF FEATURE DEFINED\nSELECT SETTLE-FILE ASSIGN TO 'SETTLEPF'.\n>>END-IF")
        result = self.project([page("settle.cbl", source)])
        edge, = [row for row in result["handoffs"] if row["relation"] == "writes_record_to"]
        self.assertIn("compilation_branch_not_resolved", edge["limits"])

    def test_adjacent_pages_keep_binding_but_missing_declaration_and_version_do_not(self):
        lines = SETTLEMENT.splitlines()
        head = page("settle.cbl", "\n".join(lines[:18]), full_text=SETTLEMENT, eid="head")
        tail = page("settle.cbl", "\n".join(lines[18:]), first=19, full_text=SETTLEMENT, eid="tail")
        complete = self.project([head, tail])
        writes = [edge for edge in complete["handoffs"] if edge["relation"] == "writes_record_to"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(set(writes[0]["supplied_reference_ids"]), {"head", "tail"})
        for pages in ([tail], [head, {**tail, "source_sha256": "0" * 64}],
                      [head, {**tail, "span_truncated": True}]):
            with self.subTest(pages=pages):
                result = self.project(pages)
                self.assertFalse([edge for edge in result["handoffs"] if edge["relation"] in {"reads_record_from", "writes_record_to"}])

    def test_exclusive_physical_scope_does_not_keep_a_removed_program_binding(self):
        source = page("settle.cbl", SETTLEMENT)
        scope = {"relative_path": "settle.cbl", "source_sha256": source["source_sha256"],
                 "start_line": 19, "end_line": 23, "exclusive": True}
        result = self.project([source], {"source_scope": {"ranges": [scope]}})
        self.assertFalse(result["handoffs"])
        self.assertTrue(result["visible_rules"])

    def test_metadata_trimming_removes_every_edge_with_a_removed_origin(self):
        pages = [page("nightjob.cl", BATCH), page("settle.cbl", SETTLEMENT), page("balance.cbl", QUERY), page("ORDERLF.dds", DDS)]
        for budget in (2000, 3000, 6000, 12000, 24000):
            with self.subTest(budget=budget):
                result = self.project(pages, max_metadata_bytes=budget)
                self.assertLessEqual(self.provider_bytes(result), budget)
                self.assert_grounded(result)
                self.assertEqual(retained_handoffs(result), result["handoffs"])
        original = self.project(pages)
        self.assertFalse(original["truncated"])
        self.assertEqual(len(original["handoffs"]), 8)
        for group in FACT_GROUPS:
            for fact in original[group]:
                pruned = deepcopy(original)
                pruned[group] = [row for row in pruned[group] if row["orientation_id"] != fact["orientation_id"]]
                for edge in retained_handoffs(pruned):
                    self.assertNotIn(fact["orientation_id"], edge["source_fact_ids"])

    def test_derived_handoffs_do_not_evict_original_facts_to_make_room(self):
        pages = [page("nightjob.cl", BATCH), page("settle.cbl", SETTLEMENT), page("balance.cbl", QUERY), page("ORDERLF.dds", DDS)]
        full = self.project(pages, max_metadata_bytes=100000)
        without_edges = deepcopy(full)
        without_edges["handoffs"] = []
        budget = len(json.dumps(without_edges, ensure_ascii=False).encode()) + 600
        compact = self.project(pages, max_metadata_bytes=budget)
        self.assertLess(len(compact["handoffs"]), len(full["handoffs"]))
        for group in FACT_GROUPS:
            self.assertEqual(compact[group], full[group])
        self.assertLessEqual(self.provider_bytes(compact), budget)

    def test_shared_copy_and_same_names_do_not_create_a_cross_program_dataflow(self):
        first = SETTLEMENT.replace("CALL 'RECORD-SERVICE' USING SETTLE-REC.", "")
        second = first.replace("PROGRAM-ID. SETTLE.", "PROGRAM-ID. ARCHIVE.")
        result = self.project([page("first.cbl", first), page("second.cbl", second)])
        for edge in result["handoffs"]:
            self.assertIn(edge["relation"], {"reads_record_from", "writes_record_to"})
            paths = {location["relative_path"] for key in edge["source_fact_ids"]
                     for location in self.facts(result)[key]["source_locations"]}
            self.assertEqual(paths, {edge["from"]["source_path"]})

    def test_copy_record_binding_requires_one_supplied_member_and_keeps_its_source(self):
        source = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. EXPORTER.\nENVIRONMENT DIVISION.\n"
                  "INPUT-OUTPUT SECTION.\nFILE-CONTROL.\nSELECT OUTPUT-FILE ASSIGN TO 'RESULTS'.\n"
                  "DATA DIVISION.\nFILE SECTION.\nFD OUTPUT-FILE.\nCOPY OUTPUTREC.\n"
                  "PROCEDURE DIVISION.\nWRITE OUTPUT-REC.\nGOBACK.\n")
        member = "01 OUTPUT-REC.\n05 OUTPUT-AMOUNT PIC 9(5).\n"
        host = page("exporter.cbl", source)
        copied = page("OUTPUTREC.cpy", member)
        result = self.project([host, copied])
        edge, = [row for row in result["handoffs"] if row["relation"] == "writes_record_to"]
        self.assertEqual((edge["from"]["name"], edge["to"]["name"]), ("EXPORTER", "OUTPUT-FILE"))
        paths = {span["relative_path"] for key in edge["source_fact_ids"]
                 for span in self.facts(result)[key]["source_locations"]}
        self.assertEqual(paths, {"exporter.cbl", "OUTPUTREC.cpy"})
        self.assert_grounded(result)
        for pages in ([host], [host, copied, page("other/OUTPUTREC.cpy", member)],
                      [host, {**copied, "span_truncated": True}]):
            with self.subTest(pages=pages):
                result = self.project(pages)
                self.assertFalse([row for row in result["handoffs"] if row["relation"] == "writes_record_to"])

    def test_repeated_record_names_in_one_file_remain_with_their_program(self):
        first = SETTLEMENT.replace("CALL 'RECORD-SERVICE' USING SETTLE-REC.", "")
        second = first.replace("PROGRAM-ID. SETTLE.", "PROGRAM-ID. ARCHIVE.")
        second = second.replace("'ORDERLF'", "'ARCHIVEIN'").replace("'SETTLEPF'", "'ARCHIVEOUT'")
        result = self.project([page("combined.cbl", first + "END PROGRAM SETTLE.\n" + second + "END PROGRAM ARCHIVE.\n")],
                              max_metadata_bytes=100000)
        values = {(edge["from"]["name"], edge["relation"], edge["to"]["assigned_name"])
                  for edge in result["handoffs"]}
        self.assertEqual(values, {("SETTLE", "reads_record_from", "ORDERLF"),
                                  ("SETTLE", "writes_record_to", "SETTLEPF"),
                                  ("ARCHIVE", "reads_record_from", "ARCHIVEIN"),
                                  ("ARCHIVE", "writes_record_to", "ARCHIVEOUT")})
        self.assert_grounded(result)

    def test_framework_handoff_requires_the_exact_call_and_current_manual(self):
        from test_framework_semantics import MANUAL, SOURCE
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "entry.cbl").write_bytes(SOURCE.encode())
            manual = root / "manual.md"
            manual.write_bytes(MANUAL.encode())
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", quiet=True, framework_reference_path=manual)
            pages = [page("entry.cbl", SOURCE)]
            framework = build_framework_facts(database, pages, reference_path=manual)
            result = self.project(pages, framework_facts=framework["facts"], framework_references=framework["references"])
            edge, = [row for row in result["handoffs"] if row["relation"] == "requests_documented_framework_operation"]
            self.assertEqual((edge["from"]["name"], edge["to"]["name"], edge["argument"]), ("ROWSIO", "ADVANCE", "ROWS-PARAMS"))
            self.assertEqual(edge["basis"], "source_bound_supplied_framework_convention")
            self.assert_grounded(result)
            for supplied, refs in ((pages, []), ([], framework["references"]),
                                   (pages, [{**row, "text_truncated": True} for row in framework["references"]])):
                clipped = self.project(supplied, framework_facts=framework["facts"], framework_references=refs)
                self.assertFalse([row for row in clipped["handoffs"] if row["relation"] == "requests_documented_framework_operation"])
            wrong = deepcopy(framework["facts"])
            wrong[0]["source_ranges"][0]["start_line"] -= 1
            result = self.project(pages, framework_facts=wrong, framework_references=framework["references"])
            self.assertFalse([row for row in result["handoffs"] if row["relation"] == "requests_documented_framework_operation"])

    def test_wire_projection_shares_only_repeated_source_versions_losslessly(self):
        pages = [page("nightjob.cl", BATCH), page("settle.cbl", SETTLEMENT), page("balance.cbl", QUERY)]
        canonical = self.project(pages)
        frozen = deepcopy(canonical)
        projected = project_business_orientation(canonical)
        self.assertEqual(canonical, frozen)
        self.assertEqual(projected["handoffs"], canonical["handoffs"])
        self.assertLess(len(json.dumps(projected).encode()), len(json.dumps(canonical).encode()))
        self.assertIn("source_versions[relative_path]", projected["source_location_version_binding"])
        restored = deepcopy(projected)
        versions = restored.pop("source_versions")
        restored.pop("source_location_version_binding")
        self.assertEqual(versions, {row["relative_path"]: row["source_sha256"] for row in pages})
        for group in FACT_GROUPS:
            for fact in restored[group]:
                for location in fact["source_locations"]:
                    self.assertNotIn("source_sha256", location)
                    location["source_sha256"] = versions[location["relative_path"]]
        self.assertEqual(restored, canonical)
        self.assert_grounded(restored)
        self.assertIs(project_business_orientation(projected), projected)

    def test_wire_projection_preserves_ambiguous_or_custom_bindings_verbatim(self):
        canonical = self.project([page("settle.cbl", SETTLEMENT)])
        variants = []
        conflicting = deepcopy(canonical)
        conflicting["data_roles"][0]["source_locations"][0]["source_sha256"] = "0" * 64
        variants.append(conflicting)
        missing = deepcopy(canonical)
        del missing["programs"][0]["source_locations"][0]["source_sha256"]
        variants.append(missing)
        custom = deepcopy(canonical)
        custom["version"] = "custom-orientation/v1"
        variants.append(custom)
        for item in variants:
            with self.subTest(item=item):
                frozen = deepcopy(item)
                self.assertIs(project_business_orientation(item), item)
                self.assertEqual(item, frozen)

    def test_handoffs_are_bounded_deterministic_and_need_no_io_or_model(self):
        pages = [page("settle.cbl", SETTLEMENT), page("nightjob.cl", BATCH)]
        with patch("builtins.open", side_effect=AssertionError("unexpected file access")), \
             patch("sqlite3.connect", side_effect=AssertionError("unexpected database access")), \
             patch("socket.socket.connect", side_effect=AssertionError("unexpected network access")):
            original = self.project(pages)
            self.assertEqual(original, self.project(pages))
            bounded = self.project(pages, max_items=1)
        self.assertLessEqual(len(bounded["handoffs"]), 1)
        self.assertTrue(bounded["truncated"])
        self.assert_grounded(bounded)


if __name__ == "__main__":
    unittest.main()
