"""Source-grounded progress and review for ordinary local business questions."""

from dataclasses import dataclass
import re

from online_deduction_review import deduction_review_requested as detailed_deduction_requested
from online_grounded_findings import (
    _programs, _SUBJECTS, _subject_words, grounded_preview, grounded_sequence_answer,
)
from online_source_recovery import named_scope, question_terms
from question_intent import business_sequence_requested
from response_language import localized_text
from source_reading import _identify_page
from statement_facts import program_identifiers
from syntax_evidence import _bodies, build_syntax_guide


_EVIDENCE = re.compile(r"\[ev_page_[a-f0-9]{24}\]")
_ORDER = re.compile(r"先|随后|隨後|然后|然後|之后|之後|接着|接著|依次|第[一二]|"
                    r"\b(first|then|after|before|next|followed by|subsequently)\b|(?:^|\n)\s*1[.)、]", re.I)
_SKIP = re.compile(r"跳[过過]|略[过過]|否[则則]|其[他餘余]|[不未无無]扣|不[作做再予]|不[调調]用|不[进進]入|"
                   r"保[持留]|不[变變]|直接返回|不[处處]理|才(?:会|會)?(?:扣|减|減|调|調|进入|進入)|"
                   r"\b(skip\w*|otherwise|unchanged|retain\w*|only if|unless|not deduct\w*)\b", re.I)
_RESULT = re.compile(r"最[终終后後]|(?:返回|回[写寫])(?:的|后|後)?(?:应收|應收|金额|金額|余额|餘額)|"
                     r"\b(final|returned|resulting|ends? up|net result)\b", re.I)
_BALANCE = re.compile(r"应收|應收|余额|餘額|金额|金額|结余|結餘|\b(receivable|amount|balance|due)\b", re.I)
_EFFECT = re.compile(r"减[去掉少]?|減[去掉少]?|抵扣|扣[除减減掉]|实际[扣生]|實際[扣生]|保留|保持|不变|不變|"
                     r"等于|等於|[=−]|为\s*\d|為\s*\d|"
                     r"\b(less|minus|deduct\w*|subtract\w*|remain\w*|retain\w*|unchanged|equals?)\b", re.I)
_RELATIONS = {
    "=": r"(?<![<>!])=(?!=)|(?<![不于於])等[于於]|(?:类型|類型|代[码碼])(?:为|為|是)|\bequals?\b",
    "<>": r"<>|!=|不等[于於]|不[是为為]|不同|\b(?:differs? from|not equal)\b",
    ">": r"(?<![=<>])>(?!=)|(?<!不)大[于於](?!\s*(?:或|等))|(?:为|為)?正(?:数|數)?|(?<!不)超[过過]|\b(?:greater than(?! or equal)|positive|above)\b",
    "<": r"(?<![=<>])<(?![=>])|(?<!不)小[于於](?!\s*(?:或|等))|低[于於]|\b(?:less than(?! or equal)|below)\b",
    ">=": r">=|≥|大[于於](?:或)?等[于於]|不少[于於]|不小[于於]|至少|\b(?:at least|greater than or equal)\b",
    "<=": r"<=|≤|小[于於](?:或)?等[于於]|不大[于於]|不超[过過]|至多|\b(?:at most|less than or equal)\b",
}
_RELATIONS = {op: re.compile(pattern, re.I) for op, pattern in _RELATIONS.items()}
# Read enough to verify a bounded local excerpt, not another copy of a large program.
_CAPTURE_LIMIT = 128_000


def _sequence_question(question):
    # These everyday synonyms still describe a deduction, not COBOL field order.
    return re.sub(r"扣[费費款除]|冲抵|沖抵", "扣减", question)


def deduction_review_requested(question):
    normalized = _sequence_question(question)
    return detailed_deduction_requested(normalized) or bool(
        re.search(r"扣[減减]|抵[扣減减]|\b(?:deduct\w*|offset\w*)\b", normalized, re.I)
        and business_sequence_requested(normalized))


def _verified_view(page, session):
    if (not isinstance(page, dict) or not isinstance(page.get("source_text"), str)
            or not page["source_text"] or len(page["source_text"]) > 32000
            or page.get("span_truncated") is not False or type(page.get("start_line")) is not int
            or page["start_line"] < 1
            or page.get("source_verification") != "content_hash"
            or any(page.get(k) for k in ("include_chain", "copy_instance", "replacement_trace"))):
        return None
    text = page["source_text"]
    lines = text.splitlines()
    if (not isinstance(page.get("relative_path"), str) or not isinstance(page.get("source_sha256"), str)
            or page.get("end_line") != page["start_line"] + len(lines) - 1
            or _identify_page(page) != page.get("evidence_id")):
        return None
    capture = session.capture(page["relative_path"])
    if capture.sha256 != page["source_sha256"]:
        return None
    # The fitted prompt can contain a prefix of a file even when the page itself
    # was not clipped. Compare its actual physical lines with this turn's capture.
    with capture.path.open(encoding=capture.encoding) as stream:
        limit = len(text) + 4 if page["start_line"] == 1 else _CAPTURE_LIMIT
        raw = stream.read(limit)
        eof = not stream.read(1)
    original = raw.lstrip("\ufeff").splitlines()
    if not eof and original and not raw.endswith(("\r", "\n")):
        original.pop()
    if original[page["start_line"] - 1:page["end_line"]] != lines:
        return None
    # Honour explicit local source layout. When the independent parser cannot
    # recognise the same layout, this particular proof does not apply.
    layout = capture.source_format
    if page["start_line"] == 1 and layout in {"free", "fixed"}:
        bodies, _ = _bodies(text, 1)
        if not bodies or any(b[3] != layout for b in bodies):
            return None
    view = {"id": page["evidence_id"], "text": text, "truncated": False, "last_line_truncated": False,
            "complete_line_count": len(lines), "citation": {"path": page["relative_path"], "hash": capture.sha256,
            "start": page["start_line"], "end": page["end_line"], "commit": None, "revision": session.index_configuration}}
    return view, eof and page["start_line"] == 1 and len(original) == len(lines)


def _source_observation(question, page, answer_language):
    """Show an identifiable operation in a paragraph without inferring its outer control flow."""
    paths, restricted, _ = named_scope(question)
    if restricted and page["relative_path"] not in paths:
        return None
    terms = {t.casefold() for t in question_terms(question, [])}
    terms.update(key for alias, key in _SUBJECTS.items() if alias in question)
    if not terms and page["relative_path"] not in paths:
        return None
    for fact in build_syntax_guide([page])["facts"]:
        if fact["kind"] not in {"SUBTRACT", "COMPUTE", "ADD", "MOVE", "CALL", "WRITE", "REWRITE"}:
            continue
        statement = fact["statement"]
        words = set(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", statement.casefold()))
        words.update(word for name in tuple(words) for word in name.split("-"))
        if not terms.intersection(words) and page["relative_path"] not in paths:
            continue
        if len(statement) > 280 or any(c in statement for c in ("`", "[", "]")):
            continue
        line = fact["start_line"]
        return localized_text(answer_language,
            f"第 {line} 行写有 `{statement}`。执行这条语句的前提仍待核对。",
            f"Line {line} contains `{statement}`. Its execution conditions are still being checked.",
            f"第 {line} 行寫有 `{statement}`。執行這條語句的前提仍待核對。")
    return None


def _topic_words(program, step):
    words = _subject_words(program, step)
    return {step.operand.casefold(), program.name.casefold(),
            *(alias for alias, key in _SUBJECTS.items() if key in words)}


def _conditional_positions(proof, text):
    """Find each required decision in source order, before the final result."""
    positions = []
    cursor = 0
    for program, step in proof:
        left, op, right = step.guard
        # A literal is a strong identifying anchor. A numeric boundary can
        # also be stated in prose (positive for > 0).
        if right.startswith(('"', "'")):
            anchors = {right[1:-1].casefold()}
        else:
            anchors = _topic_words(program, step) | {left.casefold(), right.casefold()}
        for match in _RELATIONS[op].finditer(text, cursor):
            context = text[max(cursor, match.start() - 75):match.end() + 75]
            if not any(word in context for word in anchors):
                continue
            if right[:1].isdigit() and not (right == "0" and re.search(r"正|positive", match[0], re.I)):
                if not re.search(r"(?<![\d.])" + re.escape(right) + r"(?![\d.])", context):
                    continue
            positions.append(match.start())
            cursor = match.end()
            break
        else:
            return []
    return positions


@dataclass
class DeductionEvidence:
    checked_findings: list
    closed_answer: str | None
    requires_review: bool
    decisions: tuple
    unresolved_targets: tuple = ()

    def missing_aspects(self, answer):
        if not self.requires_review or self.closed_answer and answer == self.closed_answer:
            return []
        text = _EVIDENCE.sub("", answer or "").casefold()
        result = _RESULT.search(text)
        # The conclusion may recap every subject without explaining a single
        # branch. Look for the individual decisions in the procedural text.
        body = text
        if result:
            prefix = text[:result.start()]
            # Business answers may lead with their conclusion. Move on to
            # the following rule descriptions in that case.
            if _ORDER.search(prefix):
                body = prefix
        positions = _conditional_positions(self.decisions, body) if self.decisions else []
        missing = []
        if not _ORDER.search(body) or (self.decisions and not positions):
            missing.append("processing_sequence")
        if positions:
            ends = positions[1:] + [len(body)]
            skips = all(_SKIP.search(body[start:end]) for start, end in zip(positions, ends))
        else:
            skips = bool(_SKIP.search(body)) and not self.decisions
        if not skips:
            missing.append("skip_conditions")
        ending = text[result.start():] if result else ""
        if not (_BALANCE.search(ending) and _EFFECT.search(ending)):
            missing.append("final_receivable")
        return missing


def inspect_deduction(question, pages, *, source_session, answer_language):
    """Inspect only physical pages actually supplied to this local question."""
    required = deduction_review_requested(question)
    evidence = DeductionEvidence([], None, required, ())
    if not pages or source_session is None:
        return evidence
    # Duplicated windows cannot add obligations or turn missing pages into a proof.
    originals = {(p.get("relative_path"), p.get("source_sha256")) for p in pages if isinstance(p, dict)}
    full = {}
    # A progress update may inspect existing question captures. It must never
    # pull a new file into a question or re-open a missing historical source.
    captured = {row["relative_path"]: row["sha256"] for row in source_session.source_manifest()}
    seen = set()
    scanned = 0
    named_programs = set()
    deduction_calls = set()
    for page in pages:
        if (not isinstance(page, dict) or page.get("evidence_id") in seen
                or page.get("source_sha256") != captured.get(page.get("relative_path"))):
            continue
        text = page.get("source_text")
        if not isinstance(text, str) or scanned + len(text) > _CAPTURE_LIMIT:
            continue
        scanned += len(text)
        try:
            checked = _verified_view(page, source_session)
        except (OSError, ValueError):
            # Relevance and delivery still run under their own source checks.
            continue
        if not checked:
            continue
        view, complete = checked
        seen.add(view["id"])
        if complete:
            full[(view["citation"]["path"], view["citation"]["hash"])] = view
        if required and view["citation"]["start"] == 1:
            fmt = page.get("format_hint")
            if fmt not in {"fixed", "free"}:
                bodies, _ = _bodies(view["text"], 1)
                fmt = bodies[0][3] if bodies else None
            if fmt:
                named_programs.update(name for name, _, _ in program_identifiers(view["text"], source_format=fmt))
        finding = grounded_preview(question, [view], answer_language)
        if required and finding and complete:
            for program in _programs([view]):
                last = program.steps[-1]
                if (program.closed and last.kind == "call" and last.receiver
                        and any(step.kind == "subtract" and step.receiver == last.receiver
                                for step in program.steps[:-1])):
                    deduction_calls.add(last.operand)
        if len(evidence.checked_findings) < 8:
            text = (_EVIDENCE.sub("", finding["answer"]).strip() if finding else
                    _source_observation(question, page, answer_language))
            if text:
                evidence.checked_findings.append({"text": text,
                    "sources": [{key: page[key] for key in ("evidence_id", "relative_path", "start_line", "end_line")}]})
    evidence.unresolved_targets = tuple(sorted(deduction_calls - named_programs))
    if not required or len(originals) != 2 or full.keys() != originals:
        return evidence
    complete_pages = list(full.values())
    normalized = _sequence_question(question)
    programs = {p.evidence: p for p in _programs(complete_pages)}
    # Do not discard a request for a receivable when the program returns a
    # different quantity. A named receivable can safely use the amount grammar.
    caller = next((p for p in programs.values() if p.steps[-1].kind == "call"), None)
    if caller and {"DUE", "RECEIVABLE"}.intersection(caller.steps[-1].receiver.split("-")):
        normalized = normalized.replace("最终应收", "最终金额").replace("最終應收", "最終金額")
    closed = grounded_sequence_answer(normalized, complete_pages, answer_language)
    if closed:
        evidence.closed_answer = closed["answer"]
        evidence.decisions = tuple((programs[citation], step) for citation in closed["citations"]
                                   for step in programs[citation].steps)
    return evidence
