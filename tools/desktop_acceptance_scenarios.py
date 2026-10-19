"""Real desktop HTTP lifecycle and semantic mechanism acceptance scenarios.

No product modules are imported. Every supplied evidence page is observed in
the actual request and results are read from the compiled workbench output.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import traceback
from urllib.parse import urlencode

from tools.desktop_acceptance_runtime import (LoopbackProvider, NativeWorkbench, SlowHeaders,
    decoded_navigation, read_journal, require, sha256, supplied_pages, write_profile)
from tools.acceptance_diagnostics import capture_diagnostics


RUNTIME_CASES = {"claim_repair", "soft_target_answer", "activation_trigger", "cancel_unknown", "deadline_unknown", "conversation", "dense_record"}
QUESTION = "FLOW-CLAIM OUT-AMOUNT 怎样计算？"
ACTIVATION_QUESTION = "JOB-STEP 的 batch 操作是怎么 trigger的？源码能确认外部调度吗？"


def activation_sources():
    return {"job-step.cbl": "IDENTIFICATION DIVISION.\nPROGRAM-ID. JOB-STEP.\n"
        "PROCEDURE DIVISION.\nMAIN.\nDISPLAY 'RUNNING'.\nGOBACK.\n",
        "launcher.cbl": "IDENTIFICATION DIVISION.\nPROGRAM-ID. LAUNCHER.\nDATA DIVISION.\n"
        "WORKING-STORAGE SECTION.\n01 READY-FLAG PIC 9.\nPROCEDURE DIVISION.\nMAIN.\n"
        "IF READY-FLAG = 1\nCALL 'JOB-STEP'\nEND-IF.\nGOBACK.\n"}


def _activation_supply_checks(payload):
    investigation = decoded_navigation(payload)["question_investigation"]
    obligations = {item["kind"]: item for item in investigation["required_items"]}
    pages = {page["evidence_id"]: page for page in supplied_pages(payload)}
    expected_sha = hashlib.sha256(activation_sources()["launcher.cbl"].encode()).hexdigest()
    def covers(identifier, line, text):
        page = pages.get(identifier, {})
        first = page.get("start_line", 0)
        lines = page.get("source_text", "").splitlines()
        return (page.get("relative_path") == "launcher.cbl" and page.get("source_sha256") == expected_sha
            and first <= line <= page.get("end_line", 0) and 0 <= line - first < len(lines)
            and lines[line - first] == text and not page.get("span_truncated"))
    callers = (investigation.get("activation_evidence") or {}).get("callers", [])
    matched = [row for row in callers if row.get("target_name") == "JOB-STEP"
        and row.get("relative_path") == "launcher.cbl" and row.get("source_sha256") == expected_sha
        and row.get("line") == 9 and row.get("occurrence_id") and row.get("scope_known") is True
        and row.get("binding_confirmed") is True and row.get("compilation_status") == "active"
        and row.get("conditions_supplied") is True and row.get("execution_verified") is False
        and any(covers(identifier, 9, "CALL 'JOB-STEP'") for identifier in row.get("evidence_ids", []))]
    guarded = any(any(str(guard.get("condition", "")).removeprefix("IF ").strip() == "READY-FLAG = 1"
        and guard.get("outcome") is True
        and any(covers(identifier, 8, "IF READY-FLAG = 1") for identifier in guard.get("evidence_ids", []))
        for guard in row.get("conditions", [])) for row in matched)
    return {"completeness_activation_obligations_supplied": all(obligations.get(kind, {}).get("status") == "SATISFIED"
                for kind in ("entry_point", "caller_invocation", "activation_conditions")),
        "evidence_activation_caller_and_guard": bool(matched) and guarded,
        "quality_scheduler_not_inferred": obligations.get("scheduler_binding", {}).get("status") == "OPEN"
            and obligations.get("scheduler_binding", {}).get("reason") == "external_schedule_binding_not_verified"
            and (investigation.get("activation_evidence") or {}).get("scheduler_binding_verified") is False
            and (investigation.get("activation_evidence") or {}).get("execution_verified") is False}


def _activation(command, root, case, sample_index):
    source = root / "source"
    source.mkdir()
    for name, text in activation_sources().items():
        (source / name).write_bytes(text.encode())
    output = root / "workspace"
    def respond(envelope, payload, index):
        require(index == 0, "Activation should not require another model planning request")
        require(payload["question"] == ACTIVATION_QUESTION, "Wrong activation question")
        require(all(_activation_supply_checks(payload).values()), "Activation source/guard/version not supplied")
        caller = next(page for page in supplied_pages(payload) if page["relative_path"] == "launcher.cbl"
                      and "IF READY-FLAG = 1" in page["source_text"] and "CALL 'JOB-STEP'" in page["source_text"])
        return ("JOB-STEP由LAUNCHER在READY-FLAG = 1的源码条件下通过CALL调用。"
                "源码未证明外部调度绑定，也未证明实际执行。[" + caller["evidence_id"] + "]")
    with LoopbackProvider(root / "provider", respond) as provider:
        environment = write_profile(root / "profile", provider)
        with NativeWorkbench(command, root / "profile", environment, root) as workbench:
            cold = _index(workbench, source, output, provider)
            observation = workbench.analyze(source, output, ACTIVATION_QUESTION, answer_detail="brief")
            agent = _checked_answer(observation, provider)
            answer = agent["answer"]
            visible = observation["state"]["conversation"]["messages"][-1]["content"]
            pages = provider.requests[0]["source_pages"]
            return [_row(case, sample_index, observation, provider, cold=cold, checks={
                **_activation_supply_checks(provider.requests[0]["payload"]),
                "quality_activation_condition_preserved": "READY-FLAG = 1的源码条件下通过CALL调用" in answer,
                "quality_external_scheduler_unknown": "未证明外部调度绑定" in answer and "未证明实际执行" in answer
                    and agent["claims_semantically_verified"] is False,
                "completeness_activation_in_first_request": len(provider.requests) == 1,
                "evidence_supplied_hashes_match": all(page["source_sha256"] == sha256(source / page["relative_path"])
                                                      for page in pages),
                "evidence_visible_answer_matches": visible == answer,
                "evidence_answer_ledger_hash": agent["claim_ledger"]["answer_sha256"] == hashlib.sha256(answer.encode()).hexdigest()})]


def _formula_source(dense=False):
    declarations = ("01 SOURCE-RECORD.\n05 IN-AMOUNT PIC 9(9).\n05 OUT-AMOUNT PIC 9(9).\n"
        "05 STATUS-CODE PIC 9(9).\n" + "".join(f"05 VALUE-{index:05d} PIC 9(9).\n" for index in range(1997))) if dense else (
        "01 IN-AMOUNT PIC 9(6)V99 VALUE 4.\n01 OUT-AMOUNT PIC 9(6)V99.\n01 STATUS-CODE PIC 9.\n")
    return ("IDENTIFICATION DIVISION.\nPROGRAM-ID. FLOW-CLAIM.\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n"
        + declarations + "PROCEDURE DIVISION.\nMAIN.\nMOVE 0 TO STATUS-CODE.\n"
        "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nGOBACK.\n")


def _source(root, text, filename="calculate.cbl"):
    path = root / "source"
    path.mkdir(parents=True)
    (path / filename).write_text(text, encoding="utf-8")
    return path


def _formula_response(source_hash, *, wrong=True):
    def respond(_envelope, payload, _index):
        require(payload["question"] == QUESTION, "Unexpected question reached provider")
        pages = supplied_pages(payload)
        require(bool(pages) and all(page["source_sha256"] == source_hash for page in pages), "Supplied source version mismatch")
        cited = next(page for page in pages if "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2" in page["source_text"])
        require("MOVE 0 TO STATUS-CODE" in cited["source_text"], "Independent assignment was not supplied")
        evidence = cited["evidence_id"]
        return (f"OUT-AMOUNT = IN-AMOUNT * {999 if wrong else 2}。[{evidence}] "
                f"MOVE 0 TO STATUS-CODE。[{evidence}] 输入金额保留原字段名称。")
    return respond


def _checked_answer(observation, provider):
    require(observation["job"]["status"] == "COMPLETED", "Question did not complete")
    require(not provider.errors, "Synthetic provider failed: " + str(provider.errors))
    result = observation["result"]
    require(result and result.get("agent_result"), "No business answer was returned")
    agent = result["agent_result"]
    require(agent.get("model_answer_recorded") is True, "No received model answer was recorded")
    require(agent["metrics"]["model_requests"] == 1, "Unexpected additional model request")
    return agent


def _row(case, sample_index, observation, provider, *, turn_id="1", cold=None, checks=None):
    agent = observation.get("result", {}).get("agent_result") or {}
    answer = agent.get("answer")
    run_path = (agent.get("metrics") or {}).get("quality_trace_path")
    request = provider.requests[-1] if provider.requests else None
    diagnostics = None
    if run_path:
        trace = json.loads(Path(run_path).read_text(encoding="utf-8"))
        diagnostics = capture_diagnostics(trace, agent, trace_sha256=sha256(run_path))
    return {"id": case["id"], "turn_id": turn_id, "sample_index": sample_index,
        "run_id": Path(run_path).stem if run_path else None,
        "answer_sha256": hashlib.sha256(answer.encode()).hexdigest() if answer else None,
        "elapsed_seconds": observation["elapsed_seconds"], "timings": {"cold_index_seconds": cold,
            "first_provider_seconds": max(0, request["received_at"] - observation["started_at"]) if request else None,
            "question_seconds": observation["elapsed_seconds"]},
        "checks": checks or {}, "progress_samples": observation.get("progress_samples", []),
        "phase_timings_seconds": (agent.get("metrics") or {}).get("timing_seconds", {}),
        "status": "passed", "kind": "answer", "index_reused": cold is None,
        "question_index_reused": (agent.get("metrics") or {}).get("repository_rebuilt") is False,
        "agent_status": agent.get("status"), "real_model_accuracy": None, "business_answer_correctness": None,
        "acceptance_diagnostics": diagnostics, "answer": answer, "stop_reason": agent.get("stop_reason")}


def _formula_checks(observation, provider, source_hash):
    agent = _checked_answer(observation, provider)
    ledger = agent["claim_ledger"]
    repaired = [claim for entry in ledger["entries"] for claim in entry.get("revised_claims", [])]
    answer = agent["answer"]
    formula_ok = "999" not in answer and "OUT-AMOUNT = IN-AMOUNT * 2" in answer
    supported_move = "MOVE 0 TO STATUS-CODE" in answer and ledger["counts"].get("supported", 0) >= 1
    return {"quality_wrong_formula_repaired": formula_ok,
        "quality_independent_move_preserved": supported_move,
        "completeness_critical_claims_verified": (ledger["counts"].get("contradicted") == 1
            and supported_move and any(claim.get("support_status") == "supported"
            and claim.get("assertion", {}).get("expression") == ["IN-AMOUNT", "*", "2"] for claim in repaired)),
        "evidence_supplied_hashes_match": all(page["source_sha256"] == source_hash
            for page in provider.requests[-1]["source_pages"]),
        "evidence_visible_answer_matches": observation["state"]["conversation"]["messages"][-1]["content"] == answer,
        "quality_formula_verified": formula_ok and supported_move,
        "unsupported_prose_not_falsely_verified": agent["claims_semantically_verified"] is False}


def _index(workbench, source, output, provider):
    observation = workbench.analyze(source, output, timeout=180)
    require(observation["job"]["status"] == "COMPLETED", "Offline indexing failed")
    require(not provider.requests, "Indexing contacted a model")
    return observation["elapsed_seconds"]


def _index_stamp(output):
    """Read build timestamps and source identities without mutating the index."""
    path = (output / "structural-index.sqlite").resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
        return {"metadata": database.execute("SELECT key,value FROM metadata WHERE key IN "
                "('indexed_at_utc','snapshot_id','parser_version') ORDER BY key").fetchall(),
            "sources": database.execute("SELECT relative_path,sha256,indexed_at_utc FROM source_files ORDER BY relative_path").fetchall()}


def _old_index_guard(workbench, source, output, provider):
    database = output / "structural-index.sqlite"
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE metadata SET value='business-sparse-previous' WHERE key='parser_version'")
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before, count = sha256(database), len(provider.requests)
    observation = workbench.analyze(source, output, QUESTION, timeout=30)
    return (observation["job"]["status"] == "COMPLETED"
        and observation["result"].get("reason_code") == "SOURCE_INDEX_UPDATE_REQUIRED"
        and len(provider.requests) == count and sha256(database) == before)


def _claim(command, root, case, sample_index):
    source = _source(root, _formula_source())
    output = root / "workspace"
    source_hash = sha256(source / "calculate.cbl")
    ordinary = _formula_response(source_hash)
    soft_target = case["id"] == "soft_target_answer"
    def respond(envelope, payload, index):
        if soft_target:
            time.sleep(1.25)
        return ordinary(envelope, payload, index)
    with LoopbackProvider(root / "provider", respond) as provider:
        environment = write_profile(root / "profile", provider,
            {"delivery_target_seconds": 1, "hard_question_seconds": 120}
            if soft_target else None)
        with NativeWorkbench(command, root / "profile", environment, root) as workbench:
            cold = _index(workbench, source, output, provider)
            observation = workbench.analyze(source, output, QUESTION)
            checks = _formula_checks(observation, provider, source_hash)
            if soft_target:
                agent = observation["result"]["agent_result"]
                target = agent["metrics"].get("delivery_target", {})
                requests = read_journal(output)["requests"]
                checks["quality_target_does_not_discard_first_answer"] = (
                    target.get("reached") is True and target.get("seconds") == 1
                    and target.get("hard_limit_seconds") == 120
                    and target.get("inflight_request_cancelled_by_target") is False
                    and agent.get("answer_origin") == "model" and agent.get("answer_available") is True
                    and agent.get("stop_reason") != "QUESTION_TIME_BUDGET_EXHAUSTED")
                checks["lifecycle_slow_response_journal_completed_once"] = (
                    len(requests) == 1 and requests[0]["state"] == "completed"
                    and len(provider.requests) == 1)
            row = _row(case, sample_index, observation, provider, cold=cold, checks=checks)
            if soft_target:
                row["timings"]["synthetic_provider_delay_seconds"] = 1.25
                row["timing_interpretation"] = "injected_wait_tests_delivery_not_real_model_performance"
            checks["lifecycle_old_index_requires_explicit_update"] = _old_index_guard(workbench, source, output, provider)
            return [row]


def _unknown(command, root, case, sample_index):
    source = _source(root, _formula_source())
    output = root / "workspace"
    source_hash = sha256(source / "calculate.cbl")
    slow = SlowHeaders()
    correct = _formula_response(source_hash, wrong=False)
    def respond(envelope, payload, index):
        correct(envelope, payload, index)
        return slow if index == 0 else correct(envelope, payload, index)
    budget = 3 if case["id"] == "deadline_unknown" else 30
    with LoopbackProvider(root / "provider", respond) as provider:
        environment = write_profile(root / "profile", provider, {
            "max_question_seconds": budget,
            **({"hard_question_seconds": budget} if case["id"] == "deadline_unknown" else {})})
        with NativeWorkbench(command, root / "profile", environment, root) as workbench:
            cold = _index(workbench, source, output, provider)
            index_before = _index_stamp(output)
            started = time.monotonic()
            job_id = workbench.submit(source, output, QUESTION)
            require(slow.started.wait(15), "Provider did not receive the slow request")
            until = time.monotonic() + 1
            while slow.chunks < 2 and time.monotonic() < until:
                time.sleep(0.02)
            cancelled = None
            if case["id"] == "cancel_unknown":
                cancelled = time.monotonic()
                workbench.request("POST", "/api/jobs/" + job_id + "/cancel", {})
            job = workbench.wait_job(job_id, timeout=10)
            finished = time.monotonic()
            question_index_reused = _index_stamp(output) == index_before
            journal = read_journal(output)
            require(len(journal["requests"]) == 1, "Unexpected model request before restart")
            before = journal["requests"][0]
            unknown = before["state"] == "started" and before["response_json"] is None
            conversation_id = workbench.request("GET", "/api/state")["conversation"]["id"]
            workbench.stop()
            restored = workbench.start()
            restore_ok = restored["conversation"]["id"] == conversation_id
            guarded = workbench.analyze(source, output, QUESTION, conversation_id=conversation_id, timeout=30)
            guarded_requests = read_journal(output)["requests"]
            guarded_ok = (guarded["result"].get("reason_code") == "REMOTE_REQUEST_OUTCOME_UNKNOWN"
                and len(provider.requests) == 1 and guarded_requests == journal["requests"])
            # A new explicit conversation may ask independently. It must not
            # inherit or clear the first conversation's uncertain request.
            independent = workbench.analyze(source, output, QUESTION, timeout=30)
            independent_agent = _checked_answer(independent, provider)
            all_requests = read_journal(output)["requests"]
            isolated = (len(provider.requests) == 2 and independent_agent["metrics"]["model_requests"] == 1
                and any(row == before for row in all_requests) and len(all_requests) == 2
                and independent["state"]["conversation"]["id"] != conversation_id)
            cancellation_seconds = finished - cancelled if cancelled else None
            checks = {"lifecycle_remote_unknown_preserved": unknown,
                "lifecycle_no_automatic_resend": guarded_ok,
                "lifecycle_restart_restored": restore_ok,
                "lifecycle_unknown_isolated_between_conversations": isolated}
            if cancelled:
                checks["lifecycle_cancel_prompt"] = (job["status"] == "CANCELLED"
                    and cancellation_seconds <= case.get("limits", {}).get("cancel_seconds", 2))
            else:
                checks["lifecycle_total_deadline_enforced"] = (job["status"] == "COMPLETED"
                    and 2 <= finished - started <= case.get("limits", {}).get("question_seconds", 8))
            return [{"id": case["id"], "turn_id": "1", "sample_index": sample_index,
                "run_id": before["run_id"], "answer_sha256": None, "kind": "lifecycle", "status": "passed",
                "elapsed_seconds": finished - started, "timings": {"cold_index_seconds": cold,
                    "first_provider_seconds": provider.requests[0]["received_at"] - started,
                    "question_seconds": finished - started, "cancel_seconds": cancellation_seconds},
                "checks": checks, "progress_samples": workbench.progress_samples,
                "index_reused": False,
                "question_index_reused": question_index_reused,
                "agent_status": None, "real_model_accuracy": None, "business_answer_correctness": None,
                "remote_request_state": "started", "remote_outcome": "unknown",
                "explicit_independent_conversation_requests": 1}]


def _dense(command, root, case):
    source = _source(root, _formula_source(dense=True))
    output = root / "workspace"
    source_hash = sha256(source / "calculate.cbl")
    rows = []
    with LoopbackProvider(root / "provider", _formula_response(source_hash)) as provider:
        environment = write_profile(root / "profile", provider)
        with NativeWorkbench(command, root / "profile", environment, root) as workbench:
            cold = _index(workbench, source, output, provider)
            for sample_index in range(1, case.get("repeats", 1) + 1):
                observation = workbench.analyze(source, output, QUESTION)
                checks = _formula_checks(observation, provider, source_hash)
                agent = observation["result"]["agent_result"]
                pages = provider.requests[-1]["source_pages"]
                checks["completeness_source_supplied"] = any(page["source_text"].rstrip() ==
                    (source / "calculate.cbl").read_text(encoding="utf-8").rstrip() for page in pages)
                checks["lifecycle_index_reused"] = agent["metrics"]["repository_rebuilt"] is False
                documents = []
                for path in (output / "semantic-scopes").glob("*.sqlite"):
                    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                        documents.extend(json.loads(row[0]) for row in db.execute("SELECT ir_json FROM semantic_ir_documents"))
                checks["completeness_dense_ir_fields"] = any(len(ir["data_items"]) == 2001
                    and sum(bool(item.get("picture")) for item in ir["data_items"]) == 2000
                    and {"COMPUTE", "MOVE"}.issubset({item["statement_type"] for item in ir["statements"]})
                    for ir in documents)
                row = _row(case, sample_index, observation, provider,
                    cold=cold if sample_index == 1 else None, checks=checks)
                row["source_sha256"] = source_hash
                rows.append(row)
    return rows


CONVERSATION_QUESTIONS = [
    "REPORTPG 里第一种页汇总 PAGE-SUM 和第二种账户汇总 ACCOUNT-SUM，各自从哪里累加，什么时候清零？",
    "刚才第二种情况呢？换页也会让它归零吗？",
    "它从哪里累加，又为什么最后变成零？",
]


def _conversation_source():
    return ("IDENTIFICATION DIVISION.\nPROGRAM-ID. REPORTPG.\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n"
        "01 TRAN-AMT PIC S9(7)V99 VALUE 25.00.\n01 PAGE-SUM PIC S9(7)V99 VALUE 0.\n"
        "01 ACCOUNT-SUM PIC S9(7)V99 VALUE 0.\n01 GRAND-SUM PIC S9(7)V99 VALUE 0.\n"
        "01 REPORT-PAGE-SUM PIC S9(7)V99.\n01 REPORT-ACCOUNT-SUM PIC S9(7)V99.\n"
        "01 LINE-COUNT PIC 9(3) VALUE 20.\n01 PAGE-SIZE PIC 9(3) VALUE 20.\n"
        "01 CURR-CARD PIC 9(4) VALUE 1.\n01 TRAN-CARD PIC 9(4) VALUE 2.\n"
        "01 FIRST-ITEM PIC X VALUE 'N'.\nPROCEDURE DIVISION.\nMAIN.\n"
        "PERFORM ADD-TRANSACTION.\nIF CURR-CARD NOT = TRAN-CARD\nIF FIRST-ITEM = 'N'\n"
        "PERFORM ACCOUNT-TOTALS\nEND-IF\nMOVE TRAN-CARD TO CURR-CARD\nEND-IF.\nGOBACK.\n"
        + "\n".join("*> neutral source padding " + "x" * 108 for _ in range(360)) + "\n"
        "ADD-TRANSACTION.\nADD TRAN-AMT TO PAGE-SUM ACCOUNT-SUM.\nIF LINE-COUNT >= PAGE-SIZE\n"
        "PERFORM PAGE-TOTALS\nEND-IF.\nEXIT.\nPAGE-TOTALS.\nMOVE PAGE-SUM TO REPORT-PAGE-SUM.\n"
        "ADD PAGE-SUM TO GRAND-SUM.\nMOVE 0 TO PAGE-SUM.\nEXIT.\nACCOUNT-TOTALS.\n"
        "MOVE ACCOUNT-SUM TO REPORT-ACCOUNT-SUM.\nMOVE 0 TO ACCOUNT-SUM.\nEXIT.\n")


def _aggregate_conversation(rows, *, started, finished, first_provider_seconds, cold):
    """One manifest sample covers the complete measured three-turn exchange."""
    require(len(rows) == 3 and [row["turn_id"] for row in rows] == ["1", "2", "3"],
            "Conversation sample must contain exactly three ordered turns")
    labels = {label for row in rows for label in row["checks"]}
    checks = {label: all(row["checks"].get(label) is True for row in rows) for label in labels}
    return {"id": rows[0]["id"], "turn_id": "1", "sample_index": rows[0]["sample_index"],
        "run_id": rows[-1]["run_id"], "answer_sha256": rows[-1]["answer_sha256"],
        "elapsed_seconds": finished - started,
        "timings": {"cold_index_seconds": cold, "first_provider_seconds": first_provider_seconds,
                    "question_seconds": finished - started},
        "checks": checks, "status": "passed" if all(checks.values()) else "failed", "kind": "answer",
        "index_reused": False, "question_index_reused": all(row["question_index_reused"] for row in rows),
        "agent_status": rows[-1].get("agent_status"), "real_model_accuracy": None, "business_answer_correctness": None,
        "turns": rows, "acceptance_diagnostics": rows[-1].get("acceptance_diagnostics"),
        "answer": rows[-1].get("answer"), "stop_reason": rows[-1].get("stop_reason")}


def _conversation_origin_checks(trace, pages, expected_fields):
    """VALUE provenance must be established by the executable's actual evidence."""
    from tools.desktop_acceptance_cases import ranges_supplied
    investigation = (trace.get("final") or {}).get("question_investigation") or {}
    inputs = next((item for item in investigation.get("required_items", []) if item.get("kind") == "inputs"), {})
    witnesses = inputs.get("source_flow_origins") or []
    ids = {page["evidence_id"] for page in pages}
    matched = {field: [witness for witness in witnesses if witness.get("field") == field]
               for field in expected_fields}
    closed = inputs.get("status") == "SATISFIED" and all(matched.values())
    conditional = True
    for field, proofs in matched.items():
        for proof in proofs:
            closed &= (proof.get("status") == "source_origin_supplied" and not proof.get("unknown_reasons")
                       and bool(proof.get("reader_occurrences")) and bool(proof.get("evidence_ids"))
                       and set(proof["evidence_ids"]) <= ids)
            initial = [item for item in proof.get("initial_definitions", []) if item.get("field") == field]
            conditional &= (proof.get("runtime_value_known") is False and proof.get("execution_verified") is False
                            and bool(initial) and bool(proof.get("activation_semantics")))
            for item in initial:
                locations = [{"path": ref["relative_path"], "sha256": ref["source_sha256"],
                              "start_line": ref["start_line"], "end_line": ref["end_line"]}
                             for ref in item.get("source_refs", [])]
                conditional &= (item.get("runtime_value_known") is False
                                and item.get("status") == "conditional_initialization_source"
                                and item.get("value") == ("25.00" if field == "TRAN-AMT" else "0")
                                and "first_activation" in item.get("activation_semantics", "")
                                and ranges_supplied(pages, locations))
    return {"completeness_value_origin_closed": bool(closed),
            "quality_value_activation_not_runtime_proof": bool(closed and conditional)}


def _conversation(command, root, case, sample_index):
    source = _source(root, _conversation_source(), "report.cbl")
    output = root / "workspace"
    source_hash = sha256(source / "report.cbl")
    (source / "other.cbl").write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. OTHERPG.\n"
        "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 OTHER-AMOUNT PIC 9.\n"
        "PROCEDURE DIVISION.\nMAIN.\nMOVE 9 TO OTHER-AMOUNT.\nGOBACK.\n", encoding="utf-8")
    def respond(envelope, payload, index):
        require(index < 3 and payload["question"] == CONVERSATION_QUESTIONS[index], "Unexpected conversation request")
        subject = payload["session_subject"]
        expected = ["PAGE-SUM", "ACCOUNT-SUM"] if index == 0 else ["ACCOUNT-SUM"]
        require(subject["status"] == "resolved" and subject["root_paths"] == ["report.cbl"]
            and subject["focus_entities"] == expected, "Conversation subject did not follow the second case")
        require(sum(message["role"] == "user" for message in envelope["messages"][1:-1]) == index,
            "Wrong conversation history was supplied")
        pages = supplied_pages(payload)
        require(pages and all(page["relative_path"] == "report.cbl" and page["source_sha256"] == source_hash
            for page in pages), "Unrelated or wrong-version conversation evidence")
        code = "\n".join(page["source_text"] for page in pages)
        for statement in ("ADD TRAN-AMT TO PAGE-SUM ACCOUNT-SUM.", "MOVE 0 TO PAGE-SUM.",
                "MOVE 0 TO ACCOUNT-SUM.", "IF LINE-COUNT >= PAGE-SIZE", "IF CURR-CARD NOT = TRAN-CARD"):
            require(statement in code, "Critical conversation evidence missing")
        cited = next(page for page in pages if "MOVE 0 TO ACCOUNT-SUM." in page["source_text"])
        return ("ACCOUNT-SUM 在 ACCOUNT-TOTALS 中被清零；换页执行 PAGE-TOTALS，"
            "应结合两条路径的控制条件区分。[" + cited["evidence_id"] + "]")
    rows = []
    with LoopbackProvider(root / "provider", respond) as provider:
        environment = write_profile(root / "profile", provider)
        with NativeWorkbench(command, root / "profile", environment, root) as workbench:
            cold = _index(workbench, source, output, provider)
            conversation_id = archived_reference = None
            conversation_started = time.monotonic()
            for index, question in enumerate(CONVERSATION_QUESTIONS):
                if index == 2:
                    workbench.stop()
                    restored = workbench.start()
                    require(restored["conversation"]["id"] == conversation_id, "Conversation restart did not restore history")
                observation = workbench.analyze(source, output, question, conversation_id=conversation_id)
                agent = _checked_answer(observation, provider)
                conversation = observation["state"]["conversation"]
                conversation_id = conversation["id"]
                checks = {"quality_followup_correct": agent["investigation_state"]["session_subject"]["focus_entities"] ==
                    (["PAGE-SUM", "ACCOUNT-SUM"] if index == 0 else ["ACCOUNT-SUM"]),
                    "completeness_history_isolated": len(conversation["messages"]) == 2 * (index + 1),
                    "evidence_supplied_hashes_match": all(page["source_sha256"] == source_hash
                        for page in provider.requests[-1]["source_pages"]),
                    "evidence_visible_answer_matches": conversation["messages"][-1]["content"] == agent["answer"]}
                trace = json.loads(Path(agent["metrics"]["quality_trace_path"]).read_text(encoding="utf-8"))
                checks.update(_conversation_origin_checks(trace, provider.requests[-1]["source_pages"],
                    ["TRAN-AMT", "PAGE-SUM", "ACCOUNT-SUM"] if index == 0 else ["TRAN-AMT", "ACCOUNT-SUM"]))
                if index == 0:
                    cited = next(page for page in provider.requests[0]["source_pages"] if
                        page["evidence_id"] in agent["answer"])
                    archived_reference = {"id": cited["evidence_id"], "conversation_id": conversation_id,
                        "message_id": conversation["messages"][-1]["id"]}
                rows.append(_row(case, sample_index, observation, provider, turn_id=str(index + 1),
                    cold=cold if index == 0 else None, checks=checks))
            conversation_finished = time.monotonic()
            # Preserve the original file in the captured provider requests;
            # a changed live file must not invalidate this archived answer.
            original = (source / "report.cbl").read_text(encoding="utf-8")
            (source / "report.cbl").write_text(original.replace("MOVE 0 TO ACCOUNT-SUM.", "MOVE 9 TO ACCOUNT-SUM."), encoding="utf-8")
            archived = workbench.request("GET", "/api/evidence?" + urlencode(archived_reference))
            historical = bool(archived.get("spans")) and all(span["source_sha256"] == source_hash
                for span in archived["spans"]) and any("MOVE 0 TO ACCOUNT-SUM." in span["source_text"] for span in archived["spans"])
            for row in rows:
                row["checks"]["evidence_historical_version_survives_live_change"] = historical
            combined = _aggregate_conversation(rows, started=conversation_started, finished=conversation_finished,
                first_provider_seconds=provider.requests[0]["received_at"] - conversation_started, cold=cold)
    return [combined]


def run_scenarios(command, root, manifest, case_ids=None):
    """Return flat samples; failures stay explicit for the strict parent gate."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    selected = set(case_ids) if case_ids else RUNTIME_CASES
    rows = []
    for case in manifest["cases"]:
        if case["id"] not in RUNTIME_CASES or case["id"] not in selected:
            continue
        repeats = 1 if case["id"] == "dense_record" else case.get("repeats", 1)
        for sample_index in range(1, repeats + 1):
            path = root / case["id"] / f"sample-{sample_index:02d}"
            path.mkdir(parents=True, exist_ok=False)
            start = time.monotonic()
            try:
                if case["id"] == "dense_record":
                    measured = _dense(command, path, case)
                else:
                    function = _activation if case["id"] == "activation_trigger" else _conversation if case["id"] == "conversation" else (
                        _unknown if case["id"] in {"cancel_unknown", "deadline_unknown"} else _claim)
                    measured = function(command, path, case, sample_index)
                for row in measured:
                    for assertion in case["assertions"]:
                        row["checks"].setdefault(assertion, False)
                    row["status"] = "passed" if all(row["checks"].values()) else "failed"
                    row["additional_checks"] = {key: value for key, value in row["checks"].items()
                        if key not in case["assertions"]}
                    row["checks"] = {key: row["checks"][key] for key in case["assertions"]}
                rows.extend(measured)
            except Exception as error:
                (path / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
                rows.append({"id": case["id"], "turn_id": "1", "sample_index": sample_index,
                    "run_id": None, "answer_sha256": None, "elapsed_seconds": time.monotonic() - start,
                    "timings": {}, "checks": {key: False for key in case["assertions"]},
                    "agent_status": None, "real_model_accuracy": None, "business_answer_correctness": None,
                    "status": "failed", "error": type(error).__name__ + ": " + str(error)})
    return rows
