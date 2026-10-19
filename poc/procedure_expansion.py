"""Read-only, bounded COPY inclusion with physical-source provenance.

The result is an inspection artifact, not compiler output. Controlled COPY
replacement and compile conditions require explicit compilation inputs. Unknown
library precedence or conditions remain candidates and make the result bounded.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
import re
from typing import Iterable

from repo_inventory import DEFAULT_EXTENSIONS, decode_source, iter_source_files, sha256_bytes
from compilation_context import resolve_compilation_context
from statement_facts import (CobolLexError, clean_cobol_line, cobol_code_only,
                             join_continuation, lex_cobol, compile_condition_value, program_identifiers)


_WORD = r"[A-Z0-9_$#@-]+"
_MEMBER = rf"{_WORD}(?:\.{_WORD})*"
_PROGRAM = re.compile(rf"\bPROGRAM-ID\s*\.\s*({_WORD})(?=\s|\.|$)", re.I)
_COPY_WORD = re.compile(r"(?<![A-Z0-9_$#@-])COPY(?![A-Z0-9_$#@-])", re.I)
_REPLACE_WORD = re.compile(r"(?<![A-Z0-9_$#@-])REPLACE(?![A-Z0-9_$#@-])", re.I)
_COPY = re.compile(rf"\s*COPY\s+(?:({_MEMBER})|'({_MEMBER})'|\"({_MEMBER})\")\s*\.\s*", re.I)
_COPY_HEAD = re.compile(rf"COPY\s+(?:({_MEMBER})|'({_MEMBER})'|\"({_MEMBER})\")", re.I)
LEXICAL_SOURCE_VERSION = "copy-physical-tokens-v1"


@dataclass(frozen=True)
class _Source:
    relative_path: str
    source_hash: str
    lines: tuple[str, ...]
    code: tuple[str, ...]
    masked: tuple[str, ...]
    problems: tuple[tuple[int, str], ...]
    programs: tuple[str, ...]
    line_ends: tuple[int, ...]


def _lex_line(raw: str, context=None) -> tuple[str, str, str | None]:
    """Recognize comments without interpreting comment markers in literals."""
    context = resolve_compilation_context(context)
    line = raw.expandtabs(8)
    problem = None
    fixed = context.source_format == "fixed" or context.source_format == "auto" and len(line) >= 7 and (
        line[:6].isdigit()
        or (not line[:6].strip() and line[6] in " */-Dd")
    )
    if fixed:
        indicator = line[6]
        if indicator in "*/":
            return "", "", None
        if indicator in "-Dd":
            problem = "source_continuation_not_supported" if indicator == "-" else "debug_source_not_supported"
            if indicator in "Dd" and context.debug_lines is False:
                return "", "", None
            if indicator in "Dd" and context.debug_lines is True:
                problem = None
        elif indicator != " ":
            problem = "fixed_indicator_not_supported"
    normalized, _, _ = clean_cobol_line(raw, context.source_format)
    hidden = cobol_code_only(normalized).rstrip()
    if normalized:
        try:
            lex_cobol(normalized)
        except CobolLexError as error:
            if error.reason in {"unterminated_literal", "continued_literal_not_supported"}:
                problem = problem or "source_continuation_not_supported"
    if re.match(r"\s*(?:>>|\$|CBL\b|PROCESS\b)", hidden, re.I) and not re.match(
            r"\s*>>\s*(?:IF\b|ELSE\s*$|END-IF\s*$)", hidden, re.I):
        problem = problem or "source_directive_not_supported"
    if _REPLACE_WORD.search(hidden):
        problem = problem or "replace_statement_not_supported"
    return normalized, hidden, problem


def _parse_copy(text):
    match = re.fullmatch(rf"\s*COPY\s+(?:({_MEMBER})|'({_MEMBER})'|\"({_MEMBER})\")"
        rf"(?:\s+(?:IN|OF)\s+({_MEMBER}))?(?:\s+REPLACING\s+(.+?))?\s*\.\s*", text, re.I)
    if not match:
        return None
    name = next(value for value in match.groups()[:3] if value).upper().rstrip(".")
    pairs, remaining = [], match[5] or ""
    atom = r"(?:==.*?==|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|[A-Z0-9_$#@-]+)"
    while remaining.strip():
        pair = re.match(rf"\s*({atom})\s+BY\s+({atom})\s*", remaining, re.I)
        if not pair:
            return None
        before, after = (value[2:-2] if value.startswith("==") else value for value in pair.groups())
        if not before.strip() or "\n" in after:
            return None
        pairs.append((before, after))
        remaining = remaining[pair.end():]
    return name, match[4].upper() if match[4] else None, tuple(pairs)


def _replace_tokens(text, pairs, site):
    traces, matches = [], []
    try:
        tokens = lex_cobol(text)
        patterns = [(before, after, lex_cobol(before)) for before, after in pairs]
    except CobolLexError:
        return text, traces, "copy_replacement_lexical_unsupported"
    index = 0
    while index < len(tokens):
        for before, after, pattern in patterns:
            if not pattern:
                return text, traces, "copy_replacement_empty_pattern"
            candidate = tokens[index:index + len(pattern)]
            if len(candidate) == len(pattern) and all((left.kind, left.value) == (right.kind, right.value)
                    for left, right in zip(candidate, pattern)):
                matches.append((candidate[0].start, candidate[-1].end, before, after))
                index += len(pattern)
                break
        else:
            index += 1
    for start, end, before, after in reversed(matches):
        text = text[:start] + after + text[end:]
        traces.append({"from": before, "to": after, "start_column": start + 1,
                       "end_column": end + 1, "copy_site": site})
    return text, traces, None


def _condition_value(expression, context):
    return compile_condition_value(expression, context)


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
    source_provider=None,
    source_catalog=None,
    entry_relative_path: str | None = None,
    check_cancel=None,
    compilation_context=None,
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
    context = resolve_compilation_context(compilation_context)
    lexical_identity = (LEXICAL_SOURCE_VERSION, context.fingerprint)
    entry = entry_program.upper()
    sources: dict[str, _Source] = {}
    members: dict[str, list[str]] = defaultdict(list)
    entries: list[str] = []
    boundaries: list[dict[str, object]] = []
    emitted: list[dict[str, object]] = []
    includes: list[dict[str, object]] = []
    exhausted = False

    def load(relative: str) -> None:
        if check_cancel:
            check_cancel()
        if relative in sources:
            return
        cache_key = None
        cache_get = getattr(source_provider, "cached_expansion_source", None)
        cache_put = getattr(source_provider, "cache_expansion_source", None)
        try:
            if source_provider is not None:
                captured = source_provider.capture(relative)
                if callable(cache_get) and callable(cache_put) and isinstance(getattr(captured, "sha256", None), str):
                    cache_key = (relative, captured.sha256, captured.encoding, *lexical_identity)
                cached = cache_get(cache_key) if cache_key is not None else None
                if isinstance(cached, _Source):
                    sources[relative] = cached
                    entries.extend(relative for name in cached.programs if name == entry)
                    return
                raw = captured.path.read_bytes()
                decoded = decode_source(raw, captured.encoding)
            else:
                raw = (root / relative).read_bytes()
                decoded = decode_source(raw, context.encoding)
        except OSError:
            boundaries.append({"reason": "source_read_error", "relative_path": relative, "include_chain": []})
            return
        digest = sha256_bytes(raw)
        if decoded is None:
            boundaries.append({"reason": "source_not_text", "relative_path": relative,
                               "source_hash": digest, "include_chain": []})
            return
        lines = tuple(decoded.text.splitlines())
        lexical, lexical_context = [], context
        for index, line in enumerate(lines):
            if check_cancel and index % 256 == 0:
                check_cancel()
            lexical.append(_lex_line(line, lexical_context))
            _, active_format, _ = clean_cobol_line(line, lexical_context.source_format)
            lexical_context = replace(lexical_context, source_format=active_format)
        line_ends = list(range(1, len(lines) + 1))
        logical_head = None
        for index, raw_line in enumerate(lines):
            fixed_continuation = len(raw_line) >= 7 and raw_line[6] == "-" and (
                context.source_format == "fixed" or raw_line[:6].isdigit() or not raw_line[:6].strip())
            if fixed_continuation and logical_head is not None and lexical[logical_head][0] and not lexical[logical_head][0].rstrip().endswith("."):
                joined = join_continuation(lexical[logical_head][0], lexical[index][0])
                issue = None
                try:
                    lex_cobol(joined)
                except CobolLexError as error:
                    if error.reason != "unterminated_literal":
                        continue
                    issue = "source_continuation_not_supported"
                lexical[logical_head] = (joined, cobol_code_only(joined), issue)
                lexical[index] = ("", "", None)
                line_ends[logical_head] = index + 1
            elif not fixed_continuation and lexical[index][0]:
                logical_head = index
        lexical = tuple(lexical)
        code = tuple(item[0] for item in lexical)
        masked = tuple(item[1] for item in lexical)
        problems = tuple((index, item[2]) for index, item in enumerate(lexical, 1) if item[2])
        if decoded.used_fallback:
            problems += ((1, "source_encoding_uncertain"),)
        programs = tuple(name for name, _, _ in program_identifiers("\n".join(code)))
        source = _Source(relative, digest, lines, code, masked, problems, programs, tuple(line_ends))
        if cache_key is not None and digest == cache_key[1] and callable(cache_put):
            cache_put(cache_key, source)
        sources[relative] = source
        entries.extend(relative for name in programs if name == entry)

    if source_provider is None:
        for path in iter_source_files(root, suffixes, True):
            relative = path.relative_to(root).as_posix()
            load(relative)
            # A set prevents a suffixless member from becoming ambiguous with itself.
            for key in {path.stem.upper(), path.name.upper()}:
                members[key].append(relative)
    else:
        if source_catalog is None or entry_relative_path is None:
            raise ValueError("Explicit source catalog and entry path required")
        for relative in source_catalog:
            if check_cancel:
                check_cancel()
            path = Path(relative)
            for key in {path.stem.upper(), path.name.upper()}:
                members[key].append(relative)
        if entry_relative_path not in source_catalog:
            raise ValueError("Entry path absent from source catalog")
        load(entry_relative_path)

    def origin(source: _Source, line: int) -> dict[str, object]:
        result = {"relative_path": source.relative_path, "line": line, "source_hash": source.source_hash}
        if source.line_ends[line - 1] != line:
            result["end_line"] = source.line_ends[line - 1]
        return result

    def boundary(reason: str, source: _Source, line: int, chain: list[dict[str, object]], **extra: object) -> None:
        boundaries.append({"reason": reason, "origin": origin(source, line),
                           "include_chain": list(chain), **extra})

    def emit(source: _Source, index: int, chain: list[dict[str, object]], *, code=None, replacement_trace=(), condition_candidate=False) -> bool:
        nonlocal exhausted
        if check_cancel and len(emitted) % 256 == 0:
            check_cancel()
        if len(emitted) >= max_lines:
            if not exhausted:
                boundary("copy_line_limit", source, index + 1, chain, max_lines=max_lines)
            exhausted = True
            return False
        emitted.append({"text": source.lines[index], "code": source.code[index] if code is None else code,
                        "origin": origin(source, index + 1), "include_chain": list(chain),
                        "replacement_trace": list(replacement_trace), "condition_candidate": condition_candidate})
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

    def walk(source: _Source, chain: list[dict[str, object]], ancestors: tuple[str, ...], replacements=()) -> None:
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
        conditions = []
        for index, hidden in enumerate(source.masked):
            if exhausted:
                return
            directive = re.match(r"\s*>>\s*(IF\b(.*)|ELSE\s*$|END-IF\s*$)", hidden, re.I)
            if directive:
                kind = directive[1].strip().upper()
                if kind.startswith("IF"):
                    setting = _condition_value(directive[2], context)
                    conditions.append(setting)
                    if setting is None:
                        boundary("compile_condition_unknown", source, index + 1, chain, expression=directive[2].strip())
                elif not conditions:
                    boundary("compile_condition_scope_invalid", source, index + 1, chain)
                elif kind == "ELSE":
                    conditions[-1] = not conditions[-1] if conditions[-1] is not None else None
                else:
                    conditions.pop()
                if not emit(source, index, chain, code=""):
                    return
                continue
            if False in conditions:
                if not emit(source, index, chain, code=""):
                    return
                continue
            code, traces = source.code[index], []
            for pairs, replacement_site in replacements:
                code, changes, issue = _replace_tokens(code, pairs, replacement_site)
                traces.extend(changes)
                if issue:
                    boundary(issue, source, index + 1, chain)
            hidden = cobol_code_only(code)
            if not _COPY_WORD.search(hidden):
                if not emit(source, index, chain, code=code, replacement_trace=traces, condition_candidate=None in conditions):
                    return
                continue
            include = declaration(source, index, chain)
            if exhausted:
                return
            if None in conditions:
                if include is not None:
                    include["status"] = "condition_candidate"
                    include["candidates"] = list(members.get(include["copy_name"], []))
                if not emit(source, index, chain, code=code, replacement_trace=traces, condition_candidate=True):
                    return
                continue
            match = _parse_copy(code)
            if match is None:
                boundary("copy_form_not_supported", source, index + 1, chain)
                if not emit(source, index, chain):
                    return
                continue
            name, library, replacing = match
            try:
                multiword = any(len(lex_cobol(before)) != 1 for before, _ in replacing)
            except CobolLexError:
                multiword = True
            if multiword or replacing and replacements or replacements and context.nested_copy_replacing is not True:
                reason = ("copy_multiword_replacement_requires_full_preprocessor" if multiword else
                          "nested_copy_replacement_conflict" if replacing and replacements else
                          "nested_copy_replacing_configuration_unknown" if context.nested_copy_replacing is None else
                          "nested_copy_replacing_not_supported_by_profile")
                boundary(reason, source, index + 1, chain, copy_name=name)
                if not emit(source, index, chain, code=code, replacement_trace=traces):
                    return
                continue
            site = {**origin(source, index + 1), "copy_name": name}
            nested = [*chain, site]
            candidates = list(members.get(name, []))
            all_candidates = list(candidates)
            if context.copy_library_order_known:
                libraries = [path for path in context.copy_libraries
                             if not library or Path(path).name.upper() == library or path.upper() == library]
                ranked = [[path for path in candidates if Path(path).parent.as_posix() == directory.rstrip("/")]
                          for directory in libraries]
                candidates = next((paths for paths in ranked if paths), [])
            elif library:
                boundary("copy_library_configuration_unknown", source, index + 1, chain,
                         copy_name=name, library=library, candidates=all_candidates)
                if not emit(source, index, chain, code=code, replacement_trace=traces):
                    return
                continue
            reason = None
            if not candidates:
                reason = "copy_target_not_found"
            elif len(candidates) != 1:
                reason = "copy_target_ambiguous"
            elif candidates[0] in ancestors:
                reason = "copy_cycle"
            elif len(nested) > max_depth:
                reason = "copy_depth_limit"
            else:
                load(candidates[0])
            if reason is None and candidates[0] not in sources:
                reason = "copy_target_not_found"
            elif reason is None and sources[candidates[0]].programs:
                reason = "copy_target_contains_program"
            if reason:
                boundary(reason, source, index + 1, chain, copy_name=name, candidates=candidates)
                if not emit(source, index, chain):
                    return
                continue
            target = sources[candidates[0]]
            if include is not None:
                include["copy_name"] = name
                include["resolved_path"] = target.relative_path
                include["candidates"] = all_candidates
                include["replacement_pairs"] = [list(pair) for pair in replacing]
            if len(emitted) >= max_lines and target.lines:
                emit(source, index, chain)
                return
            before_boundaries = len(boundaries)
            walk(target, nested, (*ancestors, target.relative_path),
                 (*replacements, (replacing, site)) if replacing else replacements)
            if include is not None and len(boundaries) == before_boundaries:
                include["status"] = "expanded"
        if conditions:
            boundary("compile_condition_scope_incomplete", source, len(source.lines), chain)

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
            "source_expansion_complete": complete, "compiler_equivalent": False,
            "compilation_context": context.to_dict(), "compilation_context_fingerprint": context.fingerprint}
