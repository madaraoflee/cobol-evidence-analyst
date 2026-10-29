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
BUSINESS_QUESTION = "DECISION 返回什么？"
BUSINESS_PROMPT = ("解释结果码时必须说明业务后果和下游使用者；若仓库已有调用方，"
    "要讲清它收到结果后选择什么分支。不要只回答字段赋值或罗列程序名。格式不限。")
BUSINESS_SOURCE = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. DECISION.\nDATA DIVISION.\n"
    "LINKAGE SECTION.\n01 RESULT-CODE PIC 99.\nPROCEDURE DIVISION USING RESULT-CODE.\n"
    "MAIN.\nMOVE 12 TO RESULT-CODE.\nGOBACK.\n")
BUSINESS_CALLER = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. REQUESTFLOW.\nDATA DIVISION.\n"
    "WORKING-STORAGE SECTION.\n01 RESULT-CODE PIC 99.\n01 ALLOW-PROCESS PIC X.\n"
    "PROCEDURE DIVISION.\nMAIN.\nCALL 'DECISION' USING RESULT-CODE.\n"
    "IF RESULT-CODE = 12\nMOVE 'N' TO ALLOW-PROCESS\nELSE\n"
    "MOVE 'Y' TO ALLOW-PROCESS\nEND-IF.\nGOBACK.\n")
LANGUAGE_QUESTION = "TOTAL-RULE 的 TOTAL-AMOUNT 怎么计算？"
LANGUAGE_PROMPT = "Always answer in English."
CROSS_LANGUAGE_PROMPT = BUSINESS_PROMPT + " Always answer in English."
REVIEW_SOURCE = SOURCE.replace("TOTAL-RULE", "REVIEW-RULE").replace(
    "01 BASE-AMOUNT PIC 9(5) VALUE 10.", "COPY AMOUNT-INPUT.")
REVIEW_COPY = "01 BASE-AMOUNT PIC 9(5) VALUE 10.\n"
REVIEW_QUESTION = "Explain how REVIEW-RULE computes TOTAL-AMOUNT and where the initial BASE-AMOUNT value comes from."
REVIEW_PROMPT = ("Write one English paragraph with source citations. Explain the initial input and the calculation; "
    "identify the declaration that supplies the input. Do not include headings, tables, review notes or JSON in the final answer.")
REVIEW_REVISION = "The initial BASE-AMOUNT declaration is in amount-input.cpy, included by COPY AMOUNT-INPUT in root.cbl."


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


def verify_pages(source, payload, required_text="COMPUTE TOTAL-AMOUNT"):
    pages = supplied_pages(payload)
    require(bool(pages), "No actual source pages supplied")
    for page in pages:
        path = (source / page["relative_path"]).resolve()
        require(path.is_relative_to(source.resolve()), "Source page escaped fixture")
        require(page["source_sha256"] == sha256(path), "Source version mismatch")
        lines = path.read_text(encoding="utf-8").splitlines()
        require(page["source_text"].splitlines() == lines[page["start_line"] - 1:page["end_line"]],
                "Supplied lines do not match the fixture")
    return next(page for page in pages if required_text in page["source_text"])


def continuation_turn(conversation):
    """The public continue route accepts a source-backed unfinished turn."""
    messages = conversation.get("messages", [])
    require(len(messages) == 2 and [row.get("role") for row in messages] == ["user", "assistant"],
            "Continuation fixture must contain one question and its answer")
    user, assistant = messages
    require(user.get("status") == assistant.get("status") == "partial"
        and bool(assistant.get("evidence_refs")) and bool(assistant.get("investigation_run_id")),
        "Continuation fixture must be a genuinely partial source-backed investigation")
    return user, assistant


class IndependentReviewFixture:
    """Four wire responses; checks do not import the application's reviewer."""
    def __init__(self, source):
        self.source = source
        self.pages, self.answers, self.review_inputs = {}, [], []

    def review_response(self, payload, *, changes):
        require(payload.get("schema") == "semantic-answer-review/v1"
            and payload.get("status") == "reviewable", "Independent review protocol missing")
        require("source_context" not in payload and "draft_answer" not in payload,
                "Review was replaced by ordinary answer generation")
        answer = self.answers[-1]
        require("".join(row["text"] for row in payload["units"]) == answer
            and payload["answer_sha256"] == hashlib.sha256(answer.encode()).hexdigest()
            and payload["answer_characters"] == len(answer), "Review does not cover the exact current answer")
        evidence = payload["evidence"]
        require({row["reference_id"] for row in evidence} == set(self.pages)
            and len(evidence) == len(self.pages), "Review dropped or invented supplied source")
        for row in evidence:
            page = self.pages[row["reference_id"]]
            require((row["kind"], row["path"], row["sha256"], row["start_line"], row["end_line"], row["text"])
                == ("source_page", page["relative_path"], page["source_sha256"], page["start_line"],
                    page["end_line"], page["source_text"]), "Review changed the supplied source version or original lines")
            require(row["text_sha256"] == hashlib.sha256(row["text"].encode()).hexdigest()
                and row["text_truncated"] is False and row["text_offset_chars"] == 0,
                "Review contains a truncated or unbound original")
            require(row["source_context"] == {key: page[key] for key in
                ("include_chain", "replacement_trace", "copy_instance") if key in page},
                "Review lost COPY instance context")
        canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        require(payload["evidence_sha256"] == hashlib.sha256(canonical.encode()).hexdigest(),
                "Review evidence digest does not bind its original material")
        self.review_inputs.append(payload)
        quotes = [{"reference_id": row["reference_id"], "quote": quote}
            for row in evidence for quote in ("COMPUTE TOTAL-AMOUNT = BASE-AMOUNT * 2.", REVIEW_COPY.strip())
            if quote in row["text"] and (row["path"] == "root.cbl" or row["source_context"].get("copy_instance"))]
        require(len(quotes) == 2, "Review lacks calculation or COPY input declaration")
        value = {"answer_sha256": payload["answer_sha256"], "evidence_sha256": payload["evidence_sha256"],
            "reviews": [{"unit_id": row["unit_id"], "verdict": "supported",
                "reason": "The supplied declaration and calculation support this explanation.", "evidence": quotes}
                for row in payload["units"]], "omissions": []}
        if changes:
            value["reviews"][0].update(verdict="insufficient", evidence=[],
                reason="Identify the physical COPY declaration that supplies the initial amount.")
        return json.dumps(value, ensure_ascii=False)

    def respond(self, envelope, payload, index):
        require(index < 4, "Independent review exceeded its four-request contract")
        require(payload.get("question") == REVIEW_QUESTION, "Independent review changed the question")
        system = envelope["messages"][0]["content"]
        require(REVIEW_PROMPT in system and PROMPT_A not in system,
                "Independent review used stale saved instructions")
        if index in {1, 3}:
            require(len(envelope["messages"]) == 2 and "JSON" in system,
                    "Independent review protocol or history isolation missing")
            require(len(self.answers) == (1 if index == 1 else 2), "Review ran before its draft")
            return self.review_response(payload, changes=index == 1), "stop"
        require(payload.get("schema") != "semantic-answer-review/v1"
            and payload.get("answer_detail") == "detailed", "Expected a detailed answer or revision request")
        main = verify_pages(self.source, payload)
        verify_pages(self.source, payload, REVIEW_COPY.strip())
        declaration = next((row for row in supplied_pages(payload)
            if row["relative_path"] == "amount-input.cpy" and row.get("copy_instance")), {})
        require(declaration.get("relative_path") == "amount-input.cpy"
            and declaration.get("include_chain") and declaration.get("copy_instance"),
                "COPY declaration lacks its actual inclusion identity")
        require(declaration["include_chain"] == [{"relative_path": "root.cbl", "line": 5,
                "source_hash": sha256(self.source / "root.cbl"), "copy_name": "AMOUNT-INPUT"}]
            and re.fullmatch(r"[0-9a-f]{24}", declaration["copy_instance"])
            and declaration.get("replacement_trace") == [], "COPY inclusion does not match the original caller")
        pages = {row["evidence_id"]: row for row in supplied_pages(payload)}
        if index == 0:
            self.pages = pages
        else:
            require(pages == self.pages and payload.get("draft_answer") == self.answers[0],
                    "Revision lost the reviewed draft or original material")
            review = payload.get("answer_review", {})
            require(review.get("status") == "changes_requested" and review.get("findings")
                and review.get("answer_sha256") == self.review_inputs[0]["answer_sha256"]
                and payload.get("answer_instructions") == REVIEW_PROMPT,
                "Independent finding or current instructions did not reach revision")
        answer = ("BASE-AMOUNT starts with VALUE 10, and REVIEW-RULE multiplies that amount by two to produce "
            "a TOTAL-AMOUNT of 20. The declaration supplies an initial field value; the arithmetic statement reads "
            "the field and writes the result to the separate total field. These are distinct source locations, "
            "so the initial input and the calculation can each be checked against their own original lines. "
            "The multiplication uses BASE-AMOUNT as its operand and the literal 2 as its multiplier. "
            "TOTAL-AMOUNT is the receiving field of that expression. In the supplied procedure the calculation "
            "is followed by GOBACK, which ends this execution path. The explanation concerns that visible path "
            "and its declared initial value; it does not infer additional input changes or downstream behavior "
            "from names alone. The computed value 20 follows from the declared 10 and the explicit multiplier 2. ")
        if index == 2:
            answer += REVIEW_REVISION + " "
        answer += f"[{main['evidence_id']}][{declaration['evidence_id']}]"
        self.answers.append(answer)
        return answer, "stop"


def run_independent_review_check(command, root, checks=None, observations=None):
    """Supplemental same-command HTTP case, outside the frozen 79 samples."""
    checks = checks if checks is not None else {}
    observations = observations if observations is not None else []
    source, output, profile = root / "source", root / "workspace", root / "profile"
    source.mkdir(parents=True)
    for name, body in (("root.cbl", REVIEW_SOURCE), ("amount-input.cpy", REVIEW_COPY)):
        (source / name).write_text(body, encoding="utf-8")
    fixture = IndependentReviewFixture(source)
    with CompletionProvider(root / "provider", fixture.respond) as provider:
        environment = write_profile(profile, provider, {"max_model_requests": 4, "max_answer_revisions": 1})
        with NativeWorkbench(command, profile, environment, root) as workbench:
            indexed = workbench.analyze(source, output)
            require(indexed["job"]["status"] == "COMPLETED" and not provider.requests, "Independent fixture index failed")
            workbench.request("POST", "/api/answer-instructions", {"answer_instructions": PROMPT_A})
            workbench.request("POST", "/api/answer-instructions", {"answer_instructions": REVIEW_PROMPT})
            workbench.stop()
            require(workbench.start()["answer_instructions"] == REVIEW_PROMPT, "Review instructions did not survive restart")
            result = workbench.analyze(source, output, REVIEW_QUESTION, locale="en", answer_detail="detailed")
            require(result["job"]["status"] == "COMPLETED" and result["result"], "Independent review question failed")
            agent = result["result"]["agent_result"]
            trace = json.loads((output / "quality-traces" / (agent["run_id"] + ".json")).read_text(encoding="utf-8"))
            stages = [row["stage"] for row in trace["rounds"]]
            journal = read_journal(output)["requests"]
            checks["independent_review_runs_revision_and_fresh_review_over_http"] = (
                len(provider.requests) == len(journal) == agent["metrics"]["model_requests"] == 4
                and stages[0] in {"answer", "investigate"}
                and stages[1:] == ["semantic_review", "semantic_revise", "semantic_review"]
                and not provider.errors and all(row["state"] == "completed" for row in journal)
                and {row["request_hash"] for row in journal} == {row["request_sha256"] for row in provider.requests})
            review = agent.get("answer_completion", {}).get("semantic_review", {})
            final_digest = hashlib.sha256(agent["answer"].encode()).hexdigest()
            checks["independent_review_binds_final_diagnostics_to_revised_answer"] = (
                agent["status"] == "ANALYZED" and len(fixture.answers) == len(fixture.review_inputs) == 2
                and agent["answer"] == fixture.answers[-1] and REVIEW_REVISION in agent["answer"]
                and review.get("status") == "model_reviewed" and review.get("validation") == "strict_payload_bound"
                and review.get("coverage", {}).get("complete") is True
                and review.get("answer_sha256") == review.get("final_answer_sha256") == final_digest
                and agent.get("claims_semantically_verified") is False and review.get("semantic_verification") is False
                and trace.get("semantic_review") == review
                and [row["status"] for row in trace.get("semantic_reviews", [])] == ["changes_requested", "model_reviewed"])
            checks["independent_review_preserves_source_copy_identity_and_current_requirements"] = (
                len(fixture.review_inputs) == 2 and fixture.review_inputs[0]["evidence"] == fixture.review_inputs[1]["evidence"]
                and fixture.review_inputs[0]["answer_sha256"] != fixture.review_inputs[1]["answer_sha256"])
            conversation = result["state"]["conversation"]
            assistant = conversation["messages"][-1]
            previews = [sample["progress"].get("answer_preview", {}).get("text", "")
                for sample in result["progress_samples"]]
            review_json = [row["response_text"] for row in provider.requests if row["payload"].get("schema") == "semantic-answer-review/v1"]
            workbench.stop()
            workbench.start()
            reopened = workbench.request("GET", "/api/conversations/" + conversation["id"])["conversation"]
            visible = [agent["answer"], assistant["content"], reopened["messages"][-1]["content"], *previews]
            checks["independent_review_json_never_becomes_visible_answer"] = (
                assistant["status"] == "completed" and assistant["content"] == reopened["messages"][-1]["content"] == agent["answer"]
                and len(review_json) == 2 and all(not any(body in text for body in review_json)
                    and '"answer_sha256"' not in text and '"reviews"' not in text for text in visible))
            observations.append({"phase": "independent_review", "run_id": agent["run_id"],
                "model_requests": len(provider.requests), "stages": stages, "answer_sha256": final_digest,
                "reviewed_answer_sha256": [row["answer_sha256"] for row in fixture.review_inputs],
                "evidence_sha256": review.get("evidence_sha256"), "elapsed_seconds": result["elapsed_seconds"]})
    return {"checks": checks, "observations": observations, "synthetic_model_requests": len(provider.requests),
        "fixture_sha256": {"independent_review/" + name: sha256(source / name) for name in ("root.cbl", "amount-input.cpy")}}


def run_checks(command, root, report=None):
    source = root / "source"
    source.mkdir()
    (source / "root.cbl").write_text(SOURCE, encoding="utf-8")
    (source / "decision.cbl").write_text(BUSINESS_SOURCE, encoding="utf-8")
    (source / "caller.cbl").write_text(BUSINESS_CALLER, encoding="utf-8")
    output, profile = root / "workspace", root / "profile"
    checks, observations = {}, []
    if report is not None:
        report.update(checks=checks, observations=observations)
    phase = {"name": "default", "count": 0, "draft": None}
    expected_prompt = [PROMPT_A]
    expected_question = [QUESTION]

    def respond(envelope, payload, index):
        require(index < 11, "Unexpected extra model attempt")
        phase["count"] += 1
        system = envelope["messages"][0]["content"]
        require(expected_prompt[0] in system, "Current saved answer instructions missing from system")
        if expected_prompt[0] == PROMPT_B:
            require(PROMPT_A not in system, "Earlier answer instructions reused")
        require(payload["question"] == expected_question[0], "Original question changed")
        if phase["name"] == "business_content":
            decision = verify_pages(source, payload, "MOVE 12 TO RESULT-CODE")
            caller = verify_pages(source, payload, "MOVE 'N' TO ALLOW-PROCESS")
            require("business_overview" in payload.get("question_investigation", {}).get(
                "analysis_plan", {}).get("intents", []), "Saved business requirements did not affect planning")
            if phase["count"] == 1:
                return "程序把 RESULT-CODE 设置为12并返回调用方。" + f"[{decision['evidence_id']}]", "stop"
            require(phase["count"] == 2 and "program_roles" in payload.get("answer_review", {}).get(
                "missing_aspects", []), "Saved business content omission was not reviewed")
            return ("REQUESTFLOW 把是否准入的判定交给 DECISION；调用正常返回12时，"
                "调用方把 ALLOW-PROCESS 设为N，走不允许后续处理的分支。"
                f"[{caller['evidence_id']}][{decision['evidence_id']}]"), "stop"
        if phase["name"] == "business_partial":
            decision = verify_pages(source, payload, "MOVE 12 TO RESULT-CODE")
            caller = verify_pages(source, payload, "MOVE 'N' TO ALLOW-PROCESS")
            require(phase["count"] == 1, "Business partial fixture ignored its one-request budget")
            return ("REQUESTFLOW 把是否准入的判定交给 DECISION；调用正常返回12时，"
                "调用方把 ALLOW-PROCESS 设为N，走不允许后续处理的分支。"
                f"[{caller['evidence_id']}][{decision['evidence_id']}] 下一步还需"), "length"
        if phase["name"] == "saved_language_repair":
            page = verify_pages(source, payload)
            require(payload.get("answer_language") == "en", "Saved English requirement did not reach local language state")
            require(phase["count"] == 1, "Local language repair unexpectedly needed another model request")
            return "TOTAL-AMOUNT = BASE-AMOUNT * 3. " + f"[{page['evidence_id']}]", "stop"
        if phase["name"] == "continue_business_english":
            decision = verify_pages(source, payload, "MOVE 12 TO RESULT-CODE")
            caller = verify_pages(source, payload, "MOVE 'N' TO ALLOW-PROCESS")
            require(payload.get("answer_language") == "en", "Continuation did not adopt saved English")
            require(phase["count"] == 1 and payload.get("draft_answer") == phase["draft"],
                    "Cross-language continuation lost its previous draft or repeated the request")
            require(payload.get("analysis_memory", {}).get("continuation_requested") is True,
                    "Cross-language continuation restarted the question")
            return ("REQUESTFLOW delegates the eligibility decision to DECISION. When that call returns 12 normally, "
                "REQUESTFLOW sets ALLOW-PROCESS to N and follows the branch that disallows further processing. "
                f"[{caller['evidence_id']}][{decision['evidence_id']}]"), "stop"
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
            (profile / "agent-settings.json").write_text(json.dumps({"version": 1,
                "agent": {"max_model_requests": 3, "max_answer_revisions": 1}}), encoding="utf-8")
            phase.update(name="business_content", count=0, draft=None)
            expected_prompt[0], expected_question[0] = BUSINESS_PROMPT, BUSINESS_QUESTION
            workbench.request("POST", "/api/answer-instructions", {"answer_instructions": BUSINESS_PROMPT})
            business = workbench.analyze(source, output, BUSINESS_QUESTION, locale="zh-CN", answer_detail="detailed")
            agent = business["result"]["agent_result"]
            checks["saved_business_content_drives_existing_omission_review"] = (
                phase["count"] == agent["metrics"]["model_requests"] == 2
                and "不允许后续处理" in agent["answer"]
                and "program_roles" not in agent.get("answer_completion", {}).get("missing_aspects", [])
                and agent["answer"] == provider.requests[-1]["response_text"]
                and agent.get("claims_semantically_verified") is False)
            observations.append({"phase": phase["name"], "run_id": agent["run_id"], "model_requests": phase["count"],
                "elapsed_seconds": business["elapsed_seconds"], "answer_sha256": hashlib.sha256(agent["answer"].encode()).hexdigest()})
            (profile / "agent-settings.json").write_text(json.dumps({"version": 1,
                "agent": {"max_model_requests": 1, "max_answer_revisions": 0}}), encoding="utf-8")
            # Keep the completed revision proof separate from a new genuine
            # partial turn. A truncated revision must not displace a prior
            # complete draft merely to manufacture a continuation fixture.
            phase.update(name="business_partial", count=0, draft=None)
            partial_business = workbench.analyze(source, output, BUSINESS_QUESTION,
                locale="zh-CN", answer_detail="detailed")
            partial_agent = partial_business["result"]["agent_result"]
            previous_conversation = partial_business["state"]["conversation"]
            previous_user, previous_assistant = continuation_turn(previous_conversation)
            checks["business_fixture_has_real_partial_continuation_boundary"] = (
                phase["count"] == partial_agent["metrics"]["model_requests"] == 1
                and provider.requests[-1]["finish_reason"] == "length"
                and partial_agent.get("answer_truncated") is True
                and previous_assistant["content"] == partial_agent["answer"]
                and "不允许后续处理" in partial_agent["answer"])
            observations.append({"phase": phase["name"], "run_id": partial_agent["run_id"],
                "model_requests": phase["count"], "elapsed_seconds": partial_business["elapsed_seconds"],
                "answer_sha256": hashlib.sha256(partial_agent["answer"].encode()).hexdigest()})
            phase.update(name="continue_business_english", count=0, draft=previous_assistant["content"])
            expected_prompt[0] = CROSS_LANGUAGE_PROMPT
            workbench.request("POST", "/api/answer-instructions", {"answer_instructions": CROSS_LANGUAGE_PROMPT})
            translated = workbench.analyze(source, output, BUSINESS_QUESTION,
                conversation_id=previous_conversation["id"], continue_message_id=previous_user["id"],
                locale="zh-CN", answer_detail="detailed")
            translated_agent = translated["result"]["agent_result"]
            translated_conversation = translated["state"]["conversation"]
            current = translated_conversation["messages"][-1]
            revisions = current.get("answer_revisions", [])
            checks["cross_language_continuation_uses_current_requirements_and_source"] = (
                phase["count"] == translated_agent["metrics"]["model_requests"] == 1
                and translated_agent.get("answer_language") == "en"
                and "disallows further processing" in translated_agent["answer"]
                and previous_assistant["content"] not in translated_agent["answer"]
                and re.search(r"[\u3400-\u9fff]", translated_agent["answer"]) is None)
            checks["cross_language_continuation_preserves_previous_answer_revision"] = (
                len(translated_conversation["messages"]) == 2
                and [row["id"] for row in translated_conversation["messages"]] == [previous_user["id"], previous_assistant["id"]]
                and len(revisions) == 1 and revisions[0]["content"] == previous_assistant["content"]
                and revisions[0]["run_id"] == previous_assistant["run_id"])
            require(bool(revisions), "Cross-language continuation lost the original answer")
            revision = revisions[0]
            reference = revision["evidence_refs"][0]["evidence_id"]
            historical = workbench.request("GET", "/api/evidence?" + urlencode({"id": reference,
                "conversation_id": previous_conversation["id"], "message_id": previous_assistant["id"],
                "revision_id": revision["revision_id"]}))
            checks["cross_language_continuation_keeps_historical_source_evidence"] = (
                historical.get("run_id") == previous_assistant["run_id"] and bool(historical.get("spans"))
                and all(span["source_sha256"] == sha256(source / span["relative_path"])
                    and span["source_text"].splitlines() == (source / span["relative_path"]).read_text(
                        encoding="utf-8").splitlines()[span["start_line"] - 1:span["end_line"]]
                    for span in historical["spans"]))
            observations.append({"phase": phase["name"], "previous_run_id": previous_assistant["run_id"],
                "run_id": translated_agent["run_id"], "revision_id": revision["revision_id"],
                "model_requests": phase["count"], "elapsed_seconds": translated["elapsed_seconds"],
                "answer_sha256": hashlib.sha256(translated_agent["answer"].encode()).hexdigest()})
            (profile / "agent-settings.json").write_text(json.dumps({"version": 1,
                "agent": {"max_model_requests": 1, "max_answer_revisions": 0}}), encoding="utf-8")
            phase.update(name="saved_language_repair", count=0, draft=None)
            expected_prompt[0], expected_question[0] = LANGUAGE_PROMPT, LANGUAGE_QUESTION
            workbench.request("POST", "/api/answer-instructions", {"answer_instructions": LANGUAGE_PROMPT})
            repaired = workbench.analyze(source, output, LANGUAGE_QUESTION, locale="zh-CN", answer_detail="detailed")
            agent = repaired["result"]["agent_result"]
            checks["local_formula_repair_preserves_saved_english"] = (
                phase["count"] == agent["metrics"]["model_requests"] == 1
                and agent.get("answer_language") == "en" and "BASE-AMOUNT * 2" in agent["answer"]
                and "BASE-AMOUNT * 3" not in agent["answer"] and re.search(r"[\u3400-\u9fff]", agent["answer"]) is None
                and "supplied source" in agent["answer"].lower()
                and agent.get("claim_ledger", {}).get("counts", {}).get("contradicted") == 1
                and agent.get("claims_semantically_verified") is False)
            observations.append({"phase": phase["name"], "run_id": agent["run_id"], "model_requests": phase["count"],
                "elapsed_seconds": repaired["elapsed_seconds"], "answer_sha256": hashlib.sha256(agent["answer"].encode()).hexdigest()})
            journal = read_journal(output)["requests"]
            checks["requests_recorded_once_without_provider_errors"] = (not provider.errors
                and len(provider.requests) == len(journal) == 11 and all(row["state"] == "completed" for row in journal)
                and {row["request_hash"] for row in journal} == {row["request_sha256"] for row in provider.requests})
    independent = run_independent_review_check(command, root / "independent-review", checks, observations)
    return {"checks": checks, "observations": observations,
        "synthetic_model_requests": len(provider.requests) + independent["synthetic_model_requests"],
        "fixture_sha256": {**{name: sha256(source / name) for name in ("root.cbl", "decision.cbl", "caller.cbl")},
            **independent["fixture_sha256"]},
        "injected_wait_seconds": 20,
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
