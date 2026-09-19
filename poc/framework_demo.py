"""Read the fixed synthetic framework guide from its current local sources.

This is source navigation data, not a stored model response. Paths, spans and
literal calls are derived from fixture files each time the guide is loaded.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "framework-workbench"
DEMO_ID = "framework-workbench"
CASE_IDS = frozenset({"online", "client_server", "batch"})
MAX_MANIFEST_BYTES = 256_000
MAX_SOURCE_FILE_BYTES = 1_000_000
MAX_SOURCE_FILES = 64
MAX_EVIDENCE_LINES = 40
_SOURCE_EXTENSIONS = frozenset({".cbl", ".cob", ".cobol", ".cpy"})
_TOKEN = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|[A-Za-z0-9_$#@-]+|\.")
_SECTION = re.compile(r"[A-Z0-9_$#@-]+\s+(?:SECTION|DIVISION)(?:\s+\d+)?\s*\.", re.I)
_LOCALES = frozenset({"zh-CN", "zh-HK", "en"})


class FrameworkDemoError(ValueError):
    """Safe failure code suitable for the local API response."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _display(value: object, *, optional: bool = False) -> str | dict[str, str]:
    if isinstance(value, str) and (value.strip() or optional) and len(value) <= 8_000:
        return value
    if isinstance(value, Mapping) and value and set(value) <= _LOCALES and all(
        isinstance(text, str) and text.strip() and len(text) <= 8_000 for text in value.values()
    ):
        return dict(value)
    raise FrameworkDemoError("FRAMEWORK_DEMO_MANIFEST_INVALID")


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise FrameworkDemoError("FRAMEWORK_DEMO_PATH_INVALID")
    path = PurePosixPath(value)
    if path.is_absolute() or ":" in value or any(part in {"..", "."} for part in value.split("/")):
        raise FrameworkDemoError("FRAMEWORK_DEMO_PATH_INVALID")
    return path.as_posix()


def _contained(root: Path, relative: str, *, directory: bool = False) -> Path:
    relative = _relative(relative)
    if root.is_symlink() or not root.is_dir():
        raise FrameworkDemoError("FRAMEWORK_DEMO_PATH_INVALID")
    candidate = root
    for part in PurePosixPath(relative).parts:
        candidate /= part
        if candidate.is_symlink():
            raise FrameworkDemoError("FRAMEWORK_DEMO_PATH_INVALID")
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        raise FrameworkDemoError("FRAMEWORK_DEMO_PATH_INVALID") from None
    if not (candidate.is_dir() if directory else candidate.is_file()):
        raise FrameworkDemoError("FRAMEWORK_DEMO_FILE_MISSING")
    return candidate


def _read(path: Path, maximum: int) -> bytes:
    try:
        with path.open("rb") as handle:
            contents = handle.read(maximum + 1)
    except OSError:
        raise FrameworkDemoError("FRAMEWORK_DEMO_FILE_UNREADABLE") from None
    if len(contents) > maximum:
        raise FrameworkDemoError("FRAMEWORK_DEMO_FILE_TOO_LARGE")
    return contents


def _code(line: str) -> str:
    if len(line) >= 7 and all(character.isdigit() or character.isspace() for character in line[:6]):
        if line[6] in {"*", "/"}:
            return ""
        line = line[7:]
    # Inline comments begin outside string literals. A comment-like marker in
    # DISPLAY text is still part of the source and must not alter tokenization.
    quote = None
    index = 0
    while index < len(line):
        character = line[index]
        if quote:
            if character == quote:
                if index + 1 < len(line) and line[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif character in {"'", '"'}:
            quote = character
        elif line[index:index + 2] == "*>":
            return line[:index]
        index += 1
    return line


def _tokens(lines: list[str]) -> list[tuple[str, int, bool]]:
    tokens = []
    for line_number, line in enumerate(lines, 1):
        for match in _TOKEN.finditer(_code(line)):
            value = match.group()
            literal = value.startswith(("'", '"'))
            tokens.append((value[1:-1].replace(value[0] * 2, value[0]) if literal else value.upper(), line_number, literal))
    return tokens


def _source_inventory(source_root: Path) -> dict[str, dict[str, object]]:
    inventory: dict[str, dict[str, object]] = {}
    try:
        paths = sorted(source_root.rglob("*"))
    except OSError:
        raise FrameworkDemoError("FRAMEWORK_DEMO_FILE_UNREADABLE") from None
    if len(paths) > 512:
        raise FrameworkDemoError("FRAMEWORK_DEMO_FILE_LIMIT")
    for path in paths:
        relative = path.relative_to(source_root).as_posix()
        if path.is_symlink():
            raise FrameworkDemoError("FRAMEWORK_DEMO_PATH_INVALID")
        if path.is_dir() or path.suffix.lower() not in _SOURCE_EXTENSIONS:
            continue
        path = _contained(source_root, relative)
        raw = _read(path, MAX_SOURCE_FILE_BYTES)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError:
            raise FrameworkDemoError("FRAMEWORK_DEMO_SOURCE_ENCODING_INVALID") from None
        lines = text.splitlines()
        inventory[relative] = {"lines": lines, "sha256": hashlib.sha256(raw).hexdigest(), "tokens": _tokens(lines)}
        if len(inventory) > MAX_SOURCE_FILES:
            raise FrameworkDemoError("FRAMEWORK_DEMO_FILE_LIMIT")
    if not inventory:
        raise FrameworkDemoError("FRAMEWORK_DEMO_SOURCE_EMPTY")
    return inventory


def _programs_and_calls(inventory: Mapping[str, Mapping[str, object]]) -> tuple[list[dict], list[dict]]:
    programs: list[dict] = []
    for relative, source in inventory.items():
        tokens = source["tokens"]
        for index, (value, line_number, literal) in enumerate(tokens[:-2]):
            if not literal and value == "PROGRAM-ID" and tokens[index + 1][0] == ".":
                name, _, name_literal = tokens[index + 2]
                if name != "." and not name_literal:
                    programs.append({"program_name": name, "relative_path": relative, "start_line": line_number})
    if not programs or len({item["program_name"] for item in programs}) != len(programs):
        raise FrameworkDemoError("FRAMEWORK_DEMO_PROGRAMS_INVALID")
    copybooks: dict[str, list[str]] = {}
    for relative in inventory:
        copybooks.setdefault(Path(relative).stem.upper(), []).append(relative)
    calls: list[dict] = []

    def scan(program: str, relative: str, first: int, last: int, stack: set[str]) -> None:
        if relative in stack:
            return
        stack = {*stack, relative}
        tokens = inventory[relative]["tokens"]
        for index, (value, line_number, literal) in enumerate(tokens[:-1]):
            if literal or not first <= line_number <= last or value not in {"CALL", "COPY"}:
                continue
            target, _, target_literal = tokens[index + 1]
            if target == ".":
                continue
            if value == "CALL":
                calls.append({"source_program": program, "target_name": target,
                              "relative_path": relative, "line": line_number, "literal_target": target_literal})
            else:
                candidates = copybooks.get(Path(target).stem.upper(), [])
                if len(candidates) == 1:
                    scan(program, candidates[0], 1, len(inventory[candidates[0]]["lines"]), stack)

    for program in programs:
        same_file_next = [other["start_line"] for other in programs if other["relative_path"] == program["relative_path"]
                          and other["start_line"] > program["start_line"]]
        last = min(same_file_next) - 1 if same_file_next else len(inventory[program["relative_path"]]["lines"])
        scan(program["program_name"], program["relative_path"], program["start_line"], last, set())
    unique = {(call["source_program"], call["target_name"], call["relative_path"], call["line"]): call for call in calls}
    return programs, list(unique.values())


def _anchor_span(lines: list[str], anchor: object) -> tuple[int, int]:
    if not isinstance(anchor, str) or not anchor.strip() or len(anchor) > 240 or "\n" in anchor:
        raise FrameworkDemoError("FRAMEWORK_DEMO_ANCHOR_INVALID")
    needle = anchor.strip().upper().rstrip(".")
    matches = [number for number, line in enumerate(lines, 1)
               if _code(line).strip().upper().rstrip(".") == needle]
    if len(matches) != 1:
        raise FrameworkDemoError("FRAMEWORK_DEMO_ANCHOR_MISSING" if not matches else "FRAMEWORK_DEMO_ANCHOR_AMBIGUOUS")
    start = matches[0]
    end = min(len(lines), start + MAX_EVIDENCE_LINES - 1)
    for number in range(start + 1, end + 1):
        if _SECTION.fullmatch(_code(lines[number - 1]).strip()):
            end = number - 1
            break
    return start, end


def select_framework_demo_case(demo: Mapping[str, object], case_id: object) -> dict:
    """Select only one of the fixed guide's known case identifiers."""
    if demo.get("id") != DEMO_ID or not isinstance(case_id, str) or case_id not in CASE_IDS:
        raise FrameworkDemoError("FRAMEWORK_DEMO_CASE_UNKNOWN")
    for case in demo.get("cases", []):
        if isinstance(case, Mapping) and case.get("id") == case_id:
            return deepcopy(dict(case))
    raise FrameworkDemoError("FRAMEWORK_DEMO_CASE_UNKNOWN")


def load_framework_demo() -> dict[str, object]:
    """Load the fixed synthetic guide; this function accepts no external path."""
    root = FIXTURE_ROOT
    manifest_path = _contained(root, "manifest.json")
    manifest_bytes = _read(manifest_path, MAX_MANIFEST_BYTES)
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError):
        raise FrameworkDemoError("FRAMEWORK_DEMO_MANIFEST_INVALID") from None
    if not isinstance(manifest, Mapping) or manifest.get("id") != DEMO_ID:
        raise FrameworkDemoError("FRAMEWORK_DEMO_MANIFEST_INVALID")
    source_root = _contained(root, "source", directory=True)
    inventory = _source_inventory(source_root)
    hasher = hashlib.sha256(manifest_bytes)
    for relative, source in inventory.items():
        hasher.update(relative.encode("utf-8"))
        hasher.update(str(source["sha256"]).encode("ascii"))
    snapshot = "synthetic-framework-" + hasher.hexdigest()
    programs, calls = _programs_and_calls(inventory)
    program_names = {item["program_name"] for item in programs}
    raw_cases = manifest.get("cases")
    if (not isinstance(raw_cases, list) or len(raw_cases) != 3
            or not all(isinstance(item, Mapping) and isinstance(item.get("id"), str) and item["id"] in CASE_IDS for item in raw_cases)
            or {item["id"] for item in raw_cases} != CASE_IDS
            or not isinstance(manifest.get("default_case"), str) or manifest["default_case"] not in CASE_IDS):
        raise FrameworkDemoError("FRAMEWORK_DEMO_MANIFEST_INVALID")
    cases: list[dict] = []
    for case in raw_cases:
        if not isinstance(case.get("kind", case["id"]), str):
            raise FrameworkDemoError("FRAMEWORK_DEMO_MANIFEST_INVALID")
        entry_program = case.get("entry_program")
        entry_path = _relative(case.get("entry_path"))
        if entry_path not in inventory or not any(item["program_name"] == entry_program and item["relative_path"] == entry_path for item in programs):
            raise FrameworkDemoError("FRAMEWORK_DEMO_ENTRY_INVALID")
        raw_steps = case.get("steps")
        if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= 32:
            raise FrameworkDemoError("FRAMEWORK_DEMO_MANIFEST_INVALID")
        evidence: list[dict] = []

        def make_evidence(relative: str, start: int, end: int, title: object) -> str:
            for existing in evidence:
                if existing["relative_path"] == relative and existing["start_line"] == start and existing["end_line"] == end:
                    return existing["evidence_id"]
            source = inventory[relative]
            identity = f"{snapshot}\0{relative}\0{start}\0{end}\0{source['sha256']}"
            evidence_id = "ev_demo_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            evidence.append({"evidence_id": evidence_id, "title": _display(title), "relative_path": relative,
                             "start_line": start, "end_line": end, "source_text": "\n".join(source["lines"][start - 1:end]),
                             "integrity": "SYNTHETIC_SOURCE", "source_sha256": source["sha256"]})
            return evidence_id

        steps = []
        for step in raw_steps:
            if not isinstance(step, Mapping):
                raise FrameworkDemoError("FRAMEWORK_DEMO_MANIFEST_INVALID")
            relative = _relative(step.get("path"))
            if relative not in inventory:
                raise FrameworkDemoError("FRAMEWORK_DEMO_FILE_MISSING")
            start, end = _anchor_span(inventory[relative]["lines"], step.get("anchor"))
            title = _display(step.get("title"))
            evidence_id = make_evidence(relative, start, end, title)
            steps.append({"title": title, "description": _display(step.get("description")), "evidence_id": evidence_id})
        reachable = {entry_program}
        while True:
            discovered = {call["target_name"] for call in calls if call["source_program"] in reachable
                          and call["literal_target"] and call["target_name"] in program_names}
            if discovered <= reachable:
                break
            reachable |= discovered
        relations = []
        for call in calls:
            if call["source_program"] not in reachable:
                continue
            relative, line_number = call["relative_path"], call["line"]
            span = next((item for item in evidence if item["relative_path"] == relative
                         and item["start_line"] <= line_number <= item["end_line"]), None)
            evidence_id = span["evidence_id"] if span else make_evidence(
                relative, max(1, line_number - 4), min(len(inventory[relative]["lines"]), line_number + 15),
                f"{call['source_program']} → {call['target_name']}",
            )
            relations.append({"source_program": call["source_program"], "target_name": call["target_name"],
                              "relation_type": "CALLS", "status": "source_observed", "evidence_id": evidence_id})
        metadata = case.get("metadata", [])
        boundaries = case.get("boundaries", [])
        if not isinstance(metadata, list) or not isinstance(boundaries, list) or len(metadata) > 32 or len(boundaries) > 32:
            raise FrameworkDemoError("FRAMEWORK_DEMO_MANIFEST_INVALID")
        normalized_metadata = []
        for item in metadata:
            if not isinstance(item, Mapping):
                raise FrameworkDemoError("FRAMEWORK_DEMO_MANIFEST_INVALID")
            normalized_metadata.append({"label": _display(item.get("label")), "value": _display(item.get("value"))})
        cases.append({
            "id": case["id"], "title": _display(case.get("title")), "kind": case.get("kind", case["id"]),
            "entry_program": entry_program, "entry_path": entry_path, "question": _display(case.get("question")),
            "purpose": _display(case.get("purpose")), "steps": steps, "evidence": evidence, "relations": relations,
            "metadata": normalized_metadata, "boundaries": [_display(item) for item in boundaries], "trace": [],
        })
    return {
        "id": DEMO_ID, "title": _display(manifest.get("title")), "description": _display(manifest.get("description")),
        "default_case": manifest["default_case"], "source_path": str(source_root.resolve()),
        "file_count": len(inventory), "programs": programs, "cases": cases, "snapshot_id": snapshot,
        "analysis_origin": "synthetic_source_guide", "model_called": False,
    }


__all__ = ["FrameworkDemoError", "load_framework_demo", "select_framework_demo_case"]
