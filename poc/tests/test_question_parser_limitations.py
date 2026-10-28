"""A supplied statement can exceed parser support without being missing source."""

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "tools")]

from business_chat import _answer_scope_gaps
from business_index import build_business_index
from business_map import build_business_map
from business_onboarding_fixture import sources
from question_investigation import build_question_investigation
from repository_discovery import ensure_repository_search


class QuestionParserLimitationsTests(unittest.TestCase):
    question = "刚才的结算流程里，如果订单状态不是 A，或者金额等于零，结果是什么？请只解释这两种情况。"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.contents = sources()
        self.build()

    def build(self):
        for path, text in self.contents.items():
            (self.source / path).write_bytes(text.encode())
        build_business_index(self.source, self.database, source_format="free", verify_content=True,
                             framework_reference_path="", quiet=True)
        ensure_repository_search(self.database, self.source)
        self.mapping = build_business_map(self.database, self.source, "请解释整个业务流程")
        # The preceding repository overview supplied all three files. Its
        # focused follow-up still carries that wider reading context.
        self.mapping.update(selected_paths=list(self.contents), direct_paths=list(self.contents))
        self.pages = [self.page(path) for path in self.contents]

    def page(self, path, first=1, last=None):
        text = self.contents[path]
        lines = text.splitlines()
        last = last or len(lines)
        return {"evidence_id": "ev:" + path, "relative_path": path,
                "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "start_line": first, "end_line": last, "source_text": "\n".join(lines[first-1:last]),
                "format_hint": "free"}

    def investigate(self, pages=None, *, question=None, extra_gaps=()):
        groups = [{"open_frontier": [{"reason": "statement_not_supported:EXEC", "relative_path": "balance.cbl",
                   "start_line": 13, "affected_storage": "all_visible_storage"}, *extra_gaps]}]
        return build_question_investigation(question or self.question, self.mapping,
            database_path=self.database, source_pages=self.pages if pages is None else pages, evidence_groups=groups)

    def test_focused_followup_keeps_supplied_sql_as_parser_limit_without_missing_evidence(self):
        result = self.investigate()
        self.assertEqual(result["open_gaps"], [])
        self.assertEqual(_answer_scope_gaps(self.question, result), ([], []))
        self.assertTrue(result["can_answer"])
        self.assertFalse(result["semantic_execution_verified"])
        limitation, = result["analysis_limitations"]
        self.assertEqual((limitation["relative_path"], limitation["start_line"], limitation["end_line"]),
                         ("balance.cbl", 13, 16))
        self.assertEqual(limitation["evidence_ids"], ["ev:balance.cbl"])
        self.assertFalse(limitation["semantic_execution_verified"])
        self.assertEqual(limitation["affected_storage"], "all_visible_storage")

    def test_missing_truncated_wrong_version_or_incomplete_statement_remains_a_gap(self):
        original = self.page("balance.cbl")
        for invalid in (None, {**original, "span_truncated": True},
                        {**original, "source_sha256": "0" * 64}, self.page("balance.cbl", 13, 15),
                        {**original, "source_text": original["source_text"].replace("END-EXEC.", "")}):
            with self.subTest(invalid=invalid):
                pages = [page for page in self.pages if page["relative_path"] != "balance.cbl"]
                if invalid:
                    pages.append(invalid)
                result = self.investigate(pages)
                self.assertFalse(result.get("analysis_limitations"))
                gap = next(gap for gap in result["open_gaps"] if gap.get("reason") == "statement_not_supported:EXEC")
                self.assertEqual((gap["relative_path"], gap["start_line"]), ("balance.cbl", 13))

    def test_nonparser_unknown_call_effects_remain_material_with_complete_source(self):
        gap = {"reason": "unknown_call_effects", "relative_path": "settle.cbl", "start_line": 30,
               "affected_storage": "all_visible_storage"}
        result = self.investigate(extra_gaps=[gap])
        self.assertEqual(result["open_gaps"], [{"kind": "semantic_candidates", **gap}])
        self.assertEqual(_answer_scope_gaps(self.question, result)[0], result["open_gaps"])

    def test_indexed_unterminated_sql_is_not_mistaken_for_complete_original_source(self):
        original = self.contents["balance.cbl"]
        for replacement in ("", "/* END-EXEC */", "'END-EXEC'", "-- END-EXEC"):
            with self.subTest(replacement=replacement):
                self.contents["balance.cbl"] = original.replace("END-EXEC.", replacement)
                self.build()
                result = self.investigate()
                self.assertFalse(result.get("analysis_limitations"))
                self.assertTrue(any(gap.get("reason") == "statement_not_supported:EXEC"
                                    for gap in result["open_gaps"]))

    def test_sql_question_still_needs_missing_parameter_declarations(self):
        question = "请解释 BALANCEQ 的业务流程，账户输入和余额返回的来源是什么？"
        pages = [page for page in self.pages if page["relative_path"] != "balance.cbl"]
        pages.append(self.page("balance.cbl", 13, 21))
        result = self.investigate(pages, question=question)
        self.assertTrue(result.get("analysis_limitations"))
        self.assertTrue(result["open_gaps"])
        self.assertTrue(any(item["status"] in {"OPEN", "PARTIAL", "UNRESOLVED"}
                            for item in result["required_items"]))

    def test_unavailable_called_implementation_is_not_removed_with_the_parser_limit(self):
        self.contents["balance.cbl"] = self.contents["balance.cbl"].replace(
            "GOBACK.", 'CALL "EXTERNAL-LOOKUP" USING CURRENT-BALANCE.\nGOBACK.')
        self.build()
        result = self.investigate(question="请解释 BALANCEQ 的完整业务流程及返回余额的来源。")
        self.assertTrue(result.get("analysis_limitations"))
        self.assertTrue(any(gap.get("reason") in {"external_implementation_unavailable", "runtime_target_unresolved"}
                            for gap in result["open_gaps"]), result["open_gaps"])
        self.assertFalse(result["semantic_execution_verified"])


if __name__ == "__main__":
    unittest.main()
