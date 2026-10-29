"""Exact-only joins at the boundary of a truncated Markdown response."""

from pathlib import Path
import hashlib
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from answer_markdown import join_answer_continuation


class AnswerContinuationTests(unittest.TestCase):
    def test_restarted_inline_identifier_completes_only_the_exact_unfinished_token(self):
        for marker in ("`", "``"):
            prefix = "应缴仍按已解释的规则计算。随后按 "
            continued = marker + "READ-STATUS" + marker + " 选择业务分支。"
            self.assertEqual(join_answer_continuation(prefix + marker + "READ", continued), prefix + continued)
            literal = marker + "TOTAL-AMOUNT" + marker
            self.assertEqual(join_answer_continuation(prefix + marker + "READ", literal),
                             prefix + marker + "READ" + literal)

    def test_ambiguous_inline_and_fenced_code_are_not_rewritten_as_identifier_restarts(self):
        for draft, continued, separator in (
            ("字段 `READ`", "`READ-STATUS`", " "),
            ("字段 `READ", "`READ` 后另有说明。", ""),
            ("字段 `A", "`AMOUNT`", ""),
            ("算式 `BASE + READ", "`READ-STATUS`", ""),
            ("金额 `100", "`100.00`", ""),
            ("```cobol\n`READ", "`READ-STATUS`", ""),
            ("说明\n\n    literal `READ", "`READ-STATUS`", ""),
            ("说明\n\n\tliteral `READ", "`READ-STATUS`", ""),
            ("> ~~~text\n> literal `READ", "`READ-STATUS`", ""),
        ):
            with self.subTest(draft=draft):
                self.assertEqual(join_answer_continuation(draft, continued), draft + separator + continued)

    def test_supplied_bare_citation_tail_does_not_swallow_new_markdown(self):
        identifier = "ev_page_0123456789abcdef01234567"
        draft = "已有业务结论保持不变。[ev_page_"
        continuation = "**输入边界**\n\n新的业务说明完整保留。"
        self.assertEqual(join_answer_continuation(draft, continuation, evidence_ids=[identifier]),
                         "已有业务结论保持不变。\n\n" + continuation)
        self.assertEqual(join_answer_continuation(draft, continuation),
                         draft + continuation)

    def test_literal_continuation_completes_only_an_actual_supplied_citation(self):
        identifier = "ev_page_0123456789abcdef01234567"
        for split in (len("ev_page_"), 18, len(identifier)):
            draft = "已有结论。[" + identifier[:split]
            continuation = identifier[split:] + "]\n\n后续业务说明。"
            with self.subTest(split=split):
                self.assertEqual(join_answer_continuation(draft, continuation, evidence_ids=[identifier]),
                                 draft + continuation)
        self.assertEqual(join_answer_continuation("[ev_page_", "**新段落**", evidence_ids=[identifier]),
                         "**新段落**")

    def test_citation_repair_keeps_unknown_code_escape_and_blockquote_tails(self):
        identifier = "ev_page_0123456789abcdef01234567"
        continuation = "**新的说明**"
        for draft in (
            "其它前缀 [ev_page_ffffffff", "未识别前缀 [ev_other_",
            "源码 `[ev_page_", "```text\n[ev_page_", "~~~text\n[ev_page_",
            "\\[ev_page_", "> 引用 [ev_page_", "    [ev_page_", "\t[ev_page_",
        ):
            with self.subTest(draft=draft):
                self.assertEqual(join_answer_continuation(draft, continuation, evidence_ids=[identifier]),
                                 join_answer_continuation(draft, continuation))

    def test_frozen_two_real_continuations_preserve_business_text_and_known_ids(self):
        frozen = json.loads((Path(__file__).parent / "fixtures" / "answer_continuation_phase4.json").read_text())
        self.assertEqual(len(frozen["cases"]), 2)
        for case in frozen["cases"]:
            with self.subTest(case=case["id"]):
                draft, continuation = case["raw_answers"]
                self.assertEqual([hashlib.sha256(text.encode()).hexdigest() for text in case["raw_answers"]],
                                 case["raw_answer_sha256"])
                joined = join_answer_continuation(draft, continuation, evidence_ids=case["evidence_ids"])
                self.assertIn(continuation, joined)
                if case["id"] == "boundary-followup":
                    self.assertTrue(draft.endswith("[ev_page_"))
                    self.assertEqual(joined, draft[:-len("[ev_page_")] + "\n\n" + continuation)
                    self.assertNotIn("[ev_page_**", joined)
                else:
                    # The model repeated the exact unfinished sentence and
                    # supplied its full reference; retain that existing join.
                    self.assertTrue(draft.endswith("[ev_page"))
                    self.assertEqual(joined, join_answer_continuation(draft, continuation))
                    self.assertIn(draft[:-len("[ev_page")], joined)
                self.assertEqual([hashlib.sha256(text.encode()).hexdigest() for text in case["raw_answers"]],
                                 case["raw_answer_sha256"])

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
