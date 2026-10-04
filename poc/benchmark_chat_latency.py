#!/usr/bin/env python3
"""Measure local work up to the first model request, without calling a model.

Use a disposable benchmark index/source tree. The normal question path may
refresh selected sources or create a local semantic cache. No source or question
text is printed; results contain only timings and counts, never answer scores.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import statistics
import sqlite3
import time

from business_chat import run_business_chat
from company_api import CompanyAPIConfig


class _FirstRequestCaptured(BaseException):
    """Stop the real pipeline precisely where its provider request is ready."""


def measure_first_request(database_path, source_root, question, *, framework_reference_path=""):
    started = time.perf_counter()
    result = {"llm_called": False, "answer_quality_measured": False}
    phases = []

    def progress(event):
        phase = event["phase"]
        if not phases or phases[-1]["phase"] != phase:
            phases.append({"phase": phase, "elapsed_seconds": round(time.perf_counter() - started, 4)})

    def transport(request):
        captured_at = time.perf_counter() - started
        payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
        pages = [page for bundle in payload["source_context"] for page in bundle["pages"]]
        paths = sorted({page["relative_path"] for page in pages})
        largest_file = None
        if paths:
            slots = ",".join("?" for _ in paths)
            with closing(sqlite3.connect(database_path)) as db:
                largest_file = db.execute(
                    f"SELECT MAX(line_count) FROM source_files WHERE relative_path IN ({slots})", paths).fetchone()[0]
        result.update(first_request_seconds=round(captured_at, 4),
            source_file_count=len(paths), largest_provided_file_line_count=largest_file,
            source_page_count=len(pages),
            source_characters=sum(len(page["source_text"]) for page in pages),
            request_bytes=len(request.body),
            retrieval_status=payload["retrieval_status"]["state"])
        raise _FirstRequestCaptured()

    try:
        run_business_chat(question, Path(database_path), Path(source_root),
            CompanyAPIConfig("https://offline.example.invalid/v1", "offline-evaluation", api_key="offline-placeholder"),
            allow_network=False, transport=transport, progress=progress,
            framework_reference_path=framework_reference_path)
    except _FirstRequestCaptured:
        result["first_request_reached"] = True
    else:
        result["first_request_reached"] = False
    result["phases"] = phases
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--framework-reference", default="")
    args = parser.parse_args()
    if not 1 <= args.repeat <= 20:
        parser.error("--repeat must be between 1 and 20")
    runs = [measure_first_request(args.database, args.source, args.question,
                                 framework_reference_path=args.framework_reference)
            for _ in range(args.repeat)]
    timings = [run["first_request_seconds"] for run in runs if run["first_request_reached"]]
    print(json.dumps({"llm_called": False, "answer_quality_measured": False, "runs": runs,
        "median_first_request_seconds": statistics.median(timings) if timings else None},
        ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
