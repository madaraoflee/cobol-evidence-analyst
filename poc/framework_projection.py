"""Isolated derived-source index with explicit original inclusion provenance."""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile

from framework_audit import _observations
from procedure_expansion import expand_program
from repo_inventory import DEFAULT_EXTENSIONS, decode_source, iter_source_files, observe_file
from structural_index import build_structural_index


def manifest(source: Path, extensions) -> list[dict]:
    result = []
    for path in iter_source_files(source, extensions, True):
        raw = path.read_bytes()
        if decode_source(raw) is not None:
            result.append({"relative_path": path.relative_to(source).as_posix(),
                           "source_hash": hashlib.sha256(raw).hexdigest()})
    return sorted(result, key=lambda row: row["relative_path"])


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class Projection:
    def __init__(self, source: Path, extensions, expansions: dict, source_files: list[dict], database: Path):
        self.source, self.extensions = source, extensions
        self.expansions, self.source_files, self.database = expansions, source_files, database
        self.source_snapshot_id = "source:" + digest(source_files)
        self.line_maps: dict[str, list] = {}
        self.derived_snapshot_id = ""

    def verify_originals(self):
        if manifest(self.source, self.extensions) != self.source_files:
            raise ValueError("Source changed during framework path analysis; discard this result and retry a stable snapshot.")

    def map_reference(self, ref: dict) -> dict:
        rows = self.line_maps.get(ref["relative_path"])
        start, end = ref["start_line"], ref["end_line"]
        if rows is None or not 1 <= start <= end <= len(rows):
            raise ValueError("Derived evidence is outside the original-source map.")
        spans = []
        for row in rows[start - 1:end]:
            origin, chain = row["origin"], row["include_chain"]
            item = {"relative_path": origin["relative_path"], "source_hash": origin["source_hash"],
                    "start_line": origin["line"], "end_line": origin["line"], "include_chain": chain}
            if (spans and all(spans[-1][key] == item[key] for key in ("relative_path", "source_hash", "include_chain"))
                    and spans[-1]["end_line"] + 1 == item["start_line"]):
                spans[-1]["end_line"] = item["end_line"]
            else:
                spans.append(item)
        return {"derived_evidence_id": ref["evidence_id"],
                "derived_location": {"relative_path": ref["relative_path"], "start_line": start, "end_line": end,
                                     "snapshot_id": self.derived_snapshot_id},
                "source_snapshot_id": self.source_snapshot_id, "source_spans": spans}

    def map_result(self, value):
        if isinstance(value, dict):
            if {"evidence_id", "relative_path", "start_line", "end_line"} <= set(value):
                return self.map_reference(value)
            return {key: self.map_result(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.map_result(item) for item in value]
        return value


@contextmanager
def projected_index(source_root: Path, entry_program: str, *, extensions=DEFAULT_EXTENSIONS,
                    scratch_root: Path | None = None, max_programs: int = 32):
    """Build only reachable literal source programs; never substitute an I/O stub.

    Program collection is structural, not a reachability proof. A missing or
    unsupported callee stays a boundary when the path reaches that call.
    Temporary derived source and SQLite must remain in an approved environment.
    """
    if type(max_programs) is not int or not 1 <= max_programs <= 128:
        raise ValueError("Invalid projected program budget.")
    source = Path(source_root).expanduser().resolve()
    if not source.is_dir():
        raise ValueError("Source must be an existing directory.")
    if scratch_root is not None:
        scratch_root = Path(scratch_root).resolve()
        if source == scratch_root or source in scratch_root.parents or scratch_root in source.parents:
            raise ValueError("Scratch and original source must be non-nested directories.")
    first = expand_program(source, entry_program, extensions=extensions)
    source_files = first["source_files"]
    available = set()
    for path in iter_source_files(source, extensions, True):
        observation = observe_file(path, source)
        if observation:
            available.update(observation.program_ids)
    pending = deque([entry_program.upper()])
    expansions = {}
    while pending:
        program = pending.popleft()
        if program in expansions:
            continue
        if len(expansions) >= max_programs:
            raise ValueError("Framework source projection exceeds the program budget.")
        expansion = first if program == entry_program.upper() else expand_program(source, program, extensions=extensions)
        if expansion["source_files"] != source_files:
            raise ValueError("Source changed between program expansions.")
        expansions[program] = expansion
        if expansion["source_expansion_complete"]:
            observations, _ = _observations(expansion)
            for call in observations["calls"]:
                # The source parser uses case-insensitive program identifiers.
                # Collection must agree so casing cannot turn real source into
                # an apparently missing program eligible for an external model.
                target = call["target"].upper()
                if call["target_kind"] == "literal" and target in available:
                    pending.append(target)
    with tempfile.TemporaryDirectory(prefix="framework-path-", dir=scratch_root) as temporary:
        scratch = Path(temporary)
        derived = scratch / "source"
        derived.mkdir()
        projection = Projection(source, extensions, expansions, source_files, scratch / "derived.sqlite")
        for index, (program, expansion) in enumerate(expansions.items()):
            if not expansion["source_expansion_complete"]:
                continue
            name = f"unit_{index:03}.cbl"
            # All physical lines remain represented. Unindented normalized code
            # avoids accidental fixed-column re-interpretation by old readers.
            (derived / name).write_text("\n".join(line["code"].strip() for line in expansion["lines"]) + "\n", encoding="utf-8")
            projection.line_maps[name] = expansion["lines"]
        if projection.line_maps:
            report = build_structural_index(derived, projection.database, quiet=True, source_format="free")
            projection.derived_snapshot_id = report["snapshot_id"]
        projection.verify_originals()
        yield projection
        projection.verify_originals()
