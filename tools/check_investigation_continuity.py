"""Supplemental final-desktop HTTP regression; never formal model acceptance.

Only neutral generated source and a synthetic TLS loopback provider are used.
The frozen 79-sample contract, scoring and timing thresholds are untouched.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.desktop_acceptance import digest, extract_package, inspect_package, source_provenance
from tools.desktop_acceptance_runtime import (LoopbackProvider, NativeWorkbench, read_journal,
    require, sha256, supplied_pages, write_profile)


SOURCE = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. TOTAL-RULE.\nDATA DIVISION.\n"
    "WORKING-STORAGE SECTION.\n01 BASE-AMOUNT PIC 9(5) VALUE 10.\n"
    "01 TOTAL-AMOUNT PIC 9(6).\nPROCEDURE DIVISION.\nMAIN.\n"
    "COMPUTE TOTAL-AMOUNT = BASE-AMOUNT * 2.\nGOBACK.\n")
QUESTION = "Explain how TOTAL-RULE computes TOTAL-AMOUNT and where the initial input value comes from."
PROMPT_A = "Write one English paragraph, without headings, tables, lists or local technical appendices."
PROMPT_B = "Keep the earlier supported explanation and finish it in one English paragraph. Explain the initial input and calculation without headings or tables."


class CompletionProvider(LoopbackProvider):
    """Reuse the isolated TLS listener while allowing a real length boundary."""
    def __init__(self, root, responder):
        super().__init__(root, responder)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_POST(self):
                try:
                    require(self.path == "/v1/chat/completions", "Unexpected provider route")
                    size = int(self.headers["Content-Length"])
                    require(0 < size <= 4 * 1024 * 1024, "Unexpected request size")
                    body = self.rfile.read(size)
                    envelope = json.loads(body)
                    payload = json.loads(envelope["messages"][-1]["content"])
                    index = len(owner.requests)
                    record = {"index": index, "received_at": time.monotonic(),
                        "request_sha256": hashlib.sha256(body).hexdigest(),
                        "envelope": envelope, "payload": payload, "source_pages": supplied_pages(payload)}
                    owner.requests.append(record)
                    (owner.root / f"request-{index + 1:03d}.json").write_bytes(body)
                    text, reason = owner.responder(envelope, payload, index)
                    require(reason in {"stop", "length"}, "Unsupported scripted finish reason")
                    record.update(response_text=text, finish_reason=reason)
                    response = json.dumps({"choices": [{"message": {"role": "assistant", "content": text},
                        "finish_reason": reason}]}, ensure_ascii=False).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
                    record["response_finished_at"] = time.monotonic()
                except Exception as error:
                    owner.errors.append(type(error).__name__ + ": " + str(error))
                    try:
                        self.send_error(500)
                    except OSError:
                        pass
                finally:
                    self.close_connection = True

        self.server.RequestHandlerClass = Handler


def verify_pages(source, payload):
    pages = supplied_pages(payload)
    require(bool(pages), "No actual source pages supplied")
    for page in pages:
        path = (source / page["relative_path"]).resolve()
        require(path.is_relative_to(source.resolve()), "Source page escaped fixture")
        require(page["source_sha256"] == sha256(path), "Source version mismatch")
        lines = path.read_text(encoding="utf-8").splitlines()
        require(page["source_text"].splitlines() == lines[page["start_line"] - 1:page["end_line"]],
                "Supplied lines do not match the fixture")
    return next(page for page in pages if "COMPUTE TOTAL-AMOUNT" in page["source_text"])


def run_checks(command, root, report=None):
    source = root / "source"
    source.mkdir()
    (source / "root.cbl").write_text(SOURCE, encoding="utf-8")
    output, profile = root / "workspace", root / "profile"
    checks, observations = {}, []
    if report is not None:
        report.update(checks=checks, observations=observations)
    phase = {"name": "default", "count": 0, "draft": None}
    expected_prompt = [PROMPT_A]

    def respond(envelope, payload, index):
        require(index < 6, "Unexpected extra model attempt")
        phase["count"] += 1
        system = envelope["messages"][0]["content"]
        require(expected_prompt[0] in system, "Current saved answer instructions missing from system")
        if expected_prompt[0] == PROMPT_B:
            require(PROMPT_A not in system, "Earlier answer instructions reused")
        require(payload["question"] == QUESTION, "Original question changed")
        page = verify_pages(source, payload)
        require("VALUE 10" in "\n".join(p["source_text"] for p in supplied_pages(payload)),
                "Initial input declaration was not supplied")
        cite = f"[{page['evidence_id']}]"
        short = "The final amount is twice the base amount." + cite
        full = "The base amount is initialized from VALUE 10; the program multiplies it by two to produce a final amount of 20." + cite
        if phase["name"] == "default":
            if phase["count"] == 1:
                time.sleep(20)
                return short, "stop"
            require(phase["count"] == 2 and payload.get("draft_answer"), "Automatic review missing")
            return full, "stop"
        if phase["name"] == "partial":
            require(phase["count"] == 1, "Explicit one-request fixture budget was ignored")
            return short, "stop"
        if phase["name"] == "truncated":
            require(phase["count"] == 1, "Explicit one-request fixture budget was ignored")
            return "The base amount is initialized from VALUE 10." + cite + " The final amount is", "length"
        require(payload.get("draft_answer") == phase["draft"], "Previous answer was not supplied intact")
        if phase["name"] == "continue_truncated":
            require(payload.get("draft_continuation") is True, "Truncated continuation path not exercised")
            return " twice the base amount, producing 20." + cite, "stop"
        require(payload.get("analysis_memory", {}).get("continuation_requested") is True,
                "Ordinary partial answer was restarted instead of continued")
        return phase["draft"] + " The base amount is initialized from VALUE 10, so the final amount is 20." + cite, "stop"

    with CompletionProvider(root / "provider", respond) as provider:
        environment = write_profile(profile, provider)
        # The shared acceptance helper intentionally installs a one-call policy.
        # This independent check must exercise the application's actual defaults.
        (profile / "agent-settings.json").write_text(json.dumps({"version": 1, "agent": {}}), encoding="utf-8")
        with NativeWorkbench(command, profile, environment, root) as workbench:
            indexed = workbench.analyze(source, output, timeout=120)
            require(indexed["job"]["status"] == "COMPLETED" and not provider.requests, "Offline index failed")
            workbench.request("POST", "/api/answer-instructions", {"answer_instructions": PROMPT_A})
            first = workbench.analyze(source, output, QUESTION, locale="en", answer_detail="detailed")
            agent = first["result"]["agent_result"]
            policy = agent["metrics"]["policy"]
            checks["default_policy_has_no_soft_target"] = (policy["delivery_target_seconds"] is None
                and policy["hard_question_seconds"] is None and policy["max_model_requests"] is None
                and policy["max_investigation_rounds"] is None)
            checks["slow_first_answer_is_automatically_completed"] = (len(provider.requests) == 2
                and agent["metrics"]["model_requests"] == 2 and agent["status"] == "ANALYZED")
            checks["custom_single_paragraph_has_no_local_appendix"] = (agent["answer"] == provider.requests[-1]["response_text"]
                and "\n" not in agent["answer"] and re.search(r"[\u3400-\u9fff]", agent["answer"]) is None)
            observations.append({"phase": "default", "run_id": agent["run_id"], "model_requests": 2,
                "elapsed_seconds": first["elapsed_seconds"], "answer_sha256": hashlib.sha256(agent["answer"].encode()).hexdigest()})
            # These separate fixtures intentionally exhaust one request to create
            # a manual continuation. They do not change the default-policy proof.
            (profile / "agent-settings.json").write_text(json.dumps({"version": 1,
                "agent": {"max_model_requests": 1, "max_answer_revisions": 0}}), encoding="utf-8")
            for name in ("partial", "truncated"):
                phase.update(name=name, count=0, draft=None)
                expected_prompt[0] = PROMPT_A
                workbench.request("POST", "/api/answer-instructions", {"answer_instructions": PROMPT_A})
                # Omitting conversation_id creates a separate question through
                # the normal analyze API, whose accepted status is HTTP 202.
                before = workbench.analyze(source, output, QUESTION, locale="en", answer_detail="detailed")
                original = before["state"]["conversation"]
                user, assistant = original["messages"]
                require(assistant["status"] == "partial" and bool(assistant["evidence_refs"]),
                        "Manual fixture did not produce a source-backed partial answer")
                phase.update(name="continue_" + name, count=0, draft=assistant["content"])
                expected_prompt[0] = PROMPT_B
                workbench.request("POST", "/api/answer-instructions", {"answer_instructions": PROMPT_B})
                workbench.stop()
                restored = workbench.start()
                require(restored["answer_instructions"] == PROMPT_B, "Updated instructions did not survive restart")
                continued = workbench.analyze(source, output, QUESTION, conversation_id=original["id"],
                    continue_message_id=user["id"], locale="en", answer_detail="detailed")
                latest = continued["state"]["conversation"]
                current = latest["messages"][-1]
                revisions = current.get("answer_revisions", [])
                checks[name + "_latest_prompt_and_existing_draft_supplied"] = phase["count"] == 1 and not provider.errors
                checks[name + "_prior_answer_preserved_as_revision"] = (len(latest["messages"]) == 2
                    and [m["id"] for m in latest["messages"]] == [user["id"], assistant["id"]]
                    and len(revisions) == 1 and revisions[0]["content"] == assistant["content"]
                    and revisions[0]["run_id"] == assistant["run_id"] and current["content"] != assistant["content"])
                checks[name + "_continuation_completes_without_discarding_text"] = (
                    current["status"] == "completed" and current["content"].startswith(assistant["content"]))
                require(bool(revisions), "Previous answer revision missing")
                revision = revisions[0]
                workbench.stop()
                workbench.start()
                reopened = workbench.request("GET", "/api/conversations/" + original["id"])["conversation"]
                restored_revision = reopened["messages"][-1]["answer_revisions"][0]
                reference = revision["evidence_refs"][0]["evidence_id"]
                evidence = workbench.request("GET", "/api/evidence?" + urlencode({"id": reference,
                    "conversation_id": original["id"], "message_id": assistant["id"], "revision_id": revision["revision_id"]}))
                checks[name + "_revision_and_citation_survive_restart"] = (restored_revision == revision
                    and evidence.get("run_id") == revision["run_id"] and bool(evidence.get("spans"))
                    and all(span["source_sha256"] == sha256(source / "root.cbl")
                        and span["source_text"].splitlines() == SOURCE.splitlines()[span["start_line"] - 1:span["end_line"]]
                        for span in evidence["spans"]))
                observations.append({"phase": name, "previous_run_id": before["result"]["agent_result"]["run_id"],
                    "run_id": continued["result"]["agent_result"]["run_id"], "revision_id": revision["revision_id"],
                    "answer_sha256": hashlib.sha256(current["content"].encode()).hexdigest()})
            journal = read_journal(output)["requests"]
            checks["requests_recorded_once_without_provider_errors"] = (not provider.errors
                and len(provider.requests) == len(journal) == 6 and all(row["state"] == "completed" for row in journal)
                and {row["request_hash"] for row in journal} == {row["request_sha256"] for row in provider.requests})
    return {"checks": checks, "observations": observations, "synthetic_model_requests": len(provider.requests),
        "fixture_sha256": {"root.cbl": sha256(source / "root.cbl")}, "injected_wait_seconds": 20,
        "timing_interpretation": "Injected loopback wait tests control flow, not real-model speed."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--archive", type=Path)
    target.add_argument("--source-root", type=Path, help="Source HTTP preview; not final executable verification.")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    require(re.fullmatch(r"[0-9a-f]{40}", args.source_commit), "Invalid source commit")
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": "investigation-continuity-check/v1", "report_execution_id": secrets.token_hex(16),
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "transport_kind": "synthetic",
        "external_model_calls": 0, "paid_model_calls": 0, "real_model_accuracy": None,
        "formal_acceptance": False, "checks": {}, "errors": [], "status": "failed"}
    try:
        if args.archive:
            require(sys.platform == "win32", "Native Windows execution required")
            report.update(inspect_package(args.archive, args.source_commit))
            binary = extract_package(args.archive, root / "extracted")
            require(digest(binary) == report["binary_sha256"], "Extracted binary mismatch")
            command = [str(binary)]
        else:
            source_root = args.source_root.resolve(strict=True)
            report.update(source_provenance(source_root, args.source_commit))
            command = [sys.executable, str(source_root / "poc/desktop_app.py")]
        report.update(run_checks(command, root, report))
        if args.archive:
            current = inspect_package(args.archive, args.source_commit)
            require(all(report[key] == value for key, value in current.items()), "Package changed during check")
            require(digest(binary) == report["binary_sha256"], "Binary changed during check")
        else:
            require(source_provenance(source_root, args.source_commit)["application_sha256"]
                == report["application_sha256"], "Source changed during check")
        report["status"] = "passed" if report["checks"] and all(report["checks"].values()) else "failed"
    except Exception as error:
        report["errors"].append({"type": type(error).__name__, "message": str(error)})
    report_path = root / "INVESTIGATION-CONTINUITY.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(report_path), "errors": report["errors"]}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
