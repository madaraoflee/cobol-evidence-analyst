"""Local, version-bound review of explicit claims at the default answer outlet.

The ledger records every answer segment, including prose outside the bounded
checker language. It never asks a model to judge itself. Supported means that
an explicit statement, condition or write request matches supplied source
syntax; it does not imply execution, successful I/O or a final business value.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import re
from typing import Any

from claim_support import check_statement_support, statement_tokens, supplied_statement_facts
from response_language import localized_text, resolve_answer_language
from source_availability import supplied_source_availability


LEDGER_VERSION = "answer-claim-ledger-v9"
_REFERENCE = re.compile(r"\[((?:ev[_:-]|fw:)[^\]\r\n]{1,160})\]")
_NAME = r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*(?:\s+(?:OF|IN)\s+[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*)*"
_ATOM = rf"(?:{_NAME}|[+-]?\d+(?:\.\d+)?|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|[()])"
_EXPRESSION = rf"{_ATOM}(?:\s*(?:\*\*|[+*/×÷-]|\b(?:multiplied\s+by|times|divided\s+by)\b|乘以|乘|除以|加上|减去)\s*{_ATOM})*"
_FORMULA = re.compile(rf"(?P<target>{_NAME})\s*(?:(?:最终|最終|最后|最後|总是|總是|一定|always|finally)\s*)?(?:ROUNDED\s*)?(?:=(?!=)|等于|等於|(?:为|為|是)\s*|\bis\s+)\s*", re.I)
_ENGLISH_DESCRIPTION = re.compile(
    r"(?:not\s+)?(?:(?:an?|the)\s+(?!(?:OF|IN|times)\s|(?:multiplied|divided)\s+by\b)[A-Za-z]|"
    r"(?:declared|defined|described|documented|stored|used|treated|represented|interpreted|"
    r"known|marked|classified|identified|calculated|computed)\s+(?:as|with|in|by|for|to|from)\b|"
    r"(?:calculated|computed|determined|derived|initialized|updated)"
    r"(?=\s*(?:[.!?,;:]|$)|\s+(?:when|if|under|after|before)\b))", re.I)
_ANSWER_TOKEN = re.compile(rf"\s*(?:'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|\d+(?:\.\d+)?|(?:multiplied\s+by|divided\s+by|times)\b|乘以|乘|除以|加上|减去|{_NAME}|\*\*|[()+*/×÷-])", re.I)
_CONDITION_TOKEN = re.compile(rf"\s*(?:'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|\d+(?:\.\d+)?|{_NAME}|>=|<=|<>|\*\*|[()=<>+*/-])", re.I)
_MULTIPLE = re.compile(rf"(?P<target>{_NAME})\s*(?:为|為|是|按|等于|等於)\s*(?P<input>{_NAME})\s*的?\s*(?P<number>\d+(?:\.\d+)?|两|兩|二|三|四|五|六|七|八|九|十)\s*倍", re.I)
_MOVE = re.compile(rf"\bMOVE\s+(?P<expression>{_ATOM})\s+TO\s+(?P<target>{_NAME})", re.I)
_CONDITION = re.compile(rf"(?P<left>{_NAME})\s*(?P<operator>>=|<=|<>|>|<|(?:NOT\s+)?=)\s*(?P<right>{_ATOM})", re.I)
_WRITE = re.compile(rf"\b(?P<effect>WRITE|REWRITE|DELETE)\s+(?P<target>{_NAME})", re.I)
_NATURAL_EFFECT = re.compile(rf"(?:写入|寫入|改写|改寫|重写|重寫|删除|刪除)\s*`?(?P<target>{_NAME})", re.I)
_NARRATIVE_MULTIPLIER = re.compile(r"(?:乘以|乘|multiplied\s+by|times)\s*(?P<after>\d+(?:\.\d+)?)|(?P<before>\d+(?:\.\d+)?)\s*倍", re.I)
_DERIVATION_CONTEXT = re.compile(r"(?:按|以|根据|根據)[^。\n]{0,48}(?:初值|初始值|给定输入|給定輸入)"
                                 r"[^。\n]{0,240}(?:推导|推導|计算|計算)[：:]")
_DECLARATION_LIST_PREFIX = re.compile(
    rf"(?:默认值|默認值|初始值|初值)\s*(?:包括|为|為|是|如下)?\s*[：:]?\s*"
    rf"(?:{_NAME}\s*=\s*(?:[+-]?\d+(?:\.\d+)?|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")\s*[、,，]\s*)*$", re.I)
_LITERAL_LIST_PREFIX = re.compile(
    rf"\s*(?:{_NAME}\s*=\s*(?:[+-]?\d+(?:\.\d+)?|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")\s*[、,，]\s*)*$", re.I)
_EXPLICIT_ASSIGNMENT = re.compile(
    r"\b(?:MOVE|COMPUTE|SET|ASSIGN)\b|(?:执行|執行|显式|顯式)?(?:赋值|賦值|写入|寫入|设置|設置|设为|設為|置为|置為)", re.I)
_NUMBERS = {"两": "2", "兩": "2", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6",
            "七": "7", "八": "8", "九": "9", "十": "10"}


@dataclass
class ClaimEntry:
    claim_id: str
    claim_type: str
    text: str
    target_entity: str | None = None
    expression: list[str] | None = None
    condition: list[str] | None = None
    effect: str | None = None
    evidence_ids: list[str] = field(default_factory=list)
    reviewed_evidence_ids: list[str] = field(default_factory=list)
    source_versions: list[dict[str, Any]] = field(default_factory=list)
    support_status: str = "unsupported"
    reason_code: str = "PROSE_SEMANTICS_NOT_SUPPORTED"
    checker_version: str | None = None
    support_scope: str | None = None
    occurrence_ids: list[str] = field(default_factory=list)
    invalid_evidence_ids: list[str] = field(default_factory=list)
    revision: str | None = None
    revised_claims: list[dict[str, Any]] = field(default_factory=list)
    verification_facts: list[dict[str, Any]] = field(default_factory=list)
    unresolved_requirements: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ClaimLedger:
    entries: list[ClaimEntry] = field(default_factory=list)
    answer_sha256: str = ""
    original_answer_sha256: str = ""
    source_fact_count: int = 0
    compilation_context_fingerprint: str | None = None

    def to_dict(self):
        counts = {name: sum(entry.support_status == name for entry in self.entries)
                  for name in ("supported", "contradicted", "unsupported")}
        return {"ledger_version": LEDGER_VERSION, "scope": "bounded_source_statement_syntax",
                "coverage": "extracted_structures_only", "prose_semantics_verified": False,
                "entries": [asdict(entry) for entry in self.entries], "counts": counts,
                "source_fact_count": self.source_fact_count,
                "compilation_context_fingerprint": self.compilation_context_fingerprint,
                "revised_claim_count": sum(entry.revision is not None for entry in self.entries),
                "answer_sha256": self.answer_sha256,
                "original_answer_sha256": self.original_answer_sha256,
                "all_claims_supported": bool(self.entries) and counts["supported"] == len(self.entries)}


def _tokens(expression):
    parts = re.split(r"('(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")", expression)
    for index in range(0, len(parts), 2):
        for phrase, operator in (("multiplied by", "*"), ("divided by", "/"), ("times", "*"),
                                 ("乘以", "*"), ("乘", "*"), ("除以", "/"), ("加上", "+"),
                                 ("减去", "-"), ("×", "*"), ("÷", "/")):
            pattern = re.escape(phrase)
            if phrase.isascii() and phrase[0].isalpha():
                pattern = r"(?<![A-Za-z0-9-])" + pattern + r"(?![A-Za-z0-9-])"
            parts[index] = re.sub(pattern, f" {operator} ", parts[index], flags=re.I)
    return statement_tokens("".join(parts).strip())


def _read_expression(text, start):
    """Read an answer expression with the shared grammar, including nesting."""
    from statement_parser import _BoundaryError, _Expression, _Source, _lex
    end = start
    for _ in range(80):
        if re.match(r"\s*END-COMPUTE(?![A-Za-z0-9-])", text[end:], re.I):
            break
        token = _ANSWER_TOKEN.match(text, end)
        if token is None or token.end() == end:
            break
        end = token.end()
    tokens = _tokens(text[start:end])
    if not tokens:
        return None, end, False
    try:
        lexical = _lex(_Source(" ".join(tokens), 1), "free", 512)
        parser = _Expression(lexical)
        if not parser.arithmetic(text_operand=True) or parser.position == 0:
            return None, end, False
        return tuple(token.value for token in lexical[:parser.position]), end, parser.position == len(lexical)
    except (_BoundaryError, ValueError):
        return None, end, False


def _source_expression_complete(text, start, end, parsed_all):
    """A parsed prefix of a mathematical derivation is not a source formula."""
    return bool(parsed_all and not re.search(r"[×÷%％]", text[start:end])
                and not re.match(r"\s*[=＝%％≈≃≅≠<>≤≥+*/×÷-]", text[end:]))


def _read_condition(text, start):
    """Consume a complete boolean prefix using the shared condition grammar."""
    from statement_parser import _BoundaryError, _Expression, _Source, _lex
    end = start
    for _ in range(160):
        token = _CONDITION_TOKEN.match(text, end)
        if token is None or token.end() == end:
            break
        end = token.end()
    try:
        lexical = _lex(_Source(text[start:end], 1), "free", 512)
        parser = _Expression(lexical)
        if not lexical or not parser.boolean() or parser.position == 0:
            return None, end
        return tuple(token.value for token in lexical[:parser.position]), start + lexical[parser.position - 1].end
    except (_BoundaryError, ValueError):
        return None, end


def _clause_prefix(text, start, limit=48):
    return re.split(r"[，,;；。!?\n（(）)]|\.(?!\d)|(?<!\d)\.", text[max(0, start - limit):start])[-1]


def _declaration_list_context(text, start):
    prefix = text[max(0, start - 1024):start]
    if _DECLARATION_LIST_PREFIX.search(prefix):
        return True
    # A declaration introduction may contain explanatory clauses before its
    # colon. Only continuous literal equalities inherit that role afterwards.
    head, separator, items = re.split(r"([：:])", prefix)[-3:] if re.search(r"[：:]", prefix) else ("", "", "")
    if not separator or not _LITERAL_LIST_PREFIX.fullmatch(items):
        return False
    head = re.split(r"[;；。!?\n]|\.(?!\d)|(?<!\d)\.", head)[-1]
    declaration = re.search(r"初值|初始值|初始化|默认值|默認值|声明|聲明", head)
    return bool(declaration and not _EXPLICIT_ASSIGNMENT.search(head[declaration.end():])
                and not re.search(r"最终|最終|最后|最後|总是|總是|一定|运行时|執行時", head[declaration.end():]))


def _assertions(text, *, table_header=False, derived_context=False):
    """Translate a bounded answer notation, not source syntax, into candidates."""
    text = _REFERENCE.sub("", text).replace("`", "")
    # Strip paired Markdown strong delimiters, preserving the COBOL power
    # operator and literal contents. A spaced ** operator is not a delimiter.
    parts = re.split(r"('(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")", text)
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(r"(?<!\w)\*\*(?=\S)(.+?)(?<=\S)\*\*(?!\w)", r"\1", parts[index])
    text = "".join(parts)
    local_derivation = _DERIVATION_CONTEXT.search(text)
    assertions, occupied = [], []
    conditions, explicit_conditions = [], []
    # A compound IF is one assertion. Splitting it into its two comparisons
    # would falsely contradict the complete source condition and can replace a
    # correct formula sharing the same sentence with an unrelated repair.
    for marker in re.finditer(r"\bIF(?=\s|\()\s*", text, re.I):
        tokens, end = _read_condition(text, marker.end())
        if tokens and any(token in {"=", "<>", "<", ">", "<=", ">="} for token in tokens):
            conditions.append((marker.start(), end, tokens))
            explicit_conditions.append((marker.start(), end))
    for match in _CONDITION.finditer(text):
        if any(first <= match.start() < last for first, last in explicit_conditions):
            continue
        tokens, end = _read_condition(text, match.start())
        prefix, suffix = _clause_prefix(text, match.start(), 32), text[end:end + 48]
        assignment_context = _EXPLICIT_ASSIGNMENT.search(prefix)
        conditional_context = (re.search(r"(?:若|如果|当|當|只有|仅当|僅當|只要|只在|只有在|仅在|僅在|检查|檢查|判断|判斷)\s*$|\b(?:if|when|unless|checks?|checking)\s*$", prefix, re.I)
            or re.match(r"\s*(?:时|時)(?:才|会|會|则|則|调用|調用|执行|執行|置|设|設|赋值|賦值|写入|寫入|进入|進入|返回|[，,；;。.]|$)", suffix)
            or re.match(r"\s*的?\s*(?:(?:源码|源碼|控制)\s*)?(?:条件|條件)(?:下|成立|不成立)?", suffix)
            or re.match(r"\s*(?:source\s+)?condition\b", suffix, re.I)
            or (table_header and match["right"].startswith(("'", '"'))))
        if tokens and conditional_context and not assignment_context:
            # Explicit natural-language condition context chooses the role;
            # the full expression still has to match a real supplied IF fact.
            conditions.append((match.start(), end, tokens))
            explicit_conditions.append((match.start(), end))
    for match in _MULTIPLE.finditer(text):
        assertions.append({"predicate": "assignment_statement", "target": match["target"].upper(),
                           "expression": [match["input"].upper(), "*", _NUMBERS.get(match["number"], match["number"])],
                           "rounded": "ROUNDED" in match[0].upper()})
        occupied.append(match.span())
    for pattern in (_MOVE, _FORMULA):
        for match in pattern.finditer(text):
            if any(first <= match.start() < last for first, last in occupied):
                continue
            if any(first <= match.start() < last for first, last in explicit_conditions):
                continue
            # IF equality is a condition, not an assignment.
            prefix = text[max(0, match.start() - 12):match.start()]
            if re.search(r"\bIF\s*$|(?:若|如果|当|當)\s*$", prefix, re.I):
                continue
            if re.search(r"假设|假設|例如|示例|假如|\b(?:suppose|assuming)\b", text[max(0, match.start() - 24):match.start()], re.I):
                continue
            if pattern is _FORMULA and re.search(r"(?:给定输入|給定輸入)\s*$", prefix):
                occupied.append(match.span())
                continue  # A supplied example input is not a source assignment.
            if (pattern is _FORMULA and re.search(r"\bis\s*$", match[0], re.I)
                    and _ENGLISH_DESCRIPTION.match(text, match.end())):
                # English descriptions are not assignment notation. Reading
                # only their first token invents formulas such as THIS = AN
                # or FIELD = DECLARED and can erase a valid cited sentence.
                continue
            tokens, expression_end, parsed_all = ((_tokens(match["expression"]), match.end(), True)
                if pattern is _MOVE else _read_expression(text, match.end()))
            if tokens:
                if pattern is _FORMULA and not _source_expression_complete(text, match.end(), expression_end, parsed_all):
                    # Keep the whole mathematical clause outside the source
                    # assertion checker, including chained equalities. Merely
                    # parsing its prefix cannot establish a contradiction.
                    clause_end = expression_end + re.match(r"[^，,;；。!?\n]*", text[expression_end:]).end()
                    assertions.append({"predicate": "mathematical_expression", "target": match["target"].upper(),
                                       "expression": list(tokens)})
                    occupied.append((match.start(), clause_end))
                    continue
                clause_prefix = _clause_prefix(text, match.start())
                explicit_assignment = _EXPLICIT_ASSIGNMENT.search(clause_prefix + match[0])
                if (pattern is _FORMULA and not explicit_assignment
                        and re.search(r"(?:为|為|是|\bis\s+)\s*$", match[0], re.I)
                        and len(tokens) == 1 and tokens[0].isdigit()
                        and re.match(r"\s*(?:位(?:定长|定長)?(?:字符|字元|数字|數字)|(?:characters?|digits?)\b)", text[expression_end:], re.I)):
                    # A field's width is descriptive syntax, not a runtime
                    # assignment. The bounded statement checker does not prove it.
                    occupied.append((match.start(), expression_end))
                    continue
                declaration_context = (pattern is _FORMULA and not explicit_assignment and (re.search(
                    r"(?:默认值|默認值|初值|初始(?:值)?|初始化|声明|聲明|工作存储|工作存儲)|\b(?:initial(?:ized)?|declar(?:ed|ation)|working-storage|VALUE)\b",
                    clause_prefix, re.I) or _declaration_list_context(text, match.start())) and not re.search(
                    r"最终|最終|最后|最後|总是|總是|一定|运行时|執行時|(?<![\w-])(?:always|finally|final|runtime)(?![\w-])",
                    clause_prefix + match[0], re.I))
                derived_result = (pattern is _FORMULA and not explicit_assignment
                    and (derived_context or local_derivation and local_derivation.end() <= match.start())
                    and not declaration_context
                    and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", "".join(tokens)) is not None
                    and re.search(r"最终|最終|最后|最後|(?<![\w-])(?:finally|final)(?![\w-])", clause_prefix + match[0], re.I))
                state_value = (pattern is _FORMULA and not explicit_assignment and not declaration_context
                    and not derived_result
                    and (re.fullmatch(r"[+-]?\d+(?:\.\d+)?", "".join(tokens)) is not None
                         or len(tokens) == 1 and tokens[0].startswith(("'", '"'))))
                assertion = {"predicate": "declaration_value" if declaration_context else
                             "derived_numeric_result" if derived_result else
                             "business_state_value" if state_value else "assignment_statement",
                             "target": match["target"].upper(), "expression": list(tokens)}
                if not declaration_context and not derived_result and not state_value:
                    assertion["rounded"] = "ROUNDED" in match[0].upper()
                assertions.append(assertion)
                occupied.append((match.start(), expression_end))
    for match in _CONDITION.finditer(text):
        if any(first <= match.start() < last for first, last in explicit_conditions):
            continue
        tokens, end = _read_condition(text, match.start())
        if tokens and not any(first <= match.start() < last for first, last, _ in conditions):
            conditions.append((match.start(), end, tokens))
    for start, _, tokens in conditions:
        if not any(first <= start < last for first, last in occupied):
            assertions.append({"predicate": "condition_statement", "condition": list(tokens), "outcome": True})
    for match in _WRITE.finditer(text):
        assertions.append({"predicate": "write_statement", "target": match["target"].upper(), "effect": match["effect"].upper()})
    for match in _NATURAL_EFFECT.finditer(text):
        effect = "DELETE" if match[0].startswith(("删除", "刪除")) else "REWRITE" if match[0].startswith(("改写", "改寫", "重写", "重寫")) else "WRITE"
        assertions.append({"predicate": "write_statement", "target": match["target"].upper(), "effect": effect})
    for match in _NARRATIVE_MULTIPLIER.finditer(text):
        if any(first <= match.start() < last for first, last in occupied):
            continue
        if re.search(r"不是|并非|並非|不等于|不等於|\bnot\b", text[max(0, match.start() - 12):match.start()], re.I):
            continue
        assertions.append({"predicate": "arithmetic_constant", "operator": "*",
                           "constant": match["after"] or match["before"]})
    unique = []
    for assertion in assertions:
        if assertion not in unique:
            unique.append(assertion)
    return unique


def _segments(answer):
    """Review table cells separately so a repair cannot erase their columns."""
    for block in re.split(r"(```[^\n]*\n.*?(?:```|\Z)|~~~[^\n]*\n.*?(?:~~~|\Z))", answer, flags=re.S):
        if block.startswith(("```", "~~~")):
            yield block, [], False, False
            continue
        for paragraph in re.split(r"(\n[ \t]*\n)", block):
            inherited = list(dict.fromkeys(_REFERENCE.findall(paragraph)))
            # A numeric conclusion following explicit inputs and a derivation
            # is not a claim that source contains a literal MOVE of that value.
            # Other formulas, guards and input declarations remain checkable.
            derivation = _DERIVATION_CONTEXT.search(paragraph)
            lines = paragraph.splitlines(keepends=True) or [paragraph]
            position = 0
            for index, line in enumerate(lines):
                line_start = position
                position += len(line)
                if line.lstrip().startswith("|"):
                    header = (index + 1 < len(lines) and "|" in lines[index + 1]
                              and re.fullmatch(r"[\s|:-]+", lines[index + 1]) is not None)
                    protected = [match.span() for match in re.finditer(r"(`+)[^`\r\n]*?\1", line)]
                    start = 0
                    for separator in re.finditer(r"(?<!\\)\|", line):
                        if any(first <= separator.start() < last for first, last in protected):
                            continue
                        yield line[start:separator.start()], inherited, header, False
                        yield "|", [], False, False
                        start = separator.end()
                    yield line[start:], inherited, header, False
                    continue
                protected = [match.span() for match in re.finditer(
                    r"(`+)[^`\r\n]*?\1|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|"
                    r"[（(][^()（）\r\n]{1,512}:\d+(?:[–-]\d+)?[）)]", line)]
                protected.extend(match.span() for match in _REFERENCE.finditer(line))
                start = 0
                for match in re.finditer(r"[。！？](?:[ \t]*\[(?:ev[_:-]|fw:)[^\]\r\n]+\])*|(?:[!?]|\.(?!\d)|(?<!\d)\.)(?:[ \t]*\[(?:ev[_:-]|fw:)[^\]\r\n]+\])*", line):
                    if any(first <= match.start() < last for first, last in protected):
                        continue
                    if (line[match.start()] == "." and 0 < match.start() < len(line) - 1
                            and re.match(r"[A-Za-z0-9_-]", line[match.start() - 1])
                            and re.match(r"[A-Za-z0-9_-]", line[match.start() + 1])):
                        continue  # A path/member/version separator is not sentence punctuation.
                    yield line[start:match.end()], inherited, False, bool(derivation and derivation.end() <= line_start + start)
                    start = match.end()
                if start < len(line):
                    yield line[start:], inherited, False, bool(derivation and derivation.end() <= line_start + start)


def _related(assertion, facts):
    predicate, target = assertion["predicate"], assertion.get("target")
    result = [fact for fact in facts if fact["predicate"] == predicate and (target is None or fact.get("target") == target)]
    if predicate == "condition_statement":
        # Only compare conditions about the same first operand; another IF in
        # the excerpt is not a correction candidate for the current condition.
        operand = assertion["condition"][:1]
        result = [fact for fact in result if fact.get("condition", [])[:1] == operand]
    return result


def _same_program_version(fact, matched):
    """An excerpt without a program header cannot narrow its physical file."""
    return any(fact["relative_path"] == source["relative_path"]
        and fact["source_sha256"] == source["source_sha256"]
        and (not fact.get("program_name") or not source.get("program_name")
             or fact["program_name"] == source["program_name"]) for source in matched)


def _render_fact(fact, answer_language="zh-CN"):
    def local(zh_cn, en, zh_hk=None):
        return localized_text(answer_language, zh_cn, en, zh_hk)

    if fact["predicate"] == "noop_procedure":
        body = ' / '.join(fact['body_kinds'])
        text = local(
            f"`{fact['target']}` 段落只有 {body}，未实现本段计算或写入逻辑",
            f"The `{fact['target']}` paragraph contains only {body}; its calculation or write logic is not implemented",
            f"`{fact['target']}` 段落只有 {body}，未實現本段計算或寫入邏輯")
    elif fact["predicate"] == "declaration_value":
        text = local("声明的初始值：", "Declared initial value: ", "聲明的初始值：") + f"`{fact['target']} = {' '.join(fact['expression'])}`"
    elif fact["predicate"] == "assignment_statement":
        rounded = " ROUNDED" if fact["rounded"] else ""
        text = f"`{fact['target']}{rounded} = {' '.join(fact['expression'])}`"
    elif fact["predicate"] == "condition_statement":
        text = f"`IF {' '.join(fact['condition'])}`"
    else:
        text = f"`{fact['effect']} {fact['target']}`" + local(
            "（显式写入请求，成功结果需运行状态确认）",
            " (an explicit write request; success requires runtime confirmation)",
            "（明確的寫入請求，成功結果需執行狀態確認）")
    guards = []
    for guard in fact.get("guards", []):
        condition = guard["condition"]
        if guard.get("outcome") in {"false", False}:
            condition = local("条件不成立：", "condition is false: ", "條件不成立：") + condition
        guards.append(condition)
    joined_guards = local("；", "; ").join(guards)
    prefix = local(f"在源码控制条件 `{joined_guards}` 下，",
                   f"Under source control condition `{joined_guards}`, ",
                   f"在源碼控制條件 `{joined_guards}` 下，") if guards else ""
    if fact.get("normal_result_condition") == "no_size_error":
        prefix += local("无 SIZE ERROR 时的算式为 ", "when there is no SIZE ERROR, the formula is ",
                        "無 SIZE ERROR 時的算式為 ")
    if fact.get("_final_path_proof"):
        prefix = local("若从入口按源码顺序正常执行至退出，且数值运算与接收字段转换没有错误或精度损失，退出前最后一次赋值为：",
            "If execution proceeds from entry to normal exit without arithmetic or storage-conversion errors or precision loss, the final assignment is: ",
            "若從入口按源碼順序正常執行至退出，且數值運算與接收欄位轉換沒有錯誤或精度損失，退出前最後一次賦值為：") + prefix
    if fact.get("compilation_status") == "candidate":
        prefix = local("编译条件尚未确认，以下仅为候选源码语句：",
                       "Compilation conditions are unconfirmed; this is a candidate source statement: ",
                       "編譯條件尚未確認，以下僅為候選源碼語句：") + prefix
    location = f"{fact['relative_path']}:{fact['start_line']}"
    return f"{prefix}{text}" + local(f"（{location}）", f" ({location})") + f"[{fact['evidence_id']}]"


def _revised_claims(facts, pages):
    return [{"assertion": {key: fact[key] for key in
        ("predicate", "target", "expression", "rounded", "condition", "outcome", "effect") if key in fact},
        "source_guards": fact.get("guards", []),
        "compilation_status": fact.get("compilation_status", "effective_source"),
        "compilation_guards": fact.get("compilation_guards", []), "evidence_id": fact["evidence_id"],
        "source_sha256": fact["source_sha256"], "occurrence_id": fact["occurrence_id"],
        **check_statement_support(fact, fact, pages[fact["evidence_id"]])} for fact in facts]



def _straight_line_final_assignment(target, matched, pages, facts, availability, source_format, compilation_context):
    """Prove one last assignment on a deliberately small complete source path.

    This proves source ordering to a normal exit, never real invocation or
    arithmetic/storage precision. Unknown syntax, aliasing, branches and calls
    keep the existing unsupported boundary rather than guessing reachability.
    """
    from structural_index import SourceDocument, normalize_cobol_lines, parse_document
    from statement_parser import parse_statements

    identities = {(fact["relative_path"], fact["source_sha256"]) for fact in matched}
    if len(identities) != 1:
        return None
    path, digest = next(iter(identities))
    coverage = availability.get(path, {})
    if coverage.get("complete_source_sha256") != digest:
        return None
    physical = [page for page in pages.values() if page.get("relative_path") == path
                and page.get("source_sha256") == digest and not page.get("include_chain")
                and not page.get("span_truncated")]
    lines = {}
    for page in physical:
        for line, text in enumerate(page["source_text"].split("\n"), page["start_line"]):
            if line in lines and lines[line] != text:
                return None
            lines[line] = text
    total = coverage["line_count"]
    if total > 2000 or set(range(1, total + 1)) - lines.keys():
        return None
    source = "\n".join(lines[line] for line in range(1, total + 1))
    if len(source) > 64000:
        return None
    normalized, hint = normalize_cobol_lines(source, source_format)
    document = SourceDocument(path, digest, "utf-8", False, hint, "COBOL", tuple(source.splitlines()), normalized)
    parsed = parse_document(document, compilation_context=compilation_context)
    units = parsed.code_units
    programs = [unit for unit in units if unit.unit_type == "Program"]
    signatures = [unit for unit in units if unit.unit_type == "ProcedureSignature"]
    if (len(programs) != 1 or len(signatures) != 1
            or signatures[0].normalized_text.upper() != "PROCEDURE DIVISION."
            or any(unit.parse_status != "complete" for unit in units)):
        return None
    signature = signatures[0]
    paragraphs = [unit for unit in units if unit.unit_type == "Paragraph"]
    if len(paragraphs) > 1 or any(unit.unit_type == "Section" and unit.start_line > signature.end_line for unit in units):
        return None
    # Only independent elementary numeric storage is modeled. Group fields,
    # redefinitions, pointers, linkage, COPY and directives require richer rules.
    declarations = [unit for unit in units if unit.unit_type == "DataItem"]
    declaration = re.compile(r"01\s+([A-Z][A-Z0-9-]*)\s+PIC(?:TURE)?\s+S?9[9V()0-9]*"
                             r"(?:\s+VALUE\s+[+-]?\d+(?:\.\d+)?)?\s*\.", re.I)
    if not declarations or any(not declaration.fullmatch(unit.normalized_text) for unit in declarations):
        return None
    declared = [declaration.fullmatch(unit.normalized_text)[1].upper() for unit in declarations]
    if len(declared) != len(set(declared)) or target not in declared:
        return None
    normalized_text = "\n".join(line.text for line in normalized)
    if re.search(r">>|\b(?:COPY|REPLACE|REDEFINES|RENAMES|LINKAGE|EXTERNAL|GLOBAL|BASED|POINTER|OCCURS|ENTRY|SPECIAL-NAMES)\b", normalized_text, re.I):
        return None
    body_lines = [line for line in normalized if line.start_line > signature.end_line]
    if paragraphs:
        paragraph = paragraphs[0]
        if not body_lines or body_lines[0].start_line != paragraph.start_line:
            return None
        body_lines = [line for line in body_lines if line.start_line > paragraph.end_line]
    if not body_lines:
        return None
    first = body_lines[0].start_line
    body = "\n".join(lines.get(line, "") for line in range(first, total + 1))
    body_result = parse_statements(body, start_line=first, source_format=hint if hint in {"free", "fixed"} else "free")
    nodes = body_result.nodes
    if (not body_result.complete or len(nodes) < 2 or nodes[-1].kind not in {"GOBACK", "STOP_RUN"}
            or any(node.kind not in {"MOVE", "COMPUTE", "CONTINUE"} or node.effects_unknown
                   or node.guards or node.children or node.else_children or node.exception_children
                   or node.normal_children or node.size_error_handler_present
                   for node in nodes[:-1])):
        return None
    writes = [(index, node) for index, node in enumerate(nodes[:-1]) if target in node.writes]
    if not writes:
        return None
    index, last_write = writes[-1]
    if (len(last_write.writes) != 1 or target in last_write.reads
            or any(name not in declared for node in nodes[:-1] for name in (*node.reads, *node.writes))
            or any(set(last_write.reads) & set(node.writes) for node in nodes[index + 1:-1])):
        return None
    expression = statement_tokens(dict(last_write.expressions).get(target, ""))
    final_facts = [fact for fact in facts if (fact["relative_path"], fact["source_sha256"]) == (path, digest)
                   and fact.get("target") == target and fact.get("predicate") == "assignment_statement"
                   and fact["start_line"] == last_write.span.start_line
                   and tuple(fact.get("expression", [])) == expression
                   and fact.get("rounded") == last_write.rounded
                   and not fact.get("guards") and fact.get("compilation_status") != "candidate"]
    if not final_facts:
        return None
    fact = final_facts[0]
    return {"fact": fact, "support_status": "supported",
            "reason_code": "COMPLETE_STRAIGHT_LINE_LAST_ASSIGNMENT",
            "checker_version": "straight-line-final-assignment-v1",
            "support_scope": "last_assignment_on_normal_straight_line_path",
            "preconditions": ["program_entry_executes", "normal_exit_reached", "lossless_valid_numeric_conversion"],
            "source_sha256": digest, "relative_path": path, "start_line": 1, "end_line": total,
            "exit_line": nodes[-1].span.start_line,
            "evidence_ids": coverage.get("complete_evidence_ids", [])}

def _final_value_requirements(matched, pages, working_set, availability):
    """Separate syntax limits from proven range gaps and external input values."""
    requirements = [{"kind": "control_flow_and_overwrite_semantics_unverified"}]
    identities = {(fact["relative_path"], fact["source_sha256"]) for fact in matched}
    for path, digest in sorted(identities):
        supplied = availability.get(path, {})
        if supplied.get("complete_source_sha256") == digest:
            requirements.append({"kind": "source_complete_semantics_unverified", "relative_path": path,
                                 "source_sha256": digest})
            continue
        manifest = next((row for row in (working_set or {}).get("source_manifest", [])
            if row.get("relative_path") == path and row.get("sha256") == digest
            and type(row.get("line_count")) is int and row["line_count"] > 0), None)
        version = next((row for row in supplied.get("supplied_versions", [])
                        if row["source_sha256"] == digest), {})
        if manifest:
            cursor, gaps = 1, []
            for span in sorted(version.get("supplied_ranges", []), key=lambda row: row["start_line"]):
                if span["start_line"] > cursor:
                    gaps.append({"start_line": cursor, "end_line": min(span["start_line"] - 1, manifest["line_count"])})
                cursor = max(cursor, span["end_line"] + 1)
            if cursor <= manifest["line_count"]:
                gaps.append({"start_line": cursor, "end_line": manifest["line_count"]})
            requirements.append({"kind": "source_ranges_not_supplied", "relative_path": path,
                "source_sha256": digest, "ranges": [gap for gap in gaps if gap["start_line"] <= gap["end_line"]]})
        else:
            requirements.append({"kind": "source_coverage_unestablished", "relative_path": path,
                                 "source_sha256": digest})
    # ACCEPT is a directly visible runtime input, not a missing implementation.
    # Do not infer external input merely because a formula contains a variable.
    operands = {token for fact in matched for token in fact.get("expression", [])}
    for page in pages.values():
        if (page.get("relative_path"), page.get("source_sha256")) not in identities:
            continue
        matched_programs = {fact.get("program_name") for fact in matched if fact["evidence_id"] == page["evidence_id"]}
        declared_programs = set(re.findall(r"\bPROGRAM-ID\.\s*([A-Z][A-Z0-9-]*)", page.get("source_text", ""), re.I))
        if (not matched_programs or len(declared_programs) > 1
                or (declared_programs and not declared_programs.intersection(matched_programs))):
            continue
        for match in re.finditer(r"(?im)^\s*(?:\d{6}\s+)?ACCEPT\s+([A-Z][A-Z0-9-]*)\b", page.get("source_text", "")):
            if match[1].upper() in operands:
                requirements.append({"kind": "external_runtime_input", "symbol": match[1].upper(),
                                     "evidence_id": page["evidence_id"]})
    return requirements


def _final_value_qualification(requirements, answer_language):
    def local(zh_cn, en, zh_hk=None):
        return localized_text(answer_language, zh_cn, en, zh_hk)
    complete = (any(row["kind"] == "source_complete_semantics_unverified" for row in requirements)
                and not any(row["kind"] in {"source_ranges_not_supplied", "source_coverage_unestablished"}
                            for row in requirements))
    text = local("最终值仍需核对控制流及后续覆盖；相关源码已齐备，但这些语句尚未证明各条执行路径的最终结果。",
                 "The relevant source is complete; the final value still requires checking control flow and later overwrites across execution paths. ",
                 "最終值仍需核對控制流及後續覆蓋；相關源碼已齊備，但這些語句尚未證明各條執行路徑的最終結果。") if complete else local(
                 "最终值仍需核对控制流及后续覆盖。",
                 "The final value still requires checking control flow and later overwrites. ",
                 "最終值仍需核對控制流及後續覆蓋。")
    gaps = [row for row in requirements if row["kind"] == "source_ranges_not_supplied" and row["ranges"]]
    if gaps:
        locations = "; ".join(f"{row['relative_path']}:" + ",".join(
            f"{span['start_line']}-{span['end_line']}" for span in row["ranges"]) for row in gaps)
        text += local(f"尚未纳入本次核对的源码范围为 {locations}。",
                      f"Source ranges not supplied for this review: {locations}. ",
                      f"尚未納入本次核對的源碼範圍為 {locations}。")
    inputs = list(dict.fromkeys(row["symbol"] for row in requirements if row["kind"] == "external_runtime_input"))
    if inputs:
        symbols = ", ".join(inputs)
        text += local(f"其中 {symbols} 由运行时输入，实际数值需结合该输入确定。",
                      f"{symbols} is supplied at runtime; its actual value depends on that input. ",
                      f"其中 {symbols} 由執行時輸入，實際數值需結合該輸入確定。")
    return text


def review_answer_claims(answer: str, source_pages, allowed, *, source_format="auto", compilation_context=None,
                         source_fact_cache=None, answer_language=None, working_set=None):
    """Review and repair only affected segments, using actually supplied pages.

    Returned ledger preserves rejected claims and exact version evidence for
    audit. The user-visible replacement is rendered from the checked fact.
    Arbitrary prose remains explicitly unsupported in the ledger. An invalid
    citation causes a local review and a scoped uncertainty when this bounded
    checker cannot establish the associated statement.
    """
    from compilation_context import resolve_compilation_context
    if answer_language is None:
        answer_language = resolve_answer_language(answer)

    def local(zh_cn, en, zh_hk=None):
        return localized_text(answer_language, zh_cn, en, zh_hk)

    fact_separator = local("；", "; ")
    sentence_end = local("。", ". ")
    context = resolve_compilation_context(compilation_context, source_options={"source_format": source_format})
    pages = {page["evidence_id"]: page for page in source_pages if page.get("evidence_id") in allowed}
    availability = supplied_source_availability(list(pages.values()), working_set)
    facts = []
    needs_declarations = any(assertion["predicate"] == "declaration_value" for assertion in _assertions(answer))
    for identifier, page in pages.items():
        key = (page.get("relative_path"), page.get("source_sha256"), page.get("start_line"),
               page.get("end_line"), page.get("source_text"), context.fingerprint)
        cached = source_fact_cache.get(key) if isinstance(source_fact_cache, dict) else None
        projected = cached if cached is not None else supplied_statement_facts(
            page, source_format=source_format, compilation_context=context)
        if needs_declarations:
            declaration_key = ("declaration_values", *key)
            declarations = source_fact_cache.get(declaration_key) if isinstance(source_fact_cache, dict) else None
            if declarations is None:
                declarations = [fact for fact in supplied_statement_facts(page, source_format=source_format,
                    compilation_context=context, include_declarations=True) if fact["predicate"] == "declaration_value"]
                if isinstance(source_fact_cache, dict):
                    source_fact_cache[declaration_key] = declarations
            projected = [*projected, *declarations]
        for fact in projected:
            facts.append({**fact, "evidence_id": identifier,
                          "relative_path": page.get("relative_path"), "source_sha256": page["source_sha256"]})
    ledger = ClaimLedger(original_answer_sha256=hashlib.sha256(answer.encode()).hexdigest(),
                         source_fact_count=len(facts), compilation_context_fingerprint=context.fingerprint)
    reviewed = []
    for segment, inherited, table_header, derived_context in _segments(answer):
        text = _REFERENCE.sub("", segment).strip()
        if not text or re.fullmatch(r"[-|: ]+", text):
            reviewed.append(segment)
            continue
        ids = list(dict.fromkeys(_REFERENCE.findall(segment) or inherited))
        invalid = [identifier for identifier in ids if identifier not in allowed]
        valid = [identifier for identifier in ids if identifier in allowed]
        assertions = _assertions(segment, table_header=table_header, derived_context=derived_context)
        replacements, retained_supported, unresolved, final_requirements = [], [], False, []
        citation_additions = []
        for fact in facts:
            if fact["predicate"] != "noop_procedure" or not re.search(r"(?<![\w-])" + re.escape(fact["target"]) + r"(?![\w-])", text, re.I):
                continue
            remaining = re.sub(re.escape(fact["target"]), "", text, flags=re.I)
            if (re.search(r"计算|計算|乘以|写入|寫入|更新|\b(?:calculates?|computes?|multiplies|writes?|updates?)\b", remaining, re.I)
                    and not re.search(r"未实现|未實現|没有|沒有|未计算|未計算|不计算|不計算|不写|不寫|不能|尚未|\b(?:not|no|unimplemented|empty)\b", remaining, re.I)):
                version = {key: fact[key] for key in ("relative_path", "source_sha256", "start_line", "end_line")}
                ledger.entries.append(ClaimEntry(f"claim-{len(ledger.entries) + 1}", "procedure_behavior", text,
                    target_entity=fact["target"], evidence_ids=[fact["evidence_id"]], source_versions=[version],
                    support_status="contradicted", reason_code="COMPLETE_PROCEDURE_HAS_ONLY_NOOP_STATEMENTS",
                    checker_version=LEDGER_VERSION, support_scope=fact["support_scope"],
                    occurrence_ids=[fact["occurrence_id"]], invalid_evidence_ids=invalid,
                    revision="rendered_complete_noop_procedure", revised_claims=[{"assertion": {"predicate": "noop_procedure", "target": fact["target"]},
                        "support_status": "supported", "support_scope": fact["support_scope"], "evidence_id": fact["evidence_id"],
                        "source_sha256": fact["source_sha256"]}]))
                replacements.append(fact)
        if replacements:
            reviewed.append(local("所引源码可直接确认：", "The cited source directly confirms: ", "所引源碼可直接確認：")
                + fact_separator.join(_render_fact(fact, answer_language) for fact in replacements)
                + sentence_end + ("\n" if segment.endswith("\n") else ""))
            continue
        for assertion in assertions or [{}]:
            entry = ClaimEntry(f"claim-{len(ledger.entries) + 1}", assertion.get("predicate", "business_explanation"),
                               text, target_entity=assertion.get("target"), expression=assertion.get("expression"),
                               condition=assertion.get("condition"), effect=assertion.get("effect"),
                               evidence_ids=valid, invalid_evidence_ids=invalid)
            entry.source_versions = [{key: allowed[identifier][key] for key in
                ("relative_path", "source_sha256", "document_sha256", "start_line", "end_line", "include_chain")
                if key in allowed[identifier]} for identifier in valid]
            ledger.entries.append(entry)
            if not assertion:
                if invalid:
                    entry.reason_code = "INVALID_CITATION_REQUIRES_REVIEW"
                    unresolved = True
                continue
            if assertion["predicate"] == "derived_numeric_result":
                entry.reason_code = "DERIVED_RESULT_NOT_FORMALLY_VERIFIED"
                entry.support_scope = "unverified_conditional_numeric_result"
                if invalid:
                    unresolved = True
                continue
            if assertion["predicate"] == "mathematical_expression":
                entry.reason_code = "MATHEMATICAL_EXPRESSION_NOT_SOURCE_STATEMENT"
                entry.support_scope = "unverified_mathematical_derivation"
                if invalid:
                    unresolved = True
                continue
            if assertion["predicate"] == "business_state_value":
                assignment = {**assertion, "predicate": "assignment_statement", "rounded": False}
                scoped = [fact for fact in facts if fact["evidence_id"] in valid] if ids else facts
                matches = [fact for fact in _related(assignment, scoped)
                    if check_statement_support(assignment, fact, pages[fact["evidence_id"]])["support_status"] == "supported"]
                if matches and re.search(r"最终|最終|最后|最後|总是|總是|一定|\b(?:always|finally|final\s+value)\b", text, re.I):
                    # A literal that matches a known write can still trigger
                    # the existing final-value control/overwrite review.
                    assertion = assignment
                else:
                    # A natural-language state/result is not a claim that
                    # source contains a literal assignment of this value.
                    entry.reason_code = "BUSINESS_STATE_VALUE_NOT_FORMALLY_VERIFIED"
                    entry.support_scope = "unverified_business_state_value"
                    if invalid:
                        unresolved = True
                    continue
            if (assertion.get("predicate") == "assignment_statement"
                    and "-" not in str(assertion.get("target", ""))
                    and not _related(assertion, facts)
                    and not re.search(r"\b(?:COMPUTE|MOVE)\b|(?<![<>=])=(?![=])", text, re.I)):
                # Ordinary words in "the basis is positive" or "flag is R"
                # are not locally bound COBOL fields. A prose recognizer's
                # uncertain entity match cannot discredit a correct sentence.
                entry.claim_type = "business_explanation"
                entry.reason_code = "PROSE_ENTITY_BINDING_UNRESOLVED"
                if invalid:
                    unresolved = True
                continue
            if (assertion.get("predicate") == "write_statement"
                    and not re.search(r"\b(?:WRITE|REWRITE|DELETE)\b", text, re.I)
                    and not any(fact["predicate"] == "write_statement" for fact in facts)):
                entry.claim_type = "external_business_effect"
                entry.reason_code = "EXTERNAL_EFFECT_SEMANTICS_NOT_SUPPORTED"
                if invalid:
                    unresolved = True
                continue
            if assertion.get("predicate") == "arithmetic_constant":
                binding_facts = [fact for fact in facts if (not ids or fact["evidence_id"] in valid)
                    and fact["predicate"] == "assignment_statement" and assertion["operator"] in fact.get("expression", [])]
                def constants(fact):
                    tokens = fact["expression"]
                    return {tokens[index + offset] for index, token in enumerate(tokens) if token == assertion["operator"]
                        for offset in (-1, 1) if 0 <= index + offset < len(tokens)
                        and re.fullmatch(r"\d+(?:\.\d+)?", tokens[index + offset])}
                supported = [fact for fact in binding_facts if assertion["constant"] in constants(fact)]
                shapes = {(fact["target"], tuple(fact["expression"]), fact["rounded"]) for fact in binding_facts}
                entry.expression = [assertion["operator"], assertion["constant"]]
                entry.support_scope = "source_arithmetic_factor"
                if supported and len({fact["target"] for fact in binding_facts}) == 1:
                    entry.target_entity = supported[0]["target"]
                    entry.support_status = "supported"
                    entry.reason_code = "ARITHMETIC_FACTOR_MATCH"
                    entry.checker_version = LEDGER_VERSION
                    entry.occurrence_ids = list(dict.fromkeys(fact["occurrence_id"] for fact in supported))
                    remainder = _NARRATIVE_MULTIPLIER.sub("", text).strip(" ，,。.!?;；:")
                    if remainder:
                        ledger.entries.append(ClaimEntry(f"claim-{len(ledger.entries) + 1}", "business_explanation", text,
                            evidence_ids=valid, source_versions=list(entry.source_versions),
                            reason_code="PROSE_REMAINDER_NOT_CHECKED", support_scope="unverified_business_explanation"))
                    continue
                if len(shapes) == 1 and any(constants(fact) for fact in binding_facts):
                    # The only cited arithmetic shape explicitly uses another
                    # factor. Its complete formula is a grounded local repair.
                    claimed_expression = list(binding_facts[0]["expression"])
                    factor_index = next(index + offset for index, token in enumerate(claimed_expression)
                        if token == assertion["operator"] for offset in (-1, 1)
                        if 0 <= index + offset < len(claimed_expression)
                        and re.fullmatch(r"\d+(?:\.\d+)?", claimed_expression[index + offset]))
                    claimed_expression[factor_index] = assertion["constant"]
                    assertion = {"predicate": "assignment_statement", "target": binding_facts[0]["target"],
                        "expression": claimed_expression,
                        "rounded": binding_facts[0]["rounded"]}
                    entry.target_entity = assertion["target"]
                else:
                    # Not understanding a long/ambiguous expression is not
                    # evidence that a business sentence is false.
                    entry.reason_code = "ARITHMETIC_ENTITY_BINDING_UNRESOLVED"
                    if invalid:
                        unresolved = True
                    continue
            cited_facts = [fact for fact in facts if fact["evidence_id"] in valid] if ids else facts
            candidates = _related(assertion, cited_facts)
            if assertion["predicate"] == "declaration_value" and (len({
                    (fact["relative_path"], fact["source_sha256"], fact.get("program_name")) for fact in candidates}) > 1
                    or len({tuple(fact["expression"]) for fact in candidates}) > 1):
                entry.reason_code = "DECLARATION_IDENTITY_AMBIGUOUS"
                entry.support_scope = "unverified_declaration_binding"
                if invalid:
                    unresolved = True
                continue
            checked = [(fact, check_statement_support(assertion, fact, pages[fact["evidence_id"]])) for fact in candidates]
            supported = [(fact, verdict) for fact, verdict in checked if verdict["support_status"] == "supported"]
            rebound = False
            if assertion["predicate"] == "declaration_value" and not candidates and ids and not invalid:
                supplied = _related(assertion, facts)
                identities = {(fact["relative_path"], fact["source_sha256"], fact.get("program_name")) for fact in supplied}
                values = {tuple(fact["expression"]) for fact in supplied}
                if len(identities) == 1 and len(values) == 1:
                    supported = [(fact, verdict) for fact in supplied
                        if (verdict := check_statement_support(assertion, fact, pages[fact["evidence_id"]]))["support_status"] == "supported"]
                    rebound = bool(supported)
            if supported:
                final_value_claim = (assertion["predicate"] == "assignment_statement" and
                    re.search(r"最终|最終|最后|最後|总是|總是|一定|\b(?:always|finally|final\s+value)\b", text, re.I))
                if final_value_claim:
                    # Citations identify the claimed formula, not the scope of
                    # a final-value review. Include every actually supplied
                    # same-program/version write and control candidate. This
                    # bounded syntax checker still cannot prove final values
                    # or exclude effects in source that was not supplied.
                    matched = [fact for fact, _ in supported]
                    proof = _straight_line_final_assignment(assertion["target"], matched, pages, facts,
                        availability, source_format, context)
                    if proof:
                        final_fact = proof.pop("fact")
                        entry.support_status = "unsupported"
                        entry.reason_code = "UNQUALIFIED_FINAL_VALUE_REQUIRES_NORMAL_PATH_PRECONDITIONS"
                        entry.support_scope = proof["support_scope"]
                        entry.checker_version = proof["checker_version"]
                        entry.reviewed_evidence_ids = proof["evidence_ids"]
                        entry.source_versions = [{key: proof[key] for key in
                            ("relative_path", "source_sha256", "start_line", "end_line")}]
                        entry.occurrence_ids = [final_fact["occurrence_id"]]
                        entry.verification_facts = [{"assertion": {"predicate": "final_assignment_on_normal_path",
                            "target": final_fact["target"], "expression": final_fact["expression"],
                            "rounded": final_fact["rounded"]}, **proof,
                            "evidence_id": final_fact["evidence_id"], "occurrence_id": final_fact["occurrence_id"]}]
                        continue
                    scope = [fact for fact in facts if _same_program_version(fact, matched)]
                    same_target = _related(assertion, scope)
                    review_facts = [*same_target, *(fact for fact in scope
                        if fact["predicate"] == "condition_statement")]
                    entry.support_status = "unsupported"
                    entry.reason_code = "FINAL_VALUE_REQUIRES_CONTROL_AND_OVERWRITE_ANALYSIS"
                    entry.support_scope = "source_statement_syntax"
                    entry.checker_version = LEDGER_VERSION
                    entry.reviewed_evidence_ids = list(dict.fromkeys(fact["evidence_id"] for fact in review_facts))
                    entry.source_versions = [{key: fact[key] for key in
                        ("relative_path", "source_sha256", "start_line", "end_line", "program_name")
                        if key in fact} for fact in review_facts]
                    entry.occurrence_ids = [fact["occurrence_id"] for fact in same_target[:8]]
                    entry.verification_facts = _revised_claims(same_target[:8], pages)
                    entry.unresolved_requirements = _final_value_requirements(matched, pages, working_set, availability)
                    continue
                entry.support_status = "supported"
                entry.reason_code = supported[0][1]["reason_code"]
                entry.checker_version = supported[0][1]["checker_version"]
                entry.support_scope = supported[0][1]["support_scope"]
                entry.occurrence_ids = list(dict.fromkeys(fact["occurrence_id"] for fact, _ in supported))
                retained_supported.extend(fact for fact, _ in supported)
                if not ids or rebound:
                    entry.evidence_ids = list(dict.fromkeys(fact["evidence_id"] for fact, _ in supported))
                    entry.source_versions = [{key: fact[key] for key in
                        ("relative_path", "source_sha256", "start_line", "end_line")} for fact, _ in supported]
                if rebound:
                    entry.reason_code = "SUPPLIED_DECLARATION_MATCH_CITATION_REBOUND"
                    entry.revision = "declaration_citation_rebound"
                    entry.reviewed_evidence_ids = entry.evidence_ids
                    entry.revised_claims = _revised_claims([fact for fact, _ in supported], pages)
                    citation_additions.extend(identifier for identifier in entry.evidence_ids if identifier not in valid)
                continue
            # A missing or mismatching citation is not repaired by deleting its
            # marker. Search the supplied facts for the named local entity and
            # render their exact statement, including alternatives if needed.
            repair = candidates if valid and not invalid else _related(assertion, facts)
            if not candidates and valid and not invalid and all(
                    str(pages.get(identifier, {}).get("source_text") or "").strip() for identifier in valid):
                # The cited program has no comparable parsed fact. A write
                # to the same field name in another program cannot refute it.
                entry.reason_code = "SOURCE_ASSERTION_NOT_LOCALLY_VERIFIABLE"
                entry.support_scope = "unverified_source_assertion"
                continue
            comparisons = checked or ([(fact, check_statement_support(assertion, fact, pages[fact["evidence_id"]]))
                for fact in repair] if invalid else [])
            # The legacy COMPUTE checker reports parsed token inequality with
            # an explicit mismatch code while retaining its older unsupported
            # status vocabulary. Only that exact comparison is a contradiction.
            contradicted = bool(comparisons) and all(
                verdict["support_status"] == "contradicted" or verdict["reason_code"] == "COMPUTE_ASSERTION_MISMATCH"
                for _, verdict in comparisons)
            entry.reason_code = ("SOURCE_STATEMENT_MISMATCH" if contradicted else
                "INVALID_CITATION_REQUIRES_REVIEW" if invalid else
                "SOURCE_ASSERTION_NOT_LOCALLY_VERIFIABLE" if pages else "EVIDENCE_NOT_SUPPLIED")
            entry.support_status = "contradicted" if contradicted else "unsupported"
            entry.checker_version = comparisons[0][1]["checker_version"] if comparisons else None
            entry.support_scope = "source_statement_syntax"
            if not contradicted and not invalid:
                # The only normal authority for destructive replacement is a
                # contradiction established by the bounded checker. Missing
                # inline citations, missing projections and uncertain control
                # semantics remain audit findings; they do not erase prose.
                entry.support_scope = "unverified_source_assertion"
                continue
            if repair:
                unique = []
                for fact in repair:
                    if (fact["relative_path"], fact["start_line"], fact.get("target"), fact.get("expression"), fact.get("condition")) not in [
                        (row["relative_path"], row["start_line"], row.get("target"), row.get("expression"), row.get("condition")) for row in unique]:
                        unique.append(fact)
                replacements.extend(unique[:8])
                entry.revision = "rendered_from_supplied_source_statements"
                entry.occurrence_ids = [fact["occurrence_id"] for fact in unique[:8]]
                entry.source_versions = [{key: fact[key] for key in
                    ("relative_path", "source_sha256", "start_line", "end_line")} for fact in unique[:8]]
                entry.revised_claims = _revised_claims(unique[:8], pages)
            else:
                unresolved = True
                entry.revision = "scoped_uncertainty"
        if replacements:
            # A repaired guard or formula must not erase other independently
            # supported structures in that same sentence.
            replacements.extend(retained_supported)
            seen, distinct = set(), []
            for fact in replacements:
                key = (fact["evidence_id"], fact["occurrence_id"], fact.get("target"))
                if key not in seen:
                    seen.add(key)
                    distinct.append(fact)
            trailing = "\n" if segment.endswith("\n") else ""
            qualification = _final_value_qualification(final_requirements, answer_language) if final_requirements else ""
            reviewed.append(local("已供应源码可直接确认：", "The supplied source directly confirms: ", "已提供源碼可直接確認：")
                + fact_separator.join(_render_fact(fact, answer_language) for fact in distinct)
                + sentence_end + qualification + trailing)
        elif unresolved:
            clean = _REFERENCE.sub(lambda match: match[0] if match[1] in allowed else "", segment)
            if assertions:
                reviewed.append(local("该处公式、条件或效果尚未得到所引源码支持，需要核对相关语句。",
                    "The cited source does not yet support this formula, condition, or effect; the relevant statements need to be checked. ",
                    "該處公式、條件或效果尚未得到所引源碼支持，需要核對相關語句。")
                    + ("\n" if segment.endswith("\n") else ""))
            else:
                reviewed.append(clean.rstrip("\n") + local("（本句有未供应的引用，这部分陈述尚未核实。）",
                    " (This sentence cites evidence that was not supplied; this part of the statement remains unverified.)",
                    "（本句有未提供的引用，這部分陳述尚未核實。）") + ("\n" if segment.endswith("\n") else ""))
        else:
            clean = _REFERENCE.sub(lambda match: match[0] if match[1] in allowed else "", segment)
            if citation_additions:
                clean = clean.rstrip("\n") + "".join(f"[{identifier}]" for identifier in dict.fromkeys(citation_additions)) + ("\n" if clean.endswith("\n") else "")
            reviewed.append(clean)
    result = "".join(reviewed)
    ledger.answer_sha256 = hashlib.sha256(result.encode()).hexdigest()
    return result, ledger
