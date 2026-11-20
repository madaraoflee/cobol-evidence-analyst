"""Verify the conditional source path of a by-reference deduction across named dynamic calls."""

from dataclasses import dataclass, replace
import re

from call_bindings import parse_call, parse_signature
from local_deduction_review import DeductionEvidence, _verified_view, deduction_review_requested
from online_grounded_findings import (
    Program, Step, _ADDITIONAL, _COMPARISON, _COMPILER_RECORD, _NAME, _comparison, _describe,
    _matches_scope, _step,
)
from response_language import localized_text
from statement_facts import CobolLexError, lex_cobol, program_identifiers
from statement_parser import parse_statements
from syntax_evidence import _bodies


@dataclass(frozen=True)
class CallSite:
    target: str
    variable: str
    argument: str
    line: int
    guard: tuple


def _assigned_call(nodes):
    if len(nodes) != 2 or [n.kind for n in nodes] != ["MOVE", "CALL"]:
        return None
    move, invoke = nodes
    if any(n.children or n.exception_children or n.normal_children for n in nodes):
        return None
    literal = re.fullmatch(rf"""MOVE\s+(["'])({_NAME})\1\s+TO\s+({_NAME})""", move.text, re.I)
    if not literal or move.guards != invoke.guards:
        return None
    call = parse_call(invoke.text)
    if (not call.dynamic or call.target != literal[3].upper() or len(call.parameters) != 1
            or call.parameters[0].mode != "REFERENCE" or call.target == call.parameters[0].name):
        return None
    guard = ()
    if invoke.guards:
        comparison = _COMPARISON.fullmatch(invoke.guards[0].condition)
        if len(invoke.guards) != 1 or not invoke.guards[0].outcome or not comparison:
            return None
        guard = comparison.groups()
    return CallSite(literal[2].upper(), call.target, call.parameters[0].name, invoke.span.start_line, guard)


def _parse(view):
    text = view["text"]
    bodies, _ = _bodies(text, 1)
    if len(bodies) != 1 or bodies[0][0] != "PROCEDURE":
        return None
    _, body, start, fmt = bodies[0]
    if ">>" in text or any(_COMPILER_RECORD.match(line) for line in text.splitlines()):
        return None
    words = [t.value for t in lex_cobol(text, source_format=fmt, max_tokens=6000)]
    names = program_identifiers(text, source_format=fmt)
    if len(names) != 1 or not re.fullmatch(_NAME, names[0][0]):
        return None
    if (words.count("PROGRAM-ID") != 1 or words.count("PROCEDURE") != 1
            or words[words.index("PROGRAM-ID"):][:4] != ["PROGRAM-ID", ".", names[0][0], "."]
            or {"COPY", "REPLACE", "EXEC", "DECLARATIVES", "REDEFINES", "RENAMES", "BASED", "ENTRY",
                "EXTERNAL", "GLOBAL", "OCCURS", "POINTER", "SYNCHRONIZED"} & set(words)):
        return None
    header = words.index("PROCEDURE")
    parameters = parse_signature(" ".join(words[header:words.index(".", header) + 1]))
    parsed = parse_statements(body, start_line=start, source_format=fmt, max_tokens=6000)
    if not parsed.complete or not parsed.nodes or parsed.nodes[-1].text.upper().strip() != "GOBACK":
        return None
    nodes = list(parsed.nodes[:-1])
    # An entry can guard both the target assignment and the call. A forwarding
    # program can consist solely of the same two adjacent, unconditional statements.
    call = None
    if nodes and nodes[-1].kind == "IF" and not nodes[-1].else_children:
        call = _assigned_call(nodes[-1].children)
        if call:
            nodes.pop()
    elif len(nodes) >= 2:
        call = _assigned_call(nodes[-2:])
        if call:
            nodes = nodes[:-2]
    steps = tuple(_step(n) for n in nodes)
    if any(n.else_children for n in nodes) or any(not s or s.kind != "subtract" for s in steps):
        return None
    program = Program(names[0][0], view["citation"]["path"], (), view["id"], parameters, steps, True)
    return program, call


def _example(chain, programs, language):
    # Illustrate the equality boundary only for this precise pair of amount transformations.
    first, last = chain[0], chain[-1]
    if (len(first.steps) != 1 or len(last.steps) != 1
            or any(p.steps or programs[p.name][1].guard for p in chain[1:-1])):
        return ""
    debit, refund = first.steps[0], last.steps[0]
    if (debit.guard[1] != "=" or debit.guard[2][:1] not in {"'", '"'}
            or debit.guard[0] in {debit.operand, debit.receiver} or debit.operand == debit.receiver
            or programs[first.name][1].guard != (debit.receiver, ">", "0")
            or refund.operand == refund.receiver or refund.guard != (refund.operand, "<", refund.receiver)):
        return ""
    return localized_text(language,
        f"例如，首项类型符合条件，初始应收为 100，`{debit.operand}` 为 20，剩余应收就是 80。"
        f"若 `{refund.operand}` 为 30，最终应收为 50；若恰好为 80，两者相等，跳过这笔扣减，仍剩 80。",
        f"For example, with a matching type, an initial receivable of 100 and `{debit.operand}` of 20 leave 80. "
        f"If `{refund.operand}` is 30, the final receivable is 50. If it is 80, equality skips the deduction and leaves 80.",
        f"例如，首項類型符合條件，初始應收為 100，`{debit.operand}` 為 20，剩餘應收就是 80。"
        f"若 `{refund.operand}` 為 30，最終應收為 50；若恰好為 80，兩者相等，跳過這筆扣減，仍剩 80。")


def _path_answer(chain, programs, balance, language):
    lead = localized_text(language,
        "按调用变量绑定到下列程序、参数布局相容、金额可准确表示且正常返回的路径，扣减顺序如下。",
        "With the call variables bound to the programs below, compatible parameter layouts, exactly representable amounts and normal return, the deductions proceed in this order.",
        "按呼叫變數繫結至下列程式、參數佈局相容、金額可準確表示且正常返回的路徑，扣減順序如下。")
    steps = []
    for program in chain:
        steps.extend(_describe(program, step, language) for step in program.steps)
        call = programs[program.name][1]
        if not call:
            continue
        action = localized_text(language,
            f"`{program.name}` 将 `{call.target}` 赋给 `{call.variable}`，随后通过该变量调用，并按引用传递 `{call.argument}`。",
            f"`{program.name}` assigns `{call.target}` to `{call.variable}`, calls through it and passes `{call.argument}` by reference.",
            f"`{program.name}` 將 `{call.target}` 賦給 `{call.variable}`，隨後透過該變數呼叫，並按參照傳遞 `{call.argument}`。")
        if call.guard:
            condition = _comparison(call.guard, language)
            skip = _comparison(call.guard, language, inverse=True)
            action = localized_text(language, f"接着单独判断，若{condition}，{action}若{skip}，跳过此次调用，保留当前金额。",
                f"Next, check the remaining amount independently. If {condition}, {action} If {skip}, skip the call and retain the current amount.",
                f"接著單獨判斷，若{condition}，{action}若{skip}，跳過此次呼叫，保留目前金額。")
        steps.append(f"{action} [{program.evidence}]")
    result = localized_text(language,
        f"最终应收 `{balance}` 等于进入时的应收减去各步实际扣减的金额。全部跳过时，应收保持原额。",
        f"The final receivable `{balance}` is the entry amount less each deduction that actually applied. If all are skipped, the entry amount remains.",
        f"最終應收 `{balance}` 等於進入時的應收減去各步實際扣減的金額。全部跳過時，應收保持原額。")
    illustration = _example(chain, programs, language)
    citations = " ".join(f"[{p.evidence}]" for p in chain)
    numbered = "\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1))
    return f"{lead}\n\n{numbered}\n\n{result} {illustration} {citations}"


def prove_literal_call_path(question, pages, *, source_session, answer, answer_language="zh-CN"):
    """Return only the concrete input fields and callsites explained by this answer.

    The caller checks these names against the complete index and matches individual
    pending obligations. Runtime binding to the shown implementations is conditional.
    """
    if (not source_session or not deduction_review_requested(question) or _ADDITIONAL.search(question)
            or not re.search(r"正常返回|normal return|returns? normally", answer, re.I)
            or not re.search(r"相容|相同布局|相同佈局|兼容|compatible", answer, re.I)):
        return None
    refs = set(re.findall(r"\[(ev_page_[a-f0-9]{24})\]", answer))
    if not 2 <= len(refs) <= 8:
        return None
    captured = {row["relative_path"]: row["sha256"] for row in source_session.source_manifest()}
    programs = {}
    definitions = {}
    for page in pages:
        source = page.get("source_text", "")
        if page.get("start_line") == 1 and source and len(source) <= 32000:
            fmt = page.get("format_hint")
            for name, _, _ in program_identifiers(source, source_format=fmt if fmt in {"free", "fixed"} else "auto"):
                definitions.setdefault(name, set()).add(page["relative_path"])
        if page.get("evidence_id") not in refs:
            continue
        if page.get("source_sha256") != captured.get(page.get("relative_path")):
            return None
        try:
            checked = _verified_view(page, source_session)
            parsed = _parse(checked[0]) if checked and checked[1] else None
        except (ValueError, OSError, CobolLexError):
            return None
        if not parsed:
            return None
        program, call = parsed
        if program.name in programs and programs[program.name][0] != program:
            return None
        programs[program.name] = (program, call)
    if refs != {p.evidence for p, _ in programs.values()} or any(
            len(definitions.get(name, set())) != 1 for name in programs):
        return None
    incoming = {c.target for _, c in programs.values() if c}
    entries = [p for p, _ in programs.values() if p.name not in incoming]
    if len(entries) != 1:
        return None
    entry = entries[0]
    if entry.parameters or not entry.steps:
        return None
    balance = entry.steps[0].receiver
    program, call = programs[entry.name]
    chain, decisions, calls, inputs = [], [], [], set()
    vocabulary = []
    while True:
        if program in chain:
            return None
        chain.append(program)
        amount = balance
        if program != entry:
            if len(program.parameters) != 1 or program.parameters[0].mode != "REFERENCE":
                return None
            amount = program.parameters[0].name
        if any(s.receiver != amount for s in program.steps):
            return None
        if program.steps:
            vocabulary.append(replace(program, name=" ".join([program.name, *(c["variable"] for c in calls)])))
        decisions.extend((program, step) for step in program.steps)
        for step in program.steps:
            inputs.update(v for v in (*step.guard, step.operand, amount) if re.fullmatch(_NAME, v))
        if not call:
            if not program.steps or not calls:
                return None
            break
        if call.target not in programs or call.argument != amount:
            return None
        if program == entry and not call.guard:
            return None
        if call.guard:
            decisions.append((program, Step("call", call.guard, call.variable, amount, call.line)))
            inputs.update(v for v in call.guard if re.fullmatch(_NAME, v))
        callee, next_call = programs[call.target]
        calls.append({"path": program.path, "line": call.line, "variable": call.variable,
                      "target": call.target, "callee_path": callee.path})
        program, call = callee, next_call
    if len(chain) != len(programs) or not _matches_scope(
            question.replace("最终应收", "最终金额").replace("最終應收", "最終金額"), vocabulary):
        return None
    # The ordinary review also checks the ordering, inverse branches and net effect.
    assessment = DeductionEvidence([], None, True, tuple(decisions), explicit_result=True)
    if assessment.missing_aspects(answer.replace("于零", "于0").replace("於零", "於0")):
        return None
    return {"paths": [p.path for p in chain], "calls": calls, "inputs": sorted(inputs), "result": balance,
            "answer": _path_answer(chain, programs, balance, answer_language)}
