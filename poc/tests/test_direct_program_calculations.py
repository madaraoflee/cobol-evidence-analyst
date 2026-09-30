from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_chat import run_business_chat
from business_index import _ensure_business_rules, build_business_index
from business_map import _rule_leads, build_business_map
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


class DirectProgramCalculationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        header = (
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. CALC001.\n"
            "*> 保费计算规则\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 WS-AMOUNT PIC 9(9)V99.\n01 WS-RATE PIC 9V999.\n"
            "01 WS-RESULT PIC 9(9)V99.\n01 WS-COUNT PIC 9(9).\n"
            "PROCEDURE DIVISION.\nMAIN-START.\n"
        )
        before = "".join(f"MOVE {index} TO WS-COUNT.\n" for index in range(800))
        calculation = (
            "IF WS-AMOUNT > 0\n"
            "COMPUTE WS-RESULT ROUNDED = WS-AMOUNT * WS-RATE\n"
            "ELSE\nMOVE 0 TO WS-RESULT\nEND-IF.\n"
        )
        after = "".join(f"MOVE {index} TO WS-COUNT.\n" for index in range(800, 6000))
        (self.source / "calc001.cbl").write_text(header + before + calculation + after + "GOBACK.\n", encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free")
        ensure_repository_search(self.database, self.source)

    def test_named_program_map_includes_its_distant_calculation(self):
        result = build_business_map(self.database, self.source, "分析 CALC001 的保费计算公式")
        self.assertEqual(result["direct_paths"], ["calc001.cbl"])
        calculation = next((rule for rule in result["rule_leads"] if rule["rule_kind"] == "COMPUTE"), None)
        self.assertIsNotNone(calculation, "A direct program's calculation must not be hidden by early MOVE statements")
        self.assertGreater(calculation["start_line"], 800)
        self.assertTrue(any(item["relative_path"] == "calc001.cbl"
                            and item["start_line"] <= calculation["start_line"] <= item["end_line"]
                            for item in result["spotlights"]))

    def test_business_description_supplies_deep_formula_and_condition_to_model(self):
        observed = []

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            observed.append(payload)
            pages = [page for bundle in payload["source_context"] for page in bundle.get("pages", [])]
            calculation = next((page for page in pages if "COMPUTE WS-RESULT ROUNDED" in page["source_text"]), None)
            self.assertIsNotNone(calculation, "The verified calculation must reach the actual model request")
            self.assertIn("IF WS-AMOUNT > 0", calculation["source_text"])
            self.assertIn("MOVE 0 TO WS-RESULT", calculation["source_text"])
            answer = f"金额大于零时按金额乘费率计算并舍入，否则结果为零。[{calculation['evidence_id']}]"
            return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": answer},
                                                                  "finish_reason": "stop"}]}))

        output = run_business_chat("保费计算规则是什么？", self.database, self.source,
            CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="local-test-key"),
            transport=transport, framework_reference_path="")
        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertEqual(len(observed), 1)
        self.assertIn("金额乘费率", output["agent_result"]["answer"])


class LocalRuleLookupWorkTests(unittest.TestCase):
    def test_common_field_operations_outside_selected_file_do_not_expand_local_lookup_work(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        _ensure_business_rules(connection)

        def insert_rules(prefix, relative, count):
            rows = [(f"{prefix}{index}", relative, "CALC001", "MAIN", "COMPUTE",
                     10 + index, 10 + index, 1,
                     f"COMPUTE OUTPUT-AMOUNT = INPUT-AMOUNT * {index + 1}",
                     '["INPUT-AMOUNT"]', '["OUTPUT-AMOUNT"]', "[]")
                    for index in range(count)]
            connection.executemany("INSERT INTO business_rules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            connection.executemany("INSERT INTO business_rule_fields VALUES (?,?,?)",
                [(row[0], name, role) for row in rows
                 for name, role in (("INPUT-AMOUNT", "read"), ("OUTPUT-AMOUNT", "write"))])

        def measured_lookup():
            steps = 0

            def progress():
                nonlocal steps
                steps += 1
                return 0

            connection.set_progress_handler(progress, 1)
            try:
                leads = _rule_leads(connection, {"target.cbl"},
                    [{"relative_path": "target.cbl", "start_line": 1, "end_line": 100}], "计算规则", None)
            finally:
                connection.set_progress_handler(None, 0)
            return leads, steps

        insert_rules("target_", "target.cbl", 3)
        before, before_steps = measured_lookup()
        insert_rules("other_", "unrelated.cbl", 5000)
        after, after_steps = measured_lookup()
        self.assertEqual(after, before)
        self.assertEqual(len(after), 3)
        # Count database work, not wall time: the other file has the same common
        # fields but must not be joined before the selected file is scoped.
        self.assertLess(after_steps, before_steps * 4)


if __name__ == "__main__":
    unittest.main()
