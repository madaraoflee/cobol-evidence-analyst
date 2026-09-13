"""Read-only, bounded COPY inclusion with physical-source provenance.

The result is an inspection artifact, not compiler output. Only standalone,
unmodified COPY statements are expanded. Library search order, replacement,
conditional compilation and continuation semantics require a separate contract.
An unresolved inclusion stays visible and makes the result incomplete.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable

from repo_inventory import DEFAULT_EXTENSIONS, decode_source, iter_source_files, sha256_bytes


_WORD = r"[A-Z0-9_$#@-]+"
_MEMBER = rf"{_WORD}(?:\.{_WORD})*"
_PROGRAM = re.compile(rf"\bPROGRAM-ID\s*\.\s*({_WORD})(?=\s|\.|$)", re.I)
_COPY_WORD = re.compile(r"(?<![A-Z0-9_$#@-])COPY(?![A-Z0-9_$#@-])", re.I)
_REPLACE_WORD = re.compile(r"(?<![A-Z0-9_$#@-])REPLACE(?![A-Z0-9_$#@-])", re.I)
_COPY = re.compile(rf"\s*COPY\s+(?:({_MEMBER})|'({_MEMBER})'|\"({_MEMBER})\")\s*\.\s*", re.I)
_COPY_HEAD = re.compile(rf"COPY\s+(?:({_MEMBER})|'({_MEMBER})'|\"({_MEMBER})\")", re.I)


@dataclass(frozen=True)
class _Source:
    relative_path: str
    source_hash: str
    lines: tuple[str, ...]
    code: tuple[str, ...]
    masked: tuple[str, ...]
    problems: tuple[tuple[int, str], ...]
    programs: tuple[str, ...]


def _lex_line(raw: str) -> tuple[str, str, str | None]:
    """Recognize comments without interpreting comment markers in literals."""
    line = raw.expandtabs(8)
    problem = None
    fixed = len(line) >= 7 and (
        line[:6].isdigit()
        or (not line[:6].strip() and line[6] in " */-Dd")
    )
    if fixed:
        indicator = line[6]
        if indicator in "*/":
            return "", "", None
        if indicator in "-Dd":
            problem = "source_continuation_not_supported" if indicator == "-" else "debug_source_not_supported"
        elif indicator != " ":
            problem = "fixed_indicator_not_supported"
        line = line[7:72]
    elif line.lstrip().startswith(("*>", "*")):
        return "", "", None

    code: list[str] = []
    masked: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            code.append(char)
            masked.append(" ")
            if char == quote:
                if index + 1 < len(line) and line[index + 1] == quote:
                    code.append(line[index + 1])
                    masked.append(" ")
                    index += 1
                else:
                    quote = None
        elif line[index:index + 2] == "*>":
            break
        elif char in "'\"":
            quote = char
            code.append(char)
            masked.append(" ")
        else:
            code.append(char)
            masked.append(char)
        index += 1
    if quote:
        problem = problem or "source_continuation_not_supported"
    normalized = "".join(code).rstrip()
    hidden = "".join(masked).rstrip()
    if re.match(r"\s*(?:>>|\$|CBL\b|PROCESS\b)", hidden, re.I):
        problem = problem or "source_directive_not_supported"
    if _REPLACE_WORD.search(hidden):
        problem = problem or "replace_statement_not_supported"
    return normalized, hidden, problem


def _extensions(values: Iterable[str] | str | None) -> frozenset[str]:
    if values is None:
        return DEFAULT_EXTENSIONS
    if isinstance(values, str):
        values = values.split(",")
    try:
        values = iter(values)
    except TypeError as error:
        raise ValueError("Source extensions must be strings or an iterable of strings.") from error
    result: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("Source extensions must be strings.")
        item = value.strip().lower()
        if not item or not re.fullmatch(r"\.?[a-z0-9_-]+", item):
            raise ValueError("Source extensions must be non-empty suffixes without paths or wildcards.")
        result.add(item if item.startswith(".") else "." + item)
    if not result:
        raise ValueError("At least one source extension is required.")
    return frozenset(result)


def expand_program(
    source_root: Path,
    entry_program: str,
    *,
    extensions: Iterable[str] | str | None = None,
    max_depth: int = 8,
    max_lines: int = 20_000,
) -> dict[str, object]:
    """Expand one uniquely identified host file, without writing any source.

    ``max_depth`` counts inclusion sites, not the host. Extensionless members
    are always discoverable; other suffixes follow ``extensions``. COPY lookup
    uses a case-insensitive basename or explicit filename and deliberately does
    not guess library/directory precedence. Invalid invocation raises ValueError;
    missing or unsupported source yields a bounded JSON-serializable report.
    Each emitted line retains original physical text, plus ``code`` for inspection.
    ``source_files`` fingerprints the full decodable discovery scope. Inclusion
    count is also capped at ``max_lines`` so empty members cannot evade the budget.
    """
    root = Path(source_root)
    if not root.is_dir():
        raise ValueError("Source root must be an existing directory.")
    if not isinstance(entry_program, str) or not re.fullmatch(_WORD, entry_program, re.I):
        raise ValueError("Entry program must be an exact unquoted PROGRAM-ID.")
    if type(max_depth) is not int or not 0 <= max_depth <= 64:
        raise ValueError("max_depth must be an integer between zero and 64.")
    if type(max_lines) is not int or max_lines < 1:
        raise ValueError("max_lines must be a positive integer.")
    suffixes = _extensions(extensions)
    entry = entry_program.upper()
    sources: dict[str, _Source] = {}
    members: dict[str, list[str]] = defaultdict(list)
    entries: list[str] = []
    boundaries: list[dict[str, object]] = []
    emitted: list[dict[str, object]] = []
    includes: list[dict[str, object]] = []
    exhausted = False

    for path in iter_source_files(root, suffixes, True):
        relative = path.relative_to(root).as_posix()
        try:
            raw = path.read_bytes()
        except OSError:
            boundaries.append({"reason": "source_read_error", "relative_path": relative, "include_chain": []})
            continue
        digest = sha256_bytes(raw)
        decoded = decode_source(raw)
        if decoded is None:
            boundaries.append({"reason": "source_not_text", "relative_path": relative,
                               "source_hash": digest, "include_chain": []})
            continue
        lines = tuple(decoded.text.splitlines())
        lexical = tuple(_lex_line(line) for line in lines)
        code = tuple(item[0] for item in lexical)
        masked = tuple(item[1] for item in lexical)
        problems = tuple((index, item[2]) for index, item in enumerate(lexical, 1) if item[2])
        if decoded.used_fallback:
            problems += ((1, "source_encoding_uncertain"),)
        programs = tuple(name.upper() for name in _PROGRAM.findall("\n".join(masked)))
        source = _Source(relative, digest, lines, code, masked, problems, programs)
        sources[relative] = source
        # A set prevents a suffixless member from becoming ambiguous with itself.
        for key in {path.stem.upper(), path.name.upper()}:
            members[key].append(relative)
        entries.extend(relative for name in programs if name == entry)

    def origin(source: _Source, line: int) -> dict[str, object]:
        return {"relative_path": source.relative_path, "line": line, "source_hash": source.source_hash}

    def boundary(reason: str, source: _Source, line: int, chain: list[dict[str, object]], **extra: object) -> None:
        boundaries.append({"reason": reason, "origin": origin(source, line),
                           "include_chain": list(chain), **extra})

    def emit(source: _Source, index: int, chain: list[dict[str, object]]) -> bool:
        nonlocal exhausted
        if len(emitted) >= max_lines:
            if not exhausted:
                boundary("copy_line_limit", source, index + 1, chain, max_lines=max_lines)
            exhausted = True
            return False
        emitted.append({"text": source.lines[index], "code": source.code[index],
                        "origin": origin(source, index + 1), "include_chain": list(chain)})
        return True

    def declaration(source: _Source, index: int, chain: list[dict[str, object]]) -> dict[str, object] | None:
        nonlocal exhausted
        keyword = _COPY_WORD.search(source.masked[index])
        head = _COPY_HEAD.match(source.code[index], keyword.start()) if keyword else None
        if head is None:
            return None
        if len(includes) >= max_lines:
            if not exhausted:
                boundary("copy_inclusion_limit", source, index + 1, chain, max_inclusions=max_lines)
            exhausted = True
            return None
        # A trailing sentence period may have been consumed by a loose header
        # match. The strict standalone form still decides whether to expand.
        name = next(group for group in head.groups() if group is not None).upper().rstrip(".")
        item = {"copy_name": name, "origin": origin(source, index + 1),
                "include_chain": list(chain), "status": "bounded"}
        includes.append(item)
        return item

    def walk(source: _Source, chain: list[dict[str, object]], ancestors: tuple[str, ...]) -> None:
        if source.problems:
            for line, reason in source.problems:
                boundary(reason, source, line, chain)
            # Replacement/directive state may affect subsequent inclusions. Keep
            # the entire original file visible, without pretending to apply it.
            for index in range(len(source.lines)):
                declaration(source, index, chain)
                if exhausted:
                    return
                if not emit(source, index, chain):
                    return
            return
        for index, hidden in enumerate(source.masked):
            if exhausted:
                return
            if not _COPY_WORD.search(hidden):
                if not emit(source, index, chain):
                    return
                continue
            include = declaration(source, index, chain)
            if exhausted:
                return
            match = _COPY.fullmatch(source.code[index])
            if match is None:
                boundary("copy_form_not_supported", source, index + 1, chain)
                if not emit(source, index, chain):
                    return
                continue
            name = next(group for group in match.groups() if group is not None).upper()
            site = {**origin(source, index + 1), "copy_name": name}
            nested = [*chain, site]
            candidates = members.get(name, [])
            reason = None
            if not candidates:
                reason = "copy_target_not_found"
            elif len(candidates) != 1:
                reason = "copy_target_ambiguous"
            elif candidates[0] in ancestors:
                reason = "copy_cycle"
            elif len(nested) > max_depth:
                reason = "copy_depth_limit"
            elif sources[candidates[0]].programs:
                reason = "copy_target_contains_program"
            if reason:
                boundary(reason, source, index + 1, chain, copy_name=name, candidates=candidates)
                if not emit(source, index, chain):
                    return
                continue
            target = sources[candidates[0]]
            if include is not None:
                include["resolved_path"] = target.relative_path
            if len(emitted) >= max_lines and target.lines:
                emit(source, index, chain)
                return
            before_boundaries = len(boundaries)
            walk(target, nested, (*ancestors, target.relative_path))
            if include is not None and len(boundaries) == before_boundaries:
                include["status"] = "expanded"

    program_source = None
    if len(entries) != 1:
        boundaries.append({"reason": "entry_program_not_found" if not entries else "entry_program_ambiguous",
                           "entry_program": entry, "candidates": sorted(entries), "include_chain": []})
    else:
        program_source = entries[0]
        host = sources[program_source]
        if len(host.programs) != 1:
            boundary("multiple_programs_in_source_not_supported", host, 1, [])
            for index in range(len(host.lines)):
                declaration(host, index, [])
                if exhausted:
                    break
                if not emit(host, index, []):
                    break
        else:
            walk(host, [], (host.relative_path,))

    complete = not boundaries and program_source is not None
    return {"entry_program": entry, "program_source": program_source,
            "status": "expanded" if complete else "bounded",
            "source_files": [{"relative_path": path, "source_hash": source.source_hash}
                             for path, source in sorted(sources.items())],
            "lines": emitted, "includes": includes, "boundaries": boundaries,
            "source_expansion_complete": complete, "compiler_equivalent": False}
