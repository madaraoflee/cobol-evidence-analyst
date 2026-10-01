"""Offline obligations for calculations expressed through assignments and branches."""

from pathlib import Path
import hashlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
from business_map import build_business_map
from question_investigation import build_question_investigation
from repository_discovery import ensure_repository_search


class ProceduralCalculationTests(unittest.TestCase):
    question = "ASSIGNFLOW NET-VALUE 怎么计算？"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"

    def build(self, *, extra="", known_input=True):
        data = "01 MODE-FLAG PIC 9" + (" VALUE 1" if known_input else "") + ".\n"
        data += "01 BASE-VALUE PIC 9(4).\n01 NET-VALUE PIC 9(4).\n"
        if not known_input:
            data += "01 RAW-VALUE PIC 9(4).\n"
        origin = "10" if known_input else "RAW-VALUE"
        body = (f"MOVE {origin} TO BASE-VALUE.\nIF MODE-FLAG = 1\n"
                "MOVE BASE-VALUE TO NET-VALUE\nELSE\nMOVE 0 TO NET-VALUE\nEND-IF.\n")
        self.text = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. ASSIGNFLOW.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n" + data +
            "PROCEDURE DIVISION.\nMAIN.\n" + body + extra + "GOBACK.\n")
        (self.source / "assign.cbl").write_text(self.text, encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free")
        ensure_repository_search(self.database, self.source)
        self.mapping = build_business_map(self.database, self.source, self.question)

    def page(self, end=None, start=1):
        lines = self.text.splitlines()
        last = len(lines) if end is None else end
        return {"relative_path": "assign.cbl", "start_line": start, "end_line": last,
            "source_text": "\n".join(lines[start - 1:last]), "evidence_id": f"ev_assign_{start}_{last}",
            "source_sha256": hashlib.sha256(self.text.encode()).hexdigest(),
            "selection_reasons": ["question_match"]}

    def ask(self, pages, **options):
        return build_question_investigation(self.question, self.mapping,
            database_path=self.database, source_pages=pages, **options)

    def item(self, result, kind):
        return next(row for row in result["required_items"] if row["kind"] == kind)

    def test_supplied_assignments_and_conditions_support_static_calculation_explanation(self):
        self.build()
        result = self.ask([self.page()])
        self.assertEqual(self.item(result, "business_steps")["status"], "SATISFIED")
        self.assertEqual(self.item(result, "formula")["status"], "NOT_APPLICABLE")
        self.assertEqual(self.item(result, "formula")["candidate_count"], 0)
        self.assertEqual(self.item(result, "inputs")["status"], "SATISFIED")
        self.assertTrue(result["can_answer"])
        self.assertFalse(result["planned_actions"])
        self.assertFalse(result["semantic_execution_verified"])

    def test_hidden_procedural_source_requires_bounded_read_before_answering(self):
        self.build()
        result = self.ask([self.page(end=8)], max_actions=1)
        self.assertFalse(result["can_answer"])
        self.assertEqual(result["state"], "needs_evidence")
        self.assertEqual(len(result["planned_actions"]), 1)
        action = result["planned_actions"][0]
        self.assertEqual(action["arguments"]["relative_path"], "assign.cbl")
        self.assertEqual(action["reason"], "business_steps_source_not_supplied")
        self.assertLessEqual(action["read_fallback"]["end_line"] - action["read_fallback"]["start_line"], 100)
        supplied = self.ask([self.page()], max_actions=1)
        self.assertTrue(supplied["can_answer"])

    def test_missing_external_callee_preserves_partial_boundary(self):
        self.build(extra="CALL 'UNAVAILABLEFLOW' USING NET-VALUE.\n")
        result = self.ask([self.page()])
        self.assertEqual(self.item(result, "business_steps")["status"], "SATISFIED")
        self.assertEqual(self.item(result, "dependencies")["reason"], "external_implementation_unavailable")
        self.assertEqual(self.item(result, "formula")["status"], "UNRESOLVED")
        self.assertEqual(result["state"], "bounded_partial")
        self.assertTrue(result["can_answer"])
        self.assertFalse(result["planned_actions"])
        self.assertIn("external_implementation_unavailable", {gap["reason"] for gap in result["open_gaps"]})

    def test_supplied_procedure_does_not_invent_unknown_input(self):
        self.build(known_input=False)
        result = self.ask([self.page()])
        self.assertEqual(self.item(result, "inputs")["reason"], "input_source_not_located")
        self.assertIn("RAW-VALUE", self.item(result, "inputs")["fields"])
        self.assertEqual(result["state"], "bounded_partial")
        self.assertFalse(result["planned_actions"])

    def test_dynamic_call_target_remains_unresolved(self):
        self.build(extra="MOVE 'VALUELEAF' TO ROUTINE-NAME.\nCALL ROUTINE-NAME USING NET-VALUE.\n")
        result = self.ask([self.page()])
        self.assertEqual(self.item(result, "dependencies")["reason"], "runtime_target_unresolved")
        self.assertEqual(result["state"], "bounded_partial")
        self.assertFalse(result["semantic_execution_verified"])

    def test_cached_candidates_keep_procedural_obligations(self):
        self.build()
        cache = {}
        before = self.ask([self.page(end=8)], candidate_cache=cache)
        self.assertFalse(before["can_answer"])
        after = self.ask([self.page()], candidate_cache=cache)
        self.assertTrue(after["candidate_cache"]["hit"])
        self.assertEqual(self.item(after, "business_steps")["status"], "SATISFIED")
        self.assertTrue(after["can_answer"])

    def test_procedural_fallback_preserves_candidate_budget_and_frontier(self):
        self.build(extra="\n".join(f"MOVE {index} TO NET-VALUE." for index in range(40)) + "\n")
        result = self.ask([self.page()], max_actions=1)
        self.assertLessEqual(self.item(result, "business_steps")["candidate_count"], 32)
        self.assertEqual(result["state"], "bounded_partial")
        self.assertTrue(any(gap["kind"] == "business_steps" and gap["reason"] == "formula_candidate_budget"
                            for gap in result["open_gaps"]))
        self.assertFalse(result["planned_actions"])

    def test_unrelated_arithmetic_cannot_supply_named_procedural_calculation(self):
        self.build(extra="UNRELATED.\nCOMPUTE OTHER-VALUE = 5 + 2.\n")
        first = self.text.splitlines().index("UNRELATED.") + 1
        partial = self.ask([self.page(start=first)], max_actions=1)
        self.assertFalse(partial["can_answer"])
        self.assertEqual(len(partial["planned_actions"]), 1)
        self.assertEqual(partial["planned_actions"][0]["arguments"]["relative_path"], "assign.cbl")
        self.assertLess(partial["planned_actions"][0]["arguments"]["line"], first)
        self.assertEqual(self.item(partial, "formula")["status"], "NOT_APPLICABLE")
        complete = self.ask([self.page()])
        self.assertTrue(complete["can_answer"])
        self.assertEqual(self.item(complete, "business_steps")["status"], "SATISFIED")
        self.assertFalse(complete["planned_actions"])
        self.assertFalse(complete["semantic_execution_verified"])

    def test_empty_procedural_fallback_keeps_formula_discovery_obligation(self):
        self.build()
        self.text = self.text.split("PROCEDURE DIVISION.", 1)[0] + (
            "PROCEDURE DIVISION.\nDISPLAY NET-VALUE.\nGOBACK.\n")
        (self.source / "assign.cbl").write_text(self.text, encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free")
        ensure_repository_search(self.database, self.source)
        self.mapping = build_business_map(self.database, self.source, self.question)
        cache = {}
        for _ in range(2):
            result = self.ask([self.page()], candidate_cache=cache)
            self.assertEqual(self.item(result, "formula")["status"], "OPEN")
            self.assertEqual(self.item(result, "formula")["reason"], "formula_not_located")
            self.assertFalse(any(item["kind"] == "business_steps" for item in result["required_items"]))
            self.assertFalse(result["can_answer"])


if __name__ == "__main__":
    unittest.main()
