"""Prepare immutable public and synthetic questions for native office acceptance.

This module makes no model or network calls. Gold describes reviewed source;
it never supplies a model answer or marks an office measurement as passed.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.desktop_acceptance_scenarios import CONVERSATION_QUESTIONS, _conversation_source


SYNTHETIC_MANIFEST = ROOT / "docs/eval/desktop-joint-v1/manifest.json"
PUBLIC_MANIFEST = ROOT / "docs/eval/public-business-v1/manifest.json"
PUBLIC_CASES = ROOT / "docs/eval/public-business-v1/cases.json"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def write_json(path, document):
    Path(path).write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def checked_file(root, relative, expected):
    root = Path(root).resolve(strict=True)
    path = (root / relative).resolve(strict=True)
    require(path.is_relative_to(root), "OFFICE_SOURCE_PATH_ESCAPE")
    require(path.is_file() and digest(path) == expected, "OFFICE_FROZEN_SOURCE_CHANGED: " + relative)
    return path


def source_range(source, path, start, end=None, role="rule"):
    file = (Path(source) / path).resolve(strict=True)
    require(file.is_relative_to(Path(source).resolve()), "OFFICE_SOURCE_PATH_ESCAPE")
    lines = file.read_text(encoding="utf-8").splitlines()
    end = start if end is None else end
    require(type(start) is int and type(end) is int and 1 <= start <= end <= len(lines),
            "OFFICE_GOLD_RANGE_INVALID")
    return {"path": path, "source_sha256": digest(file), "start_line": start, "end_line": end, "role": role}


def anchor(source, path, needle, *, count=1, occurrence=0, role="rule"):
    lines = (Path(source) / path).read_text(encoding="utf-8").splitlines()
    matches = [index for index, line in enumerate(lines, 1) if needle in line]
    require(len(matches) > occurrence, "OFFICE_GOLD_ANCHOR_MISSING: " + needle)
    require(len(matches) == 1 or occurrence > 0, "OFFICE_GOLD_ANCHOR_AMBIGUOUS: " + needle)
    return source_range(source, path, matches[occurrence], matches[occurrence] + count - 1, role)


def finding(identifier, description, *ranges):
    require(bool(ranges), "OFFICE_GOLD_SUPPORTING_RANGE_REQUIRED")
    return {"id": identifier, "description": description, "supporting_ranges": list(ranges)}


def question(identifier, prompt, findings, forbidden=(), *, split="regression", coverage=()):
    spans = {}
    for item in findings:
        for span in item["supporting_ranges"]:
            key = (span["path"], span["start_line"], span["end_line"], span["source_sha256"])
            spans[key] = {**span, "sha256": span["source_sha256"]}
    return {"id": identifier, "split": split, "question": prompt,
        "coverage": list(coverage), "expected_source_paths": sorted({span["path"] for span in spans.values()}),
        "reviewed_context": {"source_ranges": list(spans.values())},
        "gold": {"required_findings": findings,
            "forbidden_claims": [{"id": identifier + "-forbidden-" + str(index), "description": claim}
                                 for index, claim in enumerate(forbidden, 1)]}}


EXCEPTION_SOURCE = """IDENTIFICATION DIVISION.
PROGRAM-ID. EXCEPTION-CASE.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 IN-AMOUNT PIC 9(3)V99 VALUE 12.34.
01 OUT-AMOUNT PIC 9(2)V9 VALUE 0.
01 ERROR-FLAG PIC 9 VALUE 0.
01 FINAL-AMOUNT PIC 9(2)V9.
PROCEDURE DIVISION.
MAIN.
COMPUTE OUT-AMOUNT ROUNDED = IN-AMOUNT * 2
 ON SIZE ERROR MOVE 1 TO ERROR-FLAG
 NOT ON SIZE ERROR MOVE 0 TO ERROR-FLAG
END-COMPUTE.
MOVE OUT-AMOUNT TO FINAL-AMOUNT.
GOBACK.
"""

UNKNOWN_SOURCE = """IDENTIFICATION DIVISION.
PROGRAM-ID. EXTERNAL-CASE.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 IN-AMOUNT PIC 9(6)V99 VALUE 4.
01 OUT-AMOUNT PIC 9(6)V99.
01 FINAL-AMOUNT PIC 9(6)V99.
PROCEDURE DIVISION.
MAIN.
COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.
CALL 'UNAVAILABLE-SERVICE' USING OUT-AMOUNT.
MOVE OUT-AMOUNT TO FINAL-AMOUNT.
GOBACK.
"""

CONTINUATION_SOURCE = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. CONTINUATION-CASE.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01 TEXT-VALUE PIC X(60).
       01 IN-AMOUNT PIC 9(4)V99.
       01 OUT-AMOUNT PIC 9(4)V99.
       PROCEDURE DIVISION.
       MAIN.
           MOVE 'CALL *> PERFORM
      -    ' IS TEXT' TO TEXT-VALUE.
           COMPUTE OUT-AMOUNT = IN-AMOUNT
               * 2
           END-COMPUTE.
           DISPLAY TEXT-VALUE.
           GOBACK.
"""

RESERVED_SOURCE = """IDENTIFICATION DIVISION.
PROGRAM-ID. RESERVED-CASE.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 BASE-AMOUNT PIC 9(6)V99 VALUE 5.
01 TOTAL-AMOUNT PIC 9(6)V99.
01 FINAL-AMOUNT PIC 9(6)V99.
01 STATUS-FLAG PIC 9.
PROCEDURE DIVISION.
MAIN.
COMPUTE TOTAL-AMOUNT = BASE-AMOUNT * 3.
EVALUATE STATUS-FLAG
 WHEN 1 MOVE 0 TO TOTAL-AMOUNT
 WHEN 2 ADD 1 TO TOTAL-AMOUNT
 WHEN OTHER CONTINUE
END-EVALUATE.
MOVE TOTAL-AMOUNT TO FINAL-AMOUNT.
GOBACK.
"""


def record_binding_cases(source):
    """Review bare and qualified references against frozen declarations/rules."""
    record, front = "synthetic/record.cbl", "synthetic/frontend.cbl"
    record_layout = source_range(source, record, 5, 7, "declaration")
    record_formula = anchor(source, record, "COMPUTE VALUE-00000 = VALUE-00001 * 2.")
    record_body = source_range(source, record, 1, 11, "complete-program")
    qualified_layout = source_range(source, front, 9, 12, "declaration")
    qualified_formula = anchor(source, front, "COMPUTE AMOUNT OF OUTPUT-REC = AMOUNT OF INPUT-REC * 2")
    return [
        question("bare_group_field", "RECORD-RULE 中 VALUE-00000 是怎么计算的？裸字段名对应哪个记录，输入运行值可以确认吗？",
            [finding("bare-group-formula", "真实语句读取VALUE-00001，写VALUE-00000，公式VALUE-00000 = VALUE-00001 * 2；常量是2。", record_formula),
             finding("bare-group-binding", "两个裸字段分别唯一绑定SOURCE-RECORD下的VALUE-00000和VALUE-00001；均声明PIC9(9)，SOURCE-RECORD是层级限定而非额外算术操作数。", record_layout, record_formula),
             finding("bare-group-runtime-unknown", "完整源码未给出VALUE-00001的本次运行值或VALUE初值；PIC说明布局，不能凭声明断言输出具体数值。", record_body)],
            ["因声明是组内字段而称真实乘2公式不存在。", "把SOURCE-RECORD当作第三个算术输入。", "声称裸字段无法绑定或任意另选别的组。", "编造输入或输出运行数值。"],
            coverage=["bare_group_field", "field_hierarchy", "bare_reference_binding"]),
        question("qualified_output_scope", "SEMANTIC-CASE 的 AMOUNT IN OUTPUT-REC 从哪里来？它和 AMOUNT IN INPUT-REC 是同一字段吗？请核对本处公式和记录限定。",
            [finding("qualified-in-alias", "问题中的IN限定和源码OF限定标识同一声明身份：AMOUNT IN OUTPUT-REC对应AMOUNT OF OUTPUT-REC，输入同理；两个记录下的AMOUNT是不同字段。", qualified_layout, qualified_formula),
             finding("qualified-output-formula", "本处读取AMOUNT OF INPUT-REC乘2，只写AMOUNT OF OUTPUT-REC；INPUT-REC和OUTPUT-REC用于限定字段，不是额外算术操作数。", qualified_formula, qualified_layout),
             finding("qualified-output-layout", "两个AMOUNT都声明PIC9(6)V99，没有VALUE初值；本处规则可以确认，实际输入金额及具体输出数值未给出。", qualified_layout, qualified_formula)],
            ["把IN/OF当作不同字段，或将两个记录下的AMOUNT合并。", "把记录限定词当作额外读写操作数。", "以其他A/B/C字段的运算替代这个明确输出的乘2规则。", "凭PIC声明编造实际输入金额。"],
            coverage=["qualified_output_scope", "qualified_in_alias", "qualified_identity"]),
        question("ambiguous_group_field", "SEMANTIC-CASE AMOUNT 怎样计算？",
            [finding("ambiguous-group-candidates", "裸AMOUNT有AMOUNT OF INPUT-REC和AMOUNT OF OUTPUT-REC两个声明候选；应说明歧义并保留两者，不能自动任选其一为所问对象。", qualified_layout),
             finding("ambiguous-independent-formula", "对象仍待明确时，也能独立解释真实COMPUTE：写AMOUNT OF OUTPUT-REC，值为AMOUNT OF INPUT-REC * 2；这条规则不等于已确认用户指的是输出字段。", qualified_formula, qualified_layout),
             finding("ambiguous-qualified-operands", "INPUT-REC与OUTPUT-REC是AMOUNT的限定层级，不是额外算术输入；两个候选都PIC9(6)V99，声明不能提供本次运行输入金额。", qualified_layout, qualified_formula)],
            ["隐去裸AMOUNT的两个候选，自动选一个并宣称目标确定。", "因歧义整题拒答而遗漏已知直接乘2规则。", "把组名作为额外乘数或输入变量。", "编造未给出的运行金额。"],
            coverage=["ambiguous_group_field", "qualified_identity", "independent_direct_fact"]),
    ]


def report_conversation_cases(source):
    """Review the same page/account conversation used by native regressions."""
    report = "synthetic/report.cbl"
    require((Path(source) / report).read_bytes() == _conversation_source().encode("utf-8"),
            "OFFICE_REPORT_NATIVE_FIXTURE_CHANGED")
    initial = source_range(source, report, 4, 8, "initialization-declaration")
    additions = anchor(source, report, "ADD TRAN-AMT TO PAGE-SUM ACCOUNT-SUM.", role="accumulation")
    page_guard = anchor(source, report, "IF LINE-COUNT >= PAGE-SIZE", count=3, role="page-control")
    account_guard = anchor(source, report, "IF CURR-CARD NOT = TRAN-CARD", count=6, role="account-control")
    page_effect = anchor(source, report, "MOVE PAGE-SUM TO REPORT-PAGE-SUM.", count=4, role="page-effects")
    account_effect = anchor(source, report, "MOVE ACCOUNT-SUM TO REPORT-ACCOUNT-SUM.", count=3,
                            role="account-effects")
    initialization = finding("report-value-source",
        "WORKING-STORAGE的VALUE声明给出TRAN-AMT初值25.00，PAGE-SUM、ACCOUNT-SUM和GRAND-SUM初值0；这是首次激活时的初始化来源。后续激活的保留状态和本次进入情况需运行上下文确认，不能把声明初值直接说成本次运行值。", initial)
    accumulation = finding("report-shared-accumulation",
        "ADD-TRANSACTION的ADD读取TRAN-AMT及原PAGE-SUM/ACCOUNT-SUM，并分别写回两个累计项；两项各累加TRAN-AMT，而非把PAGE-SUM转入ACCOUNT-SUM。", additions)
    page = finding("report-page-control-and-effects",
        "ADD-TRANSACTION在LINE-COUNT >= PAGE-SIZE时PERFORM PAGE-TOTALS：先把PAGE-SUM复制到REPORT-PAGE-SUM，再加到GRAND-SUM，最后MOVE 0覆盖PAGE-SUM；这一页汇总段不清零ACCOUNT-SUM。", page_guard, page_effect)
    account = finding("report-account-control-and-effects",
        "MAIN仅在CURR-CARD NOT = TRAN-CARD且FIRST-ITEM = 'N'时PERFORM ACCOUNT-TOTALS：先复制ACCOUNT-SUM到REPORT-ACCOUNT-SUM，随后MOVE 0覆盖ACCOUNT-SUM。在卡号变化分支内，内层判断之后MOVE TRAN-CARD TO CURR-CARD；账户清零由这两个条件共同控制。", account_guard, account_effect)
    account_focus = finding("report-second-account-focus",
        "前一轮第二种情况指ACCOUNT-SUM账户汇总，应保留这个会话对象；PAGE-TOTALS只把PAGE-SUM清零，换页本身不是ACCOUNT-SUM清零的依据。", page_guard, page_effect, account_guard, account_effect)
    overwrite = finding("report-account-later-overwrite",
        "ACCOUNT-SUM来自ADD-TRANSACTION对TRAN-AMT的累加；满足卡号变化且FIRST-ITEM='N'时，ACCOUNT-TOTALS先保存到REPORT-ACCOUNT-SUM，再以MOVE 0覆盖累计项。所以累计曾经生效，零是后续清零，不表示先前ADD未执行或报告金额也归零。", additions, account_guard, account_effect)
    forbidden = ["把声明VALUE初值当作任意本次激活的确定运行值。",
                 "说PAGE-TOTALS或换页会把ACCOUNT-SUM清零。",
                 "遗漏账户清零的卡号变化条件或FIRST-ITEM='N'条件。",
                 "把后续清零说成先前ADD未生效，或声称已保存的报告金额也被清零。",
                 "仅凭CFG候选周期便声称源码有循环或无限重复累计。"]
    turns = [
        question("report-origin", CONVERSATION_QUESTIONS[0],
            [copy.deepcopy(initialization), copy.deepcopy(accumulation), copy.deepcopy(page), copy.deepcopy(account)],
            forbidden),
        question("report-second", CONVERSATION_QUESTIONS[1],
            [copy.deepcopy(account_focus), copy.deepcopy(accumulation), copy.deepcopy(account),
             copy.deepcopy(initialization)], forbidden),
        question("report-account-zero", CONVERSATION_QUESTIONS[2],
            [copy.deepcopy(account_focus), copy.deepcopy(overwrite), copy.deepcopy(initialization)], forbidden),
    ]
    return [{"id": "report-summary-multiturn", "split": "regression",
             "coverage": ["value_initialization_source", "page_account_multiturn", "input_origin",
                          "later_overwrite", "three_turn_focus"], "turns": turns}]


def synthetic_cases(source):
    front = "synthetic/frontend.cbl"
    def front_at(needle, **options):
        return anchor(source, front, needle, **options)
    result = []
    result.append(question("add-giving-roles", "SEMANTIC-CASE 中 ADD A TO B GIVING C 读取和写入哪些字段？第二条 GIVING A 呢？",
        [finding("giving-c", "独立字段 ADD A TO B GIVING C 读取A/B，只写C；不会把C当输入或把B写回。",
                 source_range(source, front, 5, 7, "declaration"), source_range(source, front, 15)),
         finding("giving-a", "ADD A TO B GIVING A 中A同时是输入和输出，B只读。", source_range(source, front, 16))],
        ["把独立GIVING C说成读取C或修改B。", "因去重而丢掉同一实际A字段的读取或写入角色。"],
        coverage=["add_giving", "same_entity_input_output"]))
    result.append(question("scope-terminators", "SEMANTIC-CASE 的 COMPUTE C ROUNDED = A * 2 END-COMPUTE 怎样计算？END-COMPUTE 和 END-ADD 是字段吗？",
        [finding("compute-expression", "C的该处赋值表达式是A*2，接收项C带ROUNDED。", source_range(source, front, 17)),
         finding("scope-words", "END-COMPUTE和END-ADD是结束作用域的关键字，不是字段或算术操作数。", source_range(source, front, 15, 18))],
        ["把结束词当字段或表达式操作数。"], coverage=["end_words", "rounded"]))
    result.append(question("same-line-occurrences", "SEMANTIC-CASE 第18行三条语句分别做了什么？请按顺序解释各自读写，不能合成一次赋值。",
        [finding("line-occurrences", "第18行依次ADD读取A/B写C；MOVE读取C写A；COMPUTE读取新A写B，表达式A*2。", source_range(source, front, 18))],
        ["合并三个语句位置导致读写或顺序丢失。"], coverage=["same_line_occurrences"]))
    result.append(question("literal-code-relations", "SEMANTIC-CASE 两条 DISPLAY 中写了 CALL/PERFORM，会真的调用 GHOST、OTHER 或 LOST 吗？",
        [finding("literal-not-code", "第19/20行CALL/PERFORM只属于显示字符串；双引号及it''s的转义不产生调用或执行段关系。", source_range(source, front, 19, 20))],
        ["认为字符串中的GHOST、OTHER或LOST是实际调用目标。"], coverage=["display_literal_relations", "escaped_quotes"]))
    result.append(question("literal-comment-marker", "SEMANTIC-CASE 的 MOVE 'CALL *> PERFORM' TO TEXT-VALUE 实际赋什么？后面的 CALL 'GHOST' 呢？",
        [finding("quoted-comment", "TEXT-VALUE接收完整字符串CALL *> PERFORM；字符串内*>不是注释起点。", source_range(source, front, 21)),
         finding("real-comment", "字面量结束后行尾*>开始真实注释，注释中的CALL GHOST不会执行。", source_range(source, front, 21))],
        ["将字符串截断为CALL。", "解析注释为调用。"], coverage=["literal_comment_marker"]))
    result.append(question("qualified-fields", "SEMANTIC-CASE 的 AMOUNT OF OUTPUT-REC 从哪里来？它和 AMOUNT OF INPUT-REC 是同一字段吗？",
        [finding("qualified-identity", "两个AMOUNT由INPUT-REC与OUTPUT-REC限定，属于不同声明/存储对象。", source_range(source, front, 9, 12, "declaration")),
         finding("qualified-formula", "写OUTPUT-REC的AMOUNT，读取INPUT-REC的AMOUNT乘2；组名限定词不是额外操作数。", source_range(source, front, 22))],
        ["混为同一AMOUNT。", "把INPUT-REC或OUTPUT-REC列为算术额外输入。"], coverage=["qualified_identity"]))
    result.append(question("perform-section", "SEMANTIC-CASE 的 PERFORM CALCULATE-SECTION 会执行哪些段落？返回后 C 是多少，NEXT-SECTION 的9会执行吗？",
        [finding("section-range", "PERFORM CALCULATE-SECTION涵盖FIRST-PARA和SECOND-PARA；A置1后C接收A。", source_range(source, front, 23, 29)),
         finding("section-boundary", "该次被执行section在NEXT-SECTION之前结束，返回MAIN后GOBACK；NEXT-SECTION的MOVE9不属于本次PERFORM。", source_range(source, front, 23, 31))],
        ["只执行FIRST-PARA。", "把NEXT-SECTION也包含进该PERFORM并断言最终C为9。"], coverage=["perform_section"]))
    over = "synthetic/overwrite.cbl"
    definition = source_range(source, over, 11, 12, "input-definition")
    control = source_range(source, over, 13, 16, "control-and-overwrite")
    computed = finding("actual-times-two", "该处计算是OUT-AMOUNT = IN-AMOUNT * 2；IN-AMOUNT前一条MOVE置4，正常该计算结果8。", definition)
    conditional = finding("overwrite-zero", "FLAG=1时后续MOVE0覆盖OUT-AMOUNT；否则保留8，FINAL-AMOUNT读取两种到达结果。", definition, control)
    result.append(question("formula-999-regression", "OVERWRITE-CASE 前面 OUT-AMOUNT 的公式是不是 IN-AMOUNT * 999？请核对真实常量，再解释最后结果。",
        [computed, conditional], ["接受问题中的999为真实公式。", "忽略后续条件覆盖。"],
        coverage=["wrong_formula_999", "claim_evidence_support"]))
    multi = [question("origin", "OVERWRITE-CASE 的 FINAL-AMOUNT 从哪里来的？请区分第一种FLAG=1和第二种FLAG不等于1。", [copy.deepcopy(computed), copy.deepcopy(conditional)]),
             question("zero", "为什么最后变成零？前面的计算为什么没有生效？", [copy.deepcopy(conditional)]),
             question("second", "刚才第二种情况呢？这里修改 IN-AMOUNT 会影响最后结果吗？", [
                 finding("second-condition", "延续前面第二种FLAG不等于1的条件，计算值可到达FINAL-AMOUNT；FLAG=1则被MOVE0覆盖。", definition, control)])]
    result.append({"id": "overwrite-multiturn", "split": "regression", "coverage": ["input_origin", "later_overwrite", "three_turn_focus"], "turns": multi})
    result.append(question("forward-impact", "OVERWRITE-CASE 修改计算前的 IN-AMOUNT 会影响哪些结果？哪些条件会切断这条影响？",
        [finding("forward-path", "IN-AMOUNT经乘2影响OUT-AMOUNT再到FINAL-AMOUNT；FLAG=1后的零覆盖切断对最终结果的影响，且MAIN之前改变输入会先被MOVE4重写。", definition, control)],
        ["无条件称改变输入必然改变FINAL-AMOUNT。"], coverage=["forward_slice", "input_overwrite"]))
    entry, middle, leaf = "synthetic/entry.cbl", "synthetic/middle.cbl", "synthetic/leaf.cbl"
    result.append(question("static-three-programs", "ENTRYPOINT 的 FINAL-AMOUNT 从哪里来的？沿 MIDDLE、LEAF 说明实参形参和正常返回。",
        [finding("call-bindings", "ENTRYPOINT将IN-AMOUNT/OUT-AMOUNT按位置传给MIDDLE，MIDDLE以相同位置传给LEAF；默认BY REFERENCE共享输出修改。",
                 source_range(source, entry, 10, 13), source_range(source, middle, 5, 10), source_range(source, leaf, 5, 10)),
         finding("leaf-result", "LEAF计算OUT-AMOUNT=IN-AMOUNT*2并GOBACK；正常链返回后ENTRYPOINT复制OUT-AMOUNT到FINAL-AMOUNT。输入4，因此该路径结果8。",
                 source_range(source, entry, 10, 13), source_range(source, leaf, 7, 10))],
        ["把跨程序同名字段只按名字合并而不绑定参数。", "声称链中不存在返回或赋值。"], coverage=["static_a_b_c", "parameter_binding"]))
    exc = "synthetic/exception.cbl"
    result.append(question("arithmetic-exceptions", "EXCEPTION-CASE 的 OUT-AMOUNT 怎样计算并处理 SIZE ERROR？PIC 和 ROUNDED 对结果精度有什么影响？",
        [finding("decimal-layout", "输入PIC9(3)V99有两位小数，输出PIC9(2)V9一位小数，COMPUTE用ROUNDED；给定12.34乘2=24.68，正常可表示时输出24.7。", source_range(source, exc, 5, 8, "declaration"), source_range(source, exc, 11, 15)),
         finding("size-error-branches", "ON SIZE ERROR设置ERROR-FLAG=1，NOT ON SIZE ERROR置0；输出域溢出不能用Python Decimal正常结果代替COBOL SIZE ERROR语义。", source_range(source, exc, 11, 15))],
        ["忽略PIC尺度。", "说发生SIZE ERROR仍必定按正常结果写回。", "说源码证明真实生产输入金额。"], coverage=["rounded", "size_error", "decimal_scale"]))
    unknown = "synthetic/unknown.cbl"
    result.append(question("unknown-effect-boundary", "EXTERNAL-CASE 先乘2后调用 UNAVAILABLE-SERVICE，最后 FINAL-AMOUNT 可以确认是8吗？还有哪些可以独立确认？",
        [finding("known-direct-rule", "可确认调用前IN-AMOUNT初值4及直接COMPUTE乘2；此处直接赋值可独立解释。", source_range(source, unknown, 5, 7, "declaration"), source_range(source, unknown, 10)),
         finding("unknown-return", "UNAVAILABLE-SERVICE源码/效果契约缺失，OUT-AMOUNT按引用传入可能被修改；调用后MOVE读取它，不能确认最终值8或任意其他数。", source_range(source, unknown, 10, 13))],
        ["猜测未知调用不会改输出。", "因缺失服务而拒绝解释已知直接乘2。", "把未知调用效果标为已经核验。"], coverage=["unknown_effect", "independent_direct_fact"]))
    cont = "synthetic/continuation.cbl"
    result.append(question("legal-fixed-continuation", "CONTINUATION-CASE 的续行字符串和跨行 OUT-AMOUNT 公式是什么？里面的 CALL、*>、PERFORM 会改变流程吗？",
        [finding("continued-literal", "固定格式第7列'-'连接字符串，CALL *> PERFORM与后续 IS TEXT均是TEXT-VALUE字面量，不产生调用或注释截断。", source_range(source, cont, 10, 11)),
         finding("continued-formula", "跨行表达式为OUT-AMOUNT=IN-AMOUNT*2；END-COMPUTE结束语句。", source_range(source, cont, 12, 16))],
        ["把合法续行拆成调用。", "遗漏乘2或把结束词当操作数。"], coverage=["legal_continuation", "fixed_format"]))
    reserved = "synthetic/reserved.cbl"
    result.append(question("reserved-conditional-merge", "RESERVED-CASE 不是直接把5乘3就结束吗？为什么 FINAL-AMOUNT 在不同 STATUS-FLAG 下不同？",
        [finding("reserved-base", "BASE-AMOUNT初值5，经乘3得到TOTAL-AMOUNT=15。", source_range(source, reserved, 5, 8, "declaration"), source_range(source, reserved, 11)),
         finding("reserved-merge", "STATUS-FLAG=1则后续总额置0；=2则再加1成16；WHEN OTHER保留15；最终MOVE读取分支合并结果。", source_range(source, reserved, 11, 18))],
        ["仅按段落/变量名字推断。", "遗漏WHEN2或OTHER分支。"], split="holdout", coverage=["evaluate_merge", "reserved_question"]))
    result.extend(record_binding_cases(source))
    result.extend(report_conversation_cases(source))
    return result


def public_cases(source):
    originals = json.loads(PUBLIC_CASES.read_text(encoding="utf-8"))["cases"]
    account, transaction = "app/cbl/CBACT04C.cbl", "app/cbl/CBTRN02C.cbl"
    ranges = {
        "limit-rejection": [(transaction, 403, 405), (transaction, 407, 413), (transaction, 414, 421), (transaction, 390, 402)],
        "account-update": [(account, 350, 353), (account, 353, 354), (account, 356, 356), (account, 357, 369)],
        "monthly-interest": [(account, 462, 466), (account, 467, 470), (account, 167, 169)],
        "file-record-mapping": [(account, 41, 45), (account, 84, 88), (account, 41, 45)],
        "unimplemented-procedure": [(account, 518, 521), (account, 518, 521), (account, 214, 217)],
    }
    result = []
    for original in originals:
        item = copy.deepcopy(original)
        item.pop("mechanism", None)
        item["id"] = "public-" + original["id"]
        findings = item["gold"]["required_findings"]
        require(len(findings) == len(ranges[original["id"]]), "OFFICE_PUBLIC_GOLD_SHAPE_CHANGED")
        for finding_row, (path, start, end) in zip(findings, ranges[original["id"]], strict=True):
            finding_row["supporting_ranges"] = [source_range(source, path, start, end)]
        if original["id"] == "monthly-interest":
            findings[2]["supporting_ranges"] += [source_range(source, account, 214, 217), source_range(source, account, 462, 471)]
        if original["id"] == "file-record-mapping":
            findings[1]["supporting_ranges"] += [anchor(source, account, "COPY CVACT01Y."),
                source_range(source, "app/cpy/CVACT01Y.cpy", 1, len((Path(source) / "app/cpy/CVACT01Y.cpy").read_bytes().splitlines()), "copy-layout"),
                source_range(source, account, 373, 376), source_range(source, account, 356)]
        # Public gold supports source facts only, including explicit unknown runtime data.
        item["reviewed_context"] = question(item["id"], item["question"], findings)["reviewed_context"]
        item["expected_source_paths"] = sorted({span["path"] for row in findings for span in row["supporting_ranges"]})
        item["coverage"] = ["public_" + original["id"].replace("-", "_")]
        result.append(item)
    by_id = {item["id"]: item for item in result}
    for original_id, identifier, prompt, coverage in (
        ("public-monthly-interest", "public-monthly-interest-999", "CBACT04C 的月息是不是 (TRAN-CAT-BAL * DIS-INT-RATE) / 999？请核对常量、调用条件和后续累加，不要接受我的假设。", "public_wrong_divisor"),
        ("public-unimplemented-procedure", "public-fees-invention", "我看到 CBACT04C 的 1400-COMPUTE-FEES 名字，是否可以说它按余额的1%收费？请给出这个段落实际做的事情和可确认范围。", "public_fee_invention")):
        item = copy.deepcopy(by_id[original_id])
        item.update(id=identifier, question=prompt, coverage=[coverage])
        result.append(item)
    return result


def large_cases(source, specification):
    """Five real turns about reviewed occurrences in one generated large program.

    Names alone are deliberately insufficient: the question, input declaration,
    control and successive writes all bind to the same file and physical ranges.
    """
    relative = "scale/member-" + str(specification["programs"] - 1).zfill(5) + ".cbl"
    path = Path(source) / relative
    lines = path.read_text(encoding="utf-8").splitlines()
    first = lines.index("RULE-000.") + 2
    require(lines[first - 1:first + 2] == ["IF INPUT-000 > 0", "COMPUTE BILL-AMOUNT = INPUT-000 * 1", "END-IF."],
            "OFFICE_LARGE_FIRST_RULE_CHANGED")
    following = first + 4
    require(lines[following - 1:following + 2] == ["IF INPUT-001 > 1", "COMPUTE BILL-AMOUNT = INPUT-001 * 2", "END-IF."],
            "OFFICE_LARGE_NEXT_RULE_CHANGED")
    first_rule = source_range(source, relative, first, first + 2, "first-occurrence")
    next_rule = source_range(source, relative, following, following + 2, "later-write")
    input_item = anchor(source, relative, "01 INPUT-000 PIC 9(9).", role="input-declaration")
    next_input = anchor(source, relative, "01 INPUT-001 PIC 9(9).", role="input-declaration")
    output_item = anchor(source, relative, "01 BILL-AMOUNT PIC 9(9).", role="output-declaration")
    findings = [
        finding("large-first-compute", "该文件RULE-000的第一条IF仅在INPUT-000>0时写BILL-AMOUNT=INPUT-000*1；限定这一语句位置，不把其他同名字段/程序公式混入。", first_rule),
        finding("large-input-source", "这里读取本程序WORKING-STORAGE中的INPUT-000，声明PIC9(9)，输出BILL-AMOUNT也为PIC9(9)；该声明没有给出实际运行输入，不能猜生产数值。", input_item, output_item),
        finding("large-later-overwrite", "紧接下一条IF在INPUT-001>1时再次写BILL-AMOUNT=INPUT-001*2，会覆盖前一条计算；若不成立则这一条不覆盖。当前说明只覆盖这两个相邻发生点，不能据此声称整个程序最后结果。", first_rule, next_rule, next_input)]
    prompts = [
        f"请只看 {relative} 的 RULE-000：第一条 IF 怎样计算 BILL-AMOUNT，输入在哪里声明，紧接下一条 IF 会怎样覆盖它？请给出条件与公式，不要混入其他同名程序。",
        "刚才第一条计算的 BILL-AMOUNT 从哪里来的？请保留同一个文件和 RULE-000 的发生位置，解释 INPUT-000 的声明、触发门槛，以及紧接下一条的覆盖风险。",
        "前面的计算为什么可能没有生效？仍按刚才两个相邻 IF，分别说明第一次乘数、输入声明和第二次覆盖的条件与公式，不要直接猜最终运行金额。",
        "修改这里的 INPUT-000 会影响哪些结果？仍看刚才文件里 RULE-000 前两个 IF，请说明首个条件与赋值、字段声明，以及 INPUT-001 条件怎样改变影响路径。",
        "换种说法：这个程序最后一定等于 INPUT-000 吗？请用刚才文件的首个公式、PIC 声明和后续 INPUT-001 覆盖条件，解释能确认的两次赋值和不能据此确认的最终结果。"]
    turns = [question("large-turn-" + str(index), prompt, copy.deepcopy(findings),
              ["将大库其他程序的同名BILL-AMOUNT混入目标语句。", "把首个条件公式当作整个程序无条件最终输出。", "用声明猜真实输入或金额。"])
             for index, prompt in enumerate(prompts, 1)]
    return [{"id": "large-repository-real", "split": "regression", "coverage": ["large_repository_real", "large_entity_disambiguation", "large_real_multiturn"],
             "turns": turns}]


def measurement_plan():
    return {"schema_version": "office-measurement-plan/v1", "status": "planned_not_executed",
        "execution_surface": "final_windows_exe_on_target_computer", "result": None,
        "requirements": [
            {"id": "question_latency", "procedure": "Record complete HTTP turn duration, first model request, model request count and source supply from actual native live traces; score all frozen turns.", "result": None},
            {"id": "large_repository", "procedure": "Import the whole source root including the frozen 12,024-file scale subdirectory on the target computer without a model request. Record cold import and indexed file/byte counters; every live regression turn must use this index. Five large-repository-real turns bind actual occurrences in the largest program; record p50/p95 from their real responses.", "source_specification": "docs/eval/desktop-joint-v1/manifest.json#large_corpus", "result": None},
            {"id": "desktop_start", "procedure": "Unzip the whole delivery and launch its GUI as an ordinary user; verify source chooser, settings and answer/evidence panel are usable.", "result": None},
            {"id": "source_import", "procedure": "Import frozen source with explicit format/configuration; verify frozen hashes, complete file inventory and COPY origins, rather than counting UI success alone.", "result": None},
            {"id": "conversation_recovery", "procedure": "Complete the first two real overwrite turns, close and reopen the EXE, and ask the third turn in the restored conversation; bind the actual preserved history and prior answer versions.", "result": None},
            {"id": "cancellation_recovery", "procedure": "Cancel a local-only index or investigation and restart from saved progress. For an interrupted remote request retain started/unknown state; do not repeat a possibly charged request automatically.", "result": None},
            {"id": "historical_evidence", "procedure": "After a source-free archived answer is saved, change only an isolated copy of synthetic source, rebuild, and verify the archived answer still opens its original source hash/range while a new answer uses the new version.", "result": None},
            {"id": "company_api_ca", "procedure": "Use the existing authorized model profile and CA bundle on this computer; record a real successful TLS request with model and prompt fingerprints, without exporting credentials or enterprise source.", "result": None}],
        "no_model_calls_performed": True, "office_checks_are_not_automatically_passed": True}


def generate_suite(output, public_source, *, include_scale=False):
    """Create one self-contained frozen suite, requiring verified local public files."""
    output = Path(output).resolve()
    require(not output.exists(), "OFFICE_OUTPUT_ALREADY_EXISTS")
    require(public_source is not None, "OFFICE_LOCAL_PUBLIC_SOURCE_REQUIRED")
    public_source = Path(public_source).resolve(strict=True)
    require(not output.is_relative_to(public_source) and not public_source.is_relative_to(output),
            "OFFICE_OUTPUT_SOURCE_OVERLAP")
    public = json.loads(PUBLIC_MANIFEST.read_text(encoding="utf-8"))
    frozen = json.loads(SYNTHETIC_MANIFEST.read_text(encoding="utf-8"))
    # Verify all bytes before creating output; a missing corpus cannot be silently skipped.
    public_files = [(item["path"], checked_file(public_source, item["path"], item["sha256"])) for item in public["files"]]
    license_file = checked_file(public_source, "LICENSE", public["license_sha256"])
    synthetic_files = [(name, checked_file(SYNTHETIC_MANIFEST.parent / "source", item["path"], item["sha256"]))
                       for name, item in frozen["source_files"].items()]
    source = output / "source"
    source.mkdir(parents=True)
    for relative, path in public_files:
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    synthetic = source / "synthetic"
    synthetic.mkdir()
    for name, path in synthetic_files:
        shutil.copyfile(path, synthetic / name)
    for name, text in (("exception.cbl", EXCEPTION_SOURCE), ("unknown.cbl", UNKNOWN_SOURCE),
                       ("continuation.cbl", CONTINUATION_SOURCE), ("reserved.cbl", RESERVED_SOURCE),
                       ("report.cbl", _conversation_source())):
        (synthetic / name).write_bytes(text.encode("utf-8"))
    shutil.copyfile(license_file, output / "PUBLIC-SOURCE-LICENSE.txt")
    cases = synthetic_cases(source) + public_cases(source)
    scale = None
    if include_scale:
        from tools.desktop_acceptance_cases import generate_large
        scale = generate_large(source / "scale", frozen["large_corpus"])
        cases += large_cases(source, frozen["large_corpus"])
    turn_count = sum(len(item.get("turns") or [item]) for item in cases)
    document = {"schema_version": "live-business-cases/v3", "suite_id": "office-regression-v1", "cases": cases,
        "case_origin": "Reviewed public and synthetic source. Regression questions informed development. Reserved question is frozen for this acceptance and has not been used to tune real model output; it is not an independent enterprise holdout.",
        "source_format": "auto", "encoding": "utf-8", "user_turn_count": turn_count,
        "budget": {"suggested_requests_per_turn": 3, "maximum_model_requests": turn_count * 3,
                   "preflight_requests": 0, "automatic_retries": 0,
                   "real_model_calls_performed": 0},
        "review_policy": "Every critical finding must be supported, every forbidden claim absent, actual supplied ranges recalled, and run/answer/model/prompt/evidence identity matched. Filled forms alone are not correctness.",
        "public_provenance": {"source_commit": public["commit"], "manifest_sha256": digest(PUBLIC_MANIFEST),
                              "gold_sha256": digest(PUBLIC_CASES), "license_sha256": public["license_sha256"]}}
    cases_path = output / "LIVE-CASES.json"
    write_json(cases_path, document)
    files = [{"path": path.relative_to(source).as_posix(), "sha256": digest(path), "bytes": path.stat().st_size}
             for path in sorted(source.rglob("*")) if path.is_file()]
    specification = {"schema_version": "office-source-lock/v1", "suite_id": document["suite_id"],
        "cases_sha256": digest(cases_path), "files": files,
        "synthetic_manifest_sha256": digest(SYNTHETIC_MANIFEST), "public_manifest_sha256": digest(PUBLIC_MANIFEST),
        "user_turn_count": turn_count, "coverage": sorted({tag for case in cases for tag in case.get("coverage", [])}),
        "scale_source": {"path": "source/scale", **scale} if scale else None,
        "real_model_accuracy": None, "office_acceptance_passed": False}
    write_json(output / "SOURCE-LOCK.json", specification)
    write_json(output / "WINDOWS-MEASUREMENT-PLAN.json", measurement_plan())
    return {"source": source, "cases": cases_path, "lock": output / "SOURCE-LOCK.json",
            "user_turn_count": turn_count, "maximum_model_requests": turn_count * 3,
            "source_files": len(files)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--public-source", required=True, help="Existing local checkout of the frozen public corpus; never downloaded by this tool.")
    parser.add_argument("--include-scale", action="store_true")
    options = parser.parse_args(argv)
    result = generate_suite(options.output, options.public_source, include_scale=options.include_scale)
    print(json.dumps({key: str(value) if isinstance(value, Path) else value for key, value in result.items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
