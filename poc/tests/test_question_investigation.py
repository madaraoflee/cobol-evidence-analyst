from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
from business_map import build_business_map
from business_chat import run_business_chat
from company_api import CompanyAPIConfig, TransportResponse
from evidence_context import ReadTask
from question_investigation import build_question_investigation, _calculation_paths, _indexed_candidates, _visible_ids
from repository_discovery import ensure_repository_search
from semantic_scope import build_business_evidence, prepare_semantic_scope
from source_session import QuestionSourceSession
from agent_policy import AgentPolicy


class QuestionInvestigationTests(unittest.TestCase):
    def test_large_dependency_closure_visits_the_relation_catalog_once(self):
        class SinglePassRelations(list):
            passes = 0

            def __iter__(self):
                self.passes += 1
                if self.passes > 1:
                    raise AssertionError("the relation catalog was scanned again")
                return super().__iter__()

        paths = [f"member-{index:05d}.cbl" for index in range(12000)]
        edges = SinglePassRelations()
        for index, caller in enumerate(paths):
            target = paths[(index + 1) % len(paths)]
            for kind, resolution in (("CALLS", "confirmed"), ("INCLUDES_COPY", "confirmed"),
                                     ("CALLS", "confirmed"), ("PERFORMS", "confirmed"),
                                     ("CALLS", "unresolved")):
                edges.append({"caller_path": caller, "target_path": target,
                              "relation_type": kind, "resolution": resolution})
        mapping = {"selected_paths": paths, "direct_paths": [paths[0]], "relations": edges}
        self.assertEqual(_calculation_paths(mapping, []), paths)
        self.assertEqual(edges.passes, 1)

    def test_dependency_closure_preserves_root_and_edge_order_and_resolution(self):
        def edge(caller, target, kind="CALLS", resolution="confirmed"):
            return {"caller_path": caller, "target_path": target,
                    "relation_type": kind, "resolution": resolution}

        mapping = {"selected_paths": ["first", "second", "third", "fourth", "fifth", "sixth", "unresolved", "performed"],
                   "direct_paths": ["second", "first", "second"],
                   "relations": [edge("second", "fourth"), edge("first", "third"),
                                 edge("second", "fifth", "INCLUDES_COPY"), edge("fourth", "sixth"),
                                 edge("sixth", "second"), edge("second", "fourth"),
                                 edge("second", "unresolved", resolution="unresolved"),
                                 edge("second", "performed", kind="PERFORMS"), edge("second", "outside")]}
        self.assertEqual(_calculation_paths(mapping, []),
                         ["second", "first", "fourth", "fifth", "third", "sixth"])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"

    def write(self, path, name, body, data=None):
        data = data or ("01 WORK-COUNT PIC 9(9).\n01 BASE-VALUE PIC 9(9).\n"
                        "01 FACTOR PIC 9(9).\n01 NET-VALUE PIC 9(9).")
        (self.source / path).write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. " + name + ".\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n" + data +
            "\nPROCEDURE DIVISION.\nMAIN.\n" + body + "\nGOBACK.\n", encoding="utf-8")

    def build(self):
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)

    def page(self, path, first=1, last=None, identifier=None):
        content = (self.source / path).read_text(encoding="utf-8")
        lines = content.splitlines()
        last = len(lines) if last is None else last
        return {"relative_path": path, "start_line": first, "end_line": last,
                "source_text": "\n".join(lines[first-1:last]),
                "source_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "evidence_id": identifier or f"source_{path}_{first}_{last}",
                "selection_reasons": ["question_match"]}

    def investigate(self, question, pages=(), **kwargs):
        return build_question_investigation(question,
            build_business_map(self.database, self.source, question),
            database_path=self.database, source_pages=pages, **kwargs)

    @staticmethod
    def item(result, kind):
        return next(item for item in result["required_items"] if item["kind"] == kind)

    def test_exact_late_formula_is_selected_before_budget_and_unrelated_gaps_are_excluded(self):
        self.write("entry.cbl", "VALUEPLAN", "\n".join(
            f"COMPUTE OUT-{index} = {index} * 2." for index in range(1, 41)))
        self.build()
        lines = (self.source / "entry.cbl").read_text().splitlines()
        target = next(index for index, line in enumerate(lines, 1) if "OUT-40 =" in line)
        question = "VALUEPLAN OUT-40 怎么计算？"
        before = self.investigate(question, [self.page("entry.cbl", 1, 10)])
        self.assertFalse(before["can_answer"])
        self.assertEqual(before["planned_actions"][0]["arguments"]["line"], target)
        after = self.investigate(question, [self.page("entry.cbl", target, target)])
        self.assertTrue(after["can_answer"])
        self.assertEqual(self.item(after, "formula")["candidate_count"], 1)
        self.assertEqual(after["open_gaps"], [])
        self.assertEqual(after["planned_actions"], [])

    def test_input_field_match_keeps_later_formula_with_same_output(self):
        self.write("entry.cbl", "VALUEPLAN", "\n".join(
            f"COMPUTE NET-VALUE = {index} * 2." for index in range(40))
            + "\nCOMPUTE NET-VALUE = BASE-VALUE * 3.")
        self.build()
        for question in ("VALUEPLAN BASE-VALUE 怎么参与计算？",
                         "VALUEPLAN NET-VALUE 怎么由 BASE-VALUE 计算？"):
            with self.subTest(question=question):
                candidates, _, _ = _indexed_candidates(self.database, ["entry.cbl"], question)
                formulas = [row for row in candidates if row["kind"] == "formula"]
                self.assertIn("BASE-VALUE", formulas[0]["reads"])
                self.assertLessEqual(len(formulas), 32)

    def test_explicit_result_keeps_distinct_assignments_to_same_field(self):
        self.write("entry.cbl", "VALUEPLAN", "COMPUTE NET-VALUE = 11 * 2.\n"
                   "COMPUTE NET-VALUE = 13 * 3.")
        self.build()
        result = self.investigate("VALUEPLAN NET-VALUE 怎么计算？", [self.page("entry.cbl")])
        self.assertEqual(self.item(result, "formula")["candidate_count"], 2)
        self.assertTrue(result["can_answer"])

    def test_natural_expression_question_investigates_the_formula(self):
        self.write("entry.cbl", "VALUEPLAN", "COMPUTE NET-VALUE = BASE-VALUE * 2.")
        self.build()
        for question in ("VALUEPLAN NET-VALUE 的算式是什么？", "VALUEPLAN NET-VALUE 的算式是什麼？"):
            with self.subTest(question=question):
                result = self.investigate(question, [self.page("entry.cbl")])
                formula = self.item(result, "formula")
                self.assertEqual(formula["candidate_count"], 1)
                self.assertEqual(formula["status"], "SATISFIED")
                self.assertTrue(result["can_answer"])

    def test_unfocused_formula_enumeration_remains_bounded_and_reports_its_gap(self):
        self.write("entry.cbl", "VALUEPLAN", "\n".join(
            f"COMPUTE OUT-{index} = {index} * 2." for index in range(1, 41)))
        self.build()
        result = self.investigate("VALUEPLAN 的计算公式是什么？", [self.page("entry.cbl")])
        self.assertEqual(self.item(result, "formula")["candidate_count"], 32)
        self.assertIn("formula_group_budget", {gap["reason"] for gap in result["open_gaps"]})

    def test_exact_field_priority_applies_across_source_files(self):
        self.write("entry.cbl", "VALUEPLAN", "CALL 'VALUELEAF'.\n" + "\n".join(
            f"COMPUTE OUT-{index} = {index} * 2." for index in range(1, 41)))
        self.write("leaf.cbl", "VALUELEAF", "COMPUTE NET-VALUE = 11 * 2.")
        self.build()
        result = self.investigate("VALUEPLAN NET-VALUE 怎么计算？",
                                  [self.page("entry.cbl"), self.page("leaf.cbl")])
        self.assertEqual(self.item(result, "formula")["candidate_count"], 1)
        self.assertEqual(result["open_gaps"], [])
        self.assertTrue(result["can_answer"])

    def test_exact_result_keeps_called_formula_when_parameter_name_changes(self):
        self.write("entry.cbl", "VALUEPLAN", "COMPUTE NET-VALUE = 11 * 2.\n"
                   "CALL 'VALUELEAF' USING NET-VALUE.")
        self.write("leaf.cbl", "VALUELEAF", "COMPUTE IO-RESULT = IO-RESULT * 3.",
                   "LINKAGE SECTION.\n01 IO-RESULT PIC 9(7).")
        path = self.source / "leaf.cbl"
        path.write_text(path.read_text().replace("PROCEDURE DIVISION.",
                                                "PROCEDURE DIVISION USING IO-RESULT."))
        self.build()
        question = "VALUEPLAN NET-VALUE 怎么计算？"
        before = self.investigate(question, [self.page("entry.cbl")])
        self.assertFalse(before["can_answer"])
        self.assertTrue(any(action["arguments"]["relative_path"] == "leaf.cbl"
                            for action in before["planned_actions"]))
        after = self.investigate(question, [self.page("entry.cbl"), self.page("leaf.cbl")])
        self.assertEqual(self.item(after, "formula")["candidate_count"], 2)
        self.assertEqual(after["open_gaps"], [])
        self.assertTrue(after["can_answer"])

    def test_path_budget_keeps_direct_root_ahead_of_leaf_formula_matches(self):
        self.write("entry.cbl", "VALUEPLAN", "IF MODE-VALUE = 1\n" + "\n".join(
            f"CALL 'LEAF-{index}'." for index in range(8)) + "\nEND-IF.")
        leaves = []
        for index in range(8):
            path = f"leaf-{index}.cbl"
            leaves.append(path)
            self.write(path, f"LEAF-{index}", f"COMPUTE NET-VALUE = {index} * 3.")
        self.build()
        result = self.investigate("VALUEPLAN NET-VALUE 怎么计算？",
                                  [self.page(path) for path in leaves])
        self.assertFalse(result["can_answer"])
        self.assertEqual(result["candidate_paths"][0], "entry.cbl")
        self.assertTrue(any(action["arguments"]["relative_path"] == "entry.cbl"
                            for action in result["planned_actions"]))
        self.assertIn("located_path_budget", {gap["reason"] for gap in result["open_gaps"]})

    def test_related_formula_budget_is_not_hidden_by_exact_field_match(self):
        self.write("entry.cbl", "VALUEPLAN", "\n".join(
            f"COMPUTE NET-VALUE = {index} * 2." for index in range(40)))
        self.build()
        result = self.investigate("VALUEPLAN NET-VALUE 怎么计算？", [self.page("entry.cbl")])
        self.assertEqual(self.item(result, "formula")["candidate_count"], 32)
        self.assertIn("formula_candidate_budget", {gap["reason"] for gap in result["open_gaps"]})
        self.assertEqual(result["state"], "bounded_partial")

    def test_exact_common_field_query_work_does_not_scale_with_unrelated_rules(self):
        self.write("entry.cbl", "VALUEPLAN", "COMPUTE NET-VALUE = 11 * 2.")
        self.build()
        original_connect = sqlite3.connect

        def measured():
            instructions = []
            def connect(*args, **kwargs):
                connection = original_connect(*args, **kwargs)
                connection.set_progress_handler(lambda: instructions.append(100) or 0, 100)
                return connection
            with mock.patch("question_investigation.sqlite3.connect", side_effect=connect):
                candidates, _, _ = _indexed_candidates(self.database, ["entry.cbl"],
                                                       "VALUEPLAN NET-VALUE 怎么计算？")
            self.assertEqual(sum(row["kind"] == "formula" for row in candidates), 1)
            return sum(instructions)

        baseline = measured()
        self.write("unrelated.cbl", "OTHERPLAN", "\n".join(
            f"COMPUTE NET-VALUE = {index} * 2." for index in range(5000)))
        self.build()
        # Count VM work instead of wall-clock time: a common field elsewhere
        # must not turn this one-file lookup into a repository-wide field scan.
        self.assertLessEqual(measured(), baseline * 2 + 1000)

    def test_peripheral_alias_needs_deep_formula_and_cross_paragraph_adjustments(self):
        body = "DISPLAY 'RQX 203012'.\n"
        body += "\n".join(f"COMPUTE WORK-COUNT = {n} + 1." for n in range(15))
        body += "\n" + "*> neutral filler\n" * 900
        body += ("INPUTS.\nMOVE 11 TO BASE-VALUE.\nMOVE 3 TO FACTOR.\nCALCULATE-NET.\n"
                 "IF BASE-VALUE > 5\nCOMPUTE NET-VALUE ROUNDED = BASE-VALUE * FACTOR + 29\nEND-IF.\n"
                 "ADJUST-NET.\nIF FACTOR > 2\nMOVE 77 TO NET-VALUE\nEND-IF.")
        self.write("entry.cbl", "VALUEPLAN", body)
        self.build()
        before = self.investigate("RQX 203012 的计算公式是什么？", [self.page("entry.cbl", 1, 32)])
        self.assertFalse(before["can_answer"])
        self.assertEqual(before["state"], "needs_evidence")
        self.assertTrue(any(action["arguments"].get("line", 0) > 900 for action in before["planned_actions"]))
        after = self.investigate("RQX 203012 的计算公式是什么？", [self.page("entry.cbl")])
        for kind in ("formula", "inputs", "conditions", "result_adjustments"):
            self.assertEqual(self.item(after, kind)["status"], "SATISFIED", after)
        self.assertFalse(after["semantic_execution_verified"])

    def test_called_formula_requires_callee_original_source(self):
        self.write("entry.cbl", "VALUEPLAN", "DISPLAY 'RZA'.\nCALL 'VALUELEAF' USING BASE-VALUE NET-VALUE.")
        self.write("leaf.cbl", "VALUELEAF", "*> neutral filler\n" * 800 +
                   "COMPUTE NET-VALUE ROUNDED = BASE-VALUE * 3 + 29.")
        self.build()
        before = self.investigate("RZA 计算公式", [self.page("entry.cbl")])
        self.assertFalse(before["can_answer"])
        self.assertTrue(any(action["arguments"]["relative_path"] == "leaf.cbl" for action in before["planned_actions"]))
        after = self.investigate("RZA 计算公式", [self.page("entry.cbl"), self.page("leaf.cbl")])
        self.assertEqual(self.item(after, "formula")["status"], "SATISFIED")
        self.assertEqual(self.item(after, "inputs")["status"], "UNRESOLVED")
        self.assertEqual(after["state"], "bounded_partial")

    def test_exact_target_does_not_require_incoming_callers_arithmetic(self):
        self.write("z-target.cbl", "VALUEPLAN", "MOVE 11 TO BASE-VALUE.\n"
            "IF BASE-VALUE > 5\nCOMPUTE NET-VALUE ROUNDED = BASE-VALUE * 2 + 29\nEND-IF.")
        for index in range(9):
            self.write(f"a-caller-{index:02d}.cbl", f"CALLER{index}",
                "CALL 'VALUEPLAN'.\nCOMPUTE WORK-COUNT = 1 + 2.")
        # Keep the navigation hub threshold above nine so incoming callers
        # really enter selected_paths; their arithmetic must remain optional.
        for index in range(40):
            self.write(f"neutral-{index:02d}.cbl", f"NEUTRAL{index}", "CONTINUE.")
        self.build()
        mapping = build_business_map(self.database, self.source, "VALUEPLAN 计算公式")
        self.assertEqual(mapping["direct_paths"], ["z-target.cbl"])
        self.assertEqual(len(mapping["selected_paths"]), 10)
        local = build_question_investigation("VALUEPLAN 计算公式", mapping,
            database_path=self.database, source_pages=[self.page("z-target.cbl")])
        self.assertEqual(local["candidate_paths"], ["z-target.cbl"])
        self.assertTrue(local["can_answer"])
        self.assertEqual(self.item(local, "formula")["candidate_count"], 1)
        self.assertFalse(local["planned_actions"])
        self.assertFalse(any(gap.get("reason") == "located_path_budget" for gap in local["open_gaps"]))
        requests = []
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            page = next(page for context in payload["source_context"] for page in context["pages"]
                if page["relative_path"] == "z-target.cbl" and "COMPUTE NET-VALUE ROUNDED" in page["source_text"])
            return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant",
                "content": f"基础值初始为11，大于5时乘以2再加29并舍入。[{page['evidence_id']}]"}, "finish_reason": "stop"}]}))
        result = run_business_chat("VALUEPLAN 计算公式", self.database, self.source,
            CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model", api_key="offline-only"),
            transport=transport, allow_network=False, framework_reference_path=self.root / "absent.md")
        self.assertEqual(len(requests), 1)
        investigation = result["agent_result"]["investigation_state"]["question_investigation"]
        self.assertEqual(investigation["candidate_paths"], ["z-target.cbl"])
        self.assertFalse(any(gap.get("reason") == "located_path_budget" for gap in investigation["open_gaps"]))
        self.assertEqual(result["agent_result"]["status"], "ANALYZED")

    def test_external_only_call_preserves_callsite_but_does_not_prove_formula(self):
        self.write("entry.cbl", "VALUEPLAN", "DISPLAY 'RZT'.\nCALL 'REMOTEVALUE' USING NET-VALUE.")
        self.build()
        result = self.investigate("RZT 的完整最终计算公式", [self.page("entry.cbl")])
        self.assertEqual(self.item(result, "formula")["status"], "UNRESOLVED")
        self.assertEqual(self.item(result, "dependencies")["status"], "UNRESOLVED")
        self.assertTrue(self.item(result, "dependencies")["evidence_ids"])
        self.assertEqual(result["state"], "bounded_partial")
        self.assertTrue(result["can_answer"])

    def test_unknown_input_remains_unresolved_without_assignment(self):
        self.write("entry.cbl", "VALUEPLAN", "COMPUTE NET-VALUE = BASE-VALUE * FACTOR.")
        self.build()
        result = self.investigate("VALUEPLAN 计算公式", [self.page("entry.cbl")])
        self.assertEqual(self.item(result, "inputs")["status"], "UNRESOLVED")
        self.assertEqual(set(self.item(result, "inputs")["fields"]), {"BASE-VALUE", "FACTOR"})
        self.assertEqual(result["state"], "bounded_partial")

    def test_visible_value_declaration_is_an_input_origin_candidate(self):
        self.write("entry.cbl", "VALUEPLAN", "COMPUTE NET-VALUE = BASE-VALUE * 2.",
                   "01 BASE-VALUE PIC 9(9) VALUE 11.\n01 NET-VALUE PIC 9(9).")
        self.build()
        result = self.investigate("VALUEPLAN 计算公式", [self.page("entry.cbl")])
        self.assertEqual(self.item(result, "inputs")["status"], "SATISFIED")

    def test_procedure_using_is_a_visible_input_origin_and_arithmetic_has_result_role(self):
        self.write("entry.cbl", "VALUEPLAN", "DIVIDE 2 INTO BASE-VALUE GIVING NET-VALUE.")
        path = self.source / "entry.cbl"
        path.write_text(path.read_text(encoding="utf-8").replace("PROCEDURE DIVISION.",
            "PROCEDURE DIVISION USING BASE-VALUE."), encoding="utf-8")
        self.build()
        result = self.investigate("VALUEPLAN 计算公式", [self.page("entry.cbl")])
        self.assertEqual(self.item(result, "inputs")["status"], "SATISFIED")
        line = next(index for index, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                    if text.startswith("DIVIDE"))
        with QuestionSourceSession(self.database, self.source) as session:
            with prepare_semantic_scope(self.database, session,
                    anchors=[{"relative_path": "entry.cbl", "line": line}], policy=AgentPolicy()) as scope:
                group = build_business_evidence(scope, session,
                    anchor={"relative_path": "entry.cbl", "line": line}, policy=AgentPolicy())
                result_observations = [observation for observation in group.observations
                    if observation.semantic_role == "result"]
                self.assertTrue(result_observations)
                self.assertTrue(any(ref.start_line == line for observation in result_observations
                                    for ref in observation.source_refs))

    def test_condition_comment_envelope_uses_supplied_parsed_predicate(self):
        self.write("entry.cbl", "VALUEPLAN", "MOVE 11 TO BASE-VALUE.\nIF BASE-VALUE > 5\n" +
                   "*> neutral condition separation\n" * 380 +
                   "COMPUTE NET-VALUE = BASE-VALUE * 2\nEND-IF.")
        self.build()
        line = next(index for index, text in enumerate((self.source / "entry.cbl").read_text(
            encoding="utf-8").splitlines(), 1) if text.startswith("COMPUTE"))
        with QuestionSourceSession(self.database, self.source) as session:
            with prepare_semantic_scope(self.database, session,
                    anchors=[{"relative_path": "entry.cbl", "line": line}], policy=AgentPolicy()) as scope:
                group = build_business_evidence(scope, session,
                    anchor={"relative_path": "entry.cbl", "line": line}, policy=AgentPolicy())
                result = self.investigate("VALUEPLAN 计算公式", group.supplied_locations, evidence_groups=[group])
        self.assertEqual(self.item(result, "conditions")["status"], "SATISFIED")
        self.assertFalse(any(action["reason"].startswith("conditions") for action in result["planned_actions"]))
        condition = {"relative_path": "a.cbl", "start_line": 1, "end_line": 2,
                     "statement": "IF BASE-VALUE > 0 AND FACTOR > 0", "occurrence_count": 1}
        self.assertEqual(_visible_ids(condition, [{"relative_path": "a.cbl", "start_line": 1,
            "end_line": 1, "evidence_id": "partial", "source_text": "IF BASE-VALUE > 0"}]), [])

    def test_cached_candidates_recheck_visible_pages_and_invalidate_new_snapshot(self):
        self.write("entry.cbl", "VALUEPLAN", "MOVE 11 TO BASE-VALUE.\nCOMPUTE NET-VALUE = BASE-VALUE * 2.")
        self.build()
        cache = {}
        mapping = build_business_map(self.database, self.source, "VALUEPLAN 计算公式")
        first = build_question_investigation("VALUEPLAN 计算公式", mapping, database_path=self.database,
            source_pages=[self.page("entry.cbl")], candidate_cache=cache)
        self.assertEqual(self.item(first, "formula")["status"], "SATISFIED")
        with mock.patch("question_investigation._indexed_candidates", side_effect=AssertionError("repeat query")), \
             mock.patch("question_investigation._calculation_paths", side_effect=AssertionError("repeat graph traversal")):
            trimmed = build_question_investigation("VALUEPLAN 计算公式", mapping, database_path=self.database,
                source_pages=[self.page("entry.cbl", 1, 10)], candidate_cache=cache)
        self.assertTrue(trimmed["candidate_cache"]["hit"])
        self.assertEqual(self.item(trimmed, "formula")["status"], "OPEN")
        self.write("entry.cbl", "VALUEPLAN", "MOVE 11 TO BASE-VALUE.\nCOMPUTE NET-VALUE = BASE-VALUE * 3.")
        self.build()
        changed = self.investigate("VALUEPLAN 计算公式", [self.page("entry.cbl")], candidate_cache=cache)
        self.assertFalse(changed["candidate_cache"]["hit"])
        self.assertEqual(self.item(changed, "formula")["status"], "SATISFIED")

    def test_cached_page_cleaning_does_not_accept_changed_literal_or_trimmed_source(self):
        candidate = {"relative_path": "entry.cbl", "start_line": 1, "end_line": 1,
                     "statement": "MOVE 'A' TO RESULT.", "source_format": "free"}
        page = {"relative_path": "entry.cbl", "start_line": 1, "end_line": 1,
                "source_text": "MOVE 'A' TO RESULT.", "evidence_id": "source-page"}
        cache = {}
        self.assertEqual(_visible_ids(candidate, [page], page_cache=cache), ["source-page"])
        page["source_text"] = "MOVE 'B' TO RESULT."
        self.assertEqual(_visible_ids(candidate, [page], page_cache=cache), [])
        page["source_text"] = "MOVE 'A'"
        self.assertEqual(_visible_ids(candidate, [page], page_cache=cache), [])

    def test_duplicate_id_wrong_hash_and_prefix_expression_do_not_supply_formula(self):
        self.write("entry.cbl", "VALUEPLAN", "COMPUTE NET-VALUE = BASE-VALUE * 2.")
        self.build()
        page = self.page("entry.cbl", identifier="same-id")
        conflict = {**page, "source_text": "DISPLAY 'unrelated'."}
        duplicate = self.investigate("VALUEPLAN 计算公式", [page, conflict])
        self.assertEqual(self.item(duplicate, "formula")["status"], "OPEN")
        wrong_hash = self.investigate("VALUEPLAN 计算公式", [{**page, "source_sha256": "old-hash"}])
        self.assertEqual(self.item(wrong_hash, "formula")["status"], "OPEN")
        candidate = {"relative_path": "a.cbl", "start_line": 1, "end_line": 1,
                     "statement": "COMPUTE NET-VALUE = BASE", "occurrence_count": 1}
        self.assertEqual(_visible_ids(candidate, [{"relative_path": "a.cbl", "start_line": 1,
            "end_line": 1, "evidence_id": "prefix", "source_text": "COMPUTE NET-VALUE = BASE-RATE."}]), [])
        literal = {**candidate, "statement": "MOVE 'Y' TO NET-VALUE"}
        self.assertEqual(_visible_ids(literal, [{"relative_path": "a.cbl", "start_line": 1,
            "end_line": 1, "evidence_id": "literal", "source_text": "MOVE 'y' TO NET-VALUE."}]), [])

    def test_repeated_multiline_formula_can_use_contiguous_pages(self):
        self.write("entry.cbl", "VALUEPLAN", "COMPUTE NET-VALUE = BASE-VALUE\n + FACTOR.\n"
                   "DISPLAY 'neutral'.\nCOMPUTE NET-VALUE = BASE-VALUE\n + FACTOR.")
        self.build()
        lines = (self.source / "entry.cbl").read_text(encoding="utf-8").splitlines()
        first = next(index for index, text in enumerate(lines, 1) if text.startswith("COMPUTE"))
        result = self.investigate("VALUEPLAN 计算公式", [self.page("entry.cbl", first, first),
                                                   self.page("entry.cbl", first + 1, first + 1)])
        self.assertEqual(self.item(result, "formula")["status"], "SATISFIED")

    def test_conflicting_overlap_cannot_compose_a_formula(self):
        self.write("entry.cbl", "VALUEPLAN", "COMPUTE NET-VALUE = BASE-VALUE\n + FACTOR.")
        self.build()
        lines = (self.source / "entry.cbl").read_text(encoding="utf-8").splitlines()
        first = next(index for index, text in enumerate(lines, 1) if text.startswith("COMPUTE"))
        page = self.page("entry.cbl", first, first, "first")
        conflict = {**self.page("entry.cbl", first, first + 1, "second"),
                    "source_text": "COMPUTE NET-VALUE = OTHER-VALUE\n + FACTOR."}
        result = self.investigate("VALUEPLAN 计算公式", [page, conflict])
        self.assertEqual(self.item(result, "formula")["status"], "OPEN")

    def test_unknown_dependency_does_not_locate_and_english_substrings_are_not_intents(self):
        mapping = {"selected_paths": ["entry.cbl"], "direct_paths": [], "source_identity": {"status": "none"}}
        source = {"relative_path": "entry.cbl", "selection_reasons": ["dependency"]}
        result = build_question_investigation("未知业务计算公式", mapping, source_pages=[source])
        self.assertEqual(result["state"], "unresolved")
        self.assertFalse(result["can_answer"])
        self.assertEqual(self.item(result, "formula")["status"], "OPEN")
        for question in ("COMPUTER source", "FORMULATED notes", "手册操作含义"):
            self.assertEqual(build_question_investigation(question, mapping)["required_items"], [])

    def test_eof_finishes_out_of_range_request_but_budget_truncation_stays_open(self):
        task = ReadTask("read", "entry.cbl", {"start_line": 100, "end_line": 120}, next_start_line=100)
        task.advance({"file_total_lines": 110, "pages": [{"relative_path": "entry.cbl", "start_line": 100, "end_line": 110}]})
        self.assertEqual(task.state, "complete")
        self.assertIsNone(task.next_start_line)
        partial = ReadTask("read2", "entry.cbl", {"start_line": 100, "end_line": 120}, next_start_line=100)
        partial.advance({"file_total_lines": 200, "pages": [{"relative_path": "entry.cbl", "start_line": 100, "end_line": 110}]})
        self.assertEqual(partial.state, "open")
        self.assertEqual(partial.next_start_line, 111)


if __name__ == "__main__":
    unittest.main()
