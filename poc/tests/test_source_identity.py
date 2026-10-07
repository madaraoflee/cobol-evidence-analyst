from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_chat import run_business_chat
from business_index import build_business_index
from business_map import build_business_map
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search, retrieve_repository_context
from repository_identity import resolve_source_identity


class SourceIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"

    def write_source(self, relative_path, program, body, *, linkage=False):
        destination = self.source / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        section = "LINKAGE" if linkage else "WORKING-STORAGE"
        destination.write_text(
            f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {program}.\n"
            f"DATA DIVISION.\n{section} SECTION.\n"
            "01 BASE-AMOUNT PIC 9(6) VALUE 100.\n"
            "01 OUTPUT-123 PIC 9(8).\n"
            f"PROCEDURE DIVISION.\nMAIN.\n{body}\nGOBACK.\n",
            encoding="utf-8",
        )

    def build(self):
        build_business_index(
            self.source, self.database, source_format="free", quiet=True,
            verify_content=True,
        )
        ensure_repository_search(self.database, self.source)

    def test_normalized_definition_lookup_preserves_case_and_avoids_unrelated_rows(self):
        self.write_source("entry.cbl", "MixedPlan", "COMPUTE CHARGE-VALUE = BASE-AMOUNT * 2.")
        self.build()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            # Older display text may retain source case; symbols still carry
            # the normalized definition key used by both index writers.
            connection.execute("UPDATE code_units SET name='MixedPlan' WHERE unit_type='Program'")

            def measure(question):
                work = []
                connection.set_progress_handler(lambda: work.append(100) or 0, 100)
                try:
                    result = resolve_source_identity(connection, question)
                finally:
                    connection.set_progress_handler(None, 0)
                return result, sum(work)

            questions = ("mixedplan", "billing notice", "charge-value")
            before = {question: measure(question) for question in questions}
            unit = connection.execute("SELECT * FROM code_units LIMIT 1").fetchone()
            rule = connection.execute("SELECT * FROM business_rules LIMIT 1").fetchone()
            symbol = connection.execute("SELECT * FROM symbols WHERE symbol_type='Field' LIMIT 1").fetchone()
            for index in range(12000):
                unit_row, rule_row, symbol_row = list(unit), list(rule), list(symbol)
                unit_row[0], unit_row[2], unit_row[3] = f"extra-unit-{index}", "Statement", "OTHER"
                rule_row[0] = f"extra-rule-{index}"
                symbol_row[0], symbol_row[3] = f"extra-symbol-{index}", "OTHER-FIELD"
                connection.execute("INSERT INTO code_units VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", unit_row)
                connection.execute("INSERT INTO business_rules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rule_row)
                connection.execute("INSERT INTO business_rule_fields VALUES (?,?,?)", (rule_row[0], "CHARGE-VALUE", "write"))
                connection.execute("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?)", symbol_row)
            for question in questions:
                with self.subTest(question=question):
                    result, work = measure(question)
                    self.assertEqual(result, before[question][0])
                    self.assertLessEqual(work, before[question][1] * 2 + 1000)
            self.assert_identity_result(before["mixedplan"][0], "resolved", ["entry.cbl"])
            self.assertEqual(before["charge-value"][0]["status"], "none")
            for query, values in (
                ("SELECT relative_path FROM symbols WHERE symbol_type='Program' AND name=?", ("MIXEDPLAN",)),
                ("SELECT 1 FROM business_rule_fields WHERE field_name=? LIMIT 1", ("CHARGE-VALUE",)),
            ):
                plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + query, values)]
                self.assertTrue(any("SEARCH" in detail for detail in plan), plan)
                self.assertFalse(any("SCAN" in detail for detail in plan), plan)

    def collision_fixture(self):
        self.write_source(
            "aa/target.cbl", "ALPHARULE",
            "COMPUTE OUTPUT-123 = BASE-AMOUNT * 2 + 11.",
        )
        self.write_source(
            "bb/target.cbl", "BETARULE",
            "COMPUTE OUTPUT-123 = BASE-AMOUNT * 3 + 29.",
        )
        self.write_source(
            "noise.cbl", "ONLYNOISE",
            "*> aa/target.cbl ALPHARULE BETARULE\nMOVE ZERO TO OUTPUT-123.",
        )
        self.build()

    def identity(self, question, search_terms=None):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            return resolve_source_identity(connection, question, search_terms=search_terms)

    def map_and_retrieve(self, question, **options):
        mapping = build_business_map(self.database, self.source, question, **options)
        context = retrieve_repository_context(
            self.database, self.source, question, max_pages=8, max_chars=32000,
            **options,
        )
        return mapping, context

    def assert_identity_result(self, identity, status, paths):
        self.assertEqual(identity["status"], status)
        self.assertEqual(set(identity["direct_paths"]), set(paths))
        self.assertIsInstance(identity["requested"], list)
        self.assertIsInstance(identity["candidates"], list)

    def assert_map_and_retrieval_identity(self, question, status, paths, **options):
        mapping, context = self.map_and_retrieve(question, **options)
        for result in (mapping, context):
            self.assert_identity_result(result["source_identity"], status, paths)
        self.assertEqual(set(mapping["direct_paths"]), set(paths))
        return mapping, context

    def test_complete_paths_select_only_the_requested_definition_and_formula(self):
        self.collision_fixture()
        for path, expected, other in (
            ("aa/target.cbl", "* 2 + 11", "* 3 + 29"),
            ("bb/target.cbl", "* 3 + 29", "* 2 + 11"),
        ):
            with self.subTest(path=path):
                question = f"请说明 {path} 的计算公式。"
                self.assert_identity_result(self.identity(question), "resolved", [path])
                mapping, context = self.assert_map_and_retrieval_identity(
                    question, "resolved", [path],
                )
                self.assertEqual(mapping["selected_paths"], [path])
                self.assertTrue(context["pages"])
                self.assertEqual({page["relative_path"] for page in context["pages"]}, {path})
                supplied = "\n".join(page["source_text"] for page in context["pages"])
                self.assertIn(expected, supplied)
                self.assertNotIn(other, supplied)

    def test_chinese_request_touching_a_qualified_path_keeps_its_exact_identity(self):
        self.collision_fixture()
        for path in ("aa/target.cbl", "bb/target.cbl"):
            with self.subTest(path=path):
                _, context = self.assert_map_and_retrieval_identity(
                    f"请说明{path}的计算公式", "resolved", [path])
                self.assertEqual({page["relative_path"] for page in context["pages"]}, {path})

    def test_percent_in_a_directory_is_preserved_and_never_selects_a_suffix_path(self):
        self.write_source("aa/target.cbl", "FIRSTPLAN", "COMPUTE OUTPUT-123 = BASE-AMOUNT + 11.")
        self.write_source("part%aa/target.cbl", "SECONDPLAN", "COMPUTE OUTPUT-123 = BASE-AMOUNT + 29.")
        self.build()
        _, context = self.assert_map_and_retrieval_identity(
            "Explain part%aa/target.cbl calculation", "resolved", ["part%aa/target.cbl"])
        self.assertEqual({page["relative_path"] for page in context["pages"]}, {"part%aa/target.cbl"})
        self.assertNotIn("+ 11", "\n".join(page["source_text"] for page in context["pages"]))
        _, context = self.assert_map_and_retrieval_identity(
            "Explain missing%aa/target.cbl calculation", "not_found", [])
        self.assertEqual(context["pages"], [])

    def test_parenthesized_directory_is_not_replaced_by_a_basename_match(self):
        self.write_source("aa/target.cbl", "FIRSTPLAN", "COMPUTE OUTPUT-123 = BASE-AMOUNT + 11.")
        self.write_source("foo(bar)/target.cbl", "SECONDPLAN", "COMPUTE OUTPUT-123 = BASE-AMOUNT + 29.")
        self.build()
        _, context = self.assert_map_and_retrieval_identity(
            "Explain foo(bar)/target.cbl formula", "resolved", ["foo(bar)/target.cbl"])
        self.assertEqual({page["relative_path"] for page in context["pages"]}, {"foo(bar)/target.cbl"})
        self.assertNotIn("+ 11", "\n".join(page["source_text"] for page in context["pages"]))
        _, context = self.assert_map_and_retrieval_identity(
            "Explain missing(bar)/target.cbl formula", "not_found", [])
        self.assertEqual(context["pages"], [])

    def test_percent_in_a_basename_is_not_replaced_by_a_filename_suffix(self):
        self.write_source("aa/target.cbl", "FIRSTPLAN", "COMPUTE OUTPUT-123 = BASE-AMOUNT + 11.")
        self.write_source("aa/part%target.cbl", "SECONDPLAN", "COMPUTE OUTPUT-123 = BASE-AMOUNT + 29.")
        self.build()
        _, context = self.assert_map_and_retrieval_identity(
            "Explain part%target.cbl formula", "resolved", ["aa/part%target.cbl"])
        self.assertEqual({page["relative_path"] for page in context["pages"]}, {"aa/part%target.cbl"})
        self.assertNotIn("+ 11", "\n".join(page["source_text"] for page in context["pages"]))
        _, context = self.assert_map_and_retrieval_identity(
            "Explain missing%target.cbl formula", "not_found", [])
        self.assertEqual(context["pages"], [])

    def test_section_matching_another_filename_stem_remains_a_symbol_query(self):
        self.write_source("owner.cbl", "SECTIONOWNER",
            "SECTION-ROUTE SECTION.\nLOCAL-PROCESS.\nCOMPUTE OUTPUT-123 = BASE-AMOUNT + 17.")
        self.write_source("SECTION-ROUTE.cbl", "OTHERPLAN", "CONTINUE.")
        self.build()
        question = "SECTION-ROUTE 的功能是什么？"
        self.assert_identity_result(self.identity(question), "none", [])
        mapping, _ = self.map_and_retrieve(question)
        self.assertEqual(mapping["source_identity"]["status"], "none")
        self.assertIn("owner.cbl", mapping["direct_paths"])

    def test_missing_complete_path_does_not_fall_back_to_same_basename_or_history(self):
        self.collision_fixture()
        question = "请说明 missing/target.cbl 的计算公式。"
        self.assert_identity_result(self.identity(question), "not_found", [])
        for prior_paths in ([], ["aa/target.cbl"]):
            with self.subTest(prior_paths=prior_paths):
                mapping, context = self.assert_map_and_retrieval_identity(
                    question, "not_found", [], prior_paths=prior_paths,
                )
                self.assertEqual(mapping["selected_paths"], [])
                self.assertEqual(context["pages"], [])

    def test_shared_basename_and_stem_are_ambiguous_and_supply_no_source_pages(self):
        self.collision_fixture()
        for identifier in ("target.cbl", "target"):
            with self.subTest(identifier=identifier):
                question = f"请说明 {identifier} 的计算公式。"
                identity = self.identity(question)
                self.assert_identity_result(identity, "ambiguous", [])
                candidate_paths = {
                    path for candidate in identity["candidates"]
                    for path in candidate["relative_paths"]
                }
                self.assertEqual(candidate_paths, {"aa/target.cbl", "bb/target.cbl"})
                mapping, context = self.assert_map_and_retrieval_identity(
                    question, "ambiguous", [],
                )
                self.assertEqual(mapping["selected_paths"], [])
                self.assertEqual(context["pages"], [])

    def test_duplicate_program_definitions_are_ambiguous_despite_comment_mentions(self):
        self.write_source("aa/first.cbl", "SHAREDRULE", "COMPUTE OUTPUT-123 = BASE-AMOUNT + 11.")
        self.write_source("bb/second.cbl", "SHAREDRULE", "COMPUTE OUTPUT-123 = BASE-AMOUNT + 29.")
        self.write_source("noise.cbl", "ONLYNOISE", "*> SHAREDRULE\nMOVE ZERO TO OUTPUT-123.")
        self.build()
        question = "请说明 SHAREDRULE 的计算公式。"
        identity = self.identity(question)
        self.assert_identity_result(identity, "ambiguous", [])
        self.assertEqual(
            {path for candidate in identity["candidates"] for path in candidate["relative_paths"]},
            {"aa/first.cbl", "bb/second.cbl"},
        )
        mapping, context = self.assert_map_and_retrieval_identity(question, "ambiguous", [])
        self.assertEqual(mapping["selected_paths"], [])
        self.assertEqual(context["pages"], [])

    def test_alphabetic_and_numeric_programs_resolve_definitions_not_callers_or_comments(self):
        self.write_source("alphabetic.cbl", "ALPHARULE", "COMPUTE OUTPUT-123 = BASE-AMOUNT + 17.")
        self.write_source("numeric.cbl", "710001", "COMPUTE OUTPUT-123 = BASE-AMOUNT + 31.")
        self.write_source(
            "caller.cbl", "CALLERONLY",
            "*> ALPHARULE 710001\nCALL 'ALPHARULE'.\nCALL '710001'.",
        )
        self.build()
        for program, path in (("ALPHARULE", "alphabetic.cbl"), ("710001", "numeric.cbl")):
            with self.subTest(program=program):
                question = f"请说明 {program} 的计算公式。"
                self.assert_identity_result(self.identity(question), "resolved", [path])
                mapping, context = self.assert_map_and_retrieval_identity(
                    question, "resolved", [path],
                )
                self.assertNotIn("caller.cbl", mapping["direct_paths"])
                self.assertIn(path, {page["relative_path"] for page in context["pages"]})

    def test_comment_only_program_mention_does_not_resolve_as_a_definition(self):
        self.write_source("noise.cbl", "ONLYNOISE", "*> MENTIONONLY\nMOVE ZERO TO OUTPUT-123.")
        self.build()
        identity = self.identity("请说明 MENTIONONLY 程序的计算公式。")
        self.assertEqual(identity["direct_paths"], [])
        self.assertNotEqual(identity["status"], "resolved")

    def test_numeric_hyphenated_field_keeps_ordinary_field_retrieval(self):
        self.write_source("field.cbl", "FIELDRULE", "COMPUTE OUTPUT-123 = BASE-AMOUNT * 4 + 43.")
        self.write_source("unrelated.cbl", "OTHERONLY", "CONTINUE.")
        self.build()
        question = "OUTPUT-123 的计算公式是什么？"
        self.assert_identity_result(self.identity(question), "none", [])
        mapping, context = self.map_and_retrieve(question)
        for result in (mapping, context):
            self.assert_identity_result(result["source_identity"], "none", [])
        self.assertIn("field.cbl", mapping["direct_paths"])
        self.assertIn("field.cbl", {page["relative_path"] for page in context["pages"]})
        self.assertTrue(any("* 4 + 43" in page["source_text"] for page in context["pages"]))

    def test_explicit_followup_path_uses_the_same_identity_resolution(self):
        self.collision_fixture()
        question = "请说明计算公式。"
        search_terms = ["bb/target.cbl"]
        self.assert_identity_result(self.identity(question, search_terms), "resolved", ["bb/target.cbl"])
        _, context = self.assert_map_and_retrieval_identity(
            question, "resolved", ["bb/target.cbl"], search_terms=search_terms,
        )
        self.assertEqual({page["relative_path"] for page in context["pages"]}, {"bb/target.cbl"})

    def test_followup_terms_do_not_replace_the_explicit_question_path(self):
        self.collision_fixture()
        question = "请说明 aa/target.cbl 的计算公式。"
        search_terms = ["bb/target.cbl", "OUTPUT-123"]
        self.assert_identity_result(self.identity(question, search_terms), "resolved", ["aa/target.cbl"])
        _, context = self.assert_map_and_retrieval_identity(
            question, "resolved", ["aa/target.cbl"], search_terms=search_terms,
        )
        self.assertEqual({page["relative_path"] for page in context["pages"]}, {"aa/target.cbl"})

    def test_ordinary_english_program_words_do_not_request_source_identity(self):
        self.collision_fixture()
        for question in ("What does this program calculate?", "What do programs use?"):
            with self.subTest(question=question):
                self.assert_identity_result(self.identity(question), "none", [])
                mapping, context = self.map_and_retrieve(question)
                for result in (mapping, context):
                    self.assert_identity_result(result["source_identity"], "none", [])

    def test_windows_relative_path_resolves_the_complete_normalized_path(self):
        self.collision_fixture()
        question = r"请说明 aa\target.cbl 的计算公式。"
        self.assert_identity_result(self.identity(question), "resolved", ["aa/target.cbl"])
        _, context = self.assert_map_and_retrieval_identity(question, "resolved", ["aa/target.cbl"])
        self.assertEqual({page["relative_path"] for page in context["pages"]}, {"aa/target.cbl"})

    def test_quoted_unicode_directory_with_space_resolves_the_exact_path(self):
        path = "资料 区/target.cbl"
        self.write_source(path, "UNICODERULE", "COMPUTE OUTPUT-123 = BASE-AMOUNT + 47.")
        self.write_source("bb/target.cbl", "BETARULE", "COMPUTE OUTPUT-123 = BASE-AMOUNT + 29.")
        self.write_source("noise.cbl", "ONLYNOISE", f"*> {path}\nCONTINUE.")
        self.build()
        question = f"请说明 `{path}` 的计算公式。"
        self.assert_identity_result(self.identity(question), "resolved", [path])
        _, context = self.assert_map_and_retrieval_identity(question, "resolved", [path])
        self.assertEqual({page["relative_path"] for page in context["pages"]}, {path})
        self.assertTrue(any("+ 47" in page["source_text"] for page in context["pages"]))

    def test_field_matching_filename_stem_remains_lexical(self):
        path = "OUTPUT-123.cbl"
        self.write_source(path, "FIELDRULE", "COMPUTE OUTPUT-123 = BASE-AMOUNT + 53.")
        self.write_source("other.cbl", "OTHERONLY", "MOVE ZERO TO OUTPUT-123.")
        self.build()
        field_question = "OUTPUT-123 的计算公式是什么？"
        self.assert_identity_result(self.identity(field_question), "none", [])
        mapping, context = self.map_and_retrieve(field_question)
        for result in (mapping, context):
            self.assert_identity_result(result["source_identity"], "none", [])
        self.assertIn(path, {page["relative_path"] for page in context["pages"]})

    def test_program_and_field_sharing_a_name_require_explicit_program_syntax(self):
        path = "OUTPUT-123.cbl"
        self.write_source(path, "OUTPUT-123", "COMPUTE OUTPUT-123 = BASE-AMOUNT + 53.")
        self.build()
        question = "OUTPUT-123 的计算公式是什么？"
        self.assert_identity_result(self.identity(question), "ambiguous", [])
        _, context = self.assert_map_and_retrieval_identity(question, "ambiguous", [])
        self.assertEqual(context["pages"], [])
        for program_question in (
            "请说明 PROGRAM-ID OUTPUT-123 的计算规则。",
            "What does program OUTPUT-123 calculate?",
        ):
            with self.subTest(question=program_question):
                self.assert_identity_result(self.identity(program_question), "resolved", [path])
                _, context = self.assert_map_and_retrieval_identity(program_question, "resolved", [path])
                self.assertIn(path, {page["relative_path"] for page in context["pages"]})
        field_question = "字段 OUTPUT-123 如何计算？"
        self.assert_identity_result(self.identity(field_question), "none", [])
        mapping, context = self.map_and_retrieve(field_question)
        for result in (mapping, context):
            self.assert_identity_result(result["source_identity"], "none", [])

    def test_linkage_field_definition_matching_a_stem_preserves_field_retrieval(self):
        path = "OUTPUT-123.cbl"
        self.write_source(path, "FIELDRULE", "CONTINUE.", linkage=True)
        self.build()
        question = "OUTPUT-123 如何传入？"
        self.assert_identity_result(self.identity(question), "none", [])
        mapping, context = self.map_and_retrieve(question)
        for result in (mapping, context):
            self.assert_identity_result(result["source_identity"], "none", [])
        self.assertIn(path, {page["relative_path"] for page in context["pages"]})

    def test_default_business_chat_sends_the_requested_formula_without_the_other_definition(self):
        self.collision_fixture()
        config = CompanyAPIConfig(
            "https://offline.example.invalid/v1", "neutral-model", api_key="offline-fake-key",
        )
        for path, expected, other in (
            ("aa/target.cbl", "* 2 + 11", "* 3 + 29"),
            ("bb/target.cbl", "* 3 + 29", "* 2 + 11"),
        ):
            with self.subTest(path=path):
                requests = []

                def transport(request):
                    envelope = json.loads(request.body)
                    self.assertNotIn("tools", envelope)
                    self.assertNotIn("response_format", envelope)
                    payload = json.loads(envelope["messages"][-1]["content"])
                    requests.append(payload)
                    pages = [page for bundle in payload["source_context"] for page in bundle.get("pages", [])]
                    target = next(page for page in pages if page["relative_path"] == path and expected in page["source_text"])
                    supplied = "\n".join(page["source_text"] for page in pages)
                    self.assertNotIn(other, supplied)
                    self.assertNotIn(other, json.dumps(payload, ensure_ascii=False))
                    self.assertEqual(payload["business_map"]["direct_paths"], [path])
                    answer = f"目标文件按 {expected} 计算输出。[{target['evidence_id']}]"
                    return TransportResponse(200, json.dumps({"choices": [{"message": {
                        "role": "assistant", "content": answer,
                    }, "finish_reason": "stop"}]}, ensure_ascii=False))

                outcome = run_business_chat(
                    f"请说明 {path} 的计算公式。", self.database, self.source, config,
                    transport=transport, allow_network=False,
                    framework_reference_path="",
                )
                self.assertTrue(requests)
                self.assertIn(expected, outcome["agent_result"]["answer"])
                self.assertNotIn(other, outcome["agent_result"]["answer"])

    def call_subject_fixture(self, *, duplicate_callee=False, duplicate_subject=False):
        self.write_source("main.cbl", "MAINJOB",
            "IF BASE-AMOUNT > 0\nCALL 'DETAILWRITE'\nEND-IF.")
        self.write_source("DETAILWRITE.CBL", "DETAILWRITE",
            "IF OUTPUT-123 = 7\nMOVE 99 TO OUTPUT-123\nEND-IF.")
        if duplicate_callee:
            self.write_source("other/DETAILWRITE.CBL", "DETAILWRITE", "CONTINUE.")
        if duplicate_subject:
            self.write_source("other/main.cbl", "MAINJOB", "CONTINUE.")
        self.build()

    def test_explicit_caller_remains_the_subject_when_callee_filename_is_clarified(self):
        self.call_subject_fixture(duplicate_callee=True)
        for question in (
            "MAINJOB 这个程序，什么情况会 skip record 写 DETAILWRITE?（调用的是 DETAILWRITE.CBL）",
            "MAINJOB 这个程序，什么情况会跳过调用 DETAILWRITE（调用的是 `DETAILWRITE.CBL`）",
            "MAINJOB 这个程序，什么情况会跳过调用 DETAILWRITE（调用的是 DETAILWRITE.CBL ）",
            "程序 MAINJOB 什么情况下会调用 DETAILWRITE（文件是 DETAILWRITE.CBL ）？",
            "When does program MAINJOB not call DETAILWRITE (file `DETAILWRITE.CBL`)?",
            "MAINJOB这个程序，什么情况下验证金额和状态后跳过写DETAILWRITE？（调用的是 `DETAILWRITE.CBL`）",
            "MAINJOB程序什么情况下调用DETAILWRITE？请详细说明跳过条件（调用的是 `DETAILWRITE.CBL`）",
            "MAINJOB程序什么情况下调用DETAILWRITE？请详细说明 MAINJOB 的跳过条件（调用的是 `DETAILWRITE.CBL`）",
        ):
            with self.subTest(question=question):
                mapping, context = self.assert_map_and_retrieval_identity(
                    question, "resolved", ["main.cbl"])
                self.assertEqual(mapping["source_identity"]["requested"], ["MAINJOB"])
                self.assertTrue(context["pages"])
                self.assertEqual({page["relative_path"] for page in context["pages"]}, {"main.cbl"})
                self.assertTrue(any("IF BASE-AMOUNT > 0" in page["source_text"] for page in context["pages"]))

    def test_explicit_caller_ambiguity_and_absence_do_not_fall_back_to_callee(self):
        self.call_subject_fixture(duplicate_subject=True)
        for name, status in (("MAINJOB", "ambiguous"), ("UNKNOWNJOB", "not_found")):
            with self.subTest(name=name):
                question = f"请分析 {name} 这个程序，它调用 DETAILWRITE（文件 `DETAILWRITE.CBL`）。"
                _, context = self.assert_map_and_retrieval_identity(question, status, [])
                self.assertEqual(context["pages"], [])
                self.assertEqual(context["source_identity"]["requested"], [name])

    def test_explicit_caller_keeps_stem_resolution_and_symbol_collision_protection(self):
        self.write_source("MAINJOB.cbl", "DIFFERENTENTRY",
            "IF BASE-AMOUNT > 0\nCALL 'DETAILWRITE'\nEND-IF.")
        self.write_source("DETAILWRITE.CBL", "DETAILWRITE", "CONTINUE.")
        self.build()
        question = "MAINJOB 这个程序，什么情况下调用 DETAILWRITE（文件 `DETAILWRITE.CBL`）？"
        self.assert_map_and_retrieval_identity(question, "resolved", ["MAINJOB.cbl"])
        self.write_source("field-owner.cbl", "MAINJOB", "MOVE 1 TO MAINJOB.")
        self.build()
        self.assert_map_and_retrieval_identity(question, "ambiguous", [])
        self.assert_map_and_retrieval_identity(
            "When does program MAINJOB call DETAILWRITE (file `DETAILWRITE.CBL`)?",
            "resolved", ["field-owner.cbl"])

    def test_relationship_mentions_do_not_shrink_comparisons_or_later_callee_questions(self):
        self.call_subject_fixture()
        for question in (
            "比较程序 MAINJOB 和程序 DETAILWRITE 调用 ENDSTEP 后的处理。",
            "请分析 main.cbl 和 DETAILWRITE.CBL 的调用关系。",
            "MAINJOB 和 DETAILWRITE 程序什么情况下调用 ENDSTEP？",
        ):
            with self.subTest(question=question):
                self.assert_identity_result(self.identity(question), "resolved", ["main.cbl", "DETAILWRITE.CBL"])
        for question in (
            "MAINJOB 程序调用的 `DETAILWRITE.CBL` 如何计算？",
            "MAINJOB 这个程序调用的 `DETAILWRITE.CBL` 如何计算？",
            "MAINJOB 这个程序调用 DETAILWRITE。请分析 `DETAILWRITE.CBL` 的输出。",
        ):
            with self.subTest(question=question):
                self.assert_map_and_retrieval_identity(question, "resolved", ["DETAILWRITE.CBL"])
        identity = self.identity("MAINJOB 调用的 DETAILWRITE 程序如何计算？")
        self.assertIn("DETAILWRITE.CBL", identity["direct_paths"])
        for question in (
            "主程序 MAINJOB 和它调用的 DETAILWRITE 分别做什么？",
            "程序 MAINJOB 调用 DETAILWRITE 后，DETAILWRITE 如何改写参数？",
            "不是程序 MAINJOB，请说明 DETAILWRITE 调用 ENDSTEP 的行为。",
        ):
            with self.subTest(question=question):
                identity = self.identity(question)
                self.assertNotEqual(identity.get("selection_basis"), "explicit_analysis_subject_before_call")

    def test_default_business_chat_receives_caller_source_for_call_condition_question(self):
        self.call_subject_fixture()
        requests = []

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            self.assertEqual(payload["business_map"]["direct_paths"], ["main.cbl"])
            self.assertEqual(payload["business_map"]["source_identity"]["status"], "resolved")
            self.assertEqual(payload["business_map"]["source_identity"]["selection_basis"],
                             "explicit_analysis_subject_before_call")
            self.assertEqual(payload["response_contract"]["call_analysis"]["primary_caller_paths"],
                             ["main.cbl"])
            pages = [page for bundle in payload["source_context"] for page in bundle.get("pages", [])]
            target = next(page for page in pages if page["relative_path"] == "main.cbl"
                          and "IF BASE-AMOUNT > 0" in page["source_text"])
            self.assertIn("CALL 'DETAILWRITE'", target["source_text"])
            self.assertIn("GOBACK.", target["source_text"])
            self.assertNotIn("IF OUTPUT-123 = 7", target["source_text"])
            links = [link for bundle in payload["source_context"]
                     for link in bundle.get("call_chain", {}).get("links", [])]
            self.assertTrue(any(link["caller_path"] == "main.cbl"
                                and link.get("target_path") == "DETAILWRITE.CBL" for link in links))
            answer = f"主程序在 BASE-AMOUNT 大于零时执行调用，否则跳过。[{target['evidence_id']}]"
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": answer}, "finish_reason": "stop"}]}, ensure_ascii=False))

        outcome = run_business_chat(
            "MAINJOB 这个程序，什么情况会跳过调用 DETAILWRITE（调用的是 `DETAILWRITE.CBL`）？",
            self.database, self.source,
            CompanyAPIConfig("https://offline.example.invalid/v1", "neutral-model", api_key="offline-fake-key"),
            transport=transport, allow_network=False, framework_reference_path="",
        )
        self.assertEqual(len(requests), 1)
        self.assertIn("BASE-AMOUNT", outcome["agent_result"]["answer"])


if __name__ == "__main__":
    unittest.main()
