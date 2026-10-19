"""Activation conclusions require the actual caller, guards and source version."""

from contextlib import closing
import hashlib
import sqlite3
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from activation_evidence import investigate_activation
import activation_evidence
from business_index import build_business_index
from business_map import build_business_map
from question_investigation import build_question_investigation
from question_planner import QuestionPlanner
from repository_discovery import ensure_repository_search


QUESTION = "JOB-STEP 批处理怎么trigger的？"
ACTIVATION_KINDS = {"entry_point", "caller_invocation", "activation_conditions", "scheduler_binding"}


class ActivationEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.write("job.cbl", "JOB-STEP", "COMPUTE OUT-AMOUNT = IN-AMOUNT + 1.\nGOBACK.",
            "01 IN-AMOUNT PIC 9(5) VALUE 2.\n01 OUT-AMOUNT PIC 9(5).")
        self.write("caller.cbl", "LAUNCHER", 'IF READY-FLAG = 1\nCALL "JOB-STEP"\nEND-IF.\nGOBACK.',
            "01 READY-FLAG PIC 9 VALUE 0.")

    def write(self, path, program, body, data="01 WORK-COUNT PIC 9."):
        text = (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {program}.\nDATA DIVISION.\n"
            f"WORKING-STORAGE SECTION.\n{data}\nPROCEDURE DIVISION.\nMAIN.\n{body}\n")
        self.source.joinpath(path).write_bytes(text.encode("utf-8"))

    def build(self):
        build_business_index(self.source, self.database, source_format="free", verify_content=True,
            framework_reference_path="")
        ensure_repository_search(self.database, self.source)

    def line(self, path, content):
        return next(number for number, line in enumerate(self.source.joinpath(path).read_text().splitlines(), 1)
                    if content in line)

    def page(self, path, first=1, last=None):
        raw = self.source.joinpath(path).read_bytes()
        lines = raw.decode("utf-8").splitlines()
        last = len(lines) if last is None else last
        return {"relative_path": path, "start_line": first, "end_line": last,
            "source_sha256": hashlib.sha256(raw).hexdigest(), "source_text": "\n".join(lines[first-1:last]),
            "evidence_id": f"source_{path}_{first}_{last}", "selection_reasons": ["question_match"]}

    def investigate(self, pages, question=QUESTION, **options):
        mapping = build_business_map(self.database, self.source, question)
        return build_question_investigation(question, mapping, database_path=self.database,
            source_pages=pages, **options)

    def item(self, result, kind):
        return next(item for item in result["required_items"] if item["kind"] == kind)

    def test_mixed_language_trigger_suffix_creates_all_activation_obligations(self):
        self.build()
        for question in (QUESTION, "JOB-STEP怎么trigger的？", "JOB-STEP如何triggered的？",
                         "JOB-STEP batch如何启动？"):
            with self.subTest(question=question):
                mapping = build_business_map(self.database, self.source, question)
                plan = QuestionPlanner().plan(question, mapping)
                self.assertIn("activation", plan.intents)
                self.assertTrue(ACTIVATION_KINDS <= set(plan.required_obligations))
                result = build_question_investigation(question, mapping, database_path=self.database,
                    source_pages=[self.page("job.cbl")])
                self.assertTrue(ACTIVATION_KINDS <= {row["kind"] for row in result["required_items"]})

    def test_actual_guarded_call_supports_static_call_and_condition_not_execution(self):
        self.build()
        result = self.investigate([self.page("job.cbl"), self.page("caller.cbl")])
        for kind in ("entry_point", "caller_invocation", "activation_conditions"):
            self.assertEqual(self.item(result, kind)["status"], "SATISFIED")
        caller = result["activation_evidence"]["callers"][0]
        self.assertEqual(caller["target_name"], "JOB-STEP")
        self.assertTrue(any(guard["kind"] == "IF" and guard["condition"] == "READY-FLAG = 1"
                            for guard in caller["conditions"]))
        self.assertFalse(caller["execution_verified"])
        self.assertFalse(result["activation_evidence"]["execution_verified"])
        self.assertNotEqual(self.item(result, "scheduler_binding")["status"], "SATISFIED")

    def test_caller_not_supplied_produces_concrete_source_read(self):
        self.build()
        result = self.investigate([self.page("job.cbl")])
        self.assertEqual(self.item(result, "entry_point")["status"], "SATISFIED")
        self.assertNotEqual(self.item(result, "caller_invocation")["status"], "SATISFIED")
        line = self.line("caller.cbl", 'CALL "JOB-STEP"')
        self.assertTrue(any(action["tool"] == "read" and action["arguments"]["relative_path"] == "caller.cbl"
            and action["arguments"]["start_line"] <= line <= action["arguments"]["end_line"]
            for action in result["planned_actions"]))

    def test_unrelated_supplied_source_cannot_satisfy_caller_conditions(self):
        self.write("other.cbl", "OTHER-STEP", "IF WORK-COUNT = 1\nDISPLAY WORK-COUNT\nEND-IF.\nGOBACK.")
        self.build()
        result = self.investigate([self.page("job.cbl"), self.page("other.cbl")])
        self.assertNotEqual(self.item(result, "caller_invocation")["status"], "SATISFIED")
        self.assertNotEqual(self.item(result, "activation_conditions")["status"], "SATISFIED")
        self.assertEqual(result["activation_evidence"]["callers"], [])

    def test_display_text_does_not_establish_a_call_even_with_an_index_hint(self):
        self.write("caller.cbl", "LAUNCHER", 'DISPLAY \'CALL "JOB-STEP" PERFORM LAUNCH\'.\nGOBACK.')
        self.build()
        mapping = build_business_map(self.database, self.source, QUESTION)
        self.assertFalse(any(edge["relation_type"] == "CALLS" for edge in mapping["relations"]))
        mapping["selected_paths"].append("caller.cbl")
        mapping["relations"].append({"relation_type": "CALLS", "resolution": "confirmed",
            "caller_path": "caller.cbl", "caller_line": self.line("caller.cbl", "DISPLAY"),
            "target_name": "JOB-STEP", "target_path": "job.cbl"})
        result = investigate_activation(mapping, [self.page("job.cbl"), self.page("caller.cbl")])
        self.assertNotEqual(self.item(result, "caller_invocation")["status"], "SATISFIED")
        self.assertEqual(result["activation_evidence"]["callers"], [])

    def test_unknown_compilation_condition_remains_a_candidate(self):
        self.write("caller.cbl", "LAUNCHER", '>>IF ENABLE_JOB\nIF READY-FLAG = 1\nCALL "JOB-STEP"\n'
            'END-IF\n>>END-IF\nGOBACK.', "01 READY-FLAG PIC 9 VALUE 0.")
        self.build()
        result = self.investigate([self.page("job.cbl"), self.page("caller.cbl")])
        self.assertNotEqual(self.item(result, "caller_invocation")["status"], "SATISFIED")
        self.assertNotEqual(self.item(result, "activation_conditions")["status"], "SATISFIED")
        self.assertTrue(result["activation_evidence"]["callers"])
        self.assertTrue(all(row["compilation_status"] == "candidate"
                            for row in result["activation_evidence"]["callers"]))

    def test_call_only_excerpt_does_not_erase_an_unsupplied_if_guard(self):
        self.build()
        call = self.line("caller.cbl", 'CALL "JOB-STEP"')
        guard = self.line("caller.cbl", "IF READY-FLAG")
        result = self.investigate([self.page("job.cbl"), self.page("caller.cbl", call, call)])
        self.assertEqual(self.item(result, "caller_invocation")["status"], "SATISFIED")
        self.assertNotEqual(self.item(result, "activation_conditions")["status"], "SATISFIED")
        self.assertEqual(result["activation_evidence"]["callers"][0]["conditions"], [])
        self.assertTrue(any(action["tool"] == "read" and action["arguments"]["relative_path"] == "caller.cbl"
            and action["arguments"]["start_line"] <= guard <= action["arguments"]["end_line"]
            for action in result["planned_actions"]))

    def test_compilation_directive_outside_excerpt_cannot_be_assumed_active(self):
        self.write("caller.cbl", "LAUNCHER", '>>IF ENABLE_JOB\nCALL "JOB-STEP"\n>>END-IF\nGOBACK.')
        self.build()
        call = self.line("caller.cbl", 'CALL "JOB-STEP"')
        result = self.investigate([self.page("job.cbl"), self.page("caller.cbl", call, call)])
        self.assertNotEqual(self.item(result, "caller_invocation")["status"], "SATISFIED")
        self.assertNotEqual(self.item(result, "activation_conditions")["status"], "SATISFIED")

    def test_guard_location_uses_physical_source_lines_in_a_partial_page(self):
        self.build()
        call = self.line("caller.cbl", 'CALL "JOB-STEP"')
        guard = self.line("caller.cbl", "IF READY-FLAG")
        result = self.investigate([self.page("job.cbl"), self.page("caller.cbl", guard, call+1)])
        caller = result["activation_evidence"]["callers"][0]
        self.assertEqual(caller["line"], call)
        supplied_guard = next(row for row in caller["conditions"] if row["kind"] == "IF")
        self.assertEqual(supplied_guard["span"]["start_line"], guard)
        self.assertEqual(supplied_guard["span"]["end_line"], guard)

    def test_program_name_comment_and_static_call_do_not_establish_schedule(self):
        self.write("caller.cbl", "NIGHTLY-LAUNCHER", '*> Schedule note: daily at 02:00\n'
            'CALL "JOB-STEP".\nGOBACK.')
        self.build()
        result = self.investigate([self.page("job.cbl"), self.page("caller.cbl")])
        item = self.item(result, "scheduler_binding")
        self.assertNotEqual(item["status"], "SATISFIED")
        self.assertEqual(item["evidence_ids"], [])
        self.assertFalse(result["activation_evidence"]["scheduler_binding_verified"])
        self.assertFalse(result["activation_evidence"]["execution_verified"])

    def test_activation_obligations_survive_a_combined_calculation_question(self):
        self.build()
        result = self.investigate([self.page("job.cbl"), self.page("caller.cbl")],
            question="JOB-STEP批处理怎么trigger的，OUT-AMOUNT怎么计算？")
        self.assertTrue(ACTIVATION_KINDS <= set(result["analysis_plan"]["required_obligations"]))
        self.assertTrue(ACTIVATION_KINDS <= {row["kind"] for row in result["required_items"]})
        self.assertIn("formula", {row["kind"] for row in result["required_items"]})

    def test_rule_trigger_condition_does_not_require_an_external_scheduler(self):
        self.build()
        result = self.investigate([self.page("job.cbl")],
            question="JOB-STEP 出账触发条件和金额如何计算？")
        self.assertNotIn("activation", result["analysis_plan"]["intents"])
        self.assertNotIn("scheduler_binding", result["analysis_plan"]["required_obligations"])
        self.assertIn("control_conditions", result["analysis_plan"]["required_obligations"])

    def test_missing_guard_is_read_even_when_far_before_the_call(self):
        self.write("caller.cbl", "LAUNCHER", "IF READY-FLAG = 1\n" +
            "\n".join("DISPLAY READY-FLAG" for _ in range(40)) + '\nCALL "JOB-STEP"\nEND-IF.\nGOBACK.',
            "01 READY-FLAG PIC 9.")
        self.build()
        call = self.line("caller.cbl", 'CALL "JOB-STEP"')
        guard = self.line("caller.cbl", "IF READY-FLAG")
        result = self.investigate([self.page("job.cbl"), self.page("caller.cbl", call, call)])
        self.assertNotEqual(self.item(result, "activation_conditions")["status"], "SATISFIED")
        self.assertTrue(any(action["arguments"]["start_line"] <= guard <= action["arguments"]["end_line"]
            for action in result["planned_actions"] if action["arguments"]["relative_path"] == "caller.cbl"))

    def test_line_304_call_cannot_satisfy_conditions_without_line_3_guard(self):
        self.source.joinpath("caller.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. LAUNCHER. PROCEDURE DIVISION.\n"
            "IF READY-FLAG = 1\n" + "DISPLAY READY-FLAG\n" * 300
            + 'CALL "JOB-STEP"\nEND-IF.\nGOBACK.\n')
        self.build()
        self.assertEqual(self.line("caller.cbl", "IF READY-FLAG"), 3)
        self.assertEqual(self.line("caller.cbl", 'CALL "JOB-STEP"'), 304)
        result = self.investigate([self.page("job.cbl"), self.page("caller.cbl", 304, 304)])
        self.assertNotEqual(self.item(result, "activation_conditions")["status"], "SATISFIED")
        self.assertTrue(any(action["arguments"]["start_line"] <= 3 <= action["arguments"]["end_line"]
            for action in result["planned_actions"] if action["arguments"]["relative_path"] == "caller.cbl"))

    def test_page_hash_and_contents_must_both_match_the_indexed_source(self):
        self.build()
        original = self.page("caller.cbl")
        for modified in ({**original, "source_sha256": "0" * 64},
                         {**original, "source_text": original["source_text"].replace('CALL "JOB-STEP"',
                             'DISPLAY "JOB-STEP"')}):
            result = self.investigate([self.page("job.cbl"), modified])
            self.assertNotEqual(self.item(result, "caller_invocation")["status"], "SATISFIED")
            self.assertTrue(result["planned_actions"])

    def test_repeated_request_fitting_reuses_candidates_but_rechecks_supplied_pages(self):
        self.build()
        cache = {}
        original = activation_evidence._candidates
        with mock.patch.object(activation_evidence, "_candidates", wraps=original) as candidates:
            before = self.investigate([self.page("job.cbl")], candidate_cache=cache)
            after = self.investigate([self.page("job.cbl"), self.page("caller.cbl")], candidate_cache=cache)
        self.assertEqual(candidates.call_count, 1)
        self.assertNotEqual(self.item(before, "caller_invocation")["status"], "SATISFIED")
        self.assertEqual(self.item(after, "activation_conditions")["status"], "SATISFIED")

    def test_unknown_compiled_caller_is_found_through_existing_fts_path_indexes(self):
        self.write("caller.cbl", "LAUNCHER", '>>IF ENABLE_JOB\nCALL "JOB-STEP"\n>>END-IF\nGOBACK.')
        self.build()
        mapping = build_business_map(self.database, self.source, QUESTION)
        self.assertNotIn("caller.cbl", mapping["selected_paths"])
        result = investigate_activation(mapping, [self.page("job.cbl")], database_path=self.database)
        self.assertGreater(result["activation_evidence"]["candidate_call_count"], 0)
        self.assertTrue(any(action["arguments"]["relative_path"] == "caller.cbl"
                            for action in result["planned_actions"]))
        with closing(sqlite3.connect(self.database)) as db:
            with db:
                plan = db.execute("EXPLAIN QUERY PLAN SELECT DISTINCT relative_path FROM relations "
                    "WHERE relative_path IN (?,?) AND target_name=? AND relation_type IN ('CALLS','CALL_TARGET_FROM')",
                    ("caller.cbl", "job.cbl", "JOB-STEP")).fetchall()
        self.assertTrue(any("SEARCH" in row[3] and "repo_relations_path" in row[3] for row in plan))

    def test_fts_candidate_limits_leave_an_explicit_frontier(self):
        for number in range(36):
            self.write(f"note-{number:02d}.cbl", f"NOTE-{number:02d}", 'DISPLAY "JOB-STEP".\nGOBACK.')
        self.build()
        result = self.investigate([self.page("job.cbl"), self.page("caller.cbl")])
        gap = next(gap for gap in result["open_gaps"] if gap.get("reason") == "caller_lookup_frontier")
        self.assertGreater(gap["candidate_path_count"], 32)
        self.assertGreater(gap["remaining"], 0)

    def test_same_line_calls_keep_distinct_occurrences_and_branch_outcomes(self):
        self.write("caller.cbl", "LAUNCHER", 'IF READY-FLAG = 1 CALL "JOB-STEP" ELSE CALL "JOB-STEP" END-IF.\nGOBACK.',
            "01 READY-FLAG PIC 9.")
        self.build()
        result = self.investigate([self.page("job.cbl"), self.page("caller.cbl")])
        callers = result["activation_evidence"]["callers"]
        self.assertEqual(len(callers), 2)
        self.assertEqual(len({row["occurrence_id"] for row in callers}), 2)
        self.assertEqual({row["conditions"][0]["outcome"] for row in callers}, {True, False})

    def test_else_call_needs_its_branch_marker_as_well_as_if_header(self):
        self.write("caller.cbl", "LAUNCHER", 'IF READY-FLAG = 1\nDISPLAY READY-FLAG\nELSE\nCALL "JOB-STEP"\nEND-IF.\nGOBACK.',
            "01 READY-FLAG PIC 9.")
        self.build()
        call, guard = self.line("caller.cbl", 'CALL "JOB-STEP"'), self.line("caller.cbl", "IF READY-FLAG")
        result = self.investigate([self.page("job.cbl"), self.page("caller.cbl", guard, guard),
            self.page("caller.cbl", call, call)])
        self.assertNotEqual(self.item(result, "activation_conditions")["status"], "SATISFIED")
        complete = self.investigate([self.page("job.cbl"), self.page("caller.cbl")])
        self.assertEqual(self.item(complete, "activation_conditions")["status"], "SATISFIED")

    def test_trailing_physical_blank_lines_do_not_make_scope_incomplete(self):
        path = self.source / "caller.cbl"
        path.write_bytes(path.read_bytes() + b"\n\n")
        self.build()
        result = self.investigate([self.page("job.cbl"), self.page("caller.cbl")])
        self.assertEqual(self.item(result, "activation_conditions")["status"], "SATISFIED")


if __name__ == "__main__":
    unittest.main()
