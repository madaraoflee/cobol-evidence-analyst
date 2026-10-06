"""Offline candidate coverage for bounded input chains and business steps."""

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


class BusinessDetailPlannerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"

    def write(self, path, name, body, data=""):
        (self.source / path).write_text(
            f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
            f"WORKING-STORAGE SECTION.\n{data}\nPROCEDURE DIVISION.\nMAIN.\n{body}\nGOBACK.\n")

    def build(self):
        build_business_index(self.source, self.database, source_format="free")
        ensure_repository_search(self.database, self.source)

    def page(self, path, literal=None):
        text = (self.source / path).read_text()
        lines = text.splitlines()
        first = next((i for i, line in enumerate(lines, 1) if literal in line), 1) if literal else 1
        last = first if literal else len(lines)
        return {"relative_path": path, "start_line": first, "end_line": last,
                "source_text": "\n".join(lines[first - 1:last]),
                "source_sha256": hashlib.sha256((self.source / path).read_bytes()).hexdigest(),
                "evidence_id": f"ev_{path}_{first}_{last}", "selection_reasons": ["question_match"]}

    def ask(self, question, pages, **options):
        mapping = build_business_map(self.database, self.source, question)
        return build_question_investigation(question, mapping, database_path=self.database,
            source_pages=pages, **options)

    def item(self, result, kind):
        return next(item for item in result["required_items"] if item["kind"] == kind)

    def test_first_assignment_does_not_supply_recursive_input_chain(self):
        self.write("chain.cbl", "VALUEFLOW", "COMPUTE NET-VALUE = BASE-VALUE * 2.\n"
            "MOVE MID-VALUE TO BASE-VALUE.\nMOVE RAW-VALUE TO MID-VALUE.\nMOVE 11 TO RAW-VALUE.")
        self.build()
        result = self.ask("VALUEFLOW NET-VALUE 怎么计算，输入来自哪里？",
            [self.page("chain.cbl", "COMPUTE"), self.page("chain.cbl", "MOVE MID")])
        self.assertNotEqual(self.item(result, "inputs")["status"], "SATISFIED")
        self.assertFalse(result["can_answer"])
        planned_lines = {action["arguments"].get("line") for action in result["planned_actions"]}
        self.assertIn(self.page("chain.cbl", "MOVE RAW")["start_line"], planned_lines)
        complete = self.ask("VALUEFLOW NET-VALUE 怎么计算，输入来自哪里？", [self.page("chain.cbl")])
        self.assertEqual(self.item(complete, "inputs")["status"], "SATISFIED")
        self.assertFalse(complete["planned_actions"])
        self.assertFalse(complete["semantic_execution_verified"])

    def test_recursive_terminal_input_remains_unknown(self):
        self.write("chain.cbl", "VALUEFLOW", "COMPUTE NET-VALUE = BASE-VALUE * 2.\n"
            "MOVE MID-VALUE TO BASE-VALUE.\nMOVE RAW-VALUE TO MID-VALUE.")
        self.build()
        result = self.ask("VALUEFLOW 计算规则", [self.page("chain.cbl")])
        self.assertEqual(self.item(result, "inputs")["reason"], "input_source_not_located")
        self.assertIn("RAW-VALUE", self.item(result, "inputs")["fields"])

    def test_hidden_terminal_value_declaration_gets_inspection(self):
        self.write("chain.cbl", "VALUEFLOW", "MOVE RAW-VALUE TO BASE-VALUE.\n"
            "COMPUTE NET-VALUE = BASE-VALUE * 2.", "01 RAW-VALUE PIC 9(9) VALUE 11.")
        self.build()
        before = self.ask("VALUEFLOW 计算规则", [self.page("chain.cbl", "MOVE RAW"),
                                                 self.page("chain.cbl", "COMPUTE")])
        self.assertTrue(any(action["reason"] == "input_origin_not_supplied" and
                            "RAW-VALUE" in action["arguments"]["fields"] for action in before["planned_actions"]))
        after = self.ask("VALUEFLOW 计算规则", [self.page("chain.cbl")])
        self.assertEqual(self.item(after, "inputs")["status"], "SATISFIED")
        self.assertFalse(after["planned_actions"])

    def test_cycles_and_depth_limits_are_explicit(self):
        for name, body, reason in (
            ("CYCLEFLOW", "MOVE MID-VALUE TO BASE-VALUE.\nMOVE BASE-VALUE TO MID-VALUE.", "input_dependency_cycle"),
            ("DEPTHFLOW", "\n".join(f"MOVE FIELD-{i + 1} TO {'BASE-VALUE' if i == 0 else f'FIELD-{i}'}." for i in range(7)), "input_depth_budget"),
        ):
            with self.subTest(name=name):
                self.write("chain.cbl", name, "COMPUTE NET-VALUE = BASE-VALUE * 2.\n" + body)
                self.build()
                result = self.ask(f"{name} 计算规则", [self.page("chain.cbl")])
                self.assertNotEqual(self.item(result, "inputs")["status"], "SATISFIED")
                self.assertIn(reason, {gap["reason"] for gap in result["open_gaps"]})

    def test_copy_input_is_readable_candidate_with_physical_source(self):
        self.write("host.cbl", "COPYFLOW", "COPY INPUTDATA.\nCOMPUTE NET-VALUE = BASE-VALUE * 2.\n"
            "MOVE COPY-BASE TO BASE-VALUE.")
        (self.source / "inputdata.cpy").write_text("01 COPY-BASE PIC 9(9) VALUE 11.\n")
        self.build()
        before = self.ask("COPYFLOW 计算规则", [self.page("host.cbl")])
        self.assertTrue(any(action["arguments"]["relative_path"] == "inputdata.cpy"
                            for action in before["planned_actions"]))
        after = self.ask("COPYFLOW 计算规则", [self.page("host.cbl"), self.page("inputdata.cpy")])
        self.assertEqual(self.item(after, "inputs")["status"], "SATISFIED")
        self.assertIn(self.page("inputdata.cpy")["evidence_id"], self.item(after, "inputs")["evidence_ids"])
        self.assertFalse(after["semantic_execution_verified"])

    def test_business_flow_requires_nonarithmetic_callee_steps(self):
        self.write("caller.cbl", "REQUESTFLOW", "CALL 'FLAGFLOW' USING RESULT-FLAG.")
        self.write("leaf.cbl", "FLAGFLOW", "MOVE 'Y' TO RESULT-FLAG.")
        self.build()
        before = self.ask("REQUESTFLOW 的处理目的和返回标志如何决定？", [self.page("caller.cbl")])
        self.assertEqual(self.item(before, "formula")["status"], "NOT_APPLICABLE")
        self.assertNotEqual(self.item(before, "business_steps")["status"], "SATISFIED")
        self.assertTrue(any(action["arguments"]["relative_path"] == "leaf.cbl"
                            for action in before["planned_actions"]))
        self.assertFalse(before["can_answer"])
        after = self.ask("REQUESTFLOW 的处理目的和返回标志如何决定？",
                         [self.page("caller.cbl"), self.page("leaf.cbl")])
        self.assertEqual(self.item(after, "business_steps")["status"], "SATISFIED")
        self.assertTrue(after["can_answer"])

    def test_dynamic_target_is_not_declared_external_implementation(self):
        self.write("caller.cbl", "DYNAMICFLOW", "MOVE 'VALUELEAF' TO ROUTINE-NAME.\n"
            "CALL ROUTINE-NAME USING NET-VALUE.")
        self.write("leaf.cbl", "VALUELEAF", "COMPUTE NET-VALUE = 2.")
        self.build()
        result = self.ask("DYNAMICFLOW NET-VALUE 怎么计算？", [self.page("caller.cbl")])
        self.assertEqual(self.item(result, "dependencies")["reason"], "runtime_target_unresolved")
        self.assertFalse(any(action["arguments"]["relative_path"] == "leaf.cbl"
                             for action in result["planned_actions"]))

    def test_true_external_limits_only_its_obligation(self):
        self.write("caller.cbl", "EXTERNALFLOW", "MOVE 11 TO BASE-VALUE.\n"
            "COMPUTE NET-VALUE = BASE-VALUE * 2.\nCALL 'UNAVAILABLEFLOW' USING NET-VALUE.")
        self.build()
        result = self.ask("EXTERNALFLOW NET-VALUE 怎么计算？", [self.page("caller.cbl")])
        self.assertEqual(self.item(result, "formula")["status"], "SATISFIED")
        self.assertEqual(self.item(result, "dependencies")["reason"], "external_implementation_unavailable")
        self.assertTrue(result["can_answer"])
        self.assertFalse(result["planned_actions"])

    def test_existing_action_cap_is_not_enlarged(self):
        self.write("chain.cbl", "VALUEFLOW", "COMPUTE NET-VALUE = BASE-VALUE * 2.\n"
            "MOVE MID-VALUE TO BASE-VALUE.\nMOVE RAW-VALUE TO MID-VALUE.\nMOVE 11 TO RAW-VALUE.")
        self.build()
        result = self.ask("VALUEFLOW 计算规则", [self.page("chain.cbl", "COMPUTE")], max_actions=1)
        self.assertEqual(len(result["planned_actions"]), 1)


if __name__ == "__main__":
    unittest.main()
