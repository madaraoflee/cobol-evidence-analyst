"""Resolved nested layouts and token replacement retain physical provenance."""
from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from company_api import CompanyAPIConfig, TransportResponse
from framework_semantics import build_framework_facts
from repository_discovery import read_repository_context
from source_session import QuestionSourceSession
import test_framework_copy_binding as copy_fixture

LAYOUT, SOURCE = copy_fixture.LAYOUT, copy_fixture.SOURCE


class FrameworkNestedCopyBindingTests(unittest.TestCase):
    def setUp(self):
        self.case = copy_fixture.FrameworkCopyBindingTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def write(self, name, text):
        path = self.case.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def nested(self, *, replacing=False):
        pairs = " ".join(f"==TEMPLATE-{name}== BY ==ROWS-{name}==" for name in ("PARAMS", "FUNCTION", "KEY"))
        self.case.member.write_text("COPY 'members/leaf.cpy'" + (" REPLACING\n " + pairs if replacing else "") + ".\n")
        return self.write("members/leaf.cpy", LAYOUT.replace("ROWS-", "TEMPLATE-") if replacing else LAYOUT)

    def pages(self):
        result = []
        for path in sorted(self.case.source.rglob("*")):
            if path.is_file():
                result.extend(read_repository_context(self.case.database, self.case.source,
                    relative_path=path.relative_to(self.case.source).as_posix(), start_line=1,
                    end_line=200, max_chars=20000)["pages"])
        return result

    def facts(self, pages=None):
        return build_framework_facts(self.case.database, self.pages() if pages is None else pages,
                                     reference_path=self.case.manual)

    def test_plain_nested_layout_preserves_every_include_site_and_leaf(self):
        leaf = self.nested()
        self.assertEqual(self.case.build()["framework_semantics"]["covered_external_calls"], 1)
        fact, = self.facts()["facts"]
        self.assertEqual(fact["operation"]["value"], "ADVANCE")
        refs = fact["binding_evidence_refs"]
        self.assertEqual({ref["relative_path"] for ref in refs}, {"entry.cbl", "row-layout.cpy", "members/leaf.cpy"})
        self.assertTrue(all(ref["source_sha256"] == hashlib.sha256(leaf.read_bytes()).hexdigest()
                            for ref in refs if ref["relative_path"] == "members/leaf.cpy"))
        self.assertFalse(fact["runtime_verified"])

    def test_subordinate_nested_copy_uses_insertion_order_and_enclosing_group(self):
        self.case.member.write_text("01 ROWS-PARAMS.\nCOPY 'members/function.cpy'.\n05 ROWS-KEY PIC X(8).\n")
        self.write("members/function.cpy", "05 ROWS-FUNCTION PIC X(8).\n")
        self.assertEqual(self.case.build()["framework_semantics"]["covered_external_calls"], 1)
        self.assertEqual(self.facts()["facts"][0]["operation"]["value"], "ADVANCE")

    def test_standard_replacing_reaches_actual_chat_with_transformation_and_source(self):
        leaf = self.nested(replacing=True)
        self.assertEqual(self.case.build()["framework_semantics"]["covered_external_calls"], 1)
        requests = []

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            fact, = [fact for fact in payload["framework_facts"] if fact.get("dependency_covered")]
            self.assertEqual(fact["operation"]["value"], "ADVANCE")
            refs = fact["binding_evidence_refs"]
            transformed = [ref for ref in refs if ref["relative_path"] == "members/leaf.cpy"]
            self.assertTrue(all(ref.get("replacement_trace") for ref in transformed))
            self.assertTrue(all(ref["source_sha256"] == hashlib.sha256(leaf.read_bytes()).hexdigest() for ref in transformed))
            pages = [page for group in payload["source_context"] for page in group.get("pages", [])]
            self.assertTrue(any("TEMPLATE-FUNCTION" in page["source_text"] for page in pages))
            self.assertTrue(any("REPLACING" in page["source_text"] for page in pages))
            self.assertLessEqual(set(fact["source_evidence_ids"]), {page["evidence_id"] for page in pages})
            answer = "按给定记录键请求下一条记录，随后由调用方处理返回状态。" + "".join(
                f"[{value}]" for value in fact["source_evidence_ids"] + fact["reference_ids"])
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": answer}, "finish_reason": "stop"}]}))

        config = CompanyAPIConfig("https://gateway.example.invalid/v1", "local-test", api_key="local-test")
        result = run_business_chat("ENTRYJOB 的处理流程是什么？", self.case.database, self.case.source, config,
            framework_reference_path=self.case.manual, transport=transport,
            policy=AgentPolicy(max_model_requests=1))["agent_result"]
        self.assertEqual(len(requests), 1)
        self.assertTrue(any(fact["dependency_covered"] for fact in result["framework_context"]["facts"]))

    def test_deep_member_change_invalidates_binding_without_caller_change(self):
        leaf = self.nested(replacing=True)
        self.assertEqual(self.case.build()["framework_semantics"]["covered_external_calls"], 1)
        caller, outer = self.case.caller.read_bytes(), self.case.member.read_bytes()
        leaf.write_text(leaf.read_text().replace("TEMPLATE-KEY PIC", "TEMPLATE-KEY REDEFINES TEMPLATE-FUNCTION PIC"))
        self.assertEqual(self.case.build()["framework_semantics"]["covered_external_calls"], 0)
        self.assertEqual((self.case.caller.read_bytes(), self.case.member.read_bytes()), (caller, outer))
        self.assertFalse(any(fact["dependency_covered"] for fact in self.facts()["facts"]))

    def test_missing_nested_evidence_requests_original_member_not_synthetic_source(self):
        self.nested(replacing=True)
        self.case.build()
        pages = [page for page in self.pages() if page["relative_path"] != "members/leaf.cpy"]
        result = self.facts(pages)
        self.assertFalse(result["facts"])
        self.assertTrue(any(span["relative_path"] == "members/leaf.cpy" for span in result["source_requests"]))

    def test_cycle_finishes_with_concrete_include_provenance(self):
        self.nested()
        self.write("members/leaf.cpy", LAYOUT + "COPY 'row-layout.cpy'.\n")
        self.assertEqual(self.case.build()["framework_semantics"]["covered_external_calls"], 0)
        facts = self.facts()["facts"]
        self.assertTrue(facts)
        self.assertEqual(facts[0]["layout_validation"]["reason"], "framework_layout_copy_cycle")
        self.assertTrue(any(ref["role"] == "layout_boundary" for ref in facts[0]["layout_evidence_refs"]))

    def test_ambiguous_nested_member_and_unknown_outer_replacement_stay_local_boundaries(self):
        self.nested()
        # A quoted explicit path remains unique despite a duplicate basename.
        self.write("duplicate/leaf.cpy", LAYOUT)
        self.assertEqual(self.case.build()["framework_semantics"]["covered_external_calls"], 1)
        self.case.member.write_text("COPY 'leaf.cpy'.\n")
        self.assertEqual(self.case.build()["framework_semantics"]["covered_external_calls"], 0)
        self.case.member.write_text("COPY 'members/leaf.cpy'.\n")
        self.case.caller.write_text(SOURCE.replace("'row-layout.cpy'.", "'row-layout.cpy' REPLACING ==ROWS== BY ==OTHER==."))
        self.assertEqual(self.case.build()["framework_semantics"]["covered_external_calls"], 0)

    def test_replacement_cannot_create_directives_from_unindexed_member_lines(self):
        self.case.caller.write_text(SOURCE.replace("'row-layout.cpy'.",
            "'row-layout.cpy' REPLACING ==SWITCH== BY ==REPLACE==."))
        self.case.member.write_text(LAYOUT + "SWITCH ==ROWS-KEY== BY ==ROWS-FUNCTION==.\n")
        self.assertEqual(self.case.build()["framework_semantics"]["covered_external_calls"], 0)
        fact, = self.facts()["facts"]
        self.assertFalse(fact["dependency_covered"])
        self.assertEqual(fact["layout_validation"]["reason"], "framework_replacement_outside_data_declarations")

    def test_replacement_cannot_create_alias_from_unindexed_member_lines(self):
        self.case.caller.write_text(SOURCE.replace("'row-layout.cpy'.",
            "'row-layout.cpy' REPLACING ==LEVEL== BY ==66==."))
        self.case.member.write_text(LAYOUT + "LEVEL ROWS-ALIAS RENAMES ROWS-FUNCTION THRU ROWS-KEY.\n")
        self.assertEqual(self.case.build()["framework_semantics"]["covered_external_calls"], 0)
        self.assertFalse(any(fact["dependency_covered"] for fact in self.facts()["facts"]))

    def test_declaration_line_suffix_is_not_mistaken_for_parsed_declaration(self):
        self.case.caller.write_text(SOURCE.replace("'row-layout.cpy'.",
            "'row-layout.cpy' REPLACING ==SWITCH== BY ==REPLACE==."))
        self.case.member.write_text(LAYOUT + "05 EXTRA PIC X. SWITCH =='ADVANCE'== BY =='REMOVE'==.\n")
        self.assertEqual(self.case.build()["framework_semantics"]["covered_external_calls"], 0)
        self.assertFalse(any(fact["dependency_covered"] for fact in self.facts()["facts"]))

    def test_comments_and_multiline_declarations_do_not_block_field_renaming(self):
        leaf = self.nested(replacing=True)
        leaf.write_text(leaf.read_text().replace("05 TEMPLATE-FUNCTION PIC X(8).",
            "05 TEMPLATE-FUNCTION\n    PIC X(8).") + "*> TEMPLATE-FUNCTION is a comment.\n")
        self.assertEqual(self.case.build()["framework_semantics"]["covered_external_calls"], 1)
        self.assertTrue(self.facts()["facts"][0]["dependency_covered"])

    def test_existing_index_without_framework_cache_rebinds_with_captured_original_source(self):
        self.nested(replacing=True)
        self.case.build()
        pages = self.pages()
        with closing(sqlite3.connect(self.case.database)) as db, db:
            db.execute("DROP TABLE framework_file_semantics")
        with QuestionSourceSession(self.case.database, self.case.source) as session:
            result = build_framework_facts(self.case.database, pages,
                reference_path=self.case.manual, source_session=session)
        fact, = result["facts"]
        self.assertTrue(fact["dependency_covered"])
        self.assertEqual(fact["operation"]["value"], "ADVANCE")


if __name__ == "__main__":
    unittest.main()
