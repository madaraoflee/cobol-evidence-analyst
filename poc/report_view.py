"""Bounded browser presentation of complete on-disk analysis reports."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile


DIRECT_REPORT_BYTES = 16_000_000
VIEW_REPORT_BYTES = 16_000_000
_STRING_BUDGET = 3_000_000
_ARRAY_LIMITS = {"page_summaries": 0, "program_summaries": 300, "evidence_refs": 3000,
                 "verified_evidence_refs": 3000, "tool_trace": 300, "exchanges": 100,
                 "programs": 20000}
_PRIORITY = {name: index for index, name in enumerate((
    "diagnostic_summary", "diagnostic", "diagnostics", "prior_diagnostic", "runner_status", "reason_code", "snapshot_id",
    "source_root", "source_manifest_verified", "source_options", "catalog_ready", "catalog_snapshot_id", "scope", "build_report", "repository_search",
    "agent_result", "status", "narrative", "answer", "answer_completion", "material_gap", "stop_reason",
    "model_answer_recorded", "reading_coverage", "investigation", "analysis_scope", "evidence_refs",
    "program_summaries", "api_diagnostics", "unaccepted_response",
))}


def report_sha256(path: Path, *, check_cancel=None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            if check_cancel:
                check_cancel()
            digest.update(chunk)
    if check_cancel:
        check_cancel()
    return digest.hexdigest()


def _report_digest_reuse_supported():
    # Windows modification/change timestamps did not reliably distinguish an
    # equal-size rewrite followed by restoring mtime in native regression tests.
    # Keep complete verification there; the desktop still restores in background.
    return os.name != "nt"


class ReportReadSession:
    """Reuse content verification only inside one restore/update operation.

    A new process or restoration always reads the bytes. A changed file identity,
    size, modification time or change time invalidates the operation's digest.
    Windows always rereads the bytes because its timestamps cannot prove that an
    equal-size report with restored mtime is unchanged.
    """

    def __init__(self):
        self._verified = {}

    @staticmethod
    def _signature(path):
        value = path.stat()
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns

    def sha256(self, path, *, check_cancel=None):
        if check_cancel:
            check_cancel()
        signature = self._signature(path)
        key = str(path.absolute())
        previous = self._verified.get(key)
        reuse_supported = _report_digest_reuse_supported()
        if reuse_supported and signature is not None and previous is not None and previous[0] == signature:
            return previous[1]
        value = report_sha256(path, check_cancel=check_cancel)
        if self._signature(path) != signature:
            raise ValueError("report changed during verification")
        if reuse_supported and signature is not None:
            self._verified[key] = signature, value
        return value


def project_report(report: dict) -> dict:
    """Reduce browser payload only; never alter analytical coverage or source trust."""
    omitted = []
    remaining = _STRING_BUDGET
    nodes = 0

    def project(value, path: tuple[str, ...], *, essential: bool = False):
        nonlocal remaining, nodes
        nodes += 1
        name = path[-1] if path else ""
        if isinstance(value, str):
            limit = min(len(value), 1_000_000 if essential else 65536, max(0, remaining))
            remaining -= limit
            if limit < len(value):
                omitted.append({"path": ".".join(path), "retained_characters": limit, "total_characters": len(value)})
            return value[:limit]
        if isinstance(value, list):
            limit = min(len(value), _ARRAY_LIMITS.get(name, 1000))
            if nodes > 100000:
                limit = 0
            if limit < len(value):
                omitted.append({"path": ".".join(path), "retained_items": limit, "total_items": len(value)})
            selected = value[-limit:] if name == "exchanges" and limit else value[:limit]
            return [project(item, (*path, str(index))) for index, item in enumerate(selected)]
        if isinstance(value, dict):
            keys = sorted(value, key=lambda key: _PRIORITY.get(key, 100))
            if len(keys) > 200:
                omitted.append({"path": ".".join(path), "retained_fields": 200, "total_fields": len(keys)})
            return {key: project(value[key], (*path, key), essential=essential or key in {"answer", "narrative"})
                    for key in keys[:200]}
        return value

    result = project(report, ())
    result["display_projection"] = {"complete_report_on_disk": True, "omitted": omitted[:200],
                                    "omitted_field_count": len(omitted)}
    return result


def write_report_view(path: Path, report: dict, *, check_cancel=None) -> None:
    """Write an atomic companion only when the full report exceeds browser capacity."""
    if path.stat().st_size <= DIRECT_REPORT_BYTES:
        return
    destination = path.with_name(f"{path.stem}-view.json")
    if destination.is_symlink():
        raise ValueError("invalid report view path")
    view = project_report(report)
    view["display_projection"].update(source_sha256=report_sha256(path, check_cancel=check_cancel),
                                      source_size=path.stat().st_size)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(view, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        if Path(temporary).stat().st_size > VIEW_REPORT_BYTES:
            raise ValueError("report view exceeds display capacity")
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
