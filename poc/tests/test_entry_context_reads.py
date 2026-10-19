"""Entry-context reads retain the existing activation action and its bounds."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from activation_evidence import investigate_activation
from business_index import build_business_index
from business_map import build_business_map
from question_investigation import build_question_investigation, _entry_context_reads
from repository_discovery import ensure_repository_search


class EntryContextReadTests(unittest.TestCase):
    question = "ENTRY-FLOW 从入口 COPY 骨架怎样启动？说明完整计算公式。"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"

    def build(self, *, padding=0, second=False):
        text = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRY-FLOW.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 IN-AMOUNT PIC 9(5) VALUE 2.\n01 OUT-AMOUNT PIC 9(7).\n"
            + "*> ordinary data context\n" * padding + "PROCEDURE DIVISION.\nMAIN.\n"
            "COMPUTE OUT-AMOUNT = IN-AMOUNT * 3.\n" + "*> ordinary body context\n" * 120 + "GOBACK.\n")
        self.source.joinpath("entry.cbl").write_text(text)
        if second:
            self.source.joinpath("other.cbl").write_text(text.replace("ENTRY-FLOW", "OTHER-FLOW"))
        build_business_index(self.source, self.database, source_format="free", quiet=True)
        ensure_repository_search(self.database, self.source)
        self.mapping = build_business_map(self.database, self.source, self.question)
        self.activation = investigate_activation(self.mapping, [], database_path=self.database)

    @staticmethod
    def entry_action(result):
        return next(action for action in result["planned_actions"] if action["reason"] == "entry_definition_not_supplied")

    def test_explicit_unique_entry_widens_same_action_and_keeps_semantic_gap(self):
        self.build()
        original = deepcopy(self.activation)
        changed = _entry_context_reads(self.activation, self.question, self.database, [])
        self.assertEqual(self.activation, original)
        self.assertEqual(len(changed["planned_actions"]), len(original["planned_actions"]))
        self.assertEqual(self.entry_action(changed)["arguments"], {"relative_path": "entry.cbl", "start_line": 1, "end_line": 96})
        self.assertEqual(self.entry_action(changed)["source_context_basis"], "bounded_entry_context")
        self.assertFalse(self.entry_action(changed)["semantic_execution_verified"])
        self.assertEqual(changed["required_items"], original["required_items"])
        self.assertEqual(changed["activation_evidence"], original["activation_evidence"])
        result = build_question_investigation(self.question, self.mapping, database_path=self.database)
        self.assertIn("activation", result["analysis_plan"]["intents"])
        self.assertEqual(self.entry_action(result)["arguments"]["start_line"], 1)

    def test_late_signature_reads_its_neighborhood_not_an_unrelated_header(self):
        self.build(padding=300)
        before = self.entry_action(self.activation)["arguments"]["start_line"]
        changed = self.entry_action(_entry_context_reads(self.activation, self.question, self.database, []))
        args = changed["arguments"]
        self.assertEqual(args["start_line"], before - 32)
        self.assertGreater(args["start_line"], 1)
        self.assertLessEqual(args["start_line"], before)
        self.assertGreaterEqual(args["end_line"], before)
        self.assertLessEqual(args["end_line"] - args["start_line"] + 1, 96)

    def test_exclusive_procedure_or_line_scope_does_not_expand(self):
        self.build()
        for scope in ({"relative_path": "entry.cbl", "start_line": 8, "end_line": 130, "range_basis": "indexed_procedure"},
                      {"relative_path": "entry.cbl", "start_line": 7, "end_line": 7, "exclusive": True}):
            with self.subTest(scope=scope):
                self.assertEqual(_entry_context_reads(self.activation, self.question, self.database, [scope]), self.activation)
        question = "ENTRY-FLOW 只看 MAIN 段，从入口怎样启动？"
        mapping = build_business_map(self.database, self.source, question)
        result = build_question_investigation(question, mapping, database_path=self.database)
        self.assertTrue(result["source_scope"]["ranges"])
        self.assertNotIn("source_context_basis", self.entry_action(result))

    def test_explicit_physical_line_request_is_not_widened_without_parsed_scope(self):
        self.build()
        for suffix in ("只看第7行", "仅限7-9行", "only lines 7-9", "entry.cbl:7-9"):
            with self.subTest(suffix=suffix):
                self.assertEqual(_entry_context_reads(self.activation, self.question + suffix, self.database, []), self.activation)

    def test_multiple_program_entry_candidates_are_not_widened(self):
        self.build(second=True)
        question = "ENTRY-FLOW 和 OTHER-FLOW 的入口 COPY 骨架怎样启动？"
        mapping = build_business_map(self.database, self.source, question)
        activation = investigate_activation(mapping, [], database_path=self.database)
        self.assertEqual(next(item for item in activation["required_items"] if item["kind"] == "entry_point")["candidate_count"], 2)
        self.assertEqual(_entry_context_reads(activation, question, self.database, []), activation)

    def test_trigger_without_entry_request_and_filename_alone_do_not_expand(self):
        self.build()
        for question in ("ENTRY-FLOW batch如何启动？", "entry.cbl batch如何启动？", "skeleton.cbl batch trigger?"):
            with self.subTest(question=question):
                self.assertEqual(_entry_context_reads(self.activation, question, self.database, []), self.activation)
        question = "ENTRY-FLOW 的入口公式是什么？"
        result = build_question_investigation(question, self.mapping, database_path=self.database)
        self.assertNotIn("activation", result["analysis_plan"]["intents"])
        self.assertFalse(any(action.get("source_context_basis") for action in result["planned_actions"]))

    def test_no_allowance_and_multiline_entry_keep_original_actions(self):
        self.build()
        limited = investigate_activation(self.mapping, [], database_path=self.database, max_actions=0)
        self.assertEqual(_entry_context_reads(limited, self.question, self.database, [])["planned_actions"], [])
        multi = deepcopy(self.activation)
        self.entry_action(multi)["arguments"]["end_line"] += 1
        self.assertEqual(_entry_context_reads(multi, self.question, self.database, []), multi)


if __name__ == "__main__":
    unittest.main()
