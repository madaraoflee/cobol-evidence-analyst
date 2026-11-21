"""Measure a real browser on the local synthetic workbench created by replay.py."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

import replay


def product_fingerprint(product):
    digest = hashlib.sha256()
    paths = [*product.glob("*.py"), *(p for p in (product / "web").rglob("*") if p.is_file())]
    for path in sorted(paths):
        digest.update(path.relative_to(product).as_posix().encode() + b"\x00" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--product-root", type=Path, default=replay.ROOT)
    parser.add_argument("--browser-executable", required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    root = args.workspace.resolve()
    # The selected data and saved project must be the benchmark pair. Keep all user settings separate.
    saved = root / "settings-index-0.json"
    settings = json.loads(saved.read_text())
    if any(Path(settings[key]).resolve() != root / key for key in ("source", "output")):
        raise ValueError("Expected an indexed benchmark workspace")
    sys.path[:0] = [str(args.product_root / "poc"), str(replay.ROOT / "poc/tests")]
    product = args.product_root.resolve() / "poc"
    fingerprint = product_fingerprint(product)
    results = {"product_sha256_including_web": fingerprint, "runs": []}
    for _ in range(args.runs):
        results["runs"].append(measure(args, root, saved))
        if product_fingerprint(product) != fingerprint:
            raise RuntimeError("Product changed during timing")
        replay.save(results, args.result)


def measure(args, root, saved):
    from agent_policy import AgentPolicy
    from analyze_source import analyze_source
    from company_api import CompanyAPIConfig
    from local_grep_acceptance import QUESTION
    from local_progressive_acceptance import checked_page
    from test_local_grep_followup_delivery import LocalGrepFollowupDeliveryTests
    from web_app import WorkbenchState, create_server

    session = {"root": root, "requests": [], "began": time.perf_counter()}
    def analyze(source, output, **opts):
        return analyze_source(source, output, transport=lambda req: replay.respond(req, session), **opts)

    with tempfile.TemporaryDirectory() as temp, patch("socket.socket.connect",
            side_effect=AssertionError("This model uses an in-memory transport")) as connect:
        frame_output = Path(temp) / "frames.json"
        state_path = Path(temp) / "settings.json"
        shutil.copyfile(saved, state_path)
        app = WorkbenchState(analyzer=analyze, policy_provider=AgentPolicy, state_path=state_path,
            config_provider=lambda: CompanyAPIConfig("https://sample.example.invalid/v1", "sample-model", api_key="test-only"))
        session["app"] = app
        with create_server(0, app=app, web_root=args.product_root / "poc/web") as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                subprocess.run(["node", str(Path(__file__).with_suffix(".cjs")), server.origin, QUESTION,
                                str(frame_output), args.browser_executable], check=True, timeout=240)
            finally:
                server.shutdown()
                worker.join()
        connect.assert_not_called()
        result = json.loads(frame_output.read_text())
        conversation = app.state()["conversation"]
        message = conversation["messages"][-1]
        result.update(model_rounds=len(session["requests"]), answer_status=message["status"],
                      stop_reason=message.get("stop_reason"), citations=[], model_network_connections=0)
        for ref in message.get("cited_evidence_ids", []):
            span, = app.evidence(ref, conversation["id"], message["id"])["spans"]
            if span["integrity"] != "VALID":
                raise AssertionError("Source reference could not be opened")
            name = span["relative_path"]
            result["citations"].append(checked_page(span, {name: (root / "source" / name).read_bytes()}))
        first = result.get("first_deduction") or {}
        result["first_deduction_source_verified"] = bool(first and any(
            p["relative_path"] == "start.cbl" and p["start_line"] <= 4 and p["end_line"] >= 5
            for p in session.get("supplied", {}).values()))
        try:
            refs = LocalGrepFollowupDeliveryTests().assert_complete_deductions({
                "status": message["status"], "answer": result["final"]["text"], "stop_reason": message.get("stop_reason"),
                "network_connections": connect.call_count, "while_waiting": {"checked_findings": [first] if first else []},
                "inline_citations": message.get("cited_evidence_ids", []), "citations": result["citations"]})
            result["answer_and_sources_verified"] = {ref["relative_path"] for ref in refs} == {
                "start.cbl", "legacy/route.cbl", "archive/settlement.cbl"}
        except AssertionError as error:
            result.update(answer_and_sources_verified=False, oracle_failure=str(error)[:600])
        result["passed"] = bool(result["answer_and_sources_verified"] and result["first_deduction_source_verified"]
                                and first.get("in_viewport") and result["final"]["in_viewport"]
                                and not result["report_labels_in_findings"])
        return result


if __name__ == "__main__":
    main()
