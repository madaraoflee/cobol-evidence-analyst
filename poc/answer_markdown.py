"""Readable answer guidance and lossless handling of explicit Markdown wrappers."""

from __future__ import annotations

import re

from response_language import SUPPORTED_LANGUAGES, answer_language_instruction


BUSINESS_RUNTIME_BOUNDARY_POLICY = (
    "直接解释源码确定的输入、规则和输出；缺调度、数据或绑定仅限制相关运行结论。"
    "未校验不证明查不到，零或占位值也可能匹配；查询结果须有数据或假设。"
    "无状态检查不等于异常后继续；CALL后清理须已返回，‘不影响下一条’仅指正常业务控制路径。"
    "SQL摘要和表格均限定‘查询返回且执行到状态判断时’的零/非零分支；抛错或未返回不等于非零，不说失败都返回业务码。"
    "每个独立摘要、表格或结语的空输入结论，就地写明‘按所示初始状态且 OPEN/READ 正常、首次 READ 到 AT END 时’；"
    "VALUE 仅初始化，不代表每次调用重置。按分支说明处理、写入或提示；正常关闭才说正常结束，不保证物理输出为空。"
    "未见VALUE、INITIALIZE或已知赋值且未确认适用初始化约定时，初值未知，不能凭WORKING-STORAGE/PIC推断为零；仍按进入判断时的值解释各分支。"
    "DISPLAY只说明输出请求；目的地须有手册及启动依据，未知时不罗列丢弃、重定向等无证可能性。"
    "PIC不保证外部字节有效；无符号且符合声明时，大于零才等同非零。"
    "区分数学运算结果与接收字段可存储的结果；合法输入不保证乘积或合计不会超出PIC范围，不把所有合法输入都写成可原样存入。"
    "未见调用或共享数据只说明材料内未见关联，不证明系统全局无关；night/daily文件名不证明调度频率。"
    "摘要和表格保留控制前提，文末限制不能修正前文保证；区分输出参数与返回码。"
    "同一结论及影响当前问题的缺项只说明一次，不重复清单或扩写未问的空文件、重复调用、假设异常；位置用引用，不编行号。")


BUSINESS_EXPLANATION_POLICY = (
    "面向不熟悉代码的读者，先说明这项业务处理什么对象、为什么处理、产出什么，"
    "再沿实际交接讲清谁发起、哪个程序负责哪一步、从哪个表或文件取得什么、交给谁使用。"
    "按有据的业务顺序组织，不按源码文件顺序逐个翻译；程序名、字段名用于定位，第一次出现时说明其角色。"
    "批处理作业说明如何组织处理，在线功能说明调用输入与返回结果，表说明共享数据角色，"
    "文件说明读入或写出记录；只展开本题有依据的类别，不为凑齐四类捏造环节。"
    "交接关系须来自具体调用、参数、读写、文件绑定或适用框架约定；同名、共同COPY或出现在同一目录不能证明关联。"
    "可沿 business_orientation.handoffs 对照原文串联职责；它是局部静态关系，不证明执行顺序或业务因果。"
    "明确区分读已有余额与计算余额、生成输出记录与完成付款、请求更新与成功提交。"
    "数值比较不是数据有效性校验：只看到金额大于零、没有 IS NUMERIC 等检查时，不得说非法金额会被识别、拦截或跳过。"
    "业务解释中的技术标识逐字符保留，尤其不要互换下划线与连字符；SQL 表名和查询条件不提供数据库连接配置。"
    "规则说明哪些记录进入、哪些跳过、结果如何变化；异常说明使用者还能得到什么以及该如何理解返回状态。"
    "先给出源码已经确认的业务解释；缺调度或部署信息不妨碍解释代码职责。"
    "确实影响结论的条件紧贴该结论，其余运行环境缺口集中简短说明一次，不用反复的未验证打断流程。"
    "不要先铺长篇证据免责声明，不重复用摘要、表格、正文和结尾讲同一件事。"
    "没有领域名称时直接描述可见输入到输出的作用；注释用途与源码规则吻合时注明依据，不猜业务词义。"
    "简短关系追问、公式题只解释所问内容，不强套概览。")

BUSINESS_EXPLANATION_COMPACT_POLICY = (
    "先一小段业务目的、规则和结果，再分Batch job、Online function / Programs、Tables、Files讲职责交接；"
    "详细规则归入负责类别只讲一次。有据类别展开，未见简注未确认，程序不自动等于在线；"
    "handoffs是导航，同名/COPY不证关联，窄问只答所问。")



DEFAULT_ANSWER_INSTRUCTIONS = """请面向刚接触这项业务的新人解释，不要逐行翻译代码。
开头用一小段业务概览讲清处理什么对象、关键规则和最终结果；若材料包含多条独立业务，简短区分，不补造关联。
随后直接按本题实际涉及的类别说明职责和数据交接。详细条件、步骤和异常放在负责该处理的类别中讲一次，不要先写一遍完整流程，再用分类、表格和结尾重复：
1. Batch job（批处理作业）：作业或入口、处理范围、主要步骤、业务条件、输入输出及上下游关系。只有资料有依据时才写调度频率。
2. Online function / Programs（在线功能与程序）：用户或外部请求要完成什么、入口及程序分工、输入、校验、返回结果；没有在线入口依据的程序只说明其职责。
3. Tables（数据表）：每个表保存什么业务数据、哪些步骤读写它、关键字段或关联如何影响业务；区分数据库表与程序内部表。
4. Files（文件）：每个文件承载什么业务记录、由谁产生、由谁读取、输入输出如何衔接；区分业务数据文件与源码/COPY定义。
每个名称第一次出现时解释业务角色，说明这些对象如何共同完成本题业务，不要只罗列名字。
先把本题涉及的各类职责与关系讲完整，再按需要补充细节；不展开用户未问且不影响核心结论的假设场景。
使用现有来源支持结论并保留引用；缺少的类别简短写“当前资料未确认”，不要补造。把确实影响结论的未知事项说清楚。
追问只展开这次所问的内容；已经讲清的概览不必重复。"""


def with_answer_instructions(system: str, instructions: str | None = None) -> str:
    """Apply the saved writing requirements without changing evidence or tools."""
    if not isinstance(instructions, str) or not instructions.strip():
        return system
    system = system.replace("\n业务概览默认按以下方式组织，用户自定义回答要求可以调整格式：\n"
                            + DEFAULT_ANSWER_INSTRUCTIONS, "", 1)
    # Request compaction uses a different default template. Remove its writing
    # structure too, while retaining the source-relationship boundary.
    system = system.replace(BUSINESS_EXPLANATION_COMPACT_POLICY,
                            "handoffs是导航，同名/COPY不证关联，窄问只答所问。", 1)
    for language in SUPPORTED_LANGUAGES:
        for compact in (False, True):
            system = system.replace(answer_language_instruction(language, compact=compact),
                                    answer_language_instruction(language, saved_requirements=True), 1)
    return (system + "\n\n用户在系统配置中保存的回答要求（原文）：\n" + instructions
            + "\n\n以上是用户的回答要求，应实际用于本次回答及后续补充、续写和修订。"
              "其语言、受众、结构、栏目、措辞与详略要求优先于内置默认写作格式；当前问题的明确要求优先。"
              "补查轮仍使用所需的检索动作格式，最终答案再按用户要求组织。"
              "来源事实、引用与未知范围仍以实际材料为准，不能为了填满模板编造内容。")


BUSINESS_ANSWER_POLICY = """围绕当前问题解释业务。开头给带成立条件的结论，再说明决定结果的输入、处理顺序、字段变化与异常分支，并引用来源。具体场景按给定输入和假设沿路径推导；一般规则覆盖决定结果的分支。复杂答案可用简短标题、步骤或比较表，保留必要推导；已解释的结论不再换格式重复，结尾不重列长篇缺项。补充场景须有各自条件；简答保留关键条件。
区分源码可见规则、注释说明、适用手册约定与合理推断，推断须说明依据；均不证明实际运行。没有业务名称时解释可见操作用途，不猜调度频率、联机菜单、真实表或提交结果。
写作前核对：每个值属于哪个业务对象、在哪个调用点和条件下产生、之后是否被覆盖；分清调用前跳过、尝试调用但加载失败、被调程序执行后返回业务错误。表格与正文保持同一条件范围。跨程序回写要同时沿调用方传递方式和被调方入口声明追踪。只回答当前问题涉及的行为；用户要求改法或其他情景时再展开变更建议和假设分析。
解释按优先级选择的分支时，明确区分条件不成立与分支未执行：EVALUATE 已命中前面的 WHEN，后续分支标为未执行；沿已选路径推导结果，不把未检查的条件或字段值补成事实。
追踪字段值时，按执行顺序经过每个调用点。外部实现及适用约定均未说明的引用参数及其下级字段，返回后以未知值继续推导；后续本地赋值重新建立已知值。先前的初始化同样经过这条规则。SQL INTO 的输出也按执行语义与后续赋值追踪。只展开决定当前问题结果的字段和时点。
用业务含义表达公式和处理过程；定位程序、文件、字段时保留准确标识。除非用户明确要求源码或开发实现，否则只给业务解释，不输出代码块或逐行翻译。framework_references 中的约定须与当前调用条件和实参对应。缺少被调实现和适用约定时，准确写出调用者做了什么，例如设置操作码、传入记录、发起请求、检查返回状态、选择下一分支；操作名称本身不提供读取、锁定、保存或提交的实现细节。把已知的业务规则讲清楚，仅在未知事项影响用户所问结果时简短说明。"""
BUSINESS_ANSWER_POLICY += "\n" + BUSINESS_EXPLANATION_POLICY + "\n" + BUSINESS_RUNTIME_BOUNDARY_POLICY
BUSINESS_ANSWER_POLICY += ("\n业务概览默认按以下方式组织，用户自定义回答要求可以调整格式：\n"
                           + DEFAULT_ANSWER_INSTRUCTIONS)

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


def strip_incomplete_source_citation(text: str, *, truncated: bool, evidence_ids) -> str:
    """Remove only a truncated prose tail matching an actually supplied page ID.

    This display repair neither completes an ID nor establishes answer
    completeness. Raw provider records and all preceding text stay unchanged.
    Ambiguous or code-like tails are deliberately retained.
    """
    if not truncated:
        return text
    match = re.search(r"\[(ev_page_[0-9a-f]{0,24})[ \t\r\n]*\Z", text)
    if match is None or not any(
            isinstance(identifier, str) and re.fullmatch(r"ev_page_[0-9a-f]{24}", identifier)
            and identifier.startswith(match[1]) for identifier in evidence_ids):
        return text
    prefix = text[:match.start()]
    escaped = len(prefix) - len(prefix.rstrip("\\"))
    last_line = prefix.rsplit("\n", 1)[-1]
    # Paired backslashes do not escape a following code delimiter.
    code_prefix = re.sub(r"(?<!\\)(?:\\\\)+(?=`)", "", prefix)
    if (escaped % 2 or last_line.expandtabs(4).startswith("    ")
            or last_line.lstrip().startswith(">")
            or _open_code_marker(code_prefix) is not None):
        return text
    return prefix


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


def _restarted_truncated_table(draft: str, continuation: str) -> str | None:
    """Replace only an exact repeated table ending in a missing-column row.

    The continuation may start with one heading, which is kept verbatim. No
    earlier prose or differing table content is removed, and complete tables,
    code blocks, and ambiguous last-cell truncations are left to normal joining.
    """
    if draft.endswith(("\n", "\r")) or _open_code_marker(draft) is not None:
        return None
    lines = draft.splitlines(keepends=True)
    block_start = len(lines) - 1
    while block_start and lines[block_start - 1].strip():
        block_start -= 1
    block = [line.rstrip("\r\n") for line in lines[block_start:]]
    # Require a header, delimiter, at least one completed row, and a partial row.
    if len(block) < 4 or any(not re.match(r" {0,3}\|", line) for line in block):
        return None
    columns = len(_table_cells(block[0]))
    delimiter = _table_cells(block[1])
    if (columns < 2 or len(delimiter) != columns
            or not all(re.fullmatch(r":?-{3,}:?", cell) for cell in delimiter)
            or any(len(_table_cells(line)) != columns for line in block[2:-1])
            or not 1 <= len(_table_cells(block[-1])) < columns):
        return None
    continued_lines = continuation.splitlines(keepends=True)
    table_start = 0
    while table_start < len(continued_lines) and not continued_lines[table_start].strip():
        table_start += 1
    if table_start == len(continued_lines):
        return None
    first = continued_lines[table_start].rstrip("\r\n")
    if re.fullmatch(r" {0,3}(?:#{1,6}\s+\S.*|\*\*[^*\n]+\*\*)", first):
        table_start += 1
        if table_start == len(continued_lines) or continued_lines[table_start].strip():
            return None
        while table_start < len(continued_lines) and not continued_lines[table_start].strip():
            table_start += 1
    if len(continued_lines) < table_start + len(block):
        return None
    offset = sum(len(line) for line in lines[:block_start])
    repeated = "".join(continued_lines[table_start:])
    completed_row = continued_lines[table_start + len(block) - 1].rstrip("\r\n")
    if (not repeated.startswith(draft[offset:])
            or not re.match(r" {0,3}\|", completed_row)
            or len(completed_row) <= len(block[-1])
            or len(_table_cells(completed_row)) != columns):
        return None
    return draft[:offset] + continuation


def join_answer_continuation(draft: str, continuation: str, *, evidence_ids=()) -> str:
    """Join a truncated answer without rewriting its earlier business content.

    Only exact suffix/prefix or proven partial-table repetition is removed.
    A short common word or similar but different statement is not enough to discard text. Paragraph
    and list restarts are handled when they repeat the unfinished tail exactly;
    arbitrary paraphrases remain intact, since their equivalence is unknown.
    This assembles text, not a proof of semantic completeness or correctness.
    """
    if not draft or not continuation:
        return draft or continuation
    # Some continuations repeat the entire draft, including a long answer.
    if len(draft) >= 16 and continuation.startswith(draft):
        return continuation
    known_ids = tuple(evidence_ids)
    repaired = strip_incomplete_source_citation(draft, truncated=True, evidence_ids=known_ids)
    if repaired != draft:
        # A literal continuation can complete the exact supplied citation.
        # Otherwise discard only its broken prose tail before normal joining;
        # an unfinished marker must not swallow a new Markdown heading.
        completed = re.match(r"\[(ev_page_[0-9a-f]{24})\]", draft[len(repaired):] + continuation)
        if completed and completed[1] in known_ids:
            return draft + continuation
        draft = repaired
        if not draft:
            return continuation
    restarted_table = _restarted_truncated_table(draft, continuation)
    if restarted_table is not None:
        return restarted_table
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
