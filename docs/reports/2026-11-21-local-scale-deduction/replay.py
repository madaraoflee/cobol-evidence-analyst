"""Time ordinary workbench jobs against physical, reproducible local sources."""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
PROGRAMS = 12000
BOOKS = 240
POLL = 0.05


def encoded(lines):
    return ("\n".join(lines) + "\n").encode()


def generated(extra):
    yield from extra.items()
    for n in range(BOOKS):
        yield f"records/FIELDS{n:03d}.cpy", encoded([f"01 GROUP-{n:03d}.",
            " 05 BATCH-CODE PIC X(8).", " 05 RUN-FLAG PIC X.", " 05 TOTAL-COUNT PIC 9(7)."])
    domains = ("INVOICE", "DEPOSIT", "BALANCE", "LEDGER", "SAVINGS", "POSTING", "BILLING", "MARGIN")
    for n in range(PROGRAMS - len(extra)):
        domain, unit = domains[n % len(domains)], f"UNIT{n:05d}"
        lines = ["IDENTIFICATION DIVISION.", f"PROGRAM-ID. {unit}.", "DATA DIVISION.", "WORKING-STORAGE SECTION.",
                 f"COPY FIELDS{n % BOOKS:03d}.", "01 OPEN-AMOUNT PIC 9(7)V99.", "01 VALUE-DELTA PIC 9(5)V99.",
                 "01 FINAL-AMOUNT PIC 9(7)V99.", "PROCEDURE DIVISION.", f"UPDATE-{domain}."]
        # Interleave the longer modules so the selected implementation is not the only meaningful file.
        blocks = 96 if n % 240 == 0 else 3 + n % 4
        for step in range(blocks):
            lines.extend([f"IF BATCH-CODE = '{n % 1000:03d}-{step:02d}'",
                          f" COMPUTE FINAL-AMOUNT = OPEN-AMOUNT + VALUE-DELTA * {1 + step % 5}",
                          "ELSE", " MOVE OPEN-AMOUNT TO FINAL-AMOUNT", "END-IF."])
        if n % 16 == 0:
            lines.append(f'CALL "UNIT{(n + 1):05d}" USING FINAL-AMOUNT.')
        lines.append("GOBACK.")
        yield f"modules/{domain.lower()}/{unit.lower()}.cbl", encoded(lines)


def prepare(root, extra):
    source = root / "source"
    source.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    stats = Counter()
    maximum = 0
    for name, raw in generated(extra):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != raw:
                raise ValueError("Different existing sample at " + name)
        else:
            path.write_bytes(raw)
        digest.update(name.encode() + b"\x00" + hashlib.sha256(raw).digest())
        count = len(raw.splitlines())
        maximum = max(maximum, count)
        stats.update(files=1, bytes=len(raw), physical_lines=count)
        stats[path.suffix] += 1
        stats["longer_than_400_lines"] += count > 400
    if sum(f.is_file() for f in source.rglob("*")) != stats["files"]:
        raise ValueError("Unexpected extra sources")
    return {**stats, "max_lines": maximum, "manifest_sha256": digest.hexdigest()}


def save(result, path):
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def product_fingerprint(product):
    fingerprint = hashlib.sha256()
    for file in sorted((product / "poc").glob("*.py")):
        fingerprint.update(file.name.encode() + b"\x00" + hashlib.sha256(file.read_bytes()).digest())
    return fingerprint.hexdigest()


def benchmark_programs(variant):
    from local_grep_acceptance import originals

    files = originals("explicit_grep")
    if variant in {"dense", "report"}:
        for ordinal in range(65):
            preamble = ["IDENTIFICATION DIVISION.", f"PROGRAM-ID. NOTICE-{ordinal:03d}."]
            if ordinal >= 56:
                body = ["DATA DIVISION.", "WORKING-STORAGE SECTION.", "01 DISPLAY-FLAG PIC X.",
                        "01 MESSAGE-TEXT PIC X(80).", "PROCEDURE DIVISION.", 'IF DISPLAY-FLAG = "Y"',
                        ' MOVE "reserve refund" TO MESSAGE-TEXT', "END-IF."]
            else:
                body = ["PROCEDURE DIVISION.", 'DISPLAY "reserve refund".']
            files[f"notice-{ordinal:03d}.cbl"] = encoded([*preamble, *body, "GOBACK."])
    if variant == "report":
        for i in range(9):
            files[f"hint{i}.cbl"] = encoded([
                "IDENTIFICATION DIVISION.", f"PROGRAM-ID. BANNER{i}.", "PROCEDURE DIVISION.",
                'IF DISPLAY-FLAG = "Y"', ' MOVE "reserve refund" TO MESSAGE-TEXT', "END-IF.",
                "IF PAGE-NO > 10", " SUBTRACT 1 FROM PAGE-NO",
                "END-IF.", "GOBACK."])
    return files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--product-root", type=Path, default=ROOT)
    parser.add_argument("--mode", choices=("index", "questions"), required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--variant", choices=("ordinary", "dense", "report"), default="ordinary")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    root = args.workspace.resolve()
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "settings-index-0.json"
    if args.mode == "index" and (root / "output").exists():
        parser.error("Choose a fresh --workspace to time initial indexing")
    if args.mode == "questions" and not state_path.exists():
        parser.error("Run --mode index against this workspace before timing questions")
    product = args.product_root.resolve()
    sys.path[:0] = [str(product / "poc"), str(ROOT / "poc/tests")]
    from agent_policy import AgentPolicy
    from analyze_source import analyze_source
    from company_api import CompanyAPIConfig
    from local_grep_acceptance import QUESTION
    from web_app import WorkbenchState

    files = benchmark_programs(args.variant)
    result = {"mode": args.mode, "variant": args.variant, "question": QUESTION, "corpus": prepare(root, files),
              "irrelevant_keyword_programs": len(files) - 3,
              "conditional_message_moves": {"ordinary": 0, "dense": 9, "report": 18}[args.variant],
              "irrelevant_page_number_arithmetic": 9 if args.variant == "report" else 0,
              "platform": {"system": platform.system(), "release": platform.release(), "macos": platform.mac_ver()[0],
                           "arch": platform.machine(), "logical_cpus": os.cpu_count(), "python": platform.python_version()},
              "started_utc": datetime.now(timezone.utc).isoformat(), "poll_seconds": POLL, "runs": []}
    result["product_sha256"] = product_fingerprint(product)
    save(result, args.result)
    config = CompanyAPIConfig("https://sample.example.invalid/v1", "sample-model", api_key="test-only")
    with patch("socket.socket.connect", side_effect=AssertionError("Unexpected network connection")) as network:
        for _ in range(args.runs if args.mode == "questions" else 1):
            session = {"root": root, "requests": [], "supplied": {}}

            def analyzer(source, output, **opts):
                return analyze_source(source, output, transport=lambda request: respond(request, session), **opts)

            app = WorkbenchState(analyzer=analyzer, config_provider=lambda: config,
                                 state_path=state_path, policy_provider=AgentPolicy)
            options = {"source": str(root / "source"), "output": str(root / "output")}
            if args.mode == "questions":
                options.update(question=QUESTION, allow_network=True)
            observation = run(app, options, session, args.mode)
            if product_fingerprint(product) != result["product_sha256"]:
                raise RuntimeError("Product sources changed during the timed run")
            observation["network_connections"] = network.call_count
            result["runs"].append(observation)
            save(result, args.result)
            print(json.dumps({k: observation[k] for k in ("status", "seconds", "first_business_seconds")
                              if k in observation}), flush=True)
            if observation["status"] != "COMPLETED" or (args.mode == "questions" and not observation["passed"]):
                break
        network.assert_not_called()


def run(app, options, session, mode):
    first, first_business, phases = None, None, []
    message_findings, page_number_findings = {}, {}
    start = session["began"] = time.perf_counter()
    current = app.start(options)
    accepted = time.perf_counter() - start
    while True:
        job = app.get_job(current["job_id"])
        now = time.perf_counter() - start
        progress = job["progress"]
        phase = progress.get("phase")
        if not phases or phases[-1]["phase"] != phase:
            phases.append({"phase": phase, "seconds": round(now, 4)})
        for finding in progress.get("checked_findings") or []:
            refs = finding.get("sources", [])
            if not refs:
                continue
            text = finding["text"]
            item = {"seconds": round(now, 4), "text": text, "sources": refs}
            report_page = any(ref["relative_path"].startswith(("notice-", "hint")) for ref in refs)
            if "MESSAGE-TEXT" in text and report_page:
                message_findings.setdefault(text, item)
            if "PAGE-NO" in text and report_page:
                page_number_findings.setdefault(text, item)
            if first is None:
                first = item
            covers_debit = any(ref["relative_path"] == "start.cbl" and ref["start_line"] <= 4
                               and ref["end_line"] >= 5 for ref in refs)
            if first_business is None and covers_debit and all(field in text for field in
                    ("1-RESERVECREDIT", "CREDIT-AMT", "NET-DUE", "减去", "跳过")):
                first_business = item
                print("first business", round(now, 4), text, flush=True)
        if job["status"] != "RUNNING":
            break
        time.sleep(POLL)
    observation = {"status": job["status"], "seconds": round(now, 4), "accepted_seconds": round(accepted, 4),
                   "phases": phases}
    if job["status"] != "COMPLETED":
        observation["error"] = job.get("error")
        return observation
    output = job["result"]
    observation["snapshot_id"] = output.get("snapshot_id")
    if mode == "index":
        search = output["diagnosis"].get("repository_search", {})
        fields = ("indexed_files", "content_hash_verified", "indexed_pages", "program_count", "full_text_complete")
        observation["indexed_scope"] = {k: search.get(k) for k in fields}
        return observation
    for finding in (first, first_business):
        if finding:
            finding["matches_original"] = all(
                ref["evidence_id"] in session["supplied"] and
                all(ref[k] == session["supplied"][ref["evidence_id"]][k] for k in
                    ("relative_path", "start_line", "end_line")) for ref in finding["sources"])
    observation.update(misleading_display_findings=list(message_findings.values()),
                       misleading_number_findings=list(page_number_findings.values()), first_checked=first,
                       first_business=first_business, first_business_seconds=first_business["seconds"]
                       if first_business else None)
    verify_delivery(app, output, observation, session)
    return observation


def verify_delivery(app, output, observation, session):
    from local_progressive_acceptance import checked_page
    from test_local_grep_followup_delivery import LocalGrepFollowupDeliveryTests

    conversation = output["conversation"]
    message = conversation["messages"][-1]
    citations = []
    for ref in message.get("cited_evidence_ids", []):
        if not ref.startswith("ev_page_"):
            continue
        span, = app.evidence(ref, conversation["id"], message["id"])["spans"]
        if span["integrity"] != "VALID":
            raise AssertionError("Invalid original")
        path = span["relative_path"]
        citations.append(checked_page(span, {path: (session["root"] / "source" / path).read_bytes()}))
    metrics = (output["agent"].get("agent_result") or {}).get("metrics", {})
    observation.update(product_timing_seconds=metrics.get("timing_seconds"), tool_calls=metrics.get("tool_calls"),
                       answer=message["content"], answer_status=message["status"], stop_reason=message.get("stop_reason"),
                       citations=citations, rounds=session["requests"],
                       index_reused=output.get("diagnosis", {}).get("index_reused"))
    finding = observation["first_business"]
    try:
        LocalGrepFollowupDeliveryTests().assert_complete_deductions({
            "status": message["status"], "stop_reason": message.get("stop_reason"), "answer": message["content"],
            "network_connections": 0, "while_waiting": {"checked_findings": [finding] if finding else []},
            "inline_citations": message.get("cited_evidence_ids", []), "citations": citations})
        expected = {"start.cbl", "legacy/route.cbl", "archive/settlement.cbl"}
        observation["business_answer_correct"] = ({row["relative_path"] for row in citations} == expected
                and all(term not in message["content"] for term in ("PAGE-NO", "MESSAGE-TEXT")))
        observation["passed"] = (observation["business_answer_correct"] and finding["matches_original"]
                                 and observation["first_checked"] == finding
                                 and not observation["misleading_display_findings"]
                                 and not observation["misleading_number_findings"])
    except AssertionError as error:
        observation.update(passed=False, oracle_failure=str(error)[:1000])


def respond(request, session):
    from company_api import TransportResponse
    from local_grep_acceptance import choose_reply
    from local_progressive_acceptance import checked_page

    ctx = json.loads(json.loads(request.body)["messages"][-1]["content"])
    pages = [page for bundle in ctx.get("source_context", []) for page in bundle["pages"]]
    for page in pages:
        name = page["relative_path"]
        original = {name: (session["root"] / "source" / name).read_bytes()}
        session.setdefault("supplied", {})[page["evidence_id"]] = checked_page(page, original)
    answer = choose_reply(pages, ctx.get("completed_actions", []), initial_deferral=True)
    row = {"number": len(session["requests"]) + 1, "seconds": round(time.perf_counter() - session["began"], 4),
           "source_files": sorted({p["relative_path"] for p in pages}),
           "answer_candidate_complete": isinstance(answer, str) and "最终应收等于" in answer,
           "reply": answer if isinstance(answer, dict) else answer[:50]}
    session["requests"].append(row)
    print("model reply", row["number"], row["seconds"], row["reply"], flush=True)
    text = json.dumps(answer, ensure_ascii=False) if isinstance(answer, dict) else answer
    return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": text},
                                                          "finish_reason": "stop"}]}))


if __name__ == "__main__":
    main()
