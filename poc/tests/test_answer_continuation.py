"""Exact-only joins at the boundary of a truncated Markdown response."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from answer_markdown import join_answer_continuation


class AnswerContinuationTests(unittest.TestCase):
    def test_partial_list_item_restarted_from_its_beginning(self):
        prefix = "已说明的业务结论不变。[ev:summary]\n\n1. 首项保留。\n\n"
        tail = "2. 接着检查上限：`LIMIT-AMO"
        continued = "2. 接着检查上限：`LIMIT-AMOUNT > 80` 时拒绝处理。[ev:limit]"
        self.assertEqual(join_answer_continuation(prefix + tail, continued), prefix + continued)

    def test_unfinished_paragraph_and_exact_sentence_overlap(self):
        for prefix, tail, continued in (
                ("Earlier complete paragraph.\n\n", "The remaining condition is that the amount",
                 "The remaining condition is that the amount must be positive."),
                ("The first condition is met. ", "The second condition is not yet",
                 "The second condition is not yet satisfied; processing stops.")):
            with self.subTest(tail=tail):
                self.assertEqual(join_answer_continuation(prefix + tail, continued), prefix + continued)

    def test_complete_repeated_paragraph_is_kept_once(self):
        previous = "An earlier fact stays unchanged.\n\nThe amount remains zero. [ev:result]"
        continued = "The amount remains zero. [ev:result]\n\nThe status is returned."
        self.assertEqual(join_answer_continuation(previous, continued),
                         previous + "\n\nThe status is returned.")

    def test_direct_sentence_and_identifier_continuations(self):
        cases = (
            ("若金额高于", "上限则拒绝。", "若金额高于上限则拒绝。"),
            ("The amount remains", "unchanged.", "The amount remains unchanged."),
            ("Check `LIMIT-AMO", "UNT` first.", "Check `LIMIT-AMOUNT` first."),
            ("Inspect LIMIT-", "AMOUNT next.", "Inspect LIMIT-AMOUNT next."),
            ("金额保持原值", "，随后返回。", "金额保持原值，随后返回。"),
        )
        for draft, continued, expected in cases:
            with self.subTest(draft=draft):
                self.assertEqual(join_answer_continuation(draft, continued), expected)

    def test_explicit_whitespace_and_new_markdown_blocks_are_preserved(self):
        for draft, continued, expected in (
                ("The amount is ", "zero.", "The amount is zero."),
                ("An unfinished list:\n", "- One item.", "An unfinished list:\n- One item."),
                ("First fact.[ev:fact]", "Second fact.", "First fact.[ev:fact]\n\nSecond fact."),
                ("The conditions are:", "- A positive amount.", "The conditions are:\n\n- A positive amount."),
                ("A completed fact.", "## Next\n\nAnother fact.", "A completed fact.\n\n## Next\n\nAnother fact.")):
            with self.subTest(draft=draft):
                self.assertEqual(join_answer_continuation(draft, continued), expected)

    def test_similar_conditions_are_not_fuzzily_deduplicated(self):
        draft = "Earlier verified fact.\n\n2. The amount is greater than 80."
        continued = "2. The amount is less than 80."
        self.assertEqual(join_answer_continuation(draft, continued), draft + "\n\n" + continued)

    def test_identifier_suffix_does_not_establish_a_sentence_restart(self):
        draft = "The first item uses ACCOUNT-REFERENCE"
        continued = "ACCOUNT-REFERENCE2 is unchanged."
        self.assertEqual(join_answer_continuation(draft, continued), draft + " " + continued)

    def test_shared_short_word_or_list_number_does_not_delete_text(self):
        self.assertEqual(join_answer_continuation("The first field is zero", "zero is also a valid input."),
                         "The first field is zero zero is also a valid input.")
        self.assertEqual(join_answer_continuation("An unfinished section.\n\n3. ", "3. A different section."),
                         "An unfinished section.\n\n3. 3. A different section.")

    def test_fenced_code_and_escaped_inline_ticks(self):
        self.assertEqual(join_answer_continuation("Example:\n\n```text\nfirst line", "\nsecond line\n```"),
                         "Example:\n\n```text\nfirst line\nsecond line\n```")
        self.assertEqual(join_answer_continuation("```python\nvalue = item_cou", "nt\n```"),
                         "```python\nvalue = item_count\n```")
        self.assertEqual(join_answer_continuation("Use ```ACCOUNT-REF", "ERENCE``` here."),
                         "Use ```ACCOUNT-REFERENCE``` here.")
        self.assertEqual(join_answer_continuation("```text\nA complete code line.", "```\n\nNext paragraph."),
                         "```text\nA complete code line.\n```\n\nNext paragraph.")
        self.assertEqual(join_answer_continuation("The literal marker is \\`", "and it stays visible."),
                         "The literal marker is \\` and it stays visible.")

    def test_full_draft_repeat_and_empty_parts(self):
        draft = "An earlier complete paragraph.\n\n" * 500 + "The last condition is"
        continued = draft + " now satisfied."
        self.assertEqual(join_answer_continuation(draft, continued), continued)
        self.assertEqual(join_answer_continuation("", "New text."), "New text.")
        self.assertEqual(join_answer_continuation("Existing text.", ""), "Existing text.")


if __name__ == "__main__":
    unittest.main()
