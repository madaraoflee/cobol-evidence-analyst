"""Readable answer guidance and lossless handling of explicit Markdown wrappers."""

from __future__ import annotations

import re


BUSINESS_ANSWER_POLICY = """默认交付给业务分析师可直接使用的业务知识：先回答这个问题的结论，再按需要解释业务条件、计算口径、例外、结果或变更影响，把源码理解转化成业务解释。金额、比例、单位、舍入、先后顺序和状态变化仅在实际相关且资料支持时说明。不要固定套用一组栏目，不限定业务主题。
默认不展示源代码段、伪代码、逐句代码翻译或框架内部实现细节。只有用户明确要求查看源码、具体语句或开发实现时，才展示必要的局部代码；仅询问如何计算、为什么、影响什么或改哪些程序，不表示用户要求看代码。计算式用有业务含义的名称表达。用户要求定位影响程序、文件或字段时保留准确标识，但不要让标识和调用列表取代业务解释。关键结论保留简短来源引用，源码和技术依据由来源详情查看。
framework_references 是当前项目框架资料的相关原文。解释项目行为时，把资料定义的功能码、记录操作、数据传递或错误处理约定，与当前源码实际传入的值、调用条件和返回后的业务分支对应起来，再用业务语言说明这次操作的含义与结果；不能只讲通用 COBOL 语法或复述框架目录。matched_terms 和 source_locations 仅是匹配线索，是否适用仍以资料原文和本轮源码为依据。框架公共实现缺源码时可据明确约定解释调用者的业务行为，不杜撰内部算法、额外效果或运行结果。没有相关资料或约定未写清时，只简短说明实际影响当前答案的未知点，并继续解释已有业务规则。"""

ANSWER_MARKDOWN_POLICY = """最终给用户的答案使用 Markdown 正文：开头直接回答问题，用自然段推进说明；需要比较时用表格，需要步骤或并列内容时用列表，重点可用加粗。段落、列表和表格之间留空行，长答案按需使用简短的小标题，短答案无需套模板。
不要将整篇答案放进 markdown、md、text 或其他代码围栏，不要把正文包装成 JSON，也不要输出 HTML。只有用户明确要求源码示例或开发实现时才给局部代码块，并注明语言；业务规则优先解释含义、条件和结果。保留给定的来源引用标识。Markdown 是展示约定，不改变调查方式或工具请求格式。"""


def normalize_answer_markdown(text: str) -> str:
    """Unwrap only one explicit whole-answer Markdown fence; keep code intact.

    Accept labelled code inside a wrapper even when a provider uses the same
    fence length for both. An outer closing fence before the last line means
    separate examples. Unknown outer languages and unlabelled code stay intact.
    """
    value = text.strip()
    lines = value.splitlines()
    if len(lines) < 3:
        return value
    opening = re.fullmatch(r" {0,3}(`{3,}|~{3,})(?:markdown|md)[ \t]*", lines[0], re.I)
    if opening is None:
        return value
    marker = opening[1]
    closing = re.compile(r" {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*")
    inner_closing = None
    for index, line in enumerate(lines[1:], 1):
        if inner_closing is not None:
            if inner_closing.fullmatch(line):
                inner_closing = None
            continue
        if closing.fullmatch(line):
            if index == len(lines) - 1:
                return "\n".join(lines[1:index]).strip()
            return value
        inner_opening = re.fullmatch(r" {0,3}(`{3,}|~{3,})[ \t]*([A-Za-z][^`~\r\n]*)", line)
        if inner_opening:
            inner_marker = inner_opening[1]
            inner_closing = re.compile(r" {0,3}" + re.escape(inner_marker[0]) + "{" + str(len(inner_marker)) + r",}[ \t]*")
    return value
