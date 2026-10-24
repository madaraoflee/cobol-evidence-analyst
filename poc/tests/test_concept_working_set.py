"""Offline concept-root traversal and exact physical source delivery."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_index import build_business_index
from business_chat import run_business_chat
from business_map import build_business_map
from complete_working_set import build_complete_working_set
from company_api import APIClientError, CompanyAPIConfig
from repository_discovery import ensure_repository_search
from source_session import QuestionSourceSession
from question_investigation import build_question_investigation, _business_source_reads


def program(name, body="CONTINUE.", *, purpose="", data="01 TOTAL PIC 9(7)V99."):
    return (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\n"
            + (f"*> Purpose: {purpose}.\n" if purpose else "")
            + f"DATA DIVISION.\nWORKING-STORAGE SECTION.\n{data}\n"
              f"PROCEDURE DIVISION.\nMAIN.\n{body}\nGOBACK.\n")


class ConceptWorkingSetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.files = {}
        for owner, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                            (socket, "create_connection")):
            patcher = mock.patch.object(owner, name,
                side_effect=AssertionError("network forbidden in concept source regression"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self, files):
        self.files = files
        for relative, text in files.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free",
                             quiet=True, verify_content=True)
        ensure_repository_search(self.database, self.source)

    def mapping(self, question="什么是 renewal projection"):
        return build_business_map(self.database, self.source, question)

    def collect(self, mapping=None, *, overview=True, policy=None):
        mapping = self.mapping() if mapping is None else mapping
        original = deepcopy(mapping)
        with QuestionSourceSession(self.database, self.source) as session:
            result = build_complete_working_set(self.database, session, mapping,
                policy or AgentPolicy(), business_overview=overview)
        self.assertEqual(mapping, original, "reading must not resolve a concept into program identity")
        return result

    def assert_sources(self, result, expected):
        pages = result["pages"]
        self.assertEqual({page["relative_path"] for page in pages}, set(expected))
        self.assertEqual(len(pages), len(expected), "cycles and duplicate references must not reread")
        for page in pages:
            path = page["relative_path"]
            raw = self.files[path].encode("utf-8")
            lines = self.files[path].splitlines()
            self.assertEqual(page["source_text"], "\n".join(lines))
            self.assertEqual(page["source_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual((page["start_line"], page["end_line"]), (1, len(lines)))
            self.assertFalse(page["span_truncated"])

    def test_concept_and_program_name_both_supply_multihop_original_source(self):
        self.build({
            "entry.cbl": program("ENTRY-RULE", 'CALL "CHILD-RULE" USING TOTAL.',
                purpose="renewal projection", data="COPY shared-values.\n01 TOTAL PIC 9(7)V99."),
            "child.cbl": program("CHILD-RULE", 'CALL "FINAL-RULE" USING TOTAL.'),
            "final.cbl": program("FINAL-RULE", "COMPUTE TOTAL = TOTAL * 1.25."),
            "shared-values.cpy": "01 FACTOR PIC 9V99 VALUE 1.25.\n",
            "unrelated.cbl": program("UNRELATED", "MOVE 1 TO TOTAL."),
        })
        concept = self.mapping()
        self.assertEqual(concept["source_identity"]["status"], "none")
        self.assertEqual(concept["direct_paths"], ["entry.cbl"])
        expected = {"entry.cbl", "child.cbl", "final.cbl", "shared-values.cpy"}
        result = self.collect(concept)
        self.assert_sources(result, expected)
        self.assertTrue(result["metadata"]["physical_complete"])
        self.assertFalse(result["metadata"]["source_identity_resolved"])
        self.assert_sources(self.collect(self.mapping("ENTRY-RULE 是什么")), expected)

    def test_concept_mode_requires_overview_and_does_not_override_ambiguous_identity(self):
        self.build({"entry.cbl": program("ENTRY-RULE", purpose="renewal projection")})
        mapping = self.mapping()
        self.assertEqual(self.collect(mapping, overview=False)["metadata"]["status"], "not_applicable")
        mapping["source_identity"].update(status="ambiguous", kind="program")
        self.assertEqual(self.collect(mapping)["metadata"]["status"], "not_applicable")

    def test_multiple_business_entries_share_budget_without_unrelated_programs(self):
        self.build({
            "online.cbl": program("ONLINE-RULE", 'CALL "SHARED-RULE".', purpose="renewal projection"),
            "batch.cbl": program("BATCH-RULE", 'CALL "SHARED-RULE".', purpose="renewal projection"),
            "shared.cbl": program("SHARED-RULE", "ADD 1 TO TOTAL."),
            "other.cbl": program("OTHER-RULE"),
        })
        mapping = self.mapping()
        self.assertEqual(set(mapping["direct_paths"]), {"online.cbl", "batch.cbl"})
        result = self.collect(mapping)
        self.assert_sources(result, {"online.cbl", "batch.cbl", "shared.cbl"})
        self.assertFalse(result["metadata"]["complete_root_budget_applied"])
        self.assertLessEqual(result["metadata"]["source_characters"], AgentPolicy().max_source_characters)

    def test_root_fanout_is_bounded_and_records_unsupplied_available_programs(self):
        self.build({f"entry{number}.cbl": program(f"ENTRY{number}", purpose="renewal projection")
                    for number in range(5)})
        result = self.collect(policy=AgentPolicy(max_semantic_files=2))
        self.assertEqual(len(result["pages"]), 2)
        self.assertEqual(result["metadata"]["omitted_candidate_path_count"], 3)
        self.assertEqual(len(result["metadata"]["available_unsupplied_paths"]), 3)
        self.assertFalse(result["metadata"]["closure_complete"])

    def test_only_indexed_program_definitions_can_be_concept_roots(self):
        self.build({"entry.cbl": program("ENTRY-RULE", purpose="renewal projection"),
                    "values.cpy": "*> renewal projection\n01 TOTAL PIC 9(7)V99.\n"})
        mapping = self.mapping()
        mapping["direct_paths"] = ["values.cpy", "entry.cbl"]
        result = self.collect(mapping)
        self.assert_sources(result, {"entry.cbl"})
        self.assertIn("concept_root_not_program", {row["reason"] for row in result["metadata"]["frontier"]})

    def test_cycles_repeated_calls_and_copy_paths_are_read_once(self):
        self.build({
            "entry.cbl": program("ENTRY-RULE", 'CALL "CHILD-RULE".\nCALL "CHILD-RULE".',
                purpose="renewal projection", data="COPY shared-values.\nCOPY shared-values."),
            "child.cbl": program("CHILD-RULE", 'CALL "ENTRY-RULE".', data="COPY shared-values."),
            "shared-values.cpy": "01 TOTAL PIC 9(7)V99.\n",
        })
        self.assert_sources(self.collect(), set(self.files))

    def test_missing_external_and_duplicate_local_implementations_are_distinguished(self):
        self.build({
            "entry.cbl": program("ENTRY-RULE", 'CALL "ABSENT-RULE".\nCALL "SHARED-RULE".',
                                  purpose="renewal projection"),
            "one/shared.cbl": program("SHARED-RULE"),
            "two/shared.cbl": program("SHARED-RULE"),
        })
        result = self.collect()
        self.assert_sources(result, {"entry.cbl"})
        frontier = result["metadata"]["frontier"]
        absent = next(row for row in frontier if row.get("target_name") == "ABSENT-RULE")
        duplicate = next(row for row in frontier if row.get("target_name") == "SHARED-RULE")
        self.assertEqual(absent["reason"], "external_implementation_unavailable")
        self.assertEqual(duplicate["reason"], "dependency_binding_unresolved")
        self.assertTrue(duplicate["source_available"])
        self.assertEqual(set(duplicate["candidate_paths"]), {"one/shared.cbl", "two/shared.cbl"})
        self.assertEqual(set(result["metadata"]["available_unsupplied_paths"]), set(duplicate["candidate_paths"]))

    def test_explicit_source_scope_blocks_all_dependency_expansion(self):
        self.build({"entry.cbl": program("ENTRY-RULE", 'CALL "CHILD-RULE".', purpose="renewal projection"),
                    "child.cbl": program("CHILD-RULE")})
        for question in ("什么是 renewal projection", "ENTRY-RULE 是什么"):
            mapping = self.mapping(question)
            mapping["source_identity"]["exclusive_source_scope"] = True
            self.assert_sources(self.collect(mapping), {"entry.cbl"})

    def test_oversized_root_does_not_hide_small_confirmed_implementation(self):
        self.build({
            "entry.cbl": program("ENTRY-RULE", "\n".join(["*> long background " + "x" * 80] * 30)
                                 + '\nCALL "CHILD-RULE".', purpose="renewal projection"),
            "child.cbl": program("CHILD-RULE", "ADD 1 TO TOTAL."),
        })
        result = self.collect(policy=AgentPolicy(max_complete_source_characters=512,
                                                max_source_characters=4096))
        self.assert_sources(result, {"child.cbl"})
        self.assertEqual(result["metadata"]["available_unsupplied_paths"], ["entry.cbl"])
        self.assertEqual(result["metadata"]["status"], "partial")
        self.assertFalse(result["metadata"]["physical_complete"])
        self.assertFalse(any(row["reason"] == "external_implementation_unavailable"
                             for row in result["metadata"]["frontier"]))

    def test_explicit_file_object_and_pfile_nominate_exact_definitions_only(self):
        self.build({
            "entry.cbl": ("IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRY-RULE.\n"
                "*> Purpose: renewal projection.\nENVIRONMENT DIVISION.\nINPUT-OUTPUT SECTION.\n"
                "FILE-CONTROL.\nSELECT ACCOUNT-FILE ASSIGN TO 'ACCOUNTLF'.\nDATA DIVISION.\n"
                "FILE SECTION.\nFD ACCOUNT-FILE.\n01 ACCOUNT-RECORD PIC X(8).\n"
                "PROCEDURE DIVISION.\nREAD ACCOUNT-FILE.\nGOBACK.\n"),
            "dds/ACCOUNTLF.dds": "     A          R ACCOUNTREC PFILE(ACCOUNTPF)\n",
            "dds/ACCOUNTPF.pf": "     A          R ACCOUNTREC\n     A            ACCOUNTKEY     8A\n",
            "dds/unrelated.dds": "     A          R OTHERREC TEXT('ACCOUNTLF')\n",
        })
        result = self.collect()
        self.assert_sources(result, {"entry.cbl", "dds/ACCOUNTLF.dds", "dds/ACCOUNTPF.pf"})
        self.assertTrue(result["metadata"]["physical_complete"])
        self.assertFalse(result["metadata"]["closure_complete"], "filenames cannot prove runtime library binding")
        self.assertEqual(result["metadata"]["available_unsupplied_paths"], [])
        self.assertTrue(all(row["source_available"] for row in result["metadata"]["frontier"]
                            if row["reason"] == "file_definition_binding_unresolved"))

    def test_pfile_words_in_literal_and_comment_do_not_nominate_sources(self):
        self.build({
            "entry.cbl": ("IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRY-RULE.\n"
                "*> Purpose: renewal projection.\nENVIRONMENT DIVISION.\nINPUT-OUTPUT SECTION.\n"
                "FILE-CONTROL.\nSELECT ACCOUNT-FILE ASSIGN TO 'ACCOUNTLF'.\n"
                "PROCEDURE DIVISION.\nREAD ACCOUNT-FILE.\nGOBACK.\n"),
            "ACCOUNTLF.dds": "     A          R ACCOUNTREC TEXT('PFILE(ACCOUNTPF)')\n"
                             "     A*                                     PFILE(ACCOUNTPF)\n",
            "ACCOUNTPF.pf": "     A          R ACCOUNTREC\n",
        })
        self.assert_sources(self.collect(), {"entry.cbl", "ACCOUNTLF.dds"})

    def test_file_definition_cycle_and_repeated_objects_do_not_repeat_reads(self):
        self.build({
            "entry.cbl": ("IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRY-RULE.\n"
                "*> Purpose: renewal projection.\nENVIRONMENT DIVISION.\nINPUT-OUTPUT SECTION.\n"
                "FILE-CONTROL.\nSELECT ACCOUNT-FILE ASSIGN TO 'ACCOUNTLF'.\n"
                "PROCEDURE DIVISION.\nREAD ACCOUNT-FILE.\nGOBACK.\n"),
            "ACCOUNTLF.dds": "     A          R ACCOUNTREC PFILE(ACCOUNTPF ACCOUNTPF)\n",
            "ACCOUNTPF.pf": "     A          R ACCOUNTREC PFILE(ACCOUNTLF)\n",
        })
        self.assert_sources(self.collect(), set(self.files))

    def test_multiroot_total_never_uses_single_root_bonus(self):
        body = "\n".join(["*> repeated details " + "x" * 80] * 10)
        self.build({f"entry{number}.cbl": program(f"ENTRY{number}", body, purpose="renewal projection")
                    for number in range(2)})
        result = self.collect(policy=AgentPolicy(max_source_characters=2048))
        self.assertEqual(len(result["pages"]), 1)
        self.assertLessEqual(result["metadata"]["source_characters"], 2048)
        self.assertFalse(result["metadata"]["complete_root_budget_applied"])
        self.assertEqual(len(result["metadata"]["available_unsupplied_paths"]), 1)

    def test_dependency_depth_budget_preserves_available_pending_target(self):
        self.build({f"step{number}.cbl": program(f"STEP{number}",
                    f'CALL "STEP{number + 1}".' if number < 5 else "CONTINUE.",
                    purpose="renewal projection" if number == 0 else "")
                    for number in range(6)})
        result = self.collect(self.mapping("STEP0 是什么"), overview=False)
        self.assert_sources(result, {f"step{number}.cbl" for number in range(5)})
        self.assertEqual(result["metadata"]["available_unsupplied_paths"], ["step5.cbl"])
        self.assertIn("dependency_depth_budget", {row["reason"] for row in result["metadata"]["frontier"]})

    def test_business_overview_follows_deep_chain_until_existing_file_budget(self):
        self.build({f"step{number}.cbl": program(f"STEP{number}",
                    f'CALL "STEP{number + 1}".' if number < 8 else 'CALL "STEP0".',
                    purpose="renewal projection" if number == 0 else "")
                    for number in range(9)})
        result = self.collect()
        self.assert_sources(result, set(self.files))
        self.assertEqual(result["metadata"]["depth_limit"], AgentPolicy().max_semantic_files)
        self.assertTrue(result["metadata"]["closure_complete"])
        limited = self.collect(policy=AgentPolicy(max_semantic_files=3))
        self.assert_sources(limited, {"step0.cbl", "step1.cbl", "step2.cbl"})
        self.assertEqual(limited["metadata"]["available_unsupplied_paths"], ["step3.cbl"])
        self.assertIn("dependency_file_budget", {row["reason"] for row in limited["metadata"]["frontier"]})

    def test_changed_physical_source_requires_refresh_without_trusting_old_graph(self):
        self.build({"entry.cbl": program("ENTRY-RULE", purpose="renewal projection")})
        mapping = self.mapping()
        (self.source / "entry.cbl").write_text(program("ENTRY-RULE", "MOVE 5 TO TOTAL."))
        result = self.collect(mapping)
        self.assertEqual(result["pages"], [])
        self.assertTrue(result["needs_refresh"])
        self.assertEqual(result["metadata"]["reason"], "source_hash_mismatch")

    def test_oversized_root_plans_implementation_window_beyond_header(self):
        data = "\n".join(f"01 UNUSED-{number} PIC X(40)." for number in range(180))
        self.build({
            "entry.cbl": program("ENTRY-RULE", 'CALL "CHILD-RULE" USING TOTAL.',
                purpose="renewal projection", data=data + "\n01 TOTAL PIC 9(7)V99."),
            "child.cbl": program("CHILD-RULE", "ADD 1 TO TOTAL."),
        })
        mapping = self.mapping()
        result = self.collect(mapping, policy=AgentPolicy(max_complete_source_characters=512,
                                                       max_source_characters=4096))
        investigation = build_question_investigation("什么是 renewal projection", mapping,
            database_path=self.database, source_pages=result["pages"], working_set=result["metadata"])
        action = next(action for action in investigation["planned_actions"]
                      if action["reason"] == "available_business_source_not_supplied")
        procedure = self.files["entry.cbl"].splitlines().index("PROCEDURE DIVISION.") + 1
        self.assertEqual(action["arguments"]["relative_path"], "entry.cbl")
        self.assertGreater(action["arguments"]["start_line"], 180)
        self.assertLessEqual(action["arguments"]["start_line"], procedure)
        self.assertGreaterEqual(action["arguments"]["end_line"], procedure + 2)
        self.assertFalse(investigation["can_answer"])
        self.assertEqual(investigation["source_reading"]["available_unread_paths"], ["entry.cbl"])

    def test_deep_available_source_outside_navigation_preview_still_gets_read(self):
        self.build({f"step{number}.cbl": program(f"STEP{number}",
                    f'CALL "STEP{number + 1}".' if number < 8 else "ADD 7 TO TOTAL.",
                    purpose="renewal projection" if number == 0 else "")
                    for number in range(9)})
        mapping = self.mapping()
        result = self.collect(mapping)
        mapping["selected_paths"] = ["step0.cbl"]
        mapping["relations"] = []
        visible = [page for page in result["pages"] if page["relative_path"] != "step8.cbl"]
        investigation = build_question_investigation("什么是 renewal projection", mapping,
            database_path=self.database, source_pages=visible, working_set=result["metadata"])
        actions = [action for action in investigation["planned_actions"]
                   if action["reason"] == "available_business_source_not_supplied"]
        self.assertEqual([action["arguments"]["relative_path"] for action in actions], ["step8.cbl"])
        self.assertFalse(investigation["can_answer"])
        supplied = build_question_investigation("什么是 renewal projection", mapping,
            database_path=self.database, source_pages=result["pages"], working_set=result["metadata"])
        self.assertNotIn("source_reading", supplied)

    def test_business_read_fallback_preserves_file_budget_scope_and_completed_reads(self):
        self.build({
            "entry.cbl": program("ENTRY-RULE", 'CALL "CHILD-RULE".', purpose="renewal projection"),
            "child.cbl": program("CHILD-RULE", 'CALL "FINAL-RULE".'),
            "final.cbl": program("FINAL-RULE", "ADD 7 TO TOTAL."),
        })
        mapping = self.mapping()
        result = self.collect(mapping, policy=AgentPolicy(max_semantic_files=2))
        metadata = result["metadata"]
        self.assertEqual(metadata["available_unsupplied_paths"], ["final.cbl"])
        actions, _ = _business_source_reads(self.database, mapping, metadata, [], [], [],
            max_files=2, max_actions=4)
        self.assertEqual({action["arguments"]["relative_path"] for action in actions}, {"entry.cbl", "child.cbl"})
        completed = [{"read": action["arguments"]} for action in actions]
        replay, pending = _business_source_reads(self.database, mapping, metadata, [], [], completed,
            max_files=2, max_actions=4)
        self.assertEqual(replay, [])
        self.assertTrue(pending, "trimmed source stays pending without rereading the same window")
        mapping["source_identity"]["exclusive_source_scope"] = True
        scoped, _ = _business_source_reads(self.database, mapping, metadata, [], [], [],
            max_files=2, max_actions=4)
        self.assertEqual({action["arguments"]["relative_path"] for action in scoped}, {"entry.cbl"})
        metadata["snapshot_id"] = "changed-snapshot"
        self.assertEqual(_business_source_reads(self.database, mapping, metadata, [], [], [],
            max_files=2, max_actions=4), ([], []))

    def test_business_read_navigation_cache_rechecks_current_source_visibility(self):
        self.build({"entry.cbl": program("ENTRY-RULE", "ADD 1 TO TOTAL.", purpose="renewal projection")})
        mapping = self.mapping()
        result = self.collect(mapping)
        cache = {}
        actions, _ = _business_source_reads(self.database, mapping, result["metadata"], [], [], [],
            max_files=32, max_actions=4, cache=cache)
        self.assertEqual(len(actions), 1)
        page = result["pages"][0]
        prefix = {**page, "end_line": page["end_line"] - 1,
                  "source_text": "\n".join(page["source_text"].splitlines()[:-1])}
        remainder, _ = _business_source_reads(self.database, mapping, result["metadata"], [prefix], [], [],
            max_files=32, max_actions=4, cache=cache)
        self.assertEqual(remainder[0]["arguments"]["start_line"], page["end_line"])
        with mock.patch("question_investigation.sqlite3.connect",
                        side_effect=AssertionError("cached navigation must not query again")):
            self.assertEqual(_business_source_reads(self.database, mapping, result["metadata"],
                result["pages"], [], [], max_files=32, max_actions=4, cache=cache), ([], []))

    def test_late_handoff_gets_its_own_window_instead_of_rereading_header(self):
        self.build({
            "entry.cbl": program("ENTRY-RULE", "MOVE 1 TO TOTAL.\n"
                + "\n".join("*> background " + "x" * 80 for _ in range(200))
                + '\nCALL "CHILD-RULE" USING TOTAL.', purpose="renewal projection"),
            "child.cbl": program("CHILD-RULE", "ADD 2 TO TOTAL."),
        })
        mapping = self.mapping()
        result = self.collect(mapping)
        first = next(page for page in result["pages"] if page["relative_path"] == "entry.cbl")
        header = {**first, "end_line": 120, "source_text": "\n".join(first["source_text"].splitlines()[:120])}
        visible = [header, *[page for page in result["pages"] if page["relative_path"] == "child.cbl"]]
        actions, pending = _business_source_reads(self.database, mapping, result["metadata"], visible, [], [],
            max_files=32, max_actions=4)
        self.assertEqual(len(actions), 1)
        self.assertGreater(actions[0]["arguments"]["start_line"], 200)
        self.assertEqual({row["relative_path"] for row in pending}, {"entry.cbl"})

    def test_actual_first_request_includes_child_implementation_after_long_concept_header(self):
        self.build({
            "entry.cbl": program("ENTRY-RULE", "\n".join(["*> background " + "x" * 90] * 180)
                + '\nCALL "CHILD-RULE" USING TOTAL.\nCALL "REPORT-RULE" USING TOTAL.',
                purpose="renewal projection", data="COPY shared-values.\n01 TOTAL PIC 9(7)V99."),
            "child.cbl": program("CHILD-RULE", "COMPUTE TOTAL = TOTAL * 1.25."),
            "report.cbl": program("REPORT-RULE", "DISPLAY TOTAL."),
            "shared-values.cpy": "01 FACTOR PIC 9V99 VALUE 1.25.\n",
        })
        requests = []

        def capture(request):
            envelope = json.loads(request.body)
            requests.append(json.loads(envelope["messages"][-1]["content"]))
            raise APIClientError("OFFLINE_CAPTURE_ONLY")

        config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
            api_key="offline-neutral-credential", timeout_seconds=60.0, max_output_tokens=2048)
        output = run_business_chat("什么是 renewal projection", self.database, self.source, config,
            transport=capture, allow_network=False, framework_reference_path="")
        self.assertEqual(len(requests), 1)
        self.assertFalse(output["agent_result"]["model_answer_recorded"])
        pages = [page for bundle in requests[0]["source_context"] for page in bundle.get("pages", [])]
        complete = [page for page in pages if "complete_working_set" in page.get("selection_reasons", [])]
        self.assert_sources({"pages": complete}, set(self.files))


if __name__ == "__main__":
    unittest.main()
