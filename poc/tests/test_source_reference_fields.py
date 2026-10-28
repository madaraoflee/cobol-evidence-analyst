"""A source filename locates code; its components are not requested data fields."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from business_map import build_business_map
from company_api import CompanyAPIConfig, TransportResponse
from conversation_focus import build_session_subject
from question_investigation import build_question_investigation
from question_planner import question_references
from repository_discovery import ensure_repository_search
from repository_identity import resolve_source_identity


class SourceReferenceFieldTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        (self.source / "source-dir").mkdir(parents=True)
        self.path = "source-dir/z-policy.cbl"
        self.content = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. AMOUNTRULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 BASE-AMOUNT PIC 9(3) VALUE 2.\n01 FINAL-AMOUNT PIC 9(4).\n"
            "PROCEDURE DIVISION.\nCOMPUTE FINAL-AMOUNT = BASE-AMOUNT * 3.\nGOBACK.\n")
        (self.source / self.path).write_text(self.content, encoding="utf-8")
        self.database = self.root / "index.sqlite"
        build_business_index(self.source, self.database, source_format="free", quiet=True)
        ensure_repository_search(self.database, self.source)

    def test_filename_and_path_components_do_not_become_field_references(self):
        for source in ("z-policy.cbl", "source-dir/z-policy.cbl", r"source-dir\z-policy.CBL",
                       "`source-dir/z-policy.cbl`", "`source dir/业务/z-policy.cbl`",
                       "./source-dir/z-policy.cbl", r"C:\source-dir\z-policy.CBL",
                       "source-dir/业务/z-policy.cbl"):
            with self.subTest(source=source):
                refs = question_references(f"请解释 {source} 中 FINAL-AMOUNT 的计算规则。")
                self.assertEqual(refs, ("FINAL-AMOUNT",))
        self.assertEqual(question_references("FINAL-AMOUNT in z-policy.cbl"), ("FINAL-AMOUNT",))
        self.assertEqual(question_references("FINAL-AMOUNT在z-policy.cbl中怎样计算？"), ("FINAL-AMOUNT",))
        self.assertEqual(question_references("z-policy.cbl 的 Z-POLICY 和 AMOUNT OF INPUT-REC"),
                         ("Z-POLICY", "AMOUNT OF INPUT-REC"))

    def test_first_question_has_formula_evidence_without_phantom_field_gap(self):
        requests = []
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            page = next(page for context in payload["source_context"] for page in context["pages"]
                        if page["relative_path"] == self.path)
            self.assertEqual(page["source_text"].strip(), self.content.strip())
            answer = f"最终金额等于基础金额乘以 3；基础金额初值是 2，所以结果是 6。[{page['evidence_id']}]"
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": answer}, "finish_reason": "stop"}]}))
        result = run_business_chat("请解释 z-policy.cbl 中最终金额的计算规则。",
            self.database, self.source,
            CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="test-secret"),
            transport=transport, framework_reference_path="",
            policy=AgentPolicy(max_model_requests=2))["agent_result"]
        investigation = result["investigation_state"]["question_investigation"]
        self.assertEqual(investigation["analysis_plan"]["entities"], ())
        self.assertNotIn("conversation_field_not_bound", investigation["analysis_plan"]["unresolved"])
        self.assertFalse(any(gap.get("reason") in {"requested_field_not_bound", "formula_not_located"}
                             for gap in investigation["open_gaps"]))
        self.assertEqual(result["status"], "ANALYZED")
        self.assertEqual(result["stop_reason"], "sufficient_material")
        self.assertEqual(len(requests), 1)

    def test_same_question_keeps_real_fields_and_real_missing_fields(self):
        for field, missing in (("FINAL-AMOUNT", False), ("MISSING-AMOUNT", True)):
            with self.subTest(field=field):
                question = f"请解释 source-dir/z-policy.cbl 中 {field} 的计算规则。"
                with closing(sqlite3.connect(self.database)) as connection:
                    identity = resolve_source_identity(connection, question)
                    subject = build_session_subject(connection, question, identity)
                self.assertEqual(subject["root_paths"], [self.path])
                self.assertEqual("conversation_field_not_bound" in subject["unresolved"], missing)
                self.assertEqual(subject["focus_entities"], [] if missing else [field])
                investigation = build_question_investigation(question,
                    build_business_map(self.database, self.source, question), database_path=self.database)
                gaps = [gap for gap in investigation["open_gaps"]
                        if gap.get("reason") == "requested_field_not_bound"]
                self.assertEqual(bool(gaps), missing)
                if missing:
                    self.assertEqual(gaps[0]["fields"], [field])

    def test_missing_source_still_has_its_original_identity_failure(self):
        question = "请解释 missing-dir/z-policy.cbl 中最终金额的计算规则。"
        with closing(sqlite3.connect(self.database)) as connection:
            identity = resolve_source_identity(connection, question)
        self.assertEqual(identity["status"], "not_found")
        self.assertEqual(identity["requested"], ["missing-dir/z-policy.cbl"])
        self.assertEqual(identity["direct_paths"], [])
        self.assertTrue(identity["hard_constraint"])
        self.assertEqual(question_references(question), ())


if __name__ == "__main__":
    unittest.main()
