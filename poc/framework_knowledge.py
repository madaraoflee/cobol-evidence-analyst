"""Bounded local framework-reference retrieval over a selected source snapshot.

Document text is untrusted reference data, never executable configuration or a
substitute for source evidence. Matching names establishes a vocabulary link;
it does not prove control flow, external effects, or runtime behavior.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from copy import deepcopy
from dataclasses import dataclass, replace
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
MAX_REFERENCE_DOCUMENTS = 256
MAX_REFERENCE_COLLECTION_BYTES = 32 * 1024 * 1024
REFERENCE_EXTENSIONS = frozenset({".md", ".markdown", ".txt"})
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
    document_name: str = ""
    document_sha256: str = ""


@dataclass(frozen=True)
class _Document:
    title: str
    sha256: str
    sections: tuple[_Section, ...]
    terms: frozenset[str]
    truncated: bool
    documents: tuple[dict, ...] = ()
    warnings: tuple[dict, ...] = ()


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
    value = value.strip()
    # File Explorer's Copy as path includes surrounding quotes. Keep backslashes
    # literal: interpreting escapes would turn common Windows paths into tabs.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    if not value:
        return None
    if "\x00" in value or len(value) > 4096:
        raise _ReferenceError("FRAMEWORK_REFERENCE_INVALID")
    try:
        environment = {name.casefold(): item for name, item in os.environ.items()}
        value = re.sub(r"%([^%]+)%", lambda match: environment.get(match[1].casefold(), match[0]), value)
        path = Path(os.path.expandvars(value)).expanduser()
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


def _load_file(path: Path) -> _Document:
    try:
        stat = path.stat()
        if not path.is_file():
            raise _ReferenceError("FRAMEWORK_REFERENCE_UNREADABLE")
        if stat.st_size > MAX_REFERENCE_BYTES:
            raise _ReferenceError("FRAMEWORK_REFERENCE_TOO_LARGE")
        identity = (str(path.absolute()), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
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
                           opened_stat.st_mtime_ns)
        if identity != opened_identity or len(raw) != opened_stat.st_size:
            raise _ReferenceError("FRAMEWORK_REFERENCE_CHANGED")
        # Text exported by Windows editors can use a UTF-16 byte-order mark.
        encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        text = raw.decode(encoding)
        if not text.strip() or "\x00" in text:
            raise _ReferenceError("FRAMEWORK_REFERENCE_INVALID")
        document = _parse_document(text, hashlib.sha256(raw).hexdigest())
        document = replace(document, documents=({"name": path.name, "title": document.title,
                           "sha256": document.sha256, "section_count": len(document.sections),
                           "encoding": encoding},))
    except (OSError, RuntimeError):
        raise _ReferenceError("FRAMEWORK_REFERENCE_UNREADABLE") from None
    except UnicodeError:
        raise _ReferenceError("FRAMEWORK_REFERENCE_INVALID") from None
    with _CACHE_LOCK:
        _CACHE[identity] = document
        while len(_CACHE) > MAX_REFERENCE_DOCUMENTS:
            _CACHE.popitem(last=False)
    return document


def _load_document(reference_path: Path | str | None) -> _Document | None:
    path = _resolve_reference(reference_path)
    if path is None:
        return None
    try:
        if not path.is_dir():
            return _load_file(path)
        files = []
        warnings = []
        def unreadable_directory(_error):
            warnings.append({"reason_code": "FRAMEWORK_REFERENCE_UNREADABLE"})
        for base, directories, names in os.walk(path, onerror=unreadable_directory, followlinks=False):
            directories[:] = sorted(name for name in directories if not name.startswith("."))
            for name in sorted(names):
                candidate = Path(base) / name
                if candidate.suffix.casefold() in REFERENCE_EXTENSIONS:
                    files.append(candidate)
        if not files:
            raise _ReferenceError("FRAMEWORK_REFERENCE_DIRECTORY_EMPTY")
        sections, documents = [], []
        total_bytes = 0
        truncated = False
        for candidate in files[:MAX_REFERENCE_DOCUMENTS]:
            relative_name = candidate.relative_to(path).as_posix()
            try:
                total_bytes += candidate.stat().st_size
                if total_bytes > MAX_REFERENCE_COLLECTION_BYTES:
                    warnings.append({"reason_code": "FRAMEWORK_REFERENCE_COLLECTION_LIMIT"})
                    truncated = True
                    break
                document = _load_file(candidate)
            except (_ReferenceError, OSError) as error:
                warnings.append({"name": relative_name, "reason_code": getattr(error, "code", "FRAMEWORK_REFERENCE_UNREADABLE")})
                continue
            documents.append({**document.documents[0], "name": relative_name})
            sections.extend(replace(section, document_name=relative_name,
                                    document_sha256=document.sha256) for section in document.sections)
            truncated = truncated or document.truncated
        if len(files) > MAX_REFERENCE_DOCUMENTS:
            warnings.append({"reason_code": "FRAMEWORK_REFERENCE_COLLECTION_LIMIT",
                             "omitted_document_count": len(files) - MAX_REFERENCE_DOCUMENTS})
            truncated = True
        if not documents:
            raise _ReferenceError("FRAMEWORK_REFERENCE_DIRECTORY_NO_READABLE_DOCUMENTS")
        digest = hashlib.sha256("\n".join(item["name"] + ":" + item["sha256"] for item in documents).encode("utf-8")).hexdigest()
        return _Document(f"Framework references ({len(documents)})", digest, tuple(sections),
                         frozenset(term for section in sections for term in section.terms),
                         truncated, tuple(documents), tuple(warnings))
    except (OSError, RuntimeError):
        raise _ReferenceError("FRAMEWORK_REFERENCE_UNREADABLE") from None


_LOADING_MESSAGES = {
    "FRAMEWORK_REFERENCE_READY": "框架资料已加载，可用于业务问答。",
    "FRAMEWORK_REFERENCE_NOT_CONFIGURED": "尚未指定框架资料；可选择 Markdown 或文本文件，也可选择包含这些文件的文件夹。",
    "FRAMEWORK_REFERENCE_UNREADABLE": "无法读取指定位置。请确认这是当前电脑上的文件或文件夹，并具有读取权限。",
    "FRAMEWORK_REFERENCE_INVALID": "资料不是有效文本。请保存为 UTF-8，或带字节顺序标记的 UTF-16 文本。",
    "FRAMEWORK_REFERENCE_TOO_LARGE": "单份资料超过读取范围，请将资料按章节拆分到同一个文件夹。",
    "FRAMEWORK_REFERENCE_DIRECTORY_EMPTY": "文件夹内没有 Markdown 或文本资料；支持 .md、.markdown、.txt 及其子文件夹。",
    "FRAMEWORK_REFERENCE_DIRECTORY_NO_READABLE_DOCUMENTS": "已找到资料文件，但没有可读取的文本；请检查权限和文本编码。",
    "FRAMEWORK_REFERENCE_CONFIG_INVALID": "框架资料的本机配置无法读取，请检查配置文件格式。",
    "FRAMEWORK_REFERENCE_CHANGED": "资料在读取过程中发生变化，请保存文件后重试。",
}


def _summary(document: _Document | None, *, code: str | None = None) -> dict:
    reason = code or ("FRAMEWORK_REFERENCE_READY" if document else "FRAMEWORK_REFERENCE_NOT_CONFIGURED")
    return {
        "schema_version": "framework-context/v1",
        "status": "UNAVAILABLE" if code else ("LOADED" if document else "NOT_CONFIGURED"),
        "reason_code": reason,
        "loading_message": _LOADING_MESSAGES.get(reason, "框架资料暂时无法加载，请检查指定位置。"),
        "loaded_document_count": len(document.documents) if document else 0,
        "documents": list(document.documents) if document else [],
        "loading_warnings": list(document.warnings) if document else [],
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
    return {**summary, "references": [], "source_matches": [], "external_calls": [],
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
                              check_cancel=None, *, max_candidates=None, whole_file=False):
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
    retained_limit = MAX_SOURCE_CANDIDATES if max_candidates is None else max_candidates
    programs = list(connection.execute("SELECT start_line,program_name FROM code_units "
        "WHERE relative_path=? AND unit_type='Program' ORDER BY start_line", (item["relative_path"],))) if whole_file else []
    program_position = 0
    program_name = selected["program_name"]
    source_format = active_format = item.get("format_hint") or "auto"
    coverage.update(source_scan_unit="physical_lines", source_scan_strategy="verified_stream",
                    source_hash_verified=False, max_units=None, max_chars=None,
                    max_line_chars=MAX_UNIT_CHARS, max_candidates=retained_limit)
    # Scan all selected-program lines with a bounded line buffer and bounded
    # retained matches. Still exhaust the file after the selected program ends:
    # no partial scan can establish the stored whole-file hash.
    for number, (raw, clipped) in enumerate(_verified_lines(root, item, check_cancel, MAX_UNIT_CHARS), 1):
        code, next_format, _ = _clean(raw, active_format)
        if source_format == "auto":
            active_format = next_format
        if number < selected["start_line"] or (next_entry is not None and number >= next_entry):
            continue
        while program_position < len(programs) and programs[program_position]["start_line"] <= number:
            program_name = programs[program_position]["program_name"]
            program_position += 1
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
        if len(candidates) >= retained_limit:
            coverage["truncated"] = True
            coverage["candidates_truncated"] = True
            continue
        for term in terms:
            per_term[term] += 1
        evidence_id = _stable_id("ev", item["relative_path"], item["sha256"], number, number)
        spans.append((evidence_id, item["relative_path"], number, number, item["sha256"], raw))
        candidates.append({"evidence_id": evidence_id, "relative_path": item["relative_path"],
                           "start_line": number, "end_line": number,
                           "program_name": program_name, "source_sha256": item["sha256"],
                           "matched_terms": sorted(terms)})
    coverage["source_hash_verified"] = True
    for span in spans:
        connection.execute("INSERT OR IGNORE INTO evidence_spans VALUES (?,?,?,?,?,?)", span)
        stored = connection.execute("SELECT evidence_id,relative_path,start_line,end_line,source_sha256,text "
                                    "FROM evidence_spans WHERE evidence_id=?", (span[0],)).fetchone()
        if tuple(stored) != span:
            raise ValueError("EVIDENCE_ID_CONFLICT")
    return candidates, all_terms, None


def _repository_source_candidates(connection, document, coverage, source_root, source_paths,
                                  check_cancel):
    """Search selected files independently, retaining a fair share of each file.

    A partial or missing file only removes its own reference evidence. It does
    not discard reference matches from the other readable source files.
    """
    files = [row[0] for row in connection.execute("SELECT relative_path FROM source_files ORDER BY relative_path")]
    available = set(files)
    if source_paths is not None:
        requested = list(dict.fromkeys(str(value).replace("\\", "/").removeprefix("./") for value in source_paths))
        files = [value for value in requested if value in available]
        missing = [value for value in requested if value not in available]
        coverage.update(requested_files=len(requested), unavailable_file_count=len(missing))
    coverage.update(source_scope="selected_files" if source_paths is not None else "repository",
                    files_selected=len(files), files_scanned=0, files_with_matches=0,
                    source_scan_errors=[], source_hash_verified=False)
    candidates, all_terms = [], set()
    fair_share = max(1, MAX_SOURCE_CANDIDATES // max(1, len(files)))
    for relative in files:
        if check_cancel:
            check_cancel()
        local = {"units_scanned": 0, "chars_scanned": 0, "truncated": False}
        connection.execute("SAVEPOINT framework_file")
        try:
            if source_root is not None:
                selected = {"relative_path": relative, "start_line": 1, "program_name": None}
                matches, terms, error = _sparse_source_candidates(connection, selected, None, document,
                    local, source_root, check_cancel, max_candidates=fair_share, whole_file=True)
                if error:
                    raise ValueError(error)
            else:
                kind = connection.execute("SELECT value FROM metadata WHERE key='index_kind'").fetchone()
                if kind and kind[0] == "business_sparse":
                    raise ValueError("FRAMEWORK_SOURCE_ROOT_REQUIRED")
                matches, terms, per_term = [], set(), defaultdict(int)
                for row in connection.execute(
                    "SELECT u.evidence_id,u.relative_path,u.start_line,u.end_line,u.program_name,"
                    "substr(u.normalized_text,1,?) AS code,length(u.normalized_text) AS text_length,e.source_sha256 "
                    "FROM code_units u JOIN evidence_spans e ON e.evidence_id=u.evidence_id "
                    "WHERE u.relative_path=? ORDER BY u.start_line,u.end_line,u.unit_id", (MAX_UNIT_CHARS, relative)):
                    if check_cancel:
                        check_cancel()
                    local["units_scanned"] += 1
                    local["chars_scanned"] += len(row["code"])
                    local["truncated"] |= len(row["code"]) < row["text_length"]
                    hits = {word.upper() for word in _WORD.findall(row["code"])} & document.terms
                    terms.update(hits)
                    if not hits or not any(per_term[term] < 3 for term in hits):
                        continue
                    if len(matches) >= fair_share:
                        local.update(truncated=True, candidates_truncated=True)
                        continue
                    for term in hits:
                        per_term[term] += 1
                    matches.append({key: row[key] for key in ("evidence_id", "relative_path", "start_line",
                        "end_line", "program_name", "source_sha256")} | {"matched_terms": sorted(hits)})
            connection.execute("RELEASE framework_file")
        except (OSError, ValueError):
            connection.execute("ROLLBACK TO framework_file")
            connection.execute("RELEASE framework_file")
            coverage["source_scan_errors"].append({"relative_path": relative,
                                                  "reason_code": "FRAMEWORK_SOURCE_UNAVAILABLE"})
            continue
        coverage["files_scanned"] += 1
        coverage["files_with_matches"] += bool(terms)
        coverage["units_scanned"] += local["units_scanned"]
        coverage["chars_scanned"] += local["chars_scanned"]
        coverage["truncated"] |= local["truncated"]
        if local.get("candidates_truncated"):
            coverage["candidates_truncated"] = True
        all_terms.update(terms)
        capacity = max(0, MAX_SOURCE_CANDIDATES - len(candidates))
        candidates.extend(matches[:capacity])
        if len(matches) > capacity:
            coverage.update(truncated=True, candidates_truncated=True)
    coverage.update(source_scan_unit="physical_lines" if source_root is not None else "indexed_units",
                    source_scan_strategy="verified_stream" if source_root is not None else "snapshot_index",
                    source_hash_verified=source_root is not None and bool(files) and not coverage["source_scan_errors"],
                    max_units=None, max_chars=None, max_candidates=MAX_SOURCE_CANDIDATES)
    error = "FRAMEWORK_SOURCE_UNAVAILABLE" if files and not coverage["files_scanned"] else None
    return candidates, all_terms, error


def _source_candidates(database_path: Path, entry_program: str | None, document: _Document,
                       coverage: dict, source_root=None, check_cancel=None,
                       source_paths=None) -> tuple[list[dict], set[str], str | None]:
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
        if source_paths is not None or not entry_program:
            result = _repository_source_candidates(connection, document, coverage, source_root, source_paths, check_cancel)
            connection.commit()
            return result
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


def _external_call_context(database_path, source_matches, references):
    """Link absent targets to visible caller evidence without inventing a callee.

    Nearby markers are retrieval hints, not a data-flow or execution proof.
    They let a model explain a documented operation even when generated source
    was not supplied with the repository.
    """
    by_file = defaultdict(list)
    for match in source_matches:
        by_file[match["relative_path"]].append(match)
    known_references = {item["reference_id"] for item in references}
    calls = []
    connection = None
    try:
        connection = sqlite3.connect(Path(database_path).resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        for relative, matches in by_file.items():
            for row in connection.execute(
                "SELECT r.relation_type,r.target_name,r.evidence_id,u.program_name,e.start_line,e.end_line,"
                "e.source_sha256,substr(e.text,1,?) AS source_text,length(e.text) AS source_chars "
                "FROM relations r JOIN evidence_spans e ON e.evidence_id=r.evidence_id "
                "LEFT JOIN code_units u ON u.unit_id=r.from_entity_id "
                "WHERE r.relative_path=? AND (r.relation_type='CALL_TARGET_FROM' OR "
                "(r.relation_type='CALLS' AND NOT EXISTS (SELECT 1 FROM code_units p "
                "WHERE p.unit_type='Program' AND p.program_name=r.target_name))) ORDER BY e.start_line",
                (MAX_UNIT_CHARS, relative)):
                nearby = [match for match in matches if match["source_sha256"] == row["source_sha256"]
                          and match["program_name"] == row["program_name"]
                          and match["start_line"] <= row["end_line"] + 4
                          and match["end_line"] >= row["start_line"] - 12]
                reference_ids = sorted({ref for match in nearby for ref in match["reference_ids"]} & known_references)
                if not reference_ids:
                    continue
                calls.append({"relative_path": relative, "program_name": row["program_name"],
                    "target_name": row["target_name"], "relation_type": row["relation_type"],
                    "target_source_available": False if row["relation_type"] == "CALLS" else None,
                    "target_resolution": "source_not_supplied" if row["relation_type"] == "CALLS" else "dynamic_target",
                    "evidence_id": row["evidence_id"], "start_line": row["start_line"], "end_line": row["end_line"],
                    "source_sha256": row["source_sha256"], "source_text": row["source_text"],
                    "source_text_truncated": row["source_chars"] > MAX_UNIT_CHARS,
                    "reference_ids": reference_ids,
                    "nearby_marker_evidence_ids": [match["evidence_id"] for match in nearby],
                    "interpretation_basis": "caller_markers_and_documented_conventions",
                    "parameter_binding_verified": False, "runtime_verified": False})
    except (OSError, sqlite3.Error, ValueError):
        return []
    finally:
        if connection is not None:
            connection.close()
    # Preserve different caller files when the prompt contains many call sites.
    return _representative_rows(calls, MAX_SOURCE_MATCHES)


def _representative_rows(rows, limit):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["relative_path"]].append(row)
    selected = []
    depth = 0
    while len(selected) < limit:
        round_rows = [group[depth] for group in grouped.values() if len(group) > depth]
        if not round_rows:
            break
        selected.extend(round_rows[:limit - len(selected)])
        depth += 1
    return selected


def _retrieved_source_candidates(pages, document, coverage, check_cancel):
    """Use the caller's retrieved evidence without re-opening its source files."""
    candidates, source_terms, files = [], set(), set()
    for page in pages:
        if check_cancel:
            check_cancel()
        text = page.get("text", page.get("source_text", ""))
        if not isinstance(text, str):
            continue
        active_format = page.get("format_hint") or "auto"
        terms = set()
        for line in text.splitlines():
            cleaned, next_format, _ = _clean(line, active_format)
            if active_format == "auto":
                active_format = next_format
            coverage["units_scanned"] += 1
            coverage["chars_scanned"] += len(cleaned)
            terms.update(word.upper() for word in _WORD.findall(cleaned))
        hits = terms & document.terms
        relative = page.get("relative_path", page.get("path"))
        if relative:
            files.add(relative)
        if not hits:
            continue
        source_terms.update(hits)
        if not all(page.get(key) is not None for key in ("evidence_id", "start_line", "end_line", "source_sha256")) or not relative:
            continue
        candidates.append({key: page.get(key) for key in ("evidence_id", "start_line", "end_line", "source_sha256", "program_name")}
                          | {"relative_path": relative, "matched_terms": sorted(hits)})
    coverage.update(source_scope="retrieved_pages", files_selected=len(files),
                    pages_selected=len(pages), source_scan_strategy="provided_retrieval_pages",
                    source_verification="provided_retrieval_pages", source_hash_verified=False)
    return candidates, source_terms, None


def build_framework_context(database_path: Path | None = None, *, entry_program: str | None = None,
                            question: str = "", reference_path: Path | str | None = None,
                            source_root: Path | str | None = None, source_paths: list[str] | None = None,
                            source_pages: list[dict] | None = None, check_cancel=None) -> dict:
    """Retrieve cited knowledge from an entry, selected files, or the repository.

    ``source_paths`` takes precedence over ``entry_program``. With neither, all
    indexed files are searched. Only actual source markers can yield MATCHED;
    references and source availability are optional context, not answer gates.
    ``source_pages`` consumes already retrieved text and takes precedence over
    database/file access; it never scans the repository or re-verifies files.
    """
    try:
        document = _load_document(reference_path)
    except _ReferenceError as error:
        return _empty_context(_summary(None, code=error.code))
    result = _empty_context(_summary(document))
    if document is None:
        return result
    result["coverage"]["document_truncated"] = document.truncated
    candidates, source_terms, source_error = [], set(), None
    if source_pages is not None:
        candidates, source_terms, source_error = _retrieved_source_candidates(
            source_pages, document, result["coverage"], check_cancel)
    elif database_path is not None:
        candidates, source_terms, source_error = _source_candidates(database_path, entry_program, document,
                                                                   result["coverage"], source_root, check_cancel,
                                                                   source_paths)
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
    overview_selection = not rankings and bool(question.strip() or database_path is not None or source_pages is not None)
    if overview_selection:
        # Loaded documentation remains available even when user language and
        # framework vocabulary differ. This is background, not a source match.
        rankings = [(0, index, set(), set()) for index in range(len(document.sections))]
    reference_budget = min(MAX_REFERENCE_CHARS, 8000) if source_pages is not None else MAX_REFERENCE_CHARS
    remaining = reference_budget
    covered_terms: set[str] = set()
    covered_headings: set[str] = set()
    covered_files: set[str] = set()
    covered_documents: set[str] = set()
    term_files = defaultdict(set)
    for candidate in candidates:
        for term in candidate["matched_terms"]:
            term_files[term].add(candidate["relative_path"])
    def matching_files(terms):
        return {relative for term in terms for relative in term_files[term]}
    # Prefer newly observed constructs and topics before repeated glossary rows.
    omitted_sections = 0
    while rankings and len(result["references"]) < MAX_REFERENCES:
        best = max(range(len(rankings)), key=lambda position: (
            rankings[position][0]
            + min(3, len(matching_files(rankings[position][2]) - covered_files)) * 40
            + min(5, len(rankings[position][2] - covered_terms)) * 20
            + (12 if document.sections[rankings[position][1]].heading not in covered_headings else 0)
            + (16 if document.sections[rankings[position][1]].document_name not in covered_documents else 0)
            - (15 if rankings[position][2] and not rankings[position][2] - covered_terms else 0),
            -rankings[position][1],
        ))
        _, index, source_hits, query_hits = rankings.pop(best)
        section = document.sections[index]
        if len(section.text) > remaining:
            omitted_sections += 1
            continue
        remaining -= len(section.text)
        covered_terms.update(source_hits)
        covered_headings.add(section.heading)
        covered_documents.add(section.document_name)
        covered_files.update(matching_files(source_hits))
        reference_digest = section.document_sha256 or document.sha256
        name_key = hashlib.sha256(section.document_name.encode()).hexdigest()[:8] + ":" if section.document_name else ""
        result["references"].append({
            "reference_id": f"fw:{reference_digest[:16]}:{name_key}{section.start_line}-{section.end_line}",
            "heading": section.heading, "page": section.page,
            "start_line": section.start_line, "end_line": section.end_line, "text": section.text,
            "document_name": section.document_name or document.documents[0]["name"],
            "document_sha256": reference_digest,
            "matched_terms": sorted(source_hits) if source_hits else sorted(query_hits),
            "selection_reason": "document_overview" if overview_selection else ("source_marker" if source_hits else "question_only"),
        })
    seen_evidence: set[str] = set()
    eligible_matches = []
    # Order by reference relevance first, while retaining different constructs.
    for reference in result["references"]:
        if reference["selection_reason"] != "source_marker":
            continue
        for candidate in candidates:
            if candidate["evidence_id"] in seen_evidence or not set(candidate["matched_terms"]) & set(reference["matched_terms"]):
                continue
            row = deepcopy(candidate)
            row["reference_ids"] = [item["reference_id"] for item in result["references"]
                                    if item["selection_reason"] == "source_marker"
                                    and set(item["matched_terms"]) & set(row["matched_terms"])]
            eligible_matches.append(row)
            seen_evidence.add(row["evidence_id"])
    result["source_matches"] = _representative_rows(eligible_matches, MAX_SOURCE_MATCHES)
    if source_pages is None and database_path is not None and result["source_matches"]:
        result["external_calls"] = _external_call_context(database_path, result["source_matches"], result["references"])
    if source_error:
        result.update(status="LOADED", reason_code=source_error)
        result["boundaries"].append("Some selected source could not be matched to the reference; other business evidence remains usable.")
    elif result["source_matches"]:
        result.update(status="MATCHED", reason_code="FRAMEWORK_SOURCE_MATCHED")
    elif database_path is not None or source_pages is not None:
        result.update(status="NO_MATCH", reason_code="FRAMEWORK_SOURCE_NO_MATCH")
    if result["coverage"]["truncated"] or document.truncated:
        result["boundaries"].append("Retrieval limits were reached; absence of a match does not establish absence of framework use.")
    result["coverage"].update({"matched_term_count": len(source_terms), "references_selected": len(result["references"]),
                               "reference_chars": reference_budget - remaining,
                               "reference_selection": "document_overview" if overview_selection else "relevant_sections",
                               "references_truncated": bool(rankings) or bool(omitted_sections),
                               "source_matches_selected": len(result["source_matches"]),
                               "source_matches_truncated": len(eligible_matches) > len(result["source_matches"]),
                               "external_calls_selected": len(result["external_calls"])})
    return result
