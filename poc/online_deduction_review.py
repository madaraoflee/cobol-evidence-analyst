"""Bind a model's deduction checklist to the business prose that will be shown."""

import re

from question_intent import business_sequence_requested
from statement_parser import parse_statements
from syntax_evidence import _bodies


_DEDUCTION = re.compile(r"扣[減减費费款]|抵[扣減减]|\b(?:deduct\w*|offset\w*|debit\w*)\b", re.I)
_DETAIL = re.compile(r"条件|條件|适用|適用|跳[过過]|略過|例外|最终|最終|结果|結果|"
                     r"\b(?:condition\w*|when|skip\w*|eligib\w*|result\w*|outcome\w*|final)\b", re.I)
_OPERATION = re.compile(r"^(?:\d{6}\s+)?(?:IF|ELSE|WHEN|EVALUATE|PERFORM|CALL|MOVE|SET|"
                        r"SUBTRACT|ADD|MULTIPLY|DIVIDE|COMPUTE|READ|WRITE|REWRITE|"
                        r"EXEC\s+SQL|SELECT|INSERT|UPDATE|RETURN|GOBACK)\b", re.I)

REVIEW_INSTRUCTIONS = """
For a deduction sequence, account for each step in execution order. Explain
what makes it apply, when it is skipped, what balance is passed to the next
step, and the result on the applicable paths, including equality boundaries.
Distinguish a calculated amount from a posted transaction. If a decisive
branch is missing, read it or retain the specific gap with complete:false.
For a complete answer include a deduction_review object. Its steps array is in
the same order as the answer. For each step copy short exact phrases from your
answer into applies, skips and effect, and cite its original pages in citations.
Copy one whole executable source line for the step into source_line. Set result
to the exact final-result phrase, result_citations to its page IDs and
result_source_line to the last governing calculation or its controlling condition.
Cover every established deduction in source execution order. Cite its deduction
statement or its own controlling IF. Extra file citations alone do not cover it.
In the answer itself include these citations inline. Keep the
requested language and format. Do not send this checklist in the business prose.
"""


def deduction_review_requested(question):
    return bool(_DEDUCTION.search(question) and _DETAIL.search(question) and business_sequence_requested(question))


def _covers_proven_deductions(closed, pages, review):
    # Only the closed-path analyser may nominate programs and their order.
    # Other reads, incomplete windows and unrelated branches create no new obligations.
    required = []
    for identifier in closed["citations"]:
        page = pages[identifier]
        for _, body, start, source_format in _bodies(page["text"], page["citation"]["start"])[0]:
            parsed = parse_statements(body, start_line=start, source_format=source_format, max_tokens=6000)
            for node in parsed.nodes:
                if node.kind == "IF" and len(node.children) == 1 and node.children[0].kind == "SUBTRACT":
                    child = node.children[0]
                    required.append((page["citation"], (child.guards[0].span.start_line, child.span.start_line)))

    lines_by_id = {i: p["text"].splitlines()[:p["complete_line_count"]] for i, p in pages.items()}

    def matches(bound, identifiers, source_line):
        original, positions = bound
        for identifier in identifiers:
            page = pages[identifier]
            citation = page["citation"]
            if any(citation[k] != original[k] for k in ("path", "hash", "revision", "commit")):
                continue
            lines = lines_by_id[identifier]
            if any(0 <= n - citation["start"] < len(lines)
                   and lines[n - citation["start"]].strip() == source_line.strip() for n in positions):
                return True
        return False

    if not required or not matches(required[-1], review["result_citations"], review["result_source_line"]):
        return False
    # Permit a distinct step for a call or a guard, but require each known
    # reduction to have its own matching reader-visible step, in order.
    position = 0
    for step in review["steps"]:
        if matches(required[position], step["citations"], step["source_line"]):
            position += 1
            if position == len(required):
                return True
    return False


def review_covers_answer(response, pages, *, closed=None):
    """Check coverage and provenance. This alone does not prove model interpretation."""
    review = response.get("deduction_review")
    if not isinstance(review, dict) or set(review) != {"steps", "result", "result_citations", "result_source_line"}:
        return False
    steps = review["steps"]
    answer = response["answer"]
    available = set(response["citations"])
    source_lines = {p["id"]: {line.strip() for line in p["text"].splitlines()[:p["complete_line_count"]]}
                    for p in pages if p["id"] in available}

    def present(phrase):
        return isinstance(phrase, str) and bool(phrase.strip()) and phrase in answer

    def anchored(ids, line):
        return (isinstance(ids, list) and 1 <= len(ids) <= 64
                and all(isinstance(i, str) and i in source_lines for i in ids)
                and isinstance(line, str) and bool(line.strip()) and len(line) <= 1000
                and _OPERATION.match(line.strip()) is not None
                and any(line.strip() in source_lines[i] for i in ids))

    if (not isinstance(steps, list) or not 1 <= len(steps) <= 64
            or not present(review["result"]) or not anchored(review["result_citations"], review["result_source_line"])):
        return False
    position = 0
    for step in steps:
        if not isinstance(step, dict) or set(step) != {"applies", "skips", "effect", "citations", "source_line"}:
            return False
        phrases = [step[key] for key in ("applies", "skips", "effect")]
        if (not all(present(phrase) for phrase in phrases) or len(set(phrases)) != 3
                or not anchored(step["citations"], step["source_line"])):
            return False
        # The business prose itself must describe the operations in this order.
        index = answer.find(step["effect"], position)
        if index < 0:
            return False
        position = index + len(step["effect"])
    return closed is None or _covers_proven_deductions(closed, {page["id"]: page for page in pages}, review)
