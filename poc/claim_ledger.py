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


LEDGER_VERSION = "answer-claim-ledger-v3"
_REFERENCE = re.compile(r"\[((?:ev[_:-]|fw:)[^\]\r\n]{1,160})\]")
_NAME = r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*(?:\s+(?:OF|IN)\s+[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*)*"
_ATOM = rf"(?:{_NAME}|[+-]?\d+(?:\.\d+)?|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|[()])"
_EXPRESSION = rf"{_ATOM}(?:\s*(?:\*\*|[+*/×÷-]|\b(?:multiplied\s+by|times|divided\s+by)\b|乘以|乘|除以|加上|减去)\s*{_ATOM})*"
_FORMULA = re.compile(rf"(?P<target>{_NAME})\s*(?:(?:最终|最終|最后|最後|总是|總是|一定|always|finally)\s*)?(?:ROUNDED\s*)?(?:=(?!=)|等于|等於|(?:为|為|是)\s*|\bis\s+)\s*", re.I)
_ANSWER_TOKEN = re.compile(rf"\s*(?:'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|\d+(?:\.\d+)?|(?:multiplied\s+by|divided\s+by|times)\b|乘以|乘|除以|加上|减去|{_NAME}|\*\*|[()+*/×÷-])", re.I)
_MULTIPLE = re.compile(rf"(?P<target>{_NAME})\s*(?:为|為|是|按|等于|等於)\s*(?P<input>{_NAME})\s*的?\s*(?P<number>\d+(?:\.\d+)?|两|兩|二|三|四|五|六|七|八|九|十)\s*倍", re.I)
_MOVE = re.compile(rf"\bMOVE\s+(?P<expression>{_ATOM})\s+TO\s+(?P<target>{_NAME})", re.I)
_CONDITION = re.compile(rf"(?P<left>{_NAME})\s*(?P<operator>>=|<=|<>|>|<|(?:NOT\s+)?=)\s*(?P<right>{_ATOM})", re.I)
_WRITE = re.compile(rf"\b(?P<effect>WRITE|REWRITE|DELETE)\s+(?P<target>{_NAME})", re.I)
_NATURAL_EFFECT = re.compile(rf"(?:写入|寫入|改写|改寫|重写|重寫|删除|刪除)\s*`?(?P<target>{_NAME})", re.I)
_NARRATIVE_MULTIPLIER = re.compile(r"(?:乘以|乘|multiplied\s+by|times)\s*(?P<after>\d+(?:\.\d+)?)|(?P<before>\d+(?:\.\d+)?)\s*倍", re.I)
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
        token = _ANSWER_TOKEN.match(text, end)
        if token is None or token.end() == end:
            break
        end = token.end()
    tokens = _tokens(text[start:end])
    if not tokens:
        return None, end
    try:
        lexical = _lex(_Source(" ".join(tokens), 1), "free", 512)
        parser = _Expression(lexical)
        if not parser.arithmetic() or parser.position == 0:
            return None, end
        return tuple(token.value for token in lexical[:parser.position]), end
    except (_BoundaryError, ValueError):
        return None, end


def _assertions(text):
    """Translate a bounded answer notation, not source syntax, into candidates."""
    text = _REFERENCE.sub("", text).replace("`", "")
    # Strip paired Markdown strong delimiters, preserving the COBOL power
    # operator and literal contents. A spaced ** operator is not a delimiter.
    parts = re.split(r"('(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")", text)
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(r"(?<!\w)\*\*(?=\S)(.+?)(?<=\S)\*\*(?!\w)", r"\1", parts[index])
    text = "".join(parts)
    assertions, occupied = [], []
    for match in _MULTIPLE.finditer(text):
        assertions.append({"predicate": "assignment_statement", "target": match["target"].upper(),
                           "expression": [match["input"].upper(), "*", _NUMBERS.get(match["number"], match["number"])],
                           "rounded": "ROUNDED" in match[0].upper()})
        occupied.append(match.span())
    for pattern in (_MOVE, _FORMULA):
        for match in pattern.finditer(text):
            if any(first <= match.start() < last for first, last in occupied):
                continue
            # IF equality is a condition, not an assignment.
            prefix = text[max(0, match.start() - 12):match.start()]
            if re.search(r"\bIF\s*$|(?:若|如果|当|當)\s*$", prefix, re.I):
                continue
            if re.search(r"假设|假設|例如|示例|假如|\b(?:suppose|assuming)\b", text[max(0, match.start() - 24):match.start()], re.I):
                continue
            tokens, expression_end = (_tokens(match["expression"]), match.end()) if pattern is _MOVE else _read_expression(text, match.end())
            if tokens:
                assertions.append({"predicate": "assignment_statement", "target": match["target"].upper(),
                                   "expression": list(tokens), "rounded": "ROUNDED" in match[0].upper()})
                occupied.append((match.start(), expression_end))
    for match in _CONDITION.finditer(text):
        if any(first <= match.start() < last for first, last in occupied):
            continue
        tokens = _tokens(match[0])
        if tokens:
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
    """Keep fences and table rows intact; review ordinary prose by sentence."""
    for block in re.split(r"(```[^\n]*\n.*?(?:```|\Z)|~~~[^\n]*\n.*?(?:~~~|\Z))", answer, flags=re.S):
        if block.startswith(("```", "~~~")):
            yield block, []
            continue
        for paragraph in re.split(r"(\n[ \t]*\n)", block):
            inherited = list(dict.fromkeys(_REFERENCE.findall(paragraph)))
            for line in paragraph.splitlines(keepends=True) or [paragraph]:
                if line.lstrip().startswith("|"):
                    yield line, inherited
                    continue
                start = 0
                for match in re.finditer(r"[。！？](?:[ \t]*\[(?:ev[_:-]|fw:)[^\]\r\n]+\])*|(?:[!?]|\.(?!\d)|(?<!\d)\.)(?:[ \t]*\[(?:ev[_:-]|fw:)[^\]\r\n]+\])*", line):
                    yield line[start:match.end()], inherited
                    start = match.end()
                if start < len(line):
                    yield line[start:], inherited


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


def _render_fact(fact):
    if fact["predicate"] == "noop_procedure":
        text = f"`{fact['target']}` 段落只有 {' / '.join(fact['body_kinds'])}，未实现本段计算或写入逻辑"
    elif fact["predicate"] == "assignment_statement":
        rounded = " ROUNDED" if fact["rounded"] else ""
        text = f"`{fact['target']}{rounded} = {' '.join(fact['expression'])}`"
    elif fact["predicate"] == "condition_statement":
        text = f"`IF {' '.join(fact['condition'])}`"
    else:
        text = f"`{fact['effect']} {fact['target']}`（显式写入请求，成功结果需运行状态确认）"
    guards = []
    for guard in fact.get("guards", []):
        condition = guard["condition"]
        if guard.get("outcome") in {"false", False}:
            condition = "条件不成立：" + condition
        guards.append(condition)
    prefix = "在源码控制条件 `" + "；".join(guards) + "` 下，" if guards else ""
    if fact.get("normal_result_condition") == "no_size_error":
        prefix += "无 SIZE ERROR 时的算式为 "
    if fact.get("compilation_status") == "candidate":
        prefix = "编译条件尚未确认，以下仅为候选源码语句：" + prefix
    return f"{prefix}{text}（{fact['relative_path']}:{fact['start_line']}）[{fact['evidence_id']}]"


def _revised_claims(facts, pages):
    return [{"assertion": {key: fact[key] for key in
        ("predicate", "target", "expression", "rounded", "condition", "outcome", "effect") if key in fact},
        "source_guards": fact.get("guards", []),
        "compilation_status": fact.get("compilation_status", "effective_source"),
        "compilation_guards": fact.get("compilation_guards", []), "evidence_id": fact["evidence_id"],
        "source_sha256": fact["source_sha256"], "occurrence_id": fact["occurrence_id"],
        **check_statement_support(fact, fact, pages[fact["evidence_id"]])} for fact in facts]


def review_answer_claims(answer: str, source_pages, allowed, *, source_format="auto", compilation_context=None):
    """Review and repair only affected segments, using actually supplied pages.

    Returned ledger preserves rejected claims and exact version evidence for
    audit. The user-visible replacement is rendered from the checked fact.
    Arbitrary prose remains explicitly unsupported in the ledger. An invalid
    citation causes a local review and a scoped uncertainty when this bounded
    checker cannot establish the associated statement.
    """
    from compilation_context import resolve_compilation_context
    context = resolve_compilation_context(compilation_context, source_options={"source_format": source_format})
    pages = {page["evidence_id"]: page for page in source_pages if page.get("evidence_id") in allowed}
    facts = []
    for identifier, page in pages.items():
        for fact in supplied_statement_facts(page, source_format=source_format, compilation_context=context):
            facts.append({**fact, "evidence_id": identifier,
                          "relative_path": page.get("relative_path"), "source_sha256": page["source_sha256"]})
    ledger = ClaimLedger(original_answer_sha256=hashlib.sha256(answer.encode()).hexdigest(),
                         source_fact_count=len(facts), compilation_context_fingerprint=context.fingerprint)
    reviewed = []
    for segment, inherited in _segments(answer):
        text = _REFERENCE.sub("", segment).strip()
        if not text or re.fullmatch(r"[-|: ]+", text):
            reviewed.append(segment)
            continue
        ids = list(dict.fromkeys(_REFERENCE.findall(segment) or inherited))
        invalid = [identifier for identifier in ids if identifier not in allowed]
        valid = [identifier for identifier in ids if identifier in allowed]
        assertions = _assertions(segment)
        replacements, unresolved, final_value_unverified = [], False, False
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
            reviewed.append("所引源码可直接确认：" + "；".join(_render_fact(fact) for fact in replacements) + "。" + ("\n" if segment.endswith("\n") else ""))
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
            checked = [(fact, check_statement_support(assertion, fact, pages[fact["evidence_id"]])) for fact in candidates]
            supported = [(fact, verdict) for fact, verdict in checked if verdict["support_status"] == "supported"]
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
                    scope = [fact for fact in facts if _same_program_version(fact, matched)]
                    same_target = _related(assertion, scope)
                    review_facts = [*same_target, *(fact for fact in scope
                        if fact["predicate"] == "condition_statement")]
                    entry.support_status = "unsupported"
                    entry.reason_code = "FINAL_VALUE_REQUIRES_CONTROL_AND_OVERWRITE_ANALYSIS"
                    entry.support_scope = "source_statement_syntax"
                    entry.checker_version = LEDGER_VERSION
                    entry.revision = "rendered_conditional_source_statements"
                    entry.reviewed_evidence_ids = list(dict.fromkeys(fact["evidence_id"] for fact in review_facts))
                    entry.source_versions = [{key: fact[key] for key in
                        ("relative_path", "source_sha256", "start_line", "end_line", "program_name")
                        if key in fact} for fact in review_facts]
                    entry.occurrence_ids = [fact["occurrence_id"] for fact in same_target[:8]]
                    entry.revised_claims = _revised_claims(same_target[:8], pages)
                    replacements.extend(same_target[:8])
                    final_value_unverified = True
                    continue
                entry.support_status = "supported"
                entry.reason_code = supported[0][1]["reason_code"]
                entry.checker_version = supported[0][1]["checker_version"]
                entry.support_scope = supported[0][1]["support_scope"]
                entry.occurrence_ids = list(dict.fromkeys(fact["occurrence_id"] for fact, _ in supported))
                if not ids:
                    entry.evidence_ids = list(dict.fromkeys(fact["evidence_id"] for fact, _ in supported))
                    entry.source_versions = [{key: fact[key] for key in
                        ("relative_path", "source_sha256", "start_line", "end_line")} for fact, _ in supported]
                continue
            # A missing or mismatching citation is not repaired by deleting its
            # marker. Search the supplied facts for the named local entity and
            # render their exact statement, including alternatives if needed.
            repair = _related(assertion, facts)
            entry.reason_code = ("SOURCE_STATEMENT_MISMATCH" if candidates else
                                 "CITATION_CONTENT_MISMATCH" if valid else "EVIDENCE_NOT_SUPPLIED")
            entry.support_status = "contradicted" if candidates else "unsupported"
            entry.checker_version = checked[0][1]["checker_version"] if checked else None
            entry.support_scope = "source_statement_syntax"
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
            seen, distinct = set(), []
            for fact in replacements:
                key = (fact["evidence_id"], fact["occurrence_id"], fact.get("target"))
                if key not in seen:
                    seen.add(key)
                    distinct.append(fact)
            trailing = "\n" if segment.endswith("\n") else ""
            qualification = ("这些仅为已供证语句，最终值仍需核对控制流、后续覆盖及未供证代码。"
                             if final_value_unverified else "")
            reviewed.append("已供应源码可直接确认：" + "；".join(_render_fact(fact) for fact in distinct) + "。" + qualification + trailing)
        elif unresolved:
            clean = _REFERENCE.sub(lambda match: match[0] if match[1] in allowed else "", segment)
            if assertions:
                reviewed.append("该处公式、条件或效果尚未得到所引源码支持，需要核对相关语句。" + ("\n" if segment.endswith("\n") else ""))
            else:
                reviewed.append(clean.rstrip("\n") + "（本句有未供应的引用，这部分陈述尚未核实。）" + ("\n" if segment.endswith("\n") else ""))
        else:
            reviewed.append(_REFERENCE.sub(lambda match: match[0] if match[1] in allowed else "", segment))
    result = "".join(reviewed)
    ledger.answer_sha256 = hashlib.sha256(result.encode()).hexdigest()
    return result, ledger
