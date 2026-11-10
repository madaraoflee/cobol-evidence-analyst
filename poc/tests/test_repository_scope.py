from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repository_scope import repository_inventory_requested


class RepositoryInventoryScopeTests(unittest.TestCase):
    def test_original_mixed_language_request_is_whole_repository(self):
        self.assertTrue(repository_inventory_requested("列出系统所有计算所有premium formula 的地方"))

    def test_explicit_multilingual_inventory_requests(self):
        for question in (
            "检查全库所有保费公式及其调用条件。",
            "列出全系統所有保費計算公式。",
            "整个仓库中有哪些计算公式？",
            "List every place in the system that calculates premium formulas.",
            "Find all premium formulas across the repository.",
            "Check the entire codebase for premium calculations.",
            "不要只看 entry.cbl，列出全系统所有保费公式。",
        ):
            with self.subTest(question=question):
                self.assertTrue(repository_inventory_requested(question))

    def test_normal_formula_and_overview_do_not_expand_scope(self):
        for question in (
            "entry.cbl 的所有保费公式有哪些？",
            "Explain every formula in entry.cbl.",
            "整个程序包括 RULE-A 的计算规则是什么？",
            "介绍整个系统的业务。",
            "系统保费如何计算？",
            "继续", "", None,
        ):
            with self.subTest(question=question):
                self.assertFalse(repository_inventory_requested(question))

    def test_negated_or_current_file_restriction_is_not_global(self):
        for question in (
            "不要检查全库所有公式，只看 entry.cbl。",
            "无需全系统计算清单。",
            "Do not scan the entire repository for formulas.",
            "List formulas, not the whole system.",
            "全系统公式很多，这次只分析 entry.cbl。",
            "The entire system has formulas; only entry.cbl is in scope.",
            "解释 `全系统所有计算` 这个变量。",
        ):
            with self.subTest(question=question):
                self.assertFalse(repository_inventory_requested(question))

    def test_repository_determiners_and_all_variants(self):
        for question in (
            "List all premium formulas in this repository.",
            "List every premium calculation in our system.",
            "列出系统里全部保费计算公式。",
            "Give a system-wide inventory of premium formulas.",
        ):
            with self.subTest(question=question):
                self.assertTrue(repository_inventory_requested(question))

    def test_current_named_program_restriction_takes_precedence(self):
        for question in (
            "全系统有很多保费公式，这次只分析 CALC01 程序。",
            "There are formulas across the system, but only CALC01 is in scope.",
            "List all formulas in this repository, limited to entry.cbl.",
            "There are formulas across the system; only program CALC01 please.",
            "全系统有很多公式，这次只看 `entry.sqlcblle`。",
            "List formulas across this repository; only `entry.cbl` is in scope.",
        ):
            with self.subTest(question=question):
                self.assertFalse(repository_inventory_requested(question))


if __name__ == "__main__":
    unittest.main()
