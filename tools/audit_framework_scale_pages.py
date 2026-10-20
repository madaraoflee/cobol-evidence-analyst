"""Read-only physical page audit, independent of the product's source reader."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time


def intervals(ranges, total):
    cursor, overlap, gaps, covered = 1, 0, [], 0
    for first, last in sorted(ranges):
        if first > cursor:
            gaps.append([cursor, first - 1])
        overlap += max(0, min(last, cursor - 1) - first + 1)
        covered += max(0, last - max(first, cursor) + 1)
        cursor = max(cursor, last + 1)
    if cursor <= total:
        gaps.append([cursor, total])
    return {"covered_lines": covered, "overlap_lines": overlap, "gaps": [
        {"start_line": first, "end_line": last,
         "position": "whole_file" if first == 1 and last == total else
                     "head" if first == 1 else "tail" if last == total else "middle"}
        for first, last in gaps]}


def audit(directory, label):
    directory = Path(directory)
    source = directory / "source"
    oracle = json.loads((directory / "oracle.json").read_text(encoding="utf-8"))
    database = directory / label / "index.sqlite"
    tick = time.perf_counter()
    totals = {"files": 0, "physical_lines": 0, "pages": 0, "truncated_pages": 0,
              "exact_complete_pages": 0, "valid_truncated_prefix_pages": 0,
              "metadata_covered_lines": 0, "exact_complete_text_covered_lines": 0,
              "metadata_overlap_lines": 0, "exact_text_overlap_lines": 0,
              "metadata_gap_ranges": 0, "exact_text_gap_ranges": 0,
              "source_mismatches": 0, "file_metadata_mismatches": 0,
              "invalid_page_ranges": 0, "page_identity_mismatches": 0, "page_text_mismatches": 0}
    details = []
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        expected_paths = {row["path"] for row in oracle["files"]}
        unexpected = {}
        for table in ("source_files", "repo_sources", "repo_pages"):
            observed = {row[0] for row in db.execute(f"SELECT DISTINCT relative_path FROM {table}")}
            unexpected[table] = {"extra_paths": sorted(observed - expected_paths),
                                 "missing_paths": sorted(expected_paths - observed)}
        for original in oracle["files"]:
            relative = original["path"]
            raw = (source / relative).read_bytes()
            lines = raw.decode("utf-8").splitlines()
            version = hashlib.sha256(raw).hexdigest()
            issues = []
            if version != original["sha256"] or len(lines) != original["lines"] or len(raw) != original["bytes"]:
                totals["source_mismatches"] += 1
                issues.append("original_bytes_or_lines_changed")
            for table in ("source_files", "repo_sources"):
                item = db.execute(f"SELECT sha256,line_count FROM {table} WHERE relative_path=?", (relative,)).fetchone()
                if item is None or item["sha256"] != version or item["line_count"] != len(lines):
                    totals["file_metadata_mismatches"] += 1
                    issues.append(table + "_identity_mismatch")
            rows = db.execute("SELECT p.*, e.relative_path AS actual_path, e.start_line AS actual_first, "
                "e.end_line AS actual_last, e.source_sha256 AS actual_sha, e.text AS actual_text "
                "FROM repo_pages p LEFT JOIN evidence_spans e ON e.evidence_id=p.evidence_id "
                "WHERE p.relative_path=? ORDER BY p.start_line,p.end_line,p.page_id", (relative,)).fetchall()
            nominal, complete, truncated = [], [], []
            for page in rows:
                first, last = page["start_line"], page["end_line"]
                totals["pages"] += 1
                if not 1 <= first <= last <= len(lines):
                    totals["invalid_page_ranges"] += 1
                    issues.append("page_range_invalid")
                    continue
                nominal.append((first, last))
                expected = "\n".join(lines[first - 1:last])
                if page["source_sha256"] != version:
                    totals["page_identity_mismatches"] += 1
                    issues.append("page_source_version_mismatch")
                    continue
                if page["span_truncated"]:
                    totals["truncated_pages"] += 1
                    prefix = page["fallback_text"]
                    valid = bool(prefix) and expected.startswith(prefix) and prefix != expected and page["evidence_id"] is None
                    if valid:
                        totals["valid_truncated_prefix_pages"] += 1
                    else:
                        totals["page_text_mismatches"] += 1
                        issues.append("truncated_prefix_invalid")
                    truncated.append({"start_line": first, "end_line": last,
                                      "stored_characters": len(prefix), "original_characters": len(expected),
                                      "valid_prefix_only": valid})
                    continue
                identity_ok = (page["actual_path"] == relative and page["actual_first"] == first
                               and page["actual_last"] == last and page["actual_sha"] == version)
                if not identity_ok:
                    totals["page_identity_mismatches"] += 1
                    issues.append("persisted_evidence_identity_mismatch")
                if page["actual_text"] != expected:
                    totals["page_text_mismatches"] += 1
                    issues.append("persisted_evidence_text_mismatch")
                if identity_ok and page["actual_text"] == expected:
                    complete.append((first, last))
                    totals["exact_complete_pages"] += 1
            metadata, text = intervals(nominal, len(lines)), intervals(complete, len(lines))
            totals["files"] += 1
            totals["physical_lines"] += len(lines)
            totals["metadata_covered_lines"] += metadata["covered_lines"]
            totals["exact_complete_text_covered_lines"] += text["covered_lines"]
            totals["metadata_overlap_lines"] += metadata["overlap_lines"]
            totals["exact_text_overlap_lines"] += text["overlap_lines"]
            totals["metadata_gap_ranges"] += len(metadata["gaps"])
            totals["exact_text_gap_ranges"] += len(text["gaps"])
            if issues or truncated or metadata["gaps"] or text["gaps"] or metadata["overlap_lines"] or text["overlap_lines"]:
                details.append({"path": relative, "source_sha256": version, "physical_lines": len(lines),
                    "issues": sorted(set(issues)), "metadata_coverage": metadata,
                    "exact_complete_page_text_coverage": text, "truncated_pages": truncated})
    return {"schema": "framework-scale-page-audit/v1", "scale": oracle["scale"], "label": label,
            "database": str(database), "scope": "Read-only repo_pages/evidence_spans audit after timing; not question evidence or answer quality",
            "method": "Each original file hash/physical line count is checked against independent generation records; every persisted page is compared with exact original physical lines. Truncated prefixes are explicitly excluded from complete-text coverage.",
            "totals": totals, "path_inventory": unexpected, "noncomplete_or_anomalous_files": details,
            "elapsed_seconds": time.perf_counter() - tick}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--label", default="final-v2")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.directory, args.label)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"scale": result["scale"], "totals": result["totals"], "elapsed_seconds": result["elapsed_seconds"]}))


if __name__ == "__main__":
    main()
