#!/usr/bin/env python3
"""Offline, privacy-preserving inventory for downloaded COBOL repositories.

The default report contains aggregate statistics only. It never calls a network
service and does not include source text, file paths, program names, or COPYBOOK
names unless --include-identifiers is explicitly supplied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


SCHEMA_VERSION = "1.0"

DEFAULT_EXTENSIONS = frozenset(
    {
        ".cbl",
        ".cob",
        ".cobol",
        ".cblle",
        ".sqlcblle",
        ".cpy",
        ".copy",
        ".pco",
        ".sqb",
        ".src",
        ".txt",
        ".sql",
        ".ddl",
        ".dds",
        ".pf",
        ".lf",
        ".cl",
        ".clle",
        ".jcl",
        ".job",
    }
)

IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        ".poc-index",
        ".poc-data",
        "__pycache__",
        "node_modules",
        "venv",
        ".venv",
    }
)

PROGRAM_ID_RE = re.compile(
    r"\bPROGRAM-ID\s*\.\s*([A-Z0-9_$#@-]+)", re.IGNORECASE
)
COPY_RE = re.compile(
    r"\bCOPY\s+([A-Z0-9_$#@.-]+)", re.IGNORECASE
)
LITERAL_CALL_RE = re.compile(
    r"\bCALL\s+(['\"])([^'\"]+)\1", re.IGNORECASE
)
DYNAMIC_CALL_RE = re.compile(
    r"\bCALL\s+(?!['\"])([A-Z0-9_$#@-]+)", re.IGNORECASE
)
PERFORM_RE = re.compile(r"\bPERFORM\b", re.IGNORECASE)
EXEC_SQL_RE = re.compile(r"\bEXEC\s+SQL\b", re.IGNORECASE)
DATA_DIVISION_RE = re.compile(r"\bDATA\s+DIVISION\b", re.IGNORECASE)
PROCEDURE_DIVISION_RE = re.compile(r"\bPROCEDURE\s+DIVISION\b", re.IGNORECASE)


@dataclass(frozen=True)
class DecodedSource:
    text: str
    encoding: str
    used_fallback: bool = False


@dataclass(frozen=True)
class FileObservation:
    relative_path: str
    basename: str
    suffix: str
    size_bytes: int
    line_count: int
    sha256: str
    encoding: str
    used_fallback_encoding: bool
    format_hint: str
    artifact_kind: str
    program_ids: tuple[str, ...]
    copy_targets: tuple[str, ...]
    literal_call_targets: tuple[str, ...]
    dynamic_call_targets: tuple[str, ...]
    perform_count: int
    exec_sql_count: int


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_extensions(values: Iterable[str]) -> frozenset[str]:
    normalized: set[str] = set()
    for value in values:
        candidate = value.strip().lower()
        if not candidate:
            continue
        normalized.add(candidate if candidate.startswith(".") else f".{candidate}")
    return frozenset(normalized)


def parse_extensions(raw: str | None) -> frozenset[str]:
    if not raw:
        return DEFAULT_EXTENSIONS
    parsed = normalize_extensions(raw.split(","))
    if not parsed:
        raise ValueError("At least one non-empty extension is required.")
    return parsed


def iter_source_files(
    root: Path,
    extensions: frozenset[str],
    include_extensionless: bool,
) -> Iterable[Path]:
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            [
                name
                for name in dirnames
                if name.casefold() not in IGNORED_DIRECTORIES
                and not (Path(directory) / name).is_symlink()
            ],
            key=str.casefold,
        )
        for filename in sorted(filenames, key=str.casefold):
            path = Path(directory) / filename
            if path.is_symlink() or not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix in extensions or (include_extensionless and not suffix):
                yield path


def _looks_binary(data: bytes) -> bool:
    if not data:
        return False
    if data.startswith((b"\xff\xfe", b"\xfe\xff", b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return False
    sample = data[:8192]
    null_ratio = sample.count(b"\x00") / len(sample)
    controls = sum(
        1
        for byte in sample
        if byte < 9 or (13 < byte < 32)
    )
    return null_ratio > 0.01 or controls / len(sample) > 0.05


def decode_source(data: bytes) -> DecodedSource | None:
    if _looks_binary(data):
        return None

    bom_candidates: list[tuple[bytes, str]] = [
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe\x00\x00", "utf-32"),
        (b"\x00\x00\xfe\xff", "utf-32"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
    ]
    for bom, encoding in bom_candidates:
        if data.startswith(bom):
            try:
                return DecodedSource(data.decode(encoding), encoding)
            except UnicodeDecodeError:
                return None

    for encoding in ("utf-8", "cp950", "big5", "cp1252"):
        try:
            return DecodedSource(data.decode(encoding), encoding)
        except UnicodeDecodeError:
            continue

    # Latin-1 is intentionally last because every byte sequence decodes. The
    # fallback is counted so a real repository profile can expose uncertainty.
    return DecodedSource(data.decode("latin-1"), "latin-1-fallback", True)


def normalize_cobol_text(text: str) -> tuple[str, str]:
    cleaned: list[str] = []
    fixed_votes = 0
    free_votes = 0

    for raw_line in text.splitlines():
        line = raw_line.expandtabs(8)
        candidate = line
        first_six = line[:6] if len(line) >= 6 else ""
        looks_fixed = (
            len(line) >= 7
            and (
                first_six.strip().isdigit()
                or (not first_six.strip() and line[6] in " */-Dd")
            )
        )

        if looks_fixed:
            fixed_votes += 1
            indicator = line[6]
            if indicator in "*/":
                continue
            candidate = line[7:72]
        else:
            free_votes += 1
            stripped = line.lstrip()
            if stripped.startswith("*>") or stripped.startswith("*"):
                continue

        if "*>" in candidate:
            candidate = candidate.split("*>", 1)[0]
        cleaned.append(candidate.rstrip())

    if fixed_votes > max(5, free_votes // 3):
        format_hint = "fixed"
    elif fixed_votes and free_votes:
        format_hint = "mixed"
    else:
        format_hint = "free_or_unknown"
    return "\n".join(cleaned), format_hint


def classify_artifact(
    suffix: str,
    program_ids: Sequence[str],
    normalized_text: str,
) -> str:
    if suffix in {".cpy", ".copy"}:
        return "copybook"
    if suffix in {".ddl", ".dds", ".pf", ".lf"}:
        return "ddl_or_db_file_definition"
    if suffix in {".cl", ".clle", ".jcl", ".job"}:
        return "job_or_command"
    if program_ids or PROCEDURE_DIVISION_RE.search(normalized_text):
        return "cobol_program"
    if DATA_DIVISION_RE.search(normalized_text):
        return "cobol_fragment_or_copybook"
    if suffix == ".sql":
        return "sql_or_ddl"
    return "unclassified_text"


def observe_file(path: Path, root: Path) -> FileObservation | None:
    data = path.read_bytes()
    decoded = decode_source(data)
    if decoded is None:
        return None

    normalized_text, format_hint = normalize_cobol_text(decoded.text)
    program_ids = tuple(
        match.upper() for match in PROGRAM_ID_RE.findall(normalized_text)
    )
    copy_targets = tuple(
        match.rstrip(".").upper() for match in COPY_RE.findall(normalized_text)
    )
    literal_targets = tuple(
        match[1].strip().upper() for match in LITERAL_CALL_RE.findall(normalized_text)
    )
    dynamic_targets = tuple(
        match.upper() for match in DYNAMIC_CALL_RE.findall(normalized_text)
    )
    suffix = path.suffix.lower()

    return FileObservation(
        relative_path=path.relative_to(root).as_posix(),
        basename=path.stem.upper(),
        suffix=suffix or "<extensionless>",
        size_bytes=len(data),
        line_count=len(decoded.text.splitlines()),
        sha256=sha256_bytes(data),
        encoding=decoded.encoding,
        used_fallback_encoding=decoded.used_fallback,
        format_hint=format_hint,
        artifact_kind=classify_artifact(suffix, program_ids, normalized_text),
        program_ids=program_ids,
        copy_targets=copy_targets,
        literal_call_targets=literal_targets,
        dynamic_call_targets=dynamic_targets,
        perform_count=len(PERFORM_RE.findall(normalized_text)),
        exec_sql_count=len(EXEC_SQL_RE.findall(normalized_text)),
    )


def _duplicate_summary(values: Iterable[str]) -> tuple[int, int]:
    counts = Counter(value for value in values if value)
    duplicate_groups = sum(1 for count in counts.values() if count > 1)
    duplicate_extra_items = sum(count - 1 for count in counts.values() if count > 1)
    return duplicate_groups, duplicate_extra_items


def build_inventory(
    source_root: Path,
    *,
    extensions: frozenset[str] = DEFAULT_EXTENSIONS,
    include_extensionless: bool = False,
    include_identifiers: bool = False,
    progress_every: int = 500,
    quiet: bool = False,
) -> dict[str, object]:
    root = source_root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Source root is not a directory: {source_root}")

    observations: list[FileObservation] = []
    unreadable_count = 0
    candidate_files = list(iter_source_files(root, extensions, include_extensionless))

    for index, path in enumerate(candidate_files, start=1):
        try:
            observation = observe_file(path, root)
        except OSError:
            observation = None
        if observation is None:
            unreadable_count += 1
        else:
            observations.append(observation)
        if not quiet and progress_every > 0 and index % progress_every == 0:
            print(f"Scanned {index}/{len(candidate_files)} files...", file=sys.stderr)

    observations.sort(key=lambda item: item.relative_path.casefold())
    snapshot_hasher = hashlib.sha256()
    for item in observations:
        snapshot_hasher.update(item.relative_path.encode("utf-8", errors="surrogatepass"))
        snapshot_hasher.update(b"\0")
        snapshot_hasher.update(item.sha256.encode("ascii"))
        snapshot_hasher.update(b"\n")

    program_locations: dict[str, list[str]] = defaultdict(list)
    basename_locations: dict[str, list[str]] = defaultdict(list)
    all_copy_targets: list[str] = []
    all_literal_call_targets: list[str] = []
    all_dynamic_call_targets: list[str] = []

    for item in observations:
        basename_locations[item.basename].append(item.relative_path)
        for program_id in item.program_ids:
            program_locations[program_id].append(item.relative_path)
        all_copy_targets.extend(item.copy_targets)
        all_literal_call_targets.extend(item.literal_call_targets)
        all_dynamic_call_targets.extend(item.dynamic_call_targets)

    duplicate_program_groups, duplicate_program_definitions = _duplicate_summary(
        program_id
        for item in observations
        for program_id in item.program_ids
    )
    duplicate_basename_groups, duplicate_basename_files = _duplicate_summary(
        item.basename for item in observations
    )

    available_stems = set(basename_locations)
    available_programs = set(program_locations)
    unresolved_copy_targets = sorted(set(all_copy_targets) - available_stems)
    unresolved_literal_calls = sorted(
        set(all_literal_call_targets) - available_programs
    )

    report: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "privacy": {
            "network_calls": False,
            "source_text_included": False,
            "identifiers_included": include_identifiers,
            "absolute_paths_included": False,
        },
        "snapshot": {
            "snapshot_id": f"sha256:{snapshot_hasher.hexdigest()}",
            "candidate_file_count": len(candidate_files),
            "decoded_file_count": len(observations),
            "unreadable_or_binary_file_count": unreadable_count,
        },
        "totals": {
            "bytes": sum(item.size_bytes for item in observations),
            "lines": sum(item.line_count for item in observations),
            "program_definition_count": sum(
                len(item.program_ids) for item in observations
            ),
            "unique_program_id_count": len(program_locations),
            "copy_statement_count": sum(
                len(item.copy_targets) for item in observations
            ),
            "literal_call_count": len(all_literal_call_targets),
            "dynamic_call_count": len(all_dynamic_call_targets),
            "perform_statement_count": sum(
                item.perform_count for item in observations
            ),
            "exec_sql_block_count": sum(
                item.exec_sql_count for item in observations
            ),
        },
        "distributions": {
            "extensions": dict(sorted(Counter(item.suffix for item in observations).items())),
            "encodings": dict(sorted(Counter(item.encoding for item in observations).items())),
            "format_hints": dict(
                sorted(Counter(item.format_hint for item in observations).items())
            ),
            "artifact_kinds": dict(
                sorted(Counter(item.artifact_kind for item in observations).items())
            ),
        },
        "quality_signals": {
            "fallback_decoded_file_count": sum(
                1 for item in observations if item.used_fallback_encoding
            ),
            "files_without_program_id_count": sum(
                1
                for item in observations
                if item.artifact_kind == "cobol_program" and not item.program_ids
            ),
            "duplicate_program_id_group_count": duplicate_program_groups,
            "duplicate_program_definition_count": duplicate_program_definitions,
            "duplicate_basename_group_count": duplicate_basename_groups,
            "duplicate_basename_file_count": duplicate_basename_files,
            "unresolved_copy_target_count": len(unresolved_copy_targets),
            "unresolved_literal_call_target_count": len(unresolved_literal_calls),
            "dynamic_call_target_count": len(set(all_dynamic_call_targets)),
        },
        "coverage_boundary": {
            "observed_artifact_kinds": sorted(
                set(item.artifact_kind for item in observations)
            ),
            "not_proven_by_cobol_only_snapshot": [
                "DDL/DDS definitions unless present in the scanned folder",
                "job schedules and runtime invocation times",
                "DB file contents and production records",
                "item/control table values and business labels",
                "AS400 object metadata and library resolution",
                "runtime configuration and execution traces",
            ],
        },
    }

    if include_identifiers:
        report["identifiers"] = {
            "program_ids": sorted(program_locations),
            "copy_targets": sorted(set(all_copy_targets)),
            "literal_call_targets": sorted(set(all_literal_call_targets)),
            "dynamic_call_targets": sorted(set(all_dynamic_call_targets)),
            "unresolved_copy_targets": unresolved_copy_targets,
            "unresolved_literal_call_targets": unresolved_literal_calls,
            "relative_files": [item.relative_path for item in observations],
        }

    return report


def report_to_markdown(report: dict[str, object]) -> str:
    snapshot = report["snapshot"]
    totals = report["totals"]
    quality = report["quality_signals"]
    distributions = report["distributions"]
    boundary = report["coverage_boundary"]
    assert isinstance(snapshot, dict)
    assert isinstance(totals, dict)
    assert isinstance(quality, dict)
    assert isinstance(distributions, dict)
    assert isinstance(boundary, dict)

    lines = [
        "# COBOL Repository Inventory",
        "",
        "> Aggregate-only offline report. No source text, absolute paths, or API keys are included.",
        "",
        f"- Snapshot: `{snapshot['snapshot_id']}`",
        f"- Candidate files: {snapshot['candidate_file_count']}",
        f"- Decoded files: {snapshot['decoded_file_count']}",
        f"- Unreadable/binary files: {snapshot['unreadable_or_binary_file_count']}",
        f"- Total lines: {totals['lines']}",
        f"- Program definitions: {totals['program_definition_count']}",
        f"- COPY statements: {totals['copy_statement_count']}",
        f"- Literal/Dynamic CALL: {totals['literal_call_count']} / {totals['dynamic_call_count']}",
        f"- PERFORM statements: {totals['perform_statement_count']}",
        f"- EXEC SQL blocks: {totals['exec_sql_block_count']}",
        "",
        "## Quality signals",
        "",
    ]
    for key, value in quality.items():
        lines.append(f"- {key}: {value}")

    lines.extend(["", "## Artifact kinds", ""])
    artifact_kinds = distributions.get("artifact_kinds", {})
    assert isinstance(artifact_kinds, dict)
    for key, value in artifact_kinds.items():
        lines.append(f"- {key}: {value}")

    lines.extend(["", "## Not proven by this snapshot", ""])
    missing = boundary.get("not_proven_by_cobol_only_snapshot", [])
    assert isinstance(missing, list)
    for item in missing:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def write_report(report: dict[str, object], json_path: Path, markdown_path: Path | None) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if markdown_path is not None:
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(report_to_markdown(report), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build an aggregate-only, offline inventory for a downloaded "
            "COBOL/COPYBOOK repository."
        )
    )
    parser.add_argument("source", type=Path, help="Windows or local source folder")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("repo-inventory.json"),
        help="Aggregate JSON output path (default: repo-inventory.json)",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        help="Optional aggregate Markdown output path",
    )
    parser.add_argument(
        "--extensions",
        help="Comma-separated extensions; defaults include COBOL, COPY, SQL, DDS and job files",
    )
    parser.add_argument(
        "--include-extensionless",
        action="store_true",
        help="Also inspect files without an extension",
    )
    parser.add_argument(
        "--include-identifiers",
        action="store_true",
        help=(
            "Include relative paths and source identifiers in the report. "
            "Do not share that report outside the approved environment."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress messages",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        extensions = parse_extensions(args.extensions)
        report = build_inventory(
            args.source,
            extensions=extensions,
            include_extensionless=args.include_extensionless,
            include_identifiers=args.include_identifiers,
            quiet=args.quiet,
        )
        write_report(report, args.output, args.markdown_output)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
