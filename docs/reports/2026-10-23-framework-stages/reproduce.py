"""Replay the neutral stage question with a network-blocked scripted response."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.code_root.resolve()
    sys.path[:0] = [str(root / "poc"), str(root / "poc/tests")]
    from test_prompt_projection import expand_projection
    from test_framework_stage_business import FrameworkStageBusinessTests, MANUAL, QUESTION, SOURCE

    case = FrameworkStageBusinessTests()
    try:
        case.setUp()
        started = time.perf_counter()
        answer = case.ask()
        elapsed = time.perf_counter() - started
        request = case.requests[0]
        expanded = expand_projection(request)
        orientation = expanded.get("business_analysis_brief", {}).get("business_orientation", {})
        data = {
            "question": QUESTION,
            "answer_origin": "scripted_transport_not_model",
            "source_sha256": hashlib.sha256(SOURCE.encode()).hexdigest(),
            "manual_sha256": hashlib.sha256(MANUAL.encode()).hexdigest(),
            "status": answer.get("status"),
            "answer": answer.get("answer"),
            "orientation": orientation,
            "framework_facts": expanded.get("framework_facts", []),
            "framework_references": expanded.get("framework_references", []),
            "metrics": {"scripted_requests": len(case.requests), "paid_model_calls": 0,
                        "offline_seconds": elapsed, "network_blocked": True},
        }
    finally:
        case.doCleanups()
    serialized = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(serialized, encoding="utf-8")
    else:
        print(serialized, end="")


if __name__ == "__main__":
    main()
