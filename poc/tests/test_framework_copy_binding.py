"""Physical COPY layout evidence must survive ordinary request preparation."""
from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from framework_semantics import build_framework_facts
from repository_discovery import read_repository_context, ensure_repository_search
from source_session import QuestionSourceSession
from test_framework_semantics import MANUAL


LAYOUT = "01 ROWS-PARAMS.\n  05 ROWS-FUNCTION PIC X(8).\n  05 ROWS-KEY PIC X(8).\n"
SOURCE = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRYJOB.\nDATA DIVISION.\n"
          "WORKING-STORAGE SECTION.\nCOPY 'row-layout.cpy'.\nPROCEDURE DIVISION.\n"
          "MAIN-STAGE.\nMOVE 'ADVANCE' TO ROWS-FUNCTION.\nMOVE 'KEY' TO ROWS-KEY.\n"
          "CALL 'ROWSIO' USING ROWS-PARAMS.\nGOBACK.\n")


class FrameworkCopyBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.caller = self.source / "entry.cbl"
        self.member = self.source / "row-layout.cpy"
        self.caller.write_text(SOURCE)
        self.member.write_text(LAYOUT)
        self.manual = self.root / "manual.md"
        self.manual.write_text(MANUAL)
        self.database = self.root / "index.sqlite"

    def build(self):
        report = build_business_index(self.source, self.database, source_format="free", quiet=True,
                                    verify_content=True, framework_reference_path=self.manual)
        ensure_repository_search(self.database, self.source)
        return report

    def pages(self):
        result = []
        for path in ("entry.cbl", "row-layout.cpy"):
            result.extend(read_repository_context(self.database, self.source, relative_path=path,
                start_line=1, end_line=100, max_chars=10000)["pages"])
        return result

    def facts(self, pages=None, **options):
        return build_framework_facts(self.database, self.pages() if pages is None else pages,
                                     reference_path=self.manual, **options)

    def test_complete_unique_copy_reaches_actual_chat_with_original_evidence(self):
        self.assertEqual(self.build()["framework_semantics"]["covered_external_calls"], 1)
        requests = []

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            fact, = [item for item in payload["framework_facts"] if item.get("dependency_covered")]
            self.assertEqual(fact["operation"]["value"], "ADVANCE")
            self.assertFalse(fact["runtime_verified"])
            refs = fact["binding_evidence_refs"]
            member_refs = [ref for ref in refs if ref["relative_path"] == "row-layout.cpy"]
            self.assertEqual({ref["start_line"] for ref in member_refs}, {1, 2, 3})
            self.assertTrue(all(ref["source_sha256"] == hashlib.sha256(self.member.read_bytes()).hexdigest()
                                for ref in member_refs))
            self.assertTrue(any(ref["relative_path"] == "entry.cbl" and ref["start_line"] == 5 for ref in refs))
            identifiers = {page["evidence_id"] for item in payload["source_context"] for page in item["pages"]}
            self.assertLessEqual(set(fact["source_evidence_ids"]), identifiers)
            answer = "请求下一条记录；先设置操作值和记录键，再调用记录接口。" + "".join(
                f"[{value}]" for value in fact["source_evidence_ids"] + fact["reference_ids"])
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": answer}, "finish_reason": "stop"}]}))

        config = CompanyAPIConfig("https://gateway.example.invalid/v1", "local-test", api_key="local-test")
        result = run_business_chat("ENTRYJOB 的处理流程是什么？", self.database, self.source, config,
            framework_reference_path=self.manual, transport=transport,
            policy=AgentPolicy(max_model_requests=1))["agent_result"]
        self.assertEqual(len(requests), 1)
        self.assertTrue(any(item["dependency_covered"] for item in result["framework_context"]["facts"]))

    def test_copy_member_change_invalidates_unchanged_caller_binding(self):
        self.build()
        self.assertTrue(self.facts()["facts"][0]["dependency_covered"])
        original_caller = self.caller.read_bytes()
        self.member.write_text(LAYOUT.replace("ROWS-KEY PIC", "ROWS-KEY REDEFINES ROWS-FUNCTION PIC"))
        report = self.build()
        self.assertEqual(self.caller.read_bytes(), original_caller)
        self.assertEqual(report["framework_semantics"]["covered_external_calls"], 0)
        self.assertFalse(any(row["dependency_covered"] for row in self.facts()["facts"]))
        self.member.write_text(LAYOUT)
        self.assertEqual(self.build()["framework_semantics"]["covered_external_calls"], 1)

    def test_member_declarations_must_be_visible_in_the_request(self):
        self.build()
        pages = [page for page in self.pages() if page["relative_path"] == "entry.cbl"]
        result = self.facts(pages)
        self.assertFalse(result["facts"])
        self.assertTrue(any(row["relative_path"] == "row-layout.cpy" and row["start_line"] == 3
                            for row in result["source_requests"]))

    def test_unrelated_copy_change_cannot_reuse_a_disjoint_caller_binding(self):
        self.caller.write_text(SOURCE.replace("COPY 'row-layout.cpy'.", LAYOUT + "COPY 'row-layout.cpy'."))
        self.member.write_text("01 EXTRA-FIELD PIC X.\n")
        self.assertEqual(self.build()["framework_semantics"]["covered_external_calls"], 1)
        self.member.write_text("01 ROWS-KEY PIC X(8).\n")
        self.assertEqual(self.build()["framework_semantics"]["covered_external_calls"], 0)
        self.assertFalse(any(row["dependency_covered"] for row in self.facts()["facts"]))

    def test_rebind_uses_copy_layout_when_existing_cache_is_absent(self):
        self.build()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DELETE FROM framework_file_semantics")
        with QuestionSourceSession(self.database, self.source) as session:
            facts = self.facts(source_session=session)["facts"]
        self.assertEqual(facts[0]["operation"]["value"], "ADVANCE")
        self.assertTrue(facts[0]["dependency_covered"])

    def test_unsupported_or_ambiguous_copy_cannot_prove_disjoint_storage(self):
        variants = {
            "alias": (SOURCE, LAYOUT + "66 ROWS-ALIAS RENAMES ROWS-FUNCTION THRU ROWS-KEY.\n", None),
            "redefines": (SOURCE, LAYOUT.replace("ROWS-KEY PIC", "ROWS-KEY REDEFINES ROWS-FUNCTION PIC"), None),
            "occurs": (SOURCE, LAYOUT.replace("ROWS-KEY PIC X(8).", "ROWS-KEY PIC X(8) OCCURS 2 TIMES."), None),
            "nested": (SOURCE, "COPY 'inner-layout.cpy'.\n", ("inner-layout.cpy", LAYOUT)),
            "replacing": (SOURCE.replace("'row-layout.cpy'.", "'row-layout.cpy' REPLACING ==ROWS== BY ==ITEM==."), LAYOUT, None),
            "conditional": (SOURCE, ">>IF FLAG\n" + LAYOUT + ">>END-IF\n", None),
            "duplicate_member": (SOURCE, LAYOUT, ("other/row-layout.cpy", LAYOUT)),
            "duplicate_include": (SOURCE.replace("COPY 'row-layout.cpy'.", "COPY 'row-layout.cpy'.\nCOPY 'row-layout.cpy'."), LAYOUT, None),
        }
        for label, (source, layout, extra) in variants.items():
            with self.subTest(label=label):
                self.caller.write_text(source)
                self.member.write_text(layout)
                path = None
                if extra:
                    path = self.source / extra[0]
                    path.parent.mkdir(exist_ok=True)
                    path.write_text(extra[1])
                try:
                    self.assertEqual(self.build()["framework_semantics"]["covered_external_calls"], 0)
                    self.assertFalse(any(row["dependency_covered"] for row in self.facts()["facts"]))
                finally:
                    if path:
                        path.unlink()


if __name__ == "__main__":
    unittest.main()
