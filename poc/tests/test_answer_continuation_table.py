"""Conservative exact overlap handling at Markdown table cell starts."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from answer_markdown import join_answer_continuation


# Reconstructed from the saved answer-contract rate answer and trace lengths:
# round 1: length, 1693 characters; round 2: stop, 811 characters.
# The trace did not retain raw response bodies. Their direct concatenation is
# this 2504-character answer, with the duplicated three-character table seam.
_RECORDED_JOIN = """结论：**只有费率查询成功、查到唯一一条且费率大于零，并且调用返回后 PROCESS-STATUS 仍为 0 时，BASECALC 才会计算基础金额。查不到、多条匹配、其他数据库错误、费率为零都会让 RATELOOK 返回非零状态，BASECALC 随即跳过计算。**[ev_page_316822db3e449731e8882e15][ev_page_3d21a0f1c231490ab9171fe6]

BASECALC 以 `BY CONTENT` 传入 `INPUT-PRODUCT`、`INPUT-DATE`，以 `BY REFERENCE` 传入 `CALC-BASE-RATE` 和 `ERROR-AREA` 调用 RATELOOK。RATELOOK 入口对应为 `RATE-PRODUCT`、`RATE-DATE`、`RATE-OUTPUT`、`ERROR-AREA`。RATELOOK 查到费率时，SQL 把 `RATE_CONFIG.RATE_VALUE` 选入 `RATE-OUTPUT`，即回写到调用方的 `CALC-BASE-RATE`。返回 BASECALC 后，若 `PROCESS-STATUS = ZERO`，才执行：`CALC-BASE-AMOUNT ROUNDED = INPUT-SUM / 1000 * CALC-BASE-RATE`；若发生尺寸溢出，则置 `35`、`BASE-CALC`、`BASE-OVERFLOW`。[ev_page_3d21a0f1c231490ab9171fe6][ev_page_316822db3e449731e8882e15][ev_page_df25101c6d49138df6b98f93]

费率输入和查询条件在 RATELOOK：先调用 DATECHK 校验 `INPUT-DATE`，只检查日期是否在 `20000101` 至 `20991231` 之间，以及月是否 `1–12`、日是否 `1–31`；日期无效时置状态 `21`，不会继续查费率。日期有效后才执行 SQL：`RATE_CONFIG` 中按 `PRODUCT_CODE = :RATE-PRODUCT`、`EFFECTIVE_FROM <= :RATE-DATE`、`EFFECTIVE_TO >= :RATE-DATE` 取 `RATE_VALUE`。源码没有唯一性控制，也没有 `ORDER BY`，同一产品同一日期若命中多行，会落入多条匹配分支。[ev_page_316822db3e449731e8882e15][ev_page_cd56714f14272a59a606ba81]

| 情况 | RATELOOK 的结果 | BASECALC 是否计算 |
|---|---|---|
| 查不到 | SQLCODE `100`：状态 `31`、阶段 `RATE-LOOKUP`、错误码 `RATE-MISSING` | 否 |
| 多条匹配 | SQLCODE `-811`：状态 `32`、错误码 `RATE-AMBIGUOUS` | 否 |
| 其他数据库错误 | SQLCODE 非零且不是上述值：状态 `33`、错误码 `RATE-DATABASE-ERROR` | 否 |
| 查到但费率为零 | SQLCODE `0` 后 `RATE-OUTPUT = 0`：状态 `34`、阶段 `RATE-LOOKUP`、错误码 `RATE-NOT-POSITIVE` | 否 |
| 日期无效 | DATECHK 返回非零：状态 `21`、阶段 `RATE-DATE`、错误码 `DATE-OUTSIDE-RANGE` | 否，且不查费率 |
| 调用 RATELOOK 异常 | 状态 `91`、阶段 `RATE-CALL`、错误码 `PROGRAM-UNAVAILABLE` | 否 |
| 查询成功且费率大于零 | `PROCESS-STATUS` 保持 `0`，`CALC-BASE-RATE` 有值 | 是；若是；若计算本身发生尺寸溢出，则置 `35`、阶段 `BASE-CALC`、错误码 `BASE-OVERFLOW`，此时不产生可用的基础金额 [ev_page_3d21a0f1c231490ab9171fe6]。

对最后一个问题，**源码给不出某产品今天实际采用的费率**。BASECALC 和 RATELOOK 里都没有任何费率数据：BASECALC 只用 `INPUT-SUM` 和调用后被写入的 `CALC-BASE-RATE` 做算式，RATELOOK 则把费率当作外部数据，通过 SQL 从 `RATE_CONFIG` 表按 `PRODUCT_CODE = :RATE-PRODUCT`、`EFFECTIVE_FROM <= :RATE-DATE`、`EFFECTIVE_TO >= :RATE-DATE` 取 `RATE_VALUE` [ev_page_316822db3e449731e8882e15][ev_page_3d21a0f1c231490ab9171fe6][ev_page_df25101c6d49138df6b98f93]。所以源码只能说明"取费率的口径"——用调用方传入的产品代码和日期（日期先经 DATECHK 判断是否落在 `20000101`–`20991231` 且月、日字段在 1–12 / 1–31 范围内），命中生效区间覆盖该日期的记录；至于今天是哪一条、数值是多少，取决于 `RATE_CONFIG` 中的实际数据，源码不含该数据。

另外，这套逻辑也不足以保证"今天只有一条适用记录"：查询没有唯一性约束、也没有排序规则，同一产品同一日期若命中多行，程序不会替你选一条，而是以 `-811` 结束（状态 `32`、错误码 `RATE-AMBIGUOUS`），此时 BASECALC 同样不计算基础金额 [ev_page_316822db3e449731e8882e15]。"""


class TableContinuationTests(unittest.TestCase):
    table = "| Condition | Result |\n|---|---|\n| Ready | "

    def test_recorded_table_seam_reconstructed_from_trace_lengths(self):
        draft, continued = _RECORDED_JOIN[:1693], _RECORDED_JOIN[1693:]
        self.assertEqual((len(draft), len(continued)), (1693, 811))
        self.assertTrue(draft.endswith("有值 | 是；若"))
        self.assertTrue(continued.startswith("是；若计算本身发生尺寸溢出"))
        joined = join_answer_continuation(draft, continued)
        self.assertEqual(joined, draft + continued[3:])
        self.assertEqual(joined[:len(draft)], draft)
        self.assertEqual(joined[len(draft):], continued[3:])

    def test_plain_exact_partial_clause_at_cell_start_is_kept_once(self):
        for table, tail, continued in (
                (self.table, "是；若", "是；若条件成立则继续。 |"),
                (self.table, "Yes; if", "Yes; if the condition holds. |"),
                ("Condition | Result\n:--- | ---:\nReady | ", "是；若", "是；若条件成立则继续。"),
                ("| A | B | C |\n|---|---|---|\n| 是；若", "", "是；若条件成立 | Value | End |"),
                ("| A | B | C |\n|---|---|---|\n| Value | ", "是；若", "是；若条件成立 | End |"),
                ("| Condition | Result |\n|---|---|\n| `A\\|B` | ", "是；若", "是；若条件成立。 |"),
                ("| Condition | Result |\n|---|---|\n| A\\|B | ", "是；若", "是；若条件成立。 |")):
            with self.subTest(table=table, tail=tail):
                draft = table + tail
                shared = draft.rsplit("|", 1)[-1].lstrip()
                self.assertEqual(join_answer_continuation(draft, continued), draft + continued[len(shared):])

    def test_direct_unrepeated_cell_continuation_is_preserved(self):
        draft = self.table + "是；若"
        self.assertEqual(join_answer_continuation(draft, "条件成立则继续。 |"),
                         draft + "条件成立则继续。 |")

    def test_ordinary_repeated_words_are_not_deleted(self):
        for tail, continued, separator in (("very", "very small |", " "),
                                           ("人人", "人人平等 |", ""),
                                           ("是", "是非问题 |", "")):
            with self.subTest(tail=tail):
                draft = self.table + tail
                self.assertEqual(join_answer_continuation(draft, continued), draft + separator + continued)

    def test_quotes_citations_code_and_existing_cell_text_are_unchanged(self):
        for cell in ('"是；若', "“是；若", "`是；若", "[ref:result] 是；若", "前面的说明，是；若"):
            with self.subTest(cell=cell):
                draft = self.table + cell
                self.assertEqual(join_answer_continuation(draft, "是；若继续。 |"), draft + "是；若继续。 |")
        quoted = self.table + '"是；若"'
        self.assertEqual(join_answer_continuation(quoted, '"是；若" is quoted. |'),
                         quoted + ' "是；若" is quoted. |')

    def test_only_a_supported_table_block_establishes_the_boundary(self):
        for prefix in ("Description | ",
                       "| A | B |\n|--|--|\n| Ready | ",
                       "| A | B | C |\n|---|---|\n| Ready | ",
                       "| A | B |\n|---|---|\n| X | Y | Z |\n| Ready | ",
                       "```text\n" + self.table,
                       "    | A | B |\n    |---|---|\n    | Ready | ",
                       "> | A | B |\n> |---|---|\n> | Ready | "):
            with self.subTest(prefix=prefix):
                draft = prefix + "是；若"
                self.assertEqual(join_answer_continuation(draft, "是；若继续。"), draft + "是；若继续。")

    def test_full_draft_restart_and_unrelated_text_are_preserved(self):
        draft = "Earlier complete business explanation.\n\n" + self.table + "是；若"
        complete = draft + "条件成立则继续。 |"
        self.assertEqual(join_answer_continuation(draft, complete), complete)
        self.assertEqual(join_answer_continuation(draft, "否；若条件成立则停止。 |"),
                         draft + "否；若条件成立则停止。 |")

    def test_another_markdown_block_ends_the_table(self):
        for block in ("# Heading", "- Item", "1. Item", "  > Quoted", "<div>"):
            with self.subTest(block=block):
                draft = "| A | B |\n|---|---|\n" + block + " | 是；若"
                self.assertEqual(join_answer_continuation(draft, "是；若继续。"), draft + "是；若继续。")

    def test_space_tab_indented_code_preserves_short_repetition(self):
        for indentation in ("\t", " \t", "  \t", "   \t", "    "):
            with self.subTest(indentation=indentation):
                draft = "\n".join(indentation + line for line in
                                  ("| A | B |", "|---|---|", "| Ready | 是；若"))
                self.assertEqual(join_answer_continuation(draft, "是；若继续。"),
                                 draft + "是；若继续。")

    def test_up_to_three_spaces_still_allow_a_table_restart(self):
        for indentation in ("", " ", "  ", "   "):
            with self.subTest(indentation=indentation):
                draft = "\n".join(indentation + line for line in
                                  ("| A | B |", "|---|---|", "| Ready | 是；若"))
                self.assertEqual(join_answer_continuation(draft, "是；若继续。"),
                                 draft + "继续。")

    def test_gfm_unescaped_code_pipe_does_not_hide_a_column_mismatch(self):
        # GFM splits table columns before parsing inline code. These headers
        # have three columns, so the two-column delimiter cannot form a table.
        for header in ("| `A|B` | C |", "| ``A|B`` | C |", r"| `A\\|B` | C |"):
            with self.subTest(header=header):
                draft = header + "\n|---|---|\n| Ready | 是；若"
                self.assertEqual(join_answer_continuation(draft, "是；若继续。"),
                                 draft + "是；若继续。")
        # An extra pipe in an unfinished body row also fails the conservative
        # column check; text beyond the declared cells is not a restart target.
        draft = "| A | B |\n|---|---|\n| `A|B` | 是；若"
        self.assertEqual(join_answer_continuation(draft, "是；若继续。"),
                         draft + "是；若继续。")

    def test_gfm_escaped_pipes_and_matching_columns_allow_a_table_restart(self):
        for header, delimiter in ((r"| A\|B | C |", "|---|---|"),
                                  (r"| `A\|B` | C |", "|---|---|"),
                                  (r"| `A\\\|B` | C |", "|---|---|"),
                                  ("| `A|B` | C |", "|---|---|---|")):
            with self.subTest(header=header):
                draft = header + "\n" + delimiter + "\n| Ready | 是；若"
                self.assertEqual(join_answer_continuation(draft, "是；若继续。"),
                                 draft + "继续。")


if __name__ == "__main__":
    unittest.main()
