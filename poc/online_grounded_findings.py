"""Bound a progressive business finding to the verified source in one online turn.

The full answer below is available only for a straight-line deduction flow
ending in GOBACK. A static call is described conditionally on normal binding
and return. Neither a keyword match nor file order establishes execution order.
"""
from dataclasses import dataclass
import re

from call_bindings import parse_call, parse_signature
from online_source_recovery import named_scope, question_terms
from question_intent import business_sequence_requested
from response_language import localized_text
from statement_facts import CobolLexError, lex_cobol, program_identifiers
from statement_parser import parse_statements
from syntax_evidence import _bodies


_NAME = r"[A-Z][A-Z0-9-]{0,63}"
_OPERAND = rf"""(?:{_NAME}|[0-9]+(?:\.[0-9]+)?|'[A-Z0-9 _.-]{{1,64}}'|"[A-Z0-9 _.-]{{1,64}}")"""
_COMPARISON = re.compile(rf"({_NAME})\s*(<=|>=|<>|=|<|>)\s*({_OPERAND})", re.I)
_SUBTRACTION = re.compile(rf"SUBTRACT\s+({_NAME}|[0-9]+(?:\.[0-9]+)?)\s+FROM\s+({_NAME})(?:\s+END-SUBTRACT)?", re.I)
_MONEY = re.compile(r"扣|抵减|抵減|抵消|\b(?:deduct\w*|debit\w*|offset\w*|refunds?|credits?)\b", re.I)
_ADDITIONAL = re.compile(
    r"实例|實例|举例|舉例|例子|示例|假[設设]|表格|图表|圖表|流程[图圖]|代码|代碼|源码行号|"
    r"上线|上線|公司|所有程序|全[库庫]|全系[统統]|数据库|資料庫|配置|落[账帳]|入[账帳]|记[账帳]|"
    r"是否成功|为什么|為什麼|如何修改|错误|錯誤|异常|異常|失败|失敗|冲正|沖正|回滚|回滾|\d|"
    r"\b(?:why|example|table|chart|diagram|config\w*|database|ledger|production|all programs|"
    r"repository|hypothetical|assume|format|json|xml|sql)\b", re.I)
_NEGATE = {"<": ">=", "<=": ">", ">": "<=", ">=": "<", "=": "<>", "<>": "="}
_RELATION = {
    "<": ("小于", "is less than", "小於"), "<=": ("小于或等于", "is at most", "小於或等於"),
    ">": ("大于", "is greater than", "大於"), ">=": ("大于或等于", "is at least", "大於或等於"),
    "=": ("等于", "equals", "等於"), "<>": ("不等于", "differs from", "不等於"),
}

# Only ordinary sequencing language may be discarded when matching a complete
# answer to a question. Unrecognised subjects remain and prevent substitution.
_GRAMMAR = re.compile("|".join(sorted("""
请 請 说明 說明 解释 解釋 列出 告诉我 告訴我 依次 分别 分別 具体 具體 完整
保费 保費 收取 扣减 扣減 抵扣 扣费 扣費 扣款 扣除 处理 處理 执行 執行
顺序 順序 次序 先后 先後 最终 最終 最后 最後 结果 結果 应用 應用
什么时候 什麼時候 时候 時候 什么 什麼 哪些 哪种 哪種 哪步
按照 如何 怎么 怎麼 怎样 怎樣 满足 滿足 条件 條件 适用 適用 跳过
跳過 情况 情況 进入 進入 流程 当前 目前 剩余 剩餘 各项 各項 各步
金额 金額 阶段 階段 步骤 步驟 计算 計算 是否 分别 各自 前提
在 的 及 其 与 與 先 再 和 是 时 時 按 中 何 应 應 会 會 了
""".split(), key=len, reverse=True)))
_EN_GRAMMAR = frozenset("""
a an the which where when why for are is was and or to this that what how be by from
does do can should please tell me explain describe show order sequence apply applies
applicable applicability conditions condition skip skips skipped steps step final result
outcome amount amounts of on in first then next after before under process processing
charge charges charged offset offsets deduction deductions deducted deduct deducting
each stage stages remaining end up processing return returned as collect collecting premium premiums
""".split())
_SUBJECTS = {"准备金": "reserve", "準備金": "reserve", "退款": "refund", "退回款": "refund",
             "账户余额": "balance", "帳戶餘額": "balance", "余额": "balance", "餘額": "balance",
             "利息": "interest", "税款": "tax", "稅款": "tax", "罚款": "penalty", "罰款": "penalty",
             "押金": "deposit", "奖励": "reward", "獎勵": "reward", "手续费": "fee", "手續費": "fee"}
_SUBJECT_PATTERN = re.compile("|".join(sorted(_SUBJECTS, key=len, reverse=True)))


@dataclass(frozen=True)
class Step:
    kind: str
    guard: tuple
    operand: str
    receiver: str
    line: int


@dataclass(frozen=True)
class Program:
    name: str
    path: str
    version: tuple
    evidence: str
    parameters: tuple
    steps: tuple
    closed: bool


def _step(node):
    # An ELSE, sibling statement or nested condition changes both the skip
    # description and the resulting value. Leave these to model investigation.
    if node.kind != "IF" or len(node.children) != 1:
        return None
    child = node.children[0]
    if len(child.guards) != 1 or child.exception_children or child.normal_children:
        return None
    comparison = _COMPARISON.fullmatch(child.guards[0].condition.strip())
    if comparison is None:
        return None
    guard = comparison.groups()
    if child.kind == "SUBTRACT" and not child.effects_unknown and not node.else_children:
        op = _SUBTRACTION.fullmatch(child.text.strip())
        if op:
            return Step("subtract", guard, op[1].upper(), op[2].upper(), child.span.start_line)
    if (child.kind == "MOVE" and len(child.writes) == 1 and re.fullmatch(_NAME, child.writes[0])
            and re.fullmatch(_OPERAND, child.expression or "", re.I)):
        return Step("move", guard, child.expression, child.writes[0], child.span.start_line)
    if child.kind == "CALL" and not child.children:
        try:
            call = parse_call(child.text)
        except ValueError:
            return None
        if not call.dynamic and not call.parameters:
            return Step("call", guard, call.target, "", child.span.start_line)
        if not call.dynamic and len(call.parameters) == 1 and call.parameters[0].mode == "REFERENCE":
            return Step("call", guard, call.target, call.parameters[0].name, child.span.start_line)
    return None


def _program(page):
    citation, text = page["citation"], page.get("text")
    if (citation["start"] != 1 or page["truncated"] or page["last_line_truncated"]
            or not text or len(text) > 32000 or page["complete_line_count"] != len(text.splitlines())
            or len(text.splitlines()) != citation["end"]):
        return None
    bodies, _ = _bodies(text, 1)
    if len(bodies) != 1 or bodies[0][0] != "PROCEDURE":
        return None
    _, body, start, source_format = bodies[0]
    try:
        tokens = lex_cobol(text, source_format=source_format, max_tokens=6000)
    except CobolLexError:
        return None
    names = program_identifiers(text, source_format=source_format)
    if len(names) != 1 or not re.fullmatch(_NAME, names[0][0]):
        return None
    words = [t.value for t in tokens]
    if ">>" in text or {"COPY", "REPLACE", "EXEC", "DECLARATIVES", "REDEFINES", "RENAMES", "BASED"} & set(words):
        return None
    headers = [i for i in range(len(words) - 1) if words[i:i + 2] == ["PROCEDURE", "DIVISION"]]
    if len(headers) != 1:
        return None
    try:
        first = headers[0]
        parameters = parse_signature(" ".join(words[first:words.index(".", first) + 1]))
    except ValueError:
        return None
    parsed = parse_statements(body, start_line=start, source_format=source_format, max_tokens=6000)
    if not parsed.complete or not parsed.nodes:
        return None
    # A terminal return and fully modelled preceding instructions are required
    # before deriving any amount or asserting the absence of another write.
    steps = []
    nodes = list(parsed.nodes)
    returns = nodes[-1].kind == "GOBACK"
    if returns:
        nodes.pop()
    closed = returns
    for node in nodes:
        step = _step(node)
        if step is None or node.else_children:
            closed = False
        if node.kind == "GOBACK":
            break
        if step is not None:
            steps.append(step)
    if not steps or len(steps) > 8:
        return None
    return Program(names[0][0], citation["path"], (citation["commit"], citation["revision"]),
                   page["id"], parameters, tuple(steps), closed)


def _programs(pages):
    # Keep at most one representation per file. Different excerpts of the
    # same file may contain progressively more statements.
    programs = {}
    for page in pages:
        program = _program(page)
        if program:
            previous = programs.get(program.path)
            if previous is None or (program.closed, len(program.steps)) > (previous.closed, len(previous.steps)):
                programs[program.path] = program
    return list(programs.values())


def _comparison(guard, language, *, inverse=False):
    left, operator, right = guard
    relation = _RELATION[_NEGATE[operator] if inverse else operator]
    return f"`{left}` {localized_text(language, *relation)} `{right}`"


def _describe(program, step, language):
    condition = _comparison(step.guard, language)
    source = f"[{program.evidence}]"
    if step.kind == "move":
        return localized_text(language,
            f"若{condition}，将 `{step.receiver}` 设为 `{step.operand}`。",
            f"If {condition}, set `{step.receiver}` to `{step.operand}`.",
            f"若{condition}，將 `{step.receiver}` 設為 `{step.operand}`。") + " " + source
    if step.kind == "call" and not step.receiver:
        return localized_text(language, f"若{condition}，发起对 `{step.operand}` 的调用。",
            f"If {condition}, call `{step.operand}`.", f"若{condition}，發起對 `{step.operand}` 的呼叫。") + " " + source
    if step.kind == "call":
        return localized_text(language,
            f"若{condition}，将当前的 `{step.receiver}` 传给 `{step.operand}`。",
            f"If {condition}, the caller passes the current `{step.receiver}` to `{step.operand}`.",
            f"若{condition}，將目前的 `{step.receiver}` 傳給 `{step.operand}`。") + " " + source
    skip = _comparison(step.guard, language, inverse=True)
    return localized_text(language,
        f"若{condition}，从 `{step.receiver}` 中减去 `{step.operand}`。若{skip}，跳过这笔扣减。",
        f"If {condition}, deduct `{step.operand}` from `{step.receiver}`. If {skip}, skip this deduction.",
        f"若{condition}，從 `{step.receiver}` 中減去 `{step.operand}`。若{skip}，跳過這筆扣減。") + " " + source


def _names(step):
    return " ".join([*step.guard, step.operand, step.receiver]).casefold()


def _matches_scope(question, programs):
    paths, restricted, _ = named_scope(question)
    if restricted or set(paths) - {p.path for p in programs}:
        return False
    terms = question
    for path in paths:
        terms = terms.replace(path, " ")
    terms = _SUBJECT_PATTERN.sub(lambda match: " " + _SUBJECTS[match[0]] + " ", terms)
    terms = _GRAMMAR.sub(" ", terms)
    if re.search(r"[㐀-鿿]", terms):
        return False
    terms = {word.lower() for word in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]*", terms)} - _EN_GRAMMAR
    # The two files can be tied to a named caller or to the distinct business
    # subjects in the simple chain. Navigation relevance by itself is not proof.
    stage_names = [program.name.casefold() + " " + _names(step) for program in programs
                   for step in program.steps if step.kind == "subtract"]
    vocabulary = " ".join([*(p.name.casefold() for p in programs), *stage_names])
    if any(term not in vocabulary for term in terms):
        return False
    if any(p.path in paths or p.name.casefold() in terms for p in programs if p.steps[-1].kind == "call"):
        return True
    return len(terms) >= 2 and all(any(term in stage for term in terms) for stage in stage_names)


def grounded_preview(question, pages, language):
    """Return a cited, program-scoped interim finding, or None."""
    terms = [t.casefold() for t in question_terms(question, [])]
    terms.extend(_SUBJECTS[k] for k in _SUBJECTS if k in question)
    if "金额" in question or "金額" in question:
        terms.append("amount")
    paths, restricted, _ = named_scope(question)
    selected = []
    for program in _programs(pages):
        if restricted and program.path not in paths:
            continue
        for step in program.steps:
            vocabulary = program.name.casefold() + " " + _names(step)
            score = sum(term in vocabulary for term in set(terms))
            if program.path in paths or program.name.casefold() in terms:
                score += 1
            if score:
                selected.append((score, program, step))
    if not selected:
        return None
    # Scope the observation to its program. Reading order across files never
    # establishes the order in which those programs would execute.
    _, program, step = max(selected, key=lambda row: row[0])
    message = localized_text(language, f"`{program.name}` 的第 {step.line} 行受以下条件控制。",
                             f"Line {step.line} in `{program.name}` is conditional.",
                             f"`{program.name}` 的第 {step.line} 行受以下條件控制。")
    return {"answer": message + " " + _describe(program, step, language), "citations": [program.evidence]}


def grounded_sequence_answer(question, pages, language):
    """Close a supported deduction path in source order, never in retrieval order."""
    if (not _MONEY.search(question) or not business_sequence_requested(question)
            or _ADDITIONAL.search(question) or named_scope(question)[1]):
        return None
    # Overlapping windows must still identify exactly two sources under a
    # single revision. An unparsed or ambiguous third source blocks closure.
    versions = {(p["citation"]["commit"], p["citation"]["revision"]) for p in pages}
    originals = {(p["citation"]["path"], p["citation"]["hash"]) for p in pages}
    if len(versions) != 1 or len(originals) != 2:
        return None
    programs = _programs(pages)
    if len(programs) != 2 or any(not program.closed for program in programs):
        return None
    if len({p.name for p in programs}) != 2 or len({p.version for p in programs}) != 1:
        return None
    callers = [p for p in programs if p.steps[-1].kind == "call"]
    if len(callers) != 1:
        return None
    caller = callers[0]
    worker = next(p for p in programs if p is not caller)
    call = caller.steps[-1]
    if (not caller.steps[:-1] or any(s.kind != "subtract" or s.receiver != call.receiver for s in caller.steps[:-1])
            or call.operand != worker.name or len(worker.parameters) != 1
            or worker.parameters[0].mode != "REFERENCE"
            or any(s.kind != "subtract" or s.receiver != worker.parameters[0].name for s in worker.steps)):
        return None
    if not _matches_scope(question, programs):
        return None
    ids = [caller.evidence, worker.evidence]
    prefix = localized_text(language,
        f"按 `{caller.name}` 调用所示 `{worker.name}` 实现、参数布局相容且金额可准确表示、正常返回的路径，扣减依次如下。",
        f"With `{caller.name}` bound to the shown `{worker.name}`, compatible parameter layouts, exactly representable amounts and normal return, the deductions proceed in this order.",
        f"按 `{caller.name}` 呼叫所示 `{worker.name}` 實作、參數佈局相容且金額可準確表示、正常返回的路徑，扣減依次如下。")
    items = [_describe(caller, s, language) for s in caller.steps[:-1]]
    skip_call = _comparison(call.guard, language, inverse=True)
    items.append(_describe(caller, call, language) + " " + localized_text(language,
        f"这是前一步结束后独立检查的条件。若{skip_call}，跳过整个子程序，保留此时的金额。",
        f"This is a separate check after the preceding step. If {skip_call}, skip the whole subprogram and retain the current amount.",
        f"這是前一步結束後獨立檢查的條件。若{skip_call}，跳過整個子程式，保留此時的金額。") + f" [{caller.evidence}]")
    items.extend(_describe(worker, s, language) for s in worker.steps)
    initial, target = call.receiver, worker.parameters[0].name
    result = localized_text(language,
        f"最终 `{initial}` 等于进入流程时的金额，减去各步实际生效的扣减额。子程序按引用接收的 `{target}` 对应调用方的 `{initial}`。若所有扣减均跳过，则保留进入时的金额。",
        f"The returned `{initial}` is its entry amount less each deduction that actually applied. The subprogram receives `{target}` by reference as the caller’s `{initial}`. If every deduction is skipped, the entry amount remains.",
        f"最終 `{initial}` 等於進入流程時的金額，減去各步實際生效的扣減額。子程式按參照接收的 `{target}` 對應呼叫方的 `{initial}`。若所有扣減均跳過，則保留進入時的金額。")
    numbered = "\n".join(f"{n}. {item}" for n, item in enumerate(items, 1))
    return {"answer": f"{prefix} [{ids[0]}] [{ids[1]}]\n\n{numbered}\n\n{result} [{ids[0]}] [{ids[1]}]", "citations": ids}
