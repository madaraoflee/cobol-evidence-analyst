"""Bounded local framework-reference retrieval over a selected source snapshot.

Document text is untrusted reference data, never executable configuration or a
substitute for source evidence. Matching names establishes a vocabulary link;
it does not prove control flow, external effects, or runtime behavior.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import sqlite3
from threading import RLock

from company_api import APIConfigurationError, PROJECT_ENV_FILE, _read_local_env
from business_index import _clean
from source_reading import _verified_lines
from structural_index import _stable_id


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REFERENCE_PATH = PROJECT_ROOT / ".poc-data" / "framework" / "reference.md"
MAX_REFERENCE_BYTES = 2 * 1024 * 1024
MAX_DOCUMENT_SECTIONS = 4096
MAX_SECTION_CHARS = 2400
MAX_REFERENCES = 10
MAX_REFERENCE_CHARS = 16_000
MAX_SOURCE_MATCHES = 20
MAX_SOURCE_UNITS = 120_000
MAX_SOURCE_CHARS = 8 * 1024 * 1024
MAX_UNIT_CHARS = 4096
MAX_SOURCE_CANDIDATES = 2048

_WORD = re.compile(r"(?<![A-Za-z0-9_$#@-])[A-Za-z0-9][A-Za-z0-9_$#@-]{2,127}(?![A-Za-z0-9_$#@-])")
_NUMBERED_NAME = re.compile(r"([0-9]{2,8})-[A-Z][A-Z0-9_$#@-]+\Z")
_NUMBERED_TABLE_ROW = re.compile(r"^\s*\|\s*([0-9]{2,8})\s*\|")
_PAGE = re.compile(r"(?:SOURCE_PAGE\s*:\s*|source-page-)(\d{1,4})", re.I)
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_TABLE_DIVIDER = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_CJK = re.compile(r"[\u3400-\u9fff]{2,}")
# Language syntax and generic labels cannot alone establish framework use.
_GENERIC = frozenset("""
ACCEPT ADD ALL ALTER AND ARE ASCENDING ASSIGN AT AUTHOR BEFORE BINARY BY
CALL CANCEL CLOSE COBOL CODE COMP COMP-3 COMPUTATIONAL COMPUTE CONFIGURATION
COMMIT CONTINUE COPY CORRESPONDING CURSOR DATA DATE DAY DECLARE DELETE DEPENDING DESCENDING DISPLAY
DIVIDE DIVISION ELSE END END-ADD END-CALL END-COMPUTE END-DELETE END-EVALUATE
END-IF END-MULTIPLY END-PERFORM END-READ END-REWRITE END-SEARCH END-START
END-STRING END-SUBTRACT END-WRITE END-EXEC ENVIRONMENT EVALUATE EXCEPTION EXEC
EXIT EXTEND EXTERNAL FD FETCH FILE FILE-CONTROL FILLER FROM FUNCTION GIVING GOBACK
GREATER GROUP HIGH-VALUES IDENTIFICATION IF IN INDEX INDEXED INITIALIZE INPUT
INPUT-OUTPUT INSERT INTO INVALID KEY LABEL LEADING LENGTH LESS LINKAGE LOW-VALUES
MOVE MULTIPLY NATIONAL NEGATIVE NEXT NOT NULL OBJECT OCCURS OF OPEN OR OTHER
OUTPUT PACKED-DECIMAL PERFORM PIC PICTURE POINTER POSITIVE PROCEDURE PROGRAM
PROGRAM-ID QUOTE QUOTES READ REDEFINES REEL REFERENCE RELATIVE RELEASE
REMAINDER RENAMES REPLACING REPORT RETURN REWRITE RIGHT ROLLBACK ROUNDED RUN SEARCH
SECTION SELECT SENTENCE SEPARATE SEQUENTIAL SIGN SIZE SORT SOURCE SPACE
SPACES SQL SQLCODE START STATUS STOP STRING SUBTRACT SUM SYNC SYNCHRONIZED
TABLE TALLYING TEST THAN THEN THROUGH THRU TIMES TRAILING TRUE TYPE UNIT
UNTIL UP UPON USAGE USE USING VALUE VALUES VARYING WHEN WITH WORKING-STORAGE
WRITE ZERO ZEROES ZEROS FORMAT RECORD FIELD ERROR HELP SCREEN BATCH
CALLS WRITES UPDATE PARAMETERS PARAMETER TRANSACTION
AST API JSON UTF PDF IO I-O
""".split())
_CACHE: OrderedDict[tuple, "_Document"] = OrderedDict()
_CACHE_LOCK = RLock()


class _ReferenceError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _Section:
    heading: str
    page: int | None
    start_line: int
    end_line: int
    text: str
    terms: frozenset[str]


@dataclass(frozen=True)
class _Document:
    title: str
    sha256: str
    sections: tuple[_Section, ...]
    terms: frozenset[str]
    truncated: bool


def _resolve_reference(reference_path: Path | str | None) -> Path | None:
    if reference_path is None:
        if "FRAMEWORK_REFERENCE_PATH" in os.environ:
            value = os.environ["FRAMEWORK_REFERENCE_PATH"]
        else:
            try:
                local = _read_local_env(PROJECT_ENV_FILE)
                value = local.get("FRAMEWORK_REFERENCE_PATH", str(DEFAULT_REFERENCE_PATH) if DEFAULT_REFERENCE_PATH.is_file() else "")
            except APIConfigurationError:
                raise _ReferenceError("FRAMEWORK_REFERENCE_CONFIG_INVALID") from None
    else:
        value = str(reference_path)
    if not value.strip():
        return None
    if "\x00" in value or len(value) > 4096:
        raise _ReferenceError("FRAMEWORK_REFERENCE_INVALID")
    try:
        path = Path(value.strip()).expanduser()
    except (OSError, RuntimeError, ValueError):
        raise _ReferenceError("FRAMEWORK_REFERENCE_INVALID") from None
    return path if path.is_absolute() else PROJECT_ROOT / path


def _technical_terms(text: str) -> frozenset[str]:
    return frozenset(
        word for word in _WORD.findall(text)
        if word == word.upper() and word not in _GENERIC
        and any(character.isalpha() for character in word)
        and (len(word) >= 4 or "-" in word or any(char.isdigit() for char in word))
    )


def _parse_document(text: str, digest: str) -> _Document:
    lines = text.splitlines()
    title = next((m[2] for line in lines if (m := _HEADING.match(line)) and len(m[1]) == 1), "Framework reference")
    headings: list[tuple[int, str]] = []
    page = None
    sections: list[_Section] = []
    pending: list[tuple[int, str]] = []
    table_header = ""
    fenced = False
    truncated = False

    def emit(items: list[tuple[int, str]], *, header: str = "") -> None:
        nonlocal truncated
        if not items:
            return
        body = "\n".join(line for _, line in items).strip()
        if not body:
            return
        # A physical line may be arbitrarily large; keep each retrieval bounded.
        if len(body) > MAX_SECTION_CHARS:
            body = body[:MAX_SECTION_CHARS]
            truncated = True
        if header:
            body = header[:400] + "\n" + body
        if len(sections) >= MAX_DOCUMENT_SECTIONS:
            truncated = True
            return
        heading = " / ".join(value for _, value in headings[-3:])[:320] or title[:200]
        sections.append(_Section(heading, page, items[0][0], items[-1][0], body, _technical_terms(body)))

    for number, line in enumerate(lines, 1):
        marker = _PAGE.search(line)
        if marker:
            emit(pending)
            pending = []
            page = int(marker[1])
            table_header = ""
            continue
        heading = _HEADING.match(line) if not fenced else None
        if heading:
            emit(pending)
            pending = []
            depth = len(heading[1])
            headings = [(d, value) for d, value in headings if d < depth]
            headings.append((depth, heading[2]))
            table_header = ""
            continue
        if line.lstrip().startswith("```"):
            if not fenced:
                emit(pending)
                pending = []
            fenced = not fenced
            table_header = ""
            continue
        if not fenced and line.strip().startswith("|"):
            emit(pending)
            pending = []
            if _TABLE_DIVIDER.fullmatch(line):
                continue
            next_line = lines[number] if number < len(lines) else ""
            if _TABLE_DIVIDER.fullmatch(next_line) and "|" in next_line:
                table_header = line.strip()
            else:
                emit([(number, line)], header=table_header)
            continue
        if not line.strip() or line.strip() in {"---", "***"}:
            emit(pending)
            pending = []
            if not fenced:
                table_header = ""
            continue
        table_header = ""
        if sum(len(value) + 1 for _, value in pending) + len(line) > MAX_SECTION_CHARS:
            emit(pending)
            pending = []
        pending.append((number, line))
    emit(pending)
    if not sections:
        raise _ReferenceError("FRAMEWORK_REFERENCE_INVALID")
    # A numbered stage list and its adjacent role table often use the full
    # identifier in the list but only its number in the table. Resolve this
    # abbreviation only within the same heading and only when unambiguous.
    numbered: dict[tuple[str, str], set[str]] = defaultdict(set)
    for section in sections:
        for term in section.terms:
            match = _NUMBERED_NAME.fullmatch(term)
            if match:
                numbered[(section.heading, match[1])].add(term)
    for index, section in enumerate(sections):
        rows = [_NUMBERED_TABLE_ROW.match(line) for line in section.text.splitlines()]
        aliases: set[str] = set()
        for row in rows:
            if row:
                names = numbered.get((section.heading, row[1]), set())
                if len(names) == 1:
                    aliases.update(names)
        if aliases:
            sections[index] = _Section(section.heading, section.page, section.start_line,
                                       section.end_line, section.text, section.terms | aliases)
    terms = frozenset(term for section in sections for term in section.terms)
    return _Document(title[:200], digest, tuple(sections), terms, truncated)


def _load_document(reference_path: Path | str | None) -> _Document | None:
    path = _resolve_reference(reference_path)
    if path is None:
        return None
    try:
        stat = path.stat()
        if not path.is_file():
            raise _ReferenceError("FRAMEWORK_REFERENCE_UNREADABLE")
        if stat.st_size > MAX_REFERENCE_BYTES:
            raise _ReferenceError("FRAMEWORK_REFERENCE_TOO_LARGE")
        identity = (str(path.absolute()), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        with _CACHE_LOCK:
            cached = _CACHE.get(identity)
            if cached is not None:
                _CACHE.move_to_end(identity)
                return cached
        with path.open("rb") as handle:
            raw = handle.read(MAX_REFERENCE_BYTES + 1)
            opened_stat = os.fstat(handle.fileno())
        if len(raw) > MAX_REFERENCE_BYTES:
            raise _ReferenceError("FRAMEWORK_REFERENCE_TOO_LARGE")
        # Do not cache a reference that changed during the read.
        opened_identity = (str(path.absolute()), opened_stat.st_dev, opened_stat.st_ino, opened_stat.st_size,
                           opened_stat.st_mtime_ns, opened_stat.st_ctime_ns)
        if identity != opened_identity or len(raw) != opened_stat.st_size:
            raise _ReferenceError("FRAMEWORK_REFERENCE_CHANGED")
        text = raw.decode("utf-8-sig")
        if not text.strip() or "\x00" in text:
            raise _ReferenceError("FRAMEWORK_REFERENCE_INVALID")
        document = _parse_document(text, hashlib.sha256(raw).hexdigest())
    except (OSError, RuntimeError):
        raise _ReferenceError("FRAMEWORK_REFERENCE_UNREADABLE") from None
    except UnicodeError:
        raise _ReferenceError("FRAMEWORK_REFERENCE_INVALID") from None
    with _CACHE_LOCK:
        _CACHE[identity] = document
        while len(_CACHE) > 4:
            _CACHE.popitem(last=False)
    return document


def _summary(document: _Document | None, *, code: str | None = None) -> dict:
    return {
        "schema_version": "framework-context/v1",
        "status": "UNAVAILABLE" if code else ("LOADED" if document else "NOT_CONFIGURED"),
        "reason_code": code or ("FRAMEWORK_REFERENCE_READY" if document else "FRAMEWORK_REFERENCE_NOT_CONFIGURED"),
        "document": ({"title": document.title, "sha256": document.sha256,
                      "section_count": len(document.sections)} if document else None),
        "runtime_verified": False,
    }


def framework_status(reference_path: Path | str | None = None) -> dict:
    """Return safe local readiness metadata without paths or document content."""
    try:
        return _summary(_load_document(reference_path))
    except _ReferenceError as error:
        return _summary(None, code=error.code)


def _empty_context(summary: dict) -> dict:
    return {**summary, "references": [], "source_matches": [],
            "coverage": {"units_scanned": 0, "chars_scanned": 0, "truncated": False,
                         "max_units": MAX_SOURCE_UNITS, "max_chars": MAX_SOURCE_CHARS,
                         "source_scope": "selected_entry", "snapshot_id": None},
            "boundaries": [
                "Framework reference text is supplied knowledge, not independently verified runtime behavior.",
                "Vocabulary matches do not establish branch reachability, database results, or hidden program targets.",
                "Missing source or metadata limits the corresponding conclusion; visible source remains usable.",
            ]}


def _question_terms(question: str) -> set[str]:
    result = {word.casefold() for word in _WORD.findall(question[:4000])
              if word.upper() not in _GENERIC and any(character.isalpha() for character in word)}
    for phrase in _CJK.findall(question[:4000]):
        result.update(phrase[index:index + 2] for index in range(len(phrase) - 1))
    return result


def _sparse_source_candidates(connection, selected, next_entry, document, coverage, source_root,
                              check_cancel=None):
    """Match original source with bounded buffers, then retain verified line evidence.

    Sparse indexes deliberately omit ordinary statements. Their remaining units
    cannot stand in for the source vocabulary used by framework documents.
    """
    if source_root is None:
        return [], set(), "FRAMEWORK_SOURCE_ROOT_REQUIRED"
    root_input = Path(source_root).expanduser()
    if root_input.is_symlink():
        raise ValueError("SOURCE_PATH_INVALID")
    root = root_input.resolve(strict=True)
    metadata = dict(connection.execute("SELECT key,value FROM metadata"))
    if metadata.get("source_root_hash") != hashlib.sha256(str(root).encode()).hexdigest():
        raise ValueError("SOURCE_ROOT_MISMATCH")
    row = connection.execute(
        "SELECT relative_path,sha256,encoding,line_count,format_hint FROM source_files WHERE relative_path=?",
        (selected["relative_path"],),
    ).fetchone()
    if row is None:
        raise ValueError("SOURCE_INDEX_EMPTY")
    item = dict(row)
    candidates, spans, all_terms = [], [], set()
    per_term = defaultdict(int)
    source_format = active_format = item.get("format_hint") or "auto"
    coverage.update(source_scan_unit="physical_lines", source_scan_strategy="verified_stream",
                    source_hash_verified=False, max_units=None, max_chars=None,
                    max_line_chars=MAX_UNIT_CHARS, max_candidates=MAX_SOURCE_CANDIDATES)
    # Scan all selected-program lines with a bounded line buffer and bounded
    # retained matches. Still exhaust the file after the selected program ends:
    # no partial scan can establish the stored whole-file hash.
    for number, (raw, clipped) in enumerate(_verified_lines(root, item, check_cancel, MAX_UNIT_CHARS), 1):
        code, next_format, _ = _clean(raw, active_format)
        if source_format == "auto":
            active_format = next_format
        if number < selected["start_line"] or (next_entry is not None and number >= next_entry):
            continue
        coverage["units_scanned"] += 1
        inspected = code
        coverage["chars_scanned"] += len(inspected)
        if clipped:
            coverage["truncated"] = True
            # A clipped line cannot become a complete evidence span.
            continue
        terms = {word.upper() for word in _WORD.findall(inspected)} & document.terms
        if not terms:
            continue
        all_terms.update(terms)
        if not any(per_term[term] < 3 for term in terms):
            continue
        if len(candidates) >= MAX_SOURCE_CANDIDATES:
            coverage["truncated"] = True
            coverage["candidates_truncated"] = True
            continue
        for term in terms:
            per_term[term] += 1
        evidence_id = _stable_id("ev", item["relative_path"], item["sha256"], number, number)
        spans.append((evidence_id, item["relative_path"], number, number, item["sha256"], raw))
        candidates.append({"evidence_id": evidence_id, "relative_path": item["relative_path"],
                           "start_line": number, "end_line": number,
                           "program_name": selected["program_name"], "source_sha256": item["sha256"],
                           "matched_terms": sorted(terms)})
    coverage["source_hash_verified"] = True
    for span in spans:
        connection.execute("INSERT OR IGNORE INTO evidence_spans VALUES (?,?,?,?,?,?)", span)
        stored = connection.execute("SELECT evidence_id,relative_path,start_line,end_line,source_sha256,text "
                                    "FROM evidence_spans WHERE evidence_id=?", (span[0],)).fetchone()
        if tuple(stored) != span:
            raise ValueError("EVIDENCE_ID_CONFLICT")
    return candidates, all_terms, None


def _source_candidates(database_path: Path, entry_program: str | None, document: _Document,
                       coverage: dict, source_root=None, check_cancel=None) -> tuple[list[dict], set[str], str | None]:
    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        return [], set(), "FRAMEWORK_INDEX_UNAVAILABLE"
    candidates: list[dict] = []
    all_terms: set[str] = set()
    per_term: dict[str, int] = defaultdict(int)
    connection = None
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode={'rw' if source_root is not None else 'ro'}", uri=True)
        connection.row_factory = sqlite3.Row
        if source_root is None:
            connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN IMMEDIATE" if source_root is not None else "BEGIN")
        snapshot = connection.execute("SELECT value FROM metadata WHERE key = 'snapshot_id'").fetchone()
        coverage["snapshot_id"] = snapshot[0] if snapshot else None
        if not snapshot:
            return [], set(), "FRAMEWORK_INDEX_UNAVAILABLE"
        if entry_program:
            entry = str(entry_program).strip().replace("\\", "/")
            # Entry keys are emitted by the catalog to distinguish duplicates.
            parts = entry.rsplit("::", 2)
            if len(parts) == 3 and parts[2].isdigit():
                rows = connection.execute(
                    "SELECT program_name, relative_path, start_line FROM code_units WHERE unit_type = 'Program' "
                    "AND relative_path = ? AND program_name = ? AND start_line = ? LIMIT 2",
                    (parts[0], parts[1].upper(), int(parts[2])),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT program_name, relative_path, start_line FROM code_units WHERE unit_type = 'Program' "
                    "AND (program_name = ? OR relative_path = ?) LIMIT 2", (entry.upper(), entry),
                ).fetchall()
        else:
            rows = connection.execute(
                "SELECT program_name, relative_path, start_line FROM code_units WHERE unit_type = 'Program' LIMIT 2"
            ).fetchall()
        if len(rows) != 1:
            return [], set(), "FRAMEWORK_ENTRY_AMBIGUOUS" if rows else "FRAMEWORK_ENTRY_NOT_FOUND"
        selected = rows[0]
        next_entry = connection.execute(
            "SELECT MIN(start_line) FROM code_units WHERE unit_type = 'Program' AND relative_path = ? AND start_line > ?",
            (selected["relative_path"], selected["start_line"]),
        ).fetchone()[0]
        coverage["entry_program"] = selected["program_name"]
        coverage["entry_relative_path"] = selected["relative_path"]
        kind = connection.execute("SELECT value FROM metadata WHERE key='index_kind'").fetchone()
        if kind and kind[0] == "business_sparse":
            result = _sparse_source_candidates(connection, selected, next_entry, document, coverage,
                                               source_root, check_cancel)
            connection.commit()
            return result
        cursor = connection.execute(
            "SELECT u.evidence_id, u.relative_path, u.start_line, u.end_line, u.program_name, "
            "substr(u.normalized_text, 1, ?) AS normalized_text, length(u.normalized_text) AS text_length, "
            "e.source_sha256 FROM code_units u JOIN evidence_spans e ON e.evidence_id = u.evidence_id "
            "WHERE u.program_name = ? AND u.relative_path = ? AND u.unit_type != 'Copybook' "
            "AND u.start_line >= ? AND (? IS NULL OR u.start_line < ?) "
            "ORDER BY u.start_line, u.end_line, u.unit_id LIMIT ?",
            (MAX_UNIT_CHARS, selected["program_name"], selected["relative_path"], selected["start_line"],
             next_entry, next_entry, MAX_SOURCE_UNITS + 1),
        )
        for row in cursor:
            if coverage["units_scanned"] >= MAX_SOURCE_UNITS or coverage["chars_scanned"] >= MAX_SOURCE_CHARS:
                coverage["truncated"] = True
                break
            code = row["normalized_text"][:MAX_SOURCE_CHARS - coverage["chars_scanned"]]
            coverage["units_scanned"] += 1
            coverage["chars_scanned"] += len(code)
            if len(code) < row["text_length"]:
                coverage["truncated"] = True
            terms = {word.upper() for word in _WORD.findall(code)} & document.terms
            if not terms:
                continue
            all_terms.update(terms)
            if len(candidates) >= MAX_SOURCE_CANDIDATES or not any(per_term[term] < 3 for term in terms):
                continue
            for term in terms:
                per_term[term] += 1
            candidates.append({
                "evidence_id": row["evidence_id"], "relative_path": row["relative_path"],
                "start_line": row["start_line"], "end_line": row["end_line"],
                "program_name": row["program_name"], "source_sha256": row["source_sha256"],
                "matched_terms": sorted(terms),
            })
    except (OSError, sqlite3.Error, ValueError):
        return [], set(), "FRAMEWORK_INDEX_UNAVAILABLE"
    finally:
        if connection is not None:
            connection.close()
    return candidates, all_terms, None


def build_framework_context(database_path: Path | None = None, *, entry_program: str | None = None,
                            question: str = "", reference_path: Path | str | None = None,
                            source_root: Path | str | None = None, check_cancel=None) -> dict:
    """Retrieve cited knowledge; only indexed source markers can yield MATCHED."""
    try:
        document = _load_document(reference_path)
    except _ReferenceError as error:
        return _empty_context(_summary(None, code=error.code))
    result = _empty_context(_summary(document))
    if document is None:
        return result
    result["coverage"]["document_truncated"] = document.truncated
    candidates, source_terms, source_error = [], set(), None
    if database_path is not None:
        candidates, source_terms, source_error = _source_candidates(database_path, entry_program, document,
                                                                   result["coverage"], source_root, check_cancel)
    query_terms = _question_terms(question)
    technical_query_terms = {term.upper() for term in query_terms} & document.terms
    other_query_terms = query_terms - {term.casefold() for term in technical_query_terms}
    rankings = []
    for index, section in enumerate(document.sections):
        source_hits = section.terms & source_terms
        search_text = (section.heading + "\n" + section.text).casefold()
        technical_body_hits = technical_query_terms & section.terms
        technical_heading_hits = technical_query_terms & _technical_terms(section.heading)
        query_hits = {term for term in other_query_terms if term in search_text}
        query_hits.update(term.casefold() for term in technical_body_hits | technical_heading_hits)
        if source_hits or query_hits:
            # Specific table rows and short explanations take priority over a
            # large glossary that happens to repeat many vocabulary items.
            density = len(source_hits) / max(1, len(section.terms))
            score = ((100 if source_hits else 0) + min(4, len(source_hits)) * 8 + density * 12
                     + min(4, len(technical_body_hits)) * 80
                     + min(3, len(technical_heading_hits - technical_body_hits)) * 12
                     + min(8, len(query_hits)) * .25)
            rankings.append((score, index, source_hits, query_hits))
    remaining = MAX_REFERENCE_CHARS
    covered_terms: set[str] = set()
    covered_headings: set[str] = set()
    # Prefer newly observed constructs and topics before repeated glossary rows.
    while rankings and len(result["references"]) < MAX_REFERENCES:
        best = max(range(len(rankings)), key=lambda position: (
            rankings[position][0]
            + min(5, len(rankings[position][2] - covered_terms)) * 20
            + (12 if document.sections[rankings[position][1]].heading not in covered_headings else 0)
            - (15 if rankings[position][2] and not rankings[position][2] - covered_terms else 0),
            -rankings[position][1],
        ))
        _, index, source_hits, query_hits = rankings.pop(best)
        section = document.sections[index]
        if len(section.text) > remaining:
            continue
        remaining -= len(section.text)
        covered_terms.update(source_hits)
        covered_headings.add(section.heading)
        result["references"].append({
            "reference_id": f"fw:{document.sha256[:16]}:{section.start_line}-{section.end_line}",
            "heading": section.heading, "page": section.page,
            "start_line": section.start_line, "end_line": section.end_line, "text": section.text,
            "matched_terms": sorted(source_hits) if source_hits else sorted(query_hits),
            "selection_reason": "source_marker" if source_hits else "question_only",
        })
    seen_evidence: set[str] = set()
    # Order by reference relevance first, while retaining different constructs.
    for reference in result["references"]:
        if reference["selection_reason"] != "source_marker":
            continue
        for candidate in candidates:
            if candidate["evidence_id"] in seen_evidence or not set(candidate["matched_terms"]) & set(reference["matched_terms"]):
                continue
            if len(result["source_matches"]) >= MAX_SOURCE_MATCHES:
                break
            row = deepcopy(candidate)
            row["reference_ids"] = [item["reference_id"] for item in result["references"]
                                    if item["selection_reason"] == "source_marker"
                                    and set(item["matched_terms"]) & set(row["matched_terms"])]
            result["source_matches"].append(row)
            seen_evidence.add(row["evidence_id"])
    if source_error:
        result.update(status="LOADED", reason_code=source_error)
        result["boundaries"].append("The selected source entry could not be matched to the reference in this index.")
    elif result["source_matches"]:
        result.update(status="MATCHED", reason_code="FRAMEWORK_SOURCE_MATCHED")
    elif database_path is not None:
        result.update(status="NO_MATCH", reason_code="FRAMEWORK_SOURCE_NO_MATCH")
    if result["coverage"]["truncated"] or document.truncated:
        result["boundaries"].append("Retrieval limits were reached; absence of a match does not establish absence of framework use.")
    result["coverage"].update({"matched_term_count": len(source_terms), "references_selected": len(result["references"]),
                               "reference_chars": MAX_REFERENCE_CHARS - remaining,
                               "references_truncated": bool(rankings),
                               "source_matches_selected": len(result["source_matches"])})
    return result
