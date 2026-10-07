"""Readable answer guidance and lossless handling of explicit Markdown wrappers."""

from __future__ import annotations

import re


BUSINESS_ANSWER_POLICY = """交付围绕当前问题、充分展开的业务解释。开头直接给结论，随后详细说明决定结论的条件、相关输入来源、处理顺序、字段变化、异常分支和结果，并引用来源。具体场景按用户给定的输入和假设沿执行路径逐步推导；一般规则问题则完整覆盖决定结果的分支。复杂答案使用简短标题、步骤或比较表，保留理解业务所需的细节和推导，不以缩短答案代替分析。相关补充场景明确写出各自成立条件；避免重复相同结论。用户明确要求简答时保留关键条件并压缩说明。
写作前核对：每个值属于哪个业务对象、在哪个调用点和条件下产生、之后是否被覆盖；分清调用前跳过、尝试调用但加载失败、被调程序执行后返回业务错误。表格与正文保持同一条件范围。跨程序回写要同时沿调用方传递方式和被调方入口声明追踪。只回答当前问题涉及的行为；用户要求改法或其他情景时再展开变更建议和假设分析。
解释按优先级选择的分支时，明确区分条件不成立与分支未执行：EVALUATE 已命中前面的 WHEN，后续分支标为未执行；沿已选路径推导结果，不把未检查的条件或字段值补成事实。
追踪字段值时，按执行顺序经过每个调用点。外部实现或适用约定未说明的引用参数及其下级字段，返回后以未知值继续推导；后续本地赋值重新建立已知值。先前的初始化同样经过这条规则。SQL INTO 的输出也按执行语义与后续赋值追踪。只展开决定当前问题结果的字段和时点。
用业务含义表达公式和处理过程；定位程序、文件、字段时保留准确标识。除非用户明确要求源码或开发实现，否则只给业务解释，不输出代码块或逐行翻译。framework_references 中的约定须与当前调用条件和实参对应。缺少被调实现和适用约定时，准确写出调用者做了什么，例如设置操作码、传入记录、发起请求、检查返回状态、选择下一分支；操作名称本身不提供读取、锁定、保存或提交的实现细节。把已知的业务规则讲清楚，仅在未知事项影响用户所问结果时简短说明。"""

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


def _continuation_overlap(draft: str, continuation: str) -> int:
    """Find a bounded exact suffix/prefix match in linear time."""
    pattern = continuation[:8192]
    if not pattern:
        return 0
    prefixes = [0] * len(pattern)
    matched = 0
    for index in range(1, len(pattern)):
        while matched and pattern[index] != pattern[matched]:
            matched = prefixes[matched - 1]
        if pattern[index] == pattern[matched]:
            matched += 1
        prefixes[index] = matched
    matched = 0
    for character in draft[-8192:]:
        while matched and (matched == len(pattern) or character != pattern[matched]):
            matched = prefixes[matched - 1]
        if character == pattern[matched]:
            matched += 1
    return matched


def _open_code_marker(text: str) -> tuple[str, str] | None:
    """Return an unfinished Markdown code delimiter, ignoring escaped ticks."""
    fence = None
    inline = None
    for line in text.splitlines():
        marker = re.match(r" {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if (marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence)
                    and not marker[2].strip()):
                fence = None
            continue
        if marker:
            fence, inline = marker[1], None
            continue
        for match in re.finditer(r"(?<!\\)`+", line):
            token = match[0]
            if inline is None:
                inline = token
            elif token == inline:
                inline = None
    return ("fence", fence) if fence else ("inline", inline) if inline else None


def _continuation_restart_boundary(draft: str, offset: int) -> bool:
    """A shared identifier inside a sentence cannot establish a text restart."""
    if offset == 0 or draft[offset - 1] == "\n":
        return True
    prefix = draft[:offset].rstrip()
    return prefix.endswith(tuple(".!?。！？…;；"))


def _table_pipes(line: str) -> list[int]:
    """Follow GFM column splitting before inline code is parsed."""
    positions = []
    index = 0
    while index < len(line):
        character = line[index]
        if character == "\\":
            index += 2
            continue
        if character == "|":
            positions.append(index)
        index += 1
    return positions


def _table_cells(line: str) -> list[str]:
    positions = _table_pipes(line)
    if not positions:
        return []
    boundaries = [-1, *positions, len(line)]
    cells = [line[left + 1:right].strip() for left, right in zip(boundaries, boundaries[1:])]
    if not cells[0]:
        cells.pop(0)
    if cells and not cells[-1]:
        cells.pop()
    return cells


def _short_table_cell_restart(draft: str, overlap: int) -> bool:
    """Recognize only a plain, partial clause repeated at a table cell start.

    A single word, quoted text, citation, or code cannot establish this restart.
    Require an internal semicolon so ordinary repeated words remain intact.
    The existing rules retain responsibility for longer paragraph/list restarts.
    """
    if not 3 <= overlap < 16:
        return False
    shared = draft[-overlap:]
    if (not shared[0].isalnum() or not shared[-1].isalnum()
            or not any(character in ";；" for character in shared)
            or any(not character.isalnum() and character not in " _-;；\t" for character in shared)):
        return False
    if _open_code_marker(draft) is not None:
        return False
    lines = draft.splitlines()
    block_start = len(lines) - 1
    while block_start and lines[block_start - 1].strip():
        block_start -= 1
    block = lines[block_start:]
    if len(block) < 3 or any(
            line.expandtabs(4).startswith("    ")
            or re.match(r" {0,3}(?:#{1,6}(?:\s|$)|[-*+]\s|\d{1,9}[.)]\s|>|`{3,}|~{3,}|<)", line)
            for line in block):
        return False
    header = _table_cells(block[0])
    delimiter = _table_cells(block[1])
    if (len(header) < 2 or len(header) != len(delimiter)
            or not all(re.fullmatch(r":?-{3,}:?", cell) for cell in delimiter)
            or any(not _table_pipes(line) or len(_table_cells(line)) != len(header)
                   for line in block[2:-1])
            or not 1 <= len(_table_cells(block[-1])) <= len(header)):
        return False
    row = block[-1]
    pipes = _table_pipes(row)
    return row[pipes[-1] + 1:].lstrip(" \t") == shared


def join_answer_continuation(draft: str, continuation: str) -> str:
    """Join a truncated answer without rewriting its earlier business content.

    Only exact suffix/prefix repetition is removed. A short common word or
    similar but different statement is not enough to discard text. Paragraph
    and list restarts are handled when they repeat the unfinished tail exactly;
    arbitrary paraphrases remain intact, since their equivalence is unknown.
    This assembles text, not a proof of semantic completeness or correctness.
    """
    if not draft or not continuation:
        return draft or continuation
    # Some continuations repeat the entire draft, including a long answer.
    if len(draft) >= 16 and continuation.startswith(draft):
        return continuation
    overlap = _continuation_overlap(draft, continuation)
    tail = draft.rsplit("\n", 1)[-1]
    short_tail_restart = (len(tail) >= 8 and overlap == len(tail)
                          and bool(re.search(r"\w", tail)))
    if ((overlap >= 16 or short_tail_restart)
            and _continuation_restart_boundary(draft, len(draft) - overlap)):
        return draft + continuation[overlap:]
    if _short_table_cell_restart(draft, overlap):
        return draft + continuation[overlap:]
    # Preserve an explicit boundary supplied by either response.
    if draft[-1].isspace() or continuation[0].isspace():
        return draft + continuation
    code_marker = _open_code_marker(draft)
    if code_marker:
        kind, marker = code_marker
        closing_fence = (kind == "fence" and re.match(
            r"^" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*(?:\n|$)", continuation))
        return draft + ("\n" if closing_fence else "") + continuation
    # Block starts must stay on their own line. Short duplicated list numbers
    # alone are never treated as evidence that two items have the same meaning.
    if re.match(r" {0,3}(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|[>|]|`{3,}|~{3,})", continuation):
        return draft + "\n\n" + continuation
    # Ignore closing Markdown decoration/citations only when choosing spacing.
    boundary = re.sub(r"(?:\s*\[[^\]\n]+\](?:\([^\n)]*\))?)+$", "", draft).rstrip()
    boundary = boundary.rstrip("*_'\"`”’）)]}")
    if boundary.endswith(tuple(".!?。！？…:：;；")):
        return draft + "\n\n" + continuation
    # Continue unfinished sentences and identifiers without a blank paragraph.
    # CJK text has no word spaces; Latin prose normally needs one unless the
    # split is explicitly inside an identifier or before closing punctuation.
    separator = " "
    if (draft[-1] in "-_/(（[{" or continuation[0] in ".,;:!?。！？；，：、)]}）"
            or "\u2e80" <= draft[-1] <= "\ua4cf"
            or "\u2e80" <= continuation[0] <= "\ua4cf"):
        separator = ""
    return draft + separator + continuation
