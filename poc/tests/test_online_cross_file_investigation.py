"""Cross-file original-source discovery through the ordinary online question flow."""
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).resolve().parents[1])]
import test_online_investigation as scenarios
from business_index import build_business_index
from company_api import CompanyAPIConfig
from online_investigation import _action
from online_material_export import export_material
from online_material_format import MaterialError
from online_material_query import MaterialQuery
from online_material_reader import DirectoryObjectStore, MaterialReader


class OnlineCrossFileInvestigationTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        source = root / "source"
        source.mkdir()
        self.fixture = scenarios.OnlineInvestigationTests()
        self.fixture.source_bytes = {
            "entry.cbl": b"IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRYJOB.\nDATA DIVISION.\n"
                         b"LINKAGE SECTION.\n01 PLAN-ID PIC X(16).\n01 ACC-BAL PIC 9(8).\n"
                         b"01 GROSS-AMT PIC 9(8).\n01 CREDIT-AMT PIC 9(8).\n"
                         b"PROCEDURE DIVISION USING PLAN-ID ACC-BAL GROSS-AMT CREDIT-AMT.\n"
                         b"IF PLAN-ID = 'N1-CRDKEYOPTION'\n CALL 'NETCALC' USING ACC-BAL GROSS-AMT CREDIT-AMT\n"
                         b"END-IF.\nGOBACK.",
            "notice.cbl": b"IDENTIFICATION DIVISION.\nPROGRAM-ID. NOTICE.\nPROCEDURE DIVISION.\n"
                          b"DISPLAY 'CRDKEY'.\nGOBACK.",
            "worker.cbl": b"IDENTIFICATION DIVISION.\nPROGRAM-ID. NETCALC.\nDATA DIVISION.\n"
                          b"LINKAGE SECTION.\n01 NET-DUE PIC 9(8).\n01 BASE-DUE PIC 9(8).\n"
                          b"01 PAID-AMT PIC 9(8).\nPROCEDURE DIVISION USING NET-DUE BASE-DUE PAID-AMT.\n"
                          b"COMPUTE NET-DUE = BASE-DUE - PAID-AMT.\nGOBACK.",
        }
        for path, raw in self.fixture.source_bytes.items():
            (source / path).write_bytes(raw)
        database = root / "index.sqlite"
        report = build_business_index(source, database, source_format="free", verify_content=True,
                                      framework_reference_path="")
        material = root / "material"
        export_material(database, source, material, repository_id="example/source",
                        commit="a" * 40, build_report=report)
        self.fixture.query = MaterialQuery(MaterialReader(DirectoryObjectStore(material)))
        self.fixture.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model",
                                               api_key="test-secret")
        self.fixture.requests, self.fixture.contexts, self.fixture.source_calls, self.fixture.events = [], [], [], []

    def test_missing_token_finds_pinned_caller_and_resumes_into_renamed_worker_formula(self):
        question = "CRDKEYOPT 对应的业务条件如何影响最终金额？"

        def use_suggestion(context):
            result = context["observations"][-1]["result"]
            self.assertEqual(result["total_matches"], 0)
            self.assertEqual(result["suggested_action"]["tool"], "locate_global")
            return {"actions": [result["suggested_action"]]}

        first = self.fixture.run_question([{"actions": [{"tool": "search", "text": "CRDKEYOPT"}]},
                                           use_suggestion], question=question, max_model_calls=2)
        self.assertEqual(first["status"], "needs_continuation", first["error"])
        self.assertFalse(first["citations"])

        def read_hit(context):
            hit, = context["observations"][-1]["result"]["matches"]
            self.assertEqual(context["source_context"], [])
            return {"actions": [scenarios.read(hit["path"], hit["line"], hit["line"] + 2)]}

        def find_target(context):
            caller = context["source_context"][0]["text"]
            name = re.search(r"CALL '([A-Z]+)' USING ACC-BAL", caller).group(1)
            self.assertIn("IF PLAN-ID = 'N1-CRDKEYOPTION'", caller)
            return {"actions": [{"tool": "locate_global", "text": "PROGRAM-ID. " + name}]}

        def read_target(context):
            hit, = context["observations"][-1]["result"]["matches"]
            return {"actions": [scenarios.read(hit["path"], hit["line"], hit["line"] + 7)]}

        def verify_answer(context):
            originals = {page["citation"]["path"]: page["text"] for page in context["source_context"]}
            self.assertIn("PROCEDURE DIVISION USING NET-DUE BASE-DUE PAID-AMT.", originals["worker.cbl"])
            self.assertIn("COMPUTE NET-DUE = BASE-DUE - PAID-AMT.", originals["worker.cbl"])
            self.assertIn("CALL 'NETCALC' USING ACC-BAL GROSS-AMT CREDIT-AMT", originals["entry.cbl"])
            return scenarios.answer(context, "该计划进入金额计算，结果为基础应收减去已付金额。")

        second = self.fixture.run_question([read_hit, find_target, read_target, verify_answer],
                                           question=question, checkpoint=first["checkpoint"])
        self.assertEqual(second["status"], "answered", second["error"])
        self.assertCountEqual([item["path"] for item in second["citations"]], ["worker.cbl", "entry.cbl"])
        self.assertFalse(second["coverage"]["full_repository_or_runtime_proof"])
        self.assertEqual(sum(item["action"]["tool"] == "search" for item in second["checkpoint"]["observations"]), 1)

    def test_display_only_token_hit_offers_substring_route_to_additional_condition(self):
        def inspect_hit(context):
            result = context["observations"][-1]["result"]
            self.assertEqual([item["relative_path"] for item in result["matches"]], ["notice.cbl"])
            return {"actions": [result["literal_action"]]}

        result = self.fixture.run_question([{"actions": [{"tool": "search", "text": "CRDKEY"}]}, inspect_hit],
                                           question="Which condition applies to CRDKEY?", max_model_calls=2)
        observation = result["checkpoint"]["observations"][-1]
        self.assertEqual(observation["action"]["tool"], "locate_global")
        self.assertCountEqual([(hit["path"], hit["line"]) for hit in observation["result"]["matches"]],
                              [("entry.cbl", 10), ("notice.cbl", 4)])
        self.assertFalse(observation["result"]["absence_established"])
        self.assertFalse(result["citations"])
        self.assertEqual(result["checkpoint"]["evidence"], [])

    def test_global_literal_rejects_malformed_model_actions(self):
        action = {"tool": "locate_global", "text": "fragment"}
        self.assertIsNone(_action(action)["cursor"])
        for invalid in ({"text": "A\nB"}, {"text": " "}, {"limit": 21}, {"cursor": []},
                        {"cursor": {"start": [1]}}, {"path": "../outside"}):
            with self.subTest(invalid=invalid), self.assertRaises(MaterialError):
                _action({**action, **invalid})

    def test_paged_global_locations_resume_without_crediting_formula_review(self):
        question = "List all premium calculation formulas across the entire repository."
        action = {"tool": "locate_global", "text": "CRDKEY", "limit": 1}
        first = self.fixture.run_question([{"actions": [action]}], question=question, max_model_calls=1)
        first_page = first["checkpoint"]["observations"][-1]["result"]
        self.assertEqual(len(first_page["matches"]), 1)
        self.assertIsNotNone(first_page["next_cursor"])

        def next_page(context):
            saved = context["observations"][-1]["result"]
            return {"actions": [{**action, "cursor": saved["next_cursor"]}]}

        second = self.fixture.run_question([next_page], question=question,
                                           checkpoint=first["checkpoint"], max_model_calls=1)
        last_page = second["checkpoint"]["observations"][-1]["result"]
        found = first_page["matches"] + last_page["matches"]
        self.assertCountEqual([(hit["path"], hit["line"]) for hit in found],
                              [("entry.cbl", 10), ("notice.cbl", 4)])
        self.assertEqual(second["coverage_ledger"]["files_reviewed"], 0)
        self.assertFalse(second["coverage_ledger"]["complete"])
        self.assertFalse(second["citations"])


if __name__ == "__main__":
    unittest.main()
