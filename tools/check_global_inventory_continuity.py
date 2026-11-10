"""Supplemental whole-library check through the final Windows EXE HTTP API.

The generated small files and loopback responses test retention and termination,
not real-model business accuracy. The frozen 79-sample contract is unchanged.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sys
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.check_investigation_continuity import CompletionProvider
from tools.desktop_acceptance import digest, extract_package, inspect_package, source_provenance
from tools.desktop_acceptance_runtime import NativeWorkbench, require, sha256, supplied_pages, write_profile

QUESTION = "List all premium calculation formulas across the entire repository."
FILE_COUNT = 257
EXPECTED_ERROR = "Intentional global inventory server failure"


def create_fixture(source):
    source.mkdir(parents=True)
    expected = {}
    for ordinal in range(FILE_COUNT):
        name = f"charge-{ordinal:03}.cbl"
        formula = f"COMPUTE PREMIUM-AMT = BASE-AMT * {ordinal % 7 + 1}."
        text = (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. CHARGE-{ordinal:03}.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 BASE-AMT PIC 9(5) VALUE 10.\n01 PREMIUM-AMT PIC 9(7).\n"
            f"PROCEDURE DIVISION.\nMAIN.\n*> Business charge {ordinal:03}\n{formula}\nGOBACK.\n")
        (source / name).write_text(text, encoding="utf-8")
        expected[name] = {"formula": formula, "line": 10, "sha256": sha256(source / name)}
    return expected


class InventoryResponder:
    def __init__(self, source):
        self.source = source
        self.accepted = set()
        self.review_parts = set()
        self.reviewed_narratives = set()
        self.analysis_requests = 0

    def respond(self, envelope, payload, index):
        if index == 1:
            raise RuntimeError(EXPECTED_ERROR)
        if payload.get("schema") == "semantic-answer-review/v1":
            key = (payload["answer_sha256"], payload["evidence_sha256"])
            require(key not in self.review_parts, "Completed strict review was repeated")
            self.review_parts.add(key)
            self.reviewed_narratives.update(re.findall(r"Business explanation ([a-f0-9]+)",
                "\n".join(unit["text"] for unit in payload["units"])))
            evidence = payload["evidence"]
            require(evidence and payload["units"], "Strict review lacks actual source or text")
            for page in evidence:
                path = self.source / page["path"]
                require(page["sha256"] == sha256(path), "Review changed original source identity")
                require(page["text"] in path.read_text(encoding="utf-8"), "Review lacks original source text")
            result = {"answer_sha256": key[0], "evidence_sha256": key[1], "reviews": [{
                "unit_id": unit["unit_id"], "verdict": "supported", "reason": "Controlled original-source check.",
                "evidence": [{"reference_id": evidence[0]["reference_id"], "quote": evidence[0]["text"].splitlines()[0]}]}
                for unit in payload["units"]], "omissions": []}
        else:
            require(payload.get("schema") != "business-analysis-compaction/v1", "Full catalog reached bounded model compaction")
            require(payload.get("question") == QUESTION, "Global question changed")
            items = payload.get("repository_inventory", {}).get("items", [])
            require(bool(items), "Global investigation lost its current batch")
            pages = supplied_pages(payload)
            findings = []
            for item in items:
                require(item["candidate_id"] not in self.accepted, "Completed inventory item was repeated")
                covering = [page for page in pages if page["relative_path"] == item["relative_path"]
                    and page["start_line"] <= item["start_line"] and page["end_line"] >= item["end_line"]]
                if not covering:
                    continue
                for page in covering:
                    path = self.source / page["relative_path"]
                    require(path.resolve().is_relative_to(self.source.resolve()), "Fixture source escaped its root")
                    require(page["source_sha256"] == sha256(path), "Analysis changed source identity")
                    require(page["source_text"].splitlines() == path.read_text(encoding="utf-8").splitlines()[
                        page["start_line"] - 1:page["end_line"]], "Analysis source was clipped or replaced")
                row = {"candidate_id": item["candidate_id"], "evidence_ids": [page["evidence_id"] for page in covering]}
                if item["kind"] == "source_review":
                    row.update(disposition="reviewed", summary="Reviewed the complete charge source and its business arithmetic.")
                else:
                    page = covering[0]
                    lines = page["source_text"].splitlines()
                    formula = "\n".join(lines[item["start_line"] - page["start_line"]:
                        item["end_line"] - page["start_line"] + 1]).strip()
                    require(formula.startswith("COMPUTE PREMIUM-AMT"), "Unexpected candidate arithmetic")
                    row.update(disposition="included", summary="The charge applies its shown multiplier to the declared base amount.",
                        formula=formula, conditions="Applies on the supplied MAIN path; production runtime inputs remain unverified.")
                findings.append(row)
                self.accepted.add(item["candidate_id"])
            require(findings, "Current batch made no source-backed progress")
            self.analysis_requests += 1
            result = {"answer": "## Business calculation\nThe reviewed business charges retain their formulas, applicability and original source.",
                "inventory_findings": findings}
        return json.dumps(result, ensure_ascii=False), "stop"


def validate_rows(rows, expected):
    require(len(rows) == len(expected), "Inventory row count differs from the independent fixture")
    require(len({row["relative_path"] for row in rows}) == len(expected), "Inventory duplicates a physical business position")
    for row in rows:
        spec = expected.get(row["relative_path"])
        require(spec is not None and row["formula"] == spec["formula"] and row["start_line"] == spec["line"]
            and row["end_line"] == spec["line"] and row["source_sha256"] == spec["sha256"]
            and row.get("summary") and row.get("conditions") and row.get("evidence_ids"), "Inventory lost a business formula or original binding")


def lookup_stop_checks(workbench, provider, source, output, profile):
    """No deployment request/time ceiling may hide an unproductive lookup loop."""
    (profile / "agent-settings.json").write_text(json.dumps({"version": 1, "agent": {
        "max_model_requests": None, "max_question_seconds": None, "hard_question_seconds": None,
        "max_answer_revisions": 0}}), encoding="utf-8")
    workbench.stop()
    workbench.start()
    checks, observations = {}, []
    for case, query in (("invalid_lookup", ""), ("empty_lookup", "unmatched-neutral-business-term")):
        seen, initial_requests, initial_errors = [], len(provider.requests), list(provider.errors)

        def respond(envelope, payload, index):
            seen.append(payload)
            require(len(seen) <= 2, "Unproductive lookup issued a third model request")
            require(payload.get("question") == QUESTION, "Lookup fixture question changed")
            require(bool(payload.get("repository_inventory", {}).get("durable_archive", {}).get("handle")),
                    "Lookup fixture did not enter the production paged branch")
            if len(seen) == 2:
                previous = payload["repository_inventory"].get("lookup_result", {})
                if case == "invalid_lookup":
                    require(previous.get("error") == "INVENTORY_QUERY_INVALID", "Invalid lookup explanation was lost")
                else:
                    require(previous.get("rows") == [] and "error" not in previous,
                            "A valid empty lookup became a system error")
            return json.dumps({"inventory_lookup": {"query": query, "cursor": 0}}), "stop"

        provider.responder = respond
        result = workbench.analyze(source, output, QUESTION, answer_detail="detailed", locale="en")
        require(result["result"] is not None, "Lookup stop lost the investigation result")
        agent = result["result"]["agent_result"]
        descriptor = agent.get("repository_inventory", {})
        counts = descriptor.get("counts", {})
        active_policy = agent["metrics"]["policy"]
        require(active_policy["max_model_requests"] is None and active_policy["hard_question_seconds"] is None,
                "Lookup termination was tested under an artificial request or time ceiling")
        require(result["job"]["status"] == "COMPLETED" and agent["status"] == "PARTIAL"
            and agent["metrics"].get("convergence_stop_reason") == "no_evidence_progress",
            "Lookup did not stop at the source-progress boundary")
        require(len(seen) == len(provider.requests) - initial_requests == agent["metrics"]["model_requests"] == 2
            and provider.errors == initial_errors, "Lookup stop depended on a provider failure or extra request")
        require(descriptor.get("paged") and not descriptor["complete"] and counts["reviewed"] == 0
            and counts["pending"] == counts["candidates"] == 2 * FILE_COUNT,
            "Unproductive lookup cleared or lost unfinished global work")
        checks[case + "_stops_after_two_requests_without_deployment_ceiling"] = True
        checks[case + "_preserves_every_pending_item"] = True
        observations.append({"phase": case, "run_id": agent["run_id"], "status": agent["status"],
            "stop_reason": agent["stop_reason"], "model_requests": 2, "counts": counts,
            "convergence_stop_reason": agent["metrics"]["convergence_stop_reason"],
            "request_limit": active_policy["max_model_requests"], "hard_seconds": active_policy["hard_question_seconds"],
            "elapsed_seconds": result["elapsed_seconds"]})
    return {"checks": checks, "observations": observations}


def run_lookup_checks(command, root):
    source, output, profile = root / "source", root / "workspace", root / "profile"
    create_fixture(source)
    # The responder is replaced before a question is sent.
    with CompletionProvider(root / "provider", lambda *_: (_ for _ in ()).throw(
            AssertionError("Unexpected model call before lookup fixture"))) as provider:
        environment = write_profile(profile, provider)
        with NativeWorkbench(command, profile, environment, root) as workbench:
            indexed = workbench.analyze(source, output)
            require(indexed["job"]["status"] == "COMPLETED" and not provider.requests, "Lookup fixture import failed")
            return lookup_stop_checks(workbench, provider, source, output, profile)


def run_checks(command, root):
    source, output, profile = root / "source", root / "workspace", root / "profile"
    expected = create_fixture(source)
    fixture = InventoryResponder(source)
    checks, observations = {}, []
    with CompletionProvider(root / "provider", fixture.respond) as provider:
        environment = write_profile(profile, provider, {"max_model_requests": 8, "max_answer_revisions": 0})
        with NativeWorkbench(command, profile, environment, root) as workbench:
            indexed = workbench.analyze(source, output)
            require(indexed["job"]["status"] == "COMPLETED" and not provider.requests, "Global fixture import failed")
            first = workbench.analyze(source, output, QUESTION, answer_detail="detailed", locale="en")
            require(first["result"], "Failed global turn lost its saved result")
            agent = first["result"]["agent_result"]
            descriptor = agent.get("repository_inventory", {})
            require(descriptor.get("paged") and descriptor["counts"]["reviewed"] > 0
                and not descriptor["complete"] and len(provider.requests) == 2, "Planned server failure lost progress or was automatically retried")
            first_handle, first_total = descriptor["handle"], descriptor["total"]
            conversation = first["state"]["conversation"]
            conversation_id, user_id, message_id = conversation["id"], conversation["messages"][0]["id"], conversation["messages"][1]["id"]
            accepted_before_restart = set(fixture.accepted)
            workbench.stop()
            workbench.start()
            require(len(provider.requests) == 2, "Restoring history automatically retried a failed model request")
            counts = [descriptor["counts"]["reviewed"]]
            for turn in range(48):
                result = workbench.analyze(source, output, QUESTION, conversation_id=conversation_id,
                    continue_message_id=user_id, answer_detail="detailed", locale="en")
                require(result["result"], "Explicit continuation lost its result")
                agent = result["result"]["agent_result"]
                descriptor = agent["repository_inventory"]
                counts.append(descriptor["counts"]["reviewed"])
                observations.append({"turn": turn + 1, "run_id": agent["run_id"], "status": agent["status"],
                    "counts": descriptor["counts"], "elapsed_seconds": result["elapsed_seconds"]})
                if agent["status"] == "ANALYZED":
                    break
            require(agent["status"] == "ANALYZED" and descriptor["complete"]
                and descriptor["counts"]["pending"] == descriptor["counts"]["review_pending"] == 0, "Global investigation did not finish within its finite fixture bound")
            checks["failure_restart_and_explicit_continuation_preserve_completed_work"] = (
                accepted_before_restart <= fixture.accepted and counts == sorted(counts) and len(observations) > 1)
            checks["every_original_and_business_position_classified_once"] = len(fixture.accepted) == 2 * FILE_COUNT
            checks["strict_review_completed_after_analysis"] = bool(fixture.review_parts)
            query = {"handle": descriptor["handle"], "conversation_id": conversation_id, "message_id": message_id}
            def get(**options):
                return workbench.request("GET", "/api/repository-inventory?" + urlencode({**query, **options}))
            rows, cursor = [], 0
            while True:
                page = get(cursor=cursor, page_size=37)
                require(page["handle"] == descriptor["handle"] and page["complete"] and page["total"] == FILE_COUNT, "Paged result version or completeness differs")
                rows.extend(page["rows"])
                if page["next_cursor"] is None:
                    break
                require(page["next_cursor"] > cursor, "Inventory page cursor did not advance")
                cursor = page["next_cursor"]
            validate_rows(rows, expected)
            checks["all_pages_deliver_every_business_formula"] = True
            exported = get(export="jsonl")
            data = [json.loads(line) for line in exported["jsonl"].splitlines()]
            validate_rows([row for row in data if row.get("record_type") == "formula" and row.get("disposition") == "included"], expected)
            require(any(row.get("record_type") == "business_narrative" for row in data), "Full export lost the business explanation")
            checks["complete_export_retains_formulas_and_business_explanations"] = True
            narrative_ids = {row["narrative_id"] for row in data
                if row.get("record_type") == "business_narrative" and not row.get("superseded")}
            require(narrative_ids <= fixture.reviewed_narratives, "Exported business explanation was not strictly reviewed")
            checks["all_exported_business_explanations_passed_strict_review"] = bool(narrative_ids)
            for offset in (0, FILE_COUNT // 2, FILE_COUNT - 1):
                row = get(cursor=offset, page_size=1)["rows"][0]
                evidence = get(action="evidence", candidate_id=row["candidate_id"], index=0)
                require(evidence["status"] == "READY" and evidence["spans"] and all(span["integrity"] == "VALID"
                    and span["source_sha256"] == expected[span["relative_path"]]["sha256"]
                    and row["formula"] in span["source_text"] for span in evidence["spans"]), "Saved original evidence cannot be verified")
            checks["first_middle_last_originals_are_accessible"] = True
            saved = workbench.request("GET", "/api/conversations/" + conversation_id)["conversation"]
            message = next(row for row in saved["messages"] if row["id"] == message_id)
            revision = next((row for row in message.get("answer_revisions", [])
                if row.get("repository_inventory", {}).get("handle") == first_handle), None)
            require(revision is not None, "Early result lost its history entry")
            old = get(handle=first_handle, revision_id=revision["revision_id"], page_size=1)
            checks["earlier_published_result_does_not_follow_later_progress"] = old["total"] == first_total and not old["complete"]
            checks["only_the_planned_failure_occurred"] = provider.errors == ["RuntimeError: " + EXPECTED_ERROR]
            lookup = lookup_stop_checks(workbench, provider, source, output, profile)
            checks.update(lookup["checks"])
            observations.extend(lookup["observations"])
            return {"checks": checks, "observations": observations, "source_files": FILE_COUNT,
                "formula_positions": len(rows), "synthetic_model_requests": len(provider.requests),
                "strict_review_parts": len(fixture.review_parts), "fixture_sha256": {name: value["sha256"] for name, value in expected.items()}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--archive", type=Path)
    target.add_argument("--source-root", type=Path, help="Source-only preview, not final EXE acceptance.")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    require(re.fullmatch(r"[0-9a-f]{40}", args.source_commit), "Invalid source commit")
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": "global-inventory-continuity/v1", "report_execution_id": secrets.token_hex(16),
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "status": "failed", "checks": {}, "errors": [],
        "transport_kind": "synthetic", "external_model_calls": 0, "paid_model_calls": 0,
        "formal_acceptance": False, "real_model_accuracy": None}
    try:
        if args.archive:
            require(sys.platform == "win32", "Native Windows execution required")
            report.update(inspect_package(args.archive, args.source_commit))
            binary = extract_package(args.archive, root / "extracted")
            require(digest(binary) == report["binary_sha256"], "Extracted binary changed")
            command = [str(binary)]
        else:
            source_root = args.source_root.resolve(strict=True)
            report.update(source_provenance(source_root, args.source_commit))
            command = [sys.executable, str(source_root / "poc/desktop_app.py")]
        report.update(run_checks(command, root))
        current = inspect_package(args.archive, args.source_commit) if args.archive else source_provenance(source_root, args.source_commit)
        require(current["application_sha256"] == report["application_sha256"], "Application changed during check")
        if args.archive:
            require(current == {key: report[key] for key in current} and digest(binary) == report["binary_sha256"], "Package changed during check")
        report["status"] = "passed" if report["checks"] and all(report["checks"].values()) else "failed"
    except Exception as error:
        report["errors"].append({"type": type(error).__name__, "message": str(error)})
    report_path = root / "GLOBAL-INVENTORY-CONTINUITY.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(report_path), "errors": report["errors"]}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
