#!/usr/bin/env python3
"""Compare request preparation using baseline and current evidence accounting.

Builds a disposable synthetic corpus and captures the first provider request
without making a model or network call. Every captured request must be byte-for-
byte identical; corpus generation and semantic-cache warm-up are not timed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time

from benchmark_business_facts import prepare_corpus
from business_index import build_business_index
from company_api import CompanyAPIConfig
from repository_discovery import ensure_repository_search


class _RequestCaptured(BaseException):
    """Stop as soon as the real pipeline has prepared its first request."""


def _load_baseline(directory, ref):
    path = directory / "baseline_evidence_context.py"
    path.write_bytes(subprocess.run(["git", "show", f"{ref}:poc/evidence_context.py"],
        cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True).stdout)
    spec = importlib.util.spec_from_file_location("benchmark_baseline_evidence_context", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.EvidenceContext


def _measure(context_class, database, source, reference):
    import business_chat

    result = {}
    original = business_chat.EvidenceContext
    business_chat.EvidenceContext = context_class
    started = time.perf_counter()

    def capture(request):
        elapsed = time.perf_counter() - started
        payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
        pages = [page for bundle in payload["source_context"] for page in bundle["pages"]]
        body = request.body.encode("utf-8") if isinstance(request.body, str) else request.body
        result.update(seconds=elapsed, request_sha256=hashlib.sha256(body).hexdigest(),
            source_file_count=len({page["relative_path"] for page in pages}),
            source_page_count=len(pages),
            source_characters=sum(len(page["source_text"]) for page in pages),
            request_bytes=len(body))
        raise _RequestCaptured()

    try:
        business_chat.run_business_chat("FLOW-00000 的业务逻辑和处理流程是什么？", database, source,
            CompanyAPIConfig("https://offline.example.invalid/v1", "offline-evaluation",
                             api_key="offline-placeholder"),
            allow_network=False, transport=capture, framework_reference_path=str(reference))
    except _RequestCaptured:
        return result
    finally:
        business_chat.EvidenceContext = original
    raise RuntimeError("First request was not reached")


def run(directory, *, baseline_ref, files=1, lines=5000, repeat=5):
    from evidence_context import EvidenceContext

    directory.mkdir(parents=True, exist_ok=True)
    source = prepare_corpus(directory, files, lines)
    database, reference = directory / "index.sqlite", directory / "no-reference.md"
    build_business_index(source, database, source_format="free", quiet=True,
                         framework_reference_path=reference)
    ensure_repository_search(database, source)
    baseline = _load_baseline(directory, baseline_ref)
    _measure(EvidenceContext, database, source, reference)
    runs = {"baseline": [], "current": []}
    for iteration in range(repeat):
        order = (("baseline", baseline), ("current", EvidenceContext))
        for label, context in order if iteration % 2 == 0 else reversed(order):
            runs[label].append(_measure(context, database, source, reference))
    fingerprints = {item["request_sha256"] for values in runs.values() for item in values}
    if len(fingerprints) != 1:
        raise RuntimeError("Captured provider requests differ; speed comparison is invalid")
    before = statistics.median(item["seconds"] for item in runs["baseline"])
    after = statistics.median(item["seconds"] for item in runs["current"])
    report = {"synthetic": True, "llm_called": False, "answer_quality_measured": False,
        "baseline_ref": baseline_ref, "programs": files, "program_lines": lines,
        "repeat": repeat, "warm_semantic_cache": True, "requests_identical": True,
        "baseline_median_seconds": before, "current_median_seconds": after,
        "speedup": before / after, "runs": runs}
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True, help="Disposable synthetic benchmark directory")
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--files", type=int, default=1)
    parser.add_argument("--lines", type=int, default=5000)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 20:
        parser.error("--repeat must be between 1 and 20")
    print(json.dumps(run(args.directory, baseline_ref=args.baseline_ref,
        files=args.files, lines=args.lines, repeat=args.repeat), indent=2))


if __name__ == "__main__":
    main()
