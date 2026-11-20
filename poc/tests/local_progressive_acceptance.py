"""Hold the model at the public local workbench boundary and inspect the actual conversation."""

from contextlib import ExitStack
import hashlib
import json
from threading import Event
from unittest.mock import patch

from online_progressive_acceptance import DRAFT, QUESTION


FULL = (
    "现金应收处理分两步。只有 ITEM-TYPE 等于 1-RESERVECREDIT，才从 NET-DUE 扣减 CREDIT-AMT，其他类型跳过准备金。"
    "随后独立判断当前 NET-DUE；若大于 0，CASHENTRY 将其传给 APPLYREFUND，否则跳过退款并保留当时应收。"
    "即使跳过准备金，应收为正仍检查退款。子程序只在 REFUND-BALANCE 严格小于 DUE 时减去退款余额，相等或更大则保留 DUE。"
    "按引用返回后，最终应收是初始应收减去实际扣掉的准备金和退款；两步都跳过时保持初始金额。"
    "例如应收 100、准备金 20 时，退款 30 得 50，退款 80 仍为 80。"
)
INCLUSIVE = FULL.replace("严格小于", "小于或等于").replace("相等或更大则保留", "只有更大才保留").replace("退款 80 仍为 80", "退款 80 得 0")
SCENARIOS = ("shallow_then_complete", "already_complete", "changed_equality", "missing_callee",
             "one_request_complete_sources", "one_request_missing_callee", "unsupported_refund_missing_callee")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def checked_page(page, originals):
    name, first, last = (page[k] for k in ("relative_path", "start_line", "end_line"))
    raw = originals[name]
    lines = raw.decode("utf-8").splitlines()
    if (not 1 <= first <= last <= len(lines) or page["source_sha256"] != digest(raw)
            or page["source_text"] != "\n".join(lines[first - 1:last])):
        raise AssertionError("source page does not resolve to the original: " + name)
    return {key: page[key] for key in ("relative_path", "start_line", "end_line", "source_sha256", "evidence_id")}


def replay(scenario):
    import test_conversation_workbench as local
    import test_online_automatic_source_search as original
    from agent_policy import AgentPolicy
    from company_api import TransportResponse

    if scenario not in SCENARIOS:
        raise ValueError(scenario)
    with ExitStack() as scope:
        network = scope.enter_context(patch("socket.socket.connect", side_effect=AssertionError("unexpected network")))
        fixture = original.AutomaticSourceSearchTests()
        scope.callback(fixture.doCleanups)
        fixture.setUp()
        if QUESTION != fixture.question:
            raise AssertionError("historical question changed")
        case = local.ConversationWorkbenchTests()
        scope.callback(case.doCleanups)
        case.setUp()
        request_limited = scenario.startswith("one_request_")
        missing_callee = scenario.endswith("missing_callee")
        if request_limited:
            case.app.policy_provider = lambda: AgentPolicy(max_model_requests=1)
        files = {name: fixture.case.source_bytes[name] for name in ("cash.cbl", "return.cbl")}
        if scenario == "changed_equality":
            files["return.cbl"] = files["return.cbl"].replace(b"REFUND-BALANCE < DUE", b"REFUND-BALANCE <= DUE")
        elif missing_callee:
            del files["return.cbl"]
        (case.source / "quota.cbl").unlink()
        for name, raw in files.items():
            (case.source / name).write_bytes(raw)

        requests = []
        waiting, release = Event(), Event()
        scope.callback(release.set)

        def respond(request):
            wire = json.loads(request.body)
            context = json.loads(wire["messages"][-1]["content"])
            pages = [checked_page(page, files)
                     for source in context.get("source_context", []) for page in source.get("pages", [])]
            number = len(requests) + 1
            if number > 20:
                raise AssertionError("unexpected repeated requests")
            if scenario == "unsupported_refund_missing_callee":
                answer = INCLUSIVE
            elif missing_callee or request_limited or (scenario == "shallow_then_complete" and number == 1):
                answer = DRAFT
            elif scenario == "changed_equality" and number > 1:
                answer = INCLUSIVE
            else:
                answer = FULL
            requests.append({"pages": pages, "has_revision_draft": bool(context.get("draft_answer")),
                             "deduction_missing": (context.get("answer_review") or {}).get("deduction_missing", []),
                             "requests_order_skips_and_result": all(s in context.get("task", "")
                                               for s in ("扣减顺序", "跳过条件", "最终应收")),
                             "scripted_answer": answer})
            if number == 1:
                waiting.set()
                if not release.wait(10):
                    raise AssertionError("observer did not release the first answer")
            answer += "".join("[" + identifier + "]" for identifier in dict.fromkeys(p["evidence_id"] for p in pages))
            return TransportResponse(200, json.dumps({"choices": [
                {"message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}]}))

        case.transport = respond
        case.run_job()
        app = case.app
        job = app.start({"source": str(case.source), "output": str(case.output), "source_format": "free",
                         "question": QUESTION, "allow_network": True})["job_id"]
        if not waiting.wait(10):
            raise AssertionError("first model request never arrived")
        snapshot = app.get_job(job)
        progress = snapshot["progress"]
        checked = progress.get("checked_findings") or []
        for finding in checked:
            for source in finding["sources"]:
                if not any(page["evidence_id"] == source["evidence_id"] and
                           page["relative_path"] == source["relative_path"] and
                           page["start_line"] <= source["start_line"] <= source["end_line"] <= page["end_line"]
                           for page in requests[0]["pages"]):
                    raise AssertionError("interim citation is not in the verified source window")
        pending_conversation = snapshot.get("conversation") or app.state()["conversation"]
        during = {"phase": progress.get("phase"), "checked_findings": checked,
                  "update": (progress.get("analysis_update") or {}).get("summary"),
                  "model_draft": (progress.get("answer_preview") or {}).get("text"),
                  "message_roles": [m["role"] for m in pending_conversation["messages"]]}
        release.set()
        job_result = case.wait_for_job(job)
        conversation = job_result.get("conversation") or app.state()["conversation"]
        assistant = [m for m in conversation["messages"] if m["role"] == "assistant"]
        if len(assistant) != 1:
            raise AssertionError("expected exactly one delivered assistant message")
        message = assistant[0]
        cited = []
        for ref in message.get("evidence_refs", []):
            response = app.evidence(ref["evidence_id"], conversation["id"], message["id"])
            span, = response["spans"]
            if span["integrity"] != "VALID":
                raise AssertionError("saved citation failed integrity check")
            cited.append(checked_page(span, files))
        cited.sort(key=lambda page: (page["relative_path"], page["start_line"], page["end_line"]))
        network.assert_not_called()
        return {"originals": {name: digest(raw) for name, raw in files.items()},
                "while_first_reply_held": during, "requests": requests, "job_status": job_result["status"],
                "remaining_interim_findings": bool(job_result["progress"].get("checked_findings")),
                "message_roles": [m["role"] for m in conversation["messages"]],
                "status": message["status"], "stop_reason": message.get("stop_reason"),
                "answer_origin": message.get("answer_origin"),
                "answer": message["content"], "citations": cited, "network_connections": network.call_count}
