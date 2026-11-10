"""Independent physical-site oracle for a neutral business inventory fixture.

No production parser, ranker, claim checker or completion flag is trusted here.
The authored gold supplies expected sites; actual source and delivered findings
must establish coverage separately. Synthetic prose is not a quality score.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

FIXTURE = Path(__file__).parent / "fixtures" / "global-premium-inventory-v1"


def load_gold():
    gold = json.loads((FIXTURE / "gold.json").read_text(encoding="utf-8"))
    for row in gold["files"]:
        raw = (FIXTURE / "source" / row["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise AssertionError("Authored fixture source changed: " + row["path"])
    return gold


def normalize_page(page):
    citation = page.get("citation") or page
    return {"id": page.get("id", page.get("evidence_id")),
            "path": citation.get("path", page.get("relative_path")),
            "hash": citation.get("hash", page.get("source_sha256")),
            "start": citation.get("start", page.get("start_line")),
            "end": citation.get("end", page.get("end_line")),
            "text": page.get("text", page.get("source_text")),
            "truncated": page.get("truncated", page.get("span_truncated", False))}


def verified_pages(gold, pages):
    files = {row["path"]: row for row in gold["files"]}
    accepted, rejected = [], []
    for raw in pages:
        page = normalize_page(raw)
        row = files.get(page["path"])
        valid = (row is not None and page["hash"] == row["sha256"]
                 and type(page["start"]) is int and type(page["end"]) is int
                 and 1 <= page["start"] <= page["end"] <= row["lines"]
                 and not page["truncated"])
        if valid:
            lines = (FIXTURE / "source" / page["path"]).read_text(encoding="utf-8").splitlines()
            valid = page["text"] == "\n".join(lines[page["start"] - 1:page["end"]])
        (accepted if valid else rejected).append(page)
    return accepted, rejected


def covers(page, site):
    return (page["path"] == site["path"] and page["hash"] == site["sha256"]
            and page["start"] <= site["start_line"] and page["end"] >= site["end_line"])


def supplied_coverage(gold, pages):
    accepted, rejected = verified_pages(gold, pages)
    covered = [site["site_id"] for site in gold["sites"] if any(covers(page, site) for page in accepted)]
    missing = [site["site_id"] for site in gold["sites"] if site["site_id"] not in covered]
    dependency_missing = []
    files = {row["path"]: row for row in gold["files"]}
    for path in gold["required_dependency_paths"]:
        required = set(range(1, files[path]["lines"] + 1))
        observed = set()
        for page in accepted:
            if page["path"] == path:
                observed.update(range(page["start"], page["end"] + 1))
        if required - observed:
            dependency_missing.append(path)
    return {"covered": covered, "missing": missing, "invalid_pages": len(rejected),
            "dependency_paths_missing": dependency_missing,
            "complete": not missing and not rejected and not dependency_missing}


def _compact(text):
    return re.sub(r"\s+", " ", str(text)).strip().rstrip(".")


def delivered_coverage(gold, findings, pages, answer):
    """Score actual retained findings and visible answer against independent gold.

    Fixture summaries name the authored business role and exact arithmetic.
    This strict offline convention makes omission measurable; it is not an
    automatic semantic judge for arbitrary real-model prose.
    """
    accepted, rejected = verified_pages(gold, pages)
    by_id = {page["id"]: page for page in accepted if page["id"]}
    covered, missing_prose, invalid, unexpected = set(), set(), [], []
    for finding in findings:
        if finding.get("disposition") != "included":
            continue
        matching = [site for site in gold["sites"] if finding.get("path", finding.get("relative_path")) == site["path"]
                    and finding.get("start_line") == site["start_line"]
                    and finding.get("end_line") == site["end_line"]]
        if not matching:
            unexpected.append(finding.get("path"))
            continue
        references = []
        for ref in finding.get("citations", finding.get("evidence_ids", [])):
            if isinstance(ref, str) and ref in by_id:
                references.append(by_id[ref])
            elif isinstance(ref, dict):
                citation = ref.get("citation", ref)
                references.extend(page for page in accepted
                    if all(citation.get(key) == page[key] for key in ("path", "hash", "start", "end")))
        for site in matching:
            summary = finding.get("summary", "")
            formula = finding.get("formula", "")
            if (not any(covers(page, site) for page in references)
                    or site["business_label"] not in summary
                    or _compact(site["formula"]) not in _compact(summary + " " + str(formula))):
                invalid.append(site["site_id"])
                continue
            covered.add(site["site_id"])
            if (_compact(summary) not in _compact(answer)
                    or _compact(site["formula"]) not in _compact(answer)):
                missing_prose.add(site["site_id"])
    missing = sorted({site["site_id"] for site in gold["sites"]} - covered)
    return {"covered": sorted(covered), "missing": missing,
            "missing_from_visible_answer": sorted(missing_prose),
            "invalid_findings": sorted(set(invalid)), "unexpected_included_paths": unexpected,
            "invalid_pages": len(rejected),
            "complete": not (missing or missing_prose or invalid or unexpected or rejected)}


def lineage_retention(gold, findings, pages, answer):
    """Check authored cross-file provenance survived into each visible finding.

    This scores source association, not the truth of a model's business causal
    interpretation or runtime parameter/library binding.
    """
    accepted, rejected = verified_pages(gold, pages)
    by_id = {page["id"]: page for page in accepted}
    missing = []
    for site in gold["sites"]:
        if not site["dependencies"]:
            continue
        rows = [finding for finding in findings if finding.get("disposition") == "included"
                and finding.get("path", finding.get("relative_path")) == site["path"]
                and finding.get("start_line") == site["start_line"]]
        valid = False
        for finding in rows:
            summary = finding.get("summary", "")
            ids = finding.get("citations", finding.get("evidence_ids", []))
            referenced = {by_id[identifier]["path"] for identifier in ids
                          if isinstance(identifier, str) and identifier in by_id}
            if (set(site["dependencies"]) <= referenced and all(path in summary for path in site["dependencies"])
                    and _compact(summary) in _compact(answer)):
                valid = True
                break
        if not valid:
            missing.append(site["site_id"])
    return {"missing_source_associations": missing, "invalid_pages": len(rejected),
            "complete": not missing and not rejected, "business_semantics_verified": False}
