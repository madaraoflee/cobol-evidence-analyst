"""Exercise a multi-hop literal investigation through the ordinary local workbench."""

from contextlib import ExitStack
import json
import re
from unittest.mock import patch

from local_progressive_acceptance import QUESTION, checked_page, digest


SCENARIOS = ("insufficient_then_grep", "explicit_grep", "missing_implementation", "wrong_amount_then_checked")
WRONG_FIELD = "EXTRA-SURCHARGE"
ENTRY = """IDENTIFICATION DIVISION.
PROGRAM-ID. CASHENTRY.
PROCEDURE DIVISION.
IF ITEM-TYPE = "1-RESERVECREDIT"
 SUBTRACT CREDIT-AMT FROM NET-DUE
END-IF.
IF NET-DUE > 0
 MOVE "ADJ731" TO REFUND-ROUTINE
 CALL REFUND-ROUTINE USING NET-DUE
END-IF.
GOBACK."""
DISPATCH = """IDENTIFICATION DIVISION.
PROGRAM-ID. ADJ731.
PROCEDURE DIVISION USING REMAINING-DUE.
MOVE "CUT526" TO NEXT-ROUTINE.
CALL NEXT-ROUTINE USING REMAINING-DUE.
GOBACK."""
WORKER = """IDENTIFICATION DIVISION.
PROGRAM-ID. CUT526.
PROCEDURE DIVISION USING REMAINING-DUE.
IF POOL-AMOUNT < REMAINING-DUE
 SUBTRACT POOL-AMOUNT FROM REMAINING-DUE
END-IF.
GOBACK."""
INSUFFICIENT = "源码不足以分析。"


def originals(scenario):
    files = {"start.cbl": ENTRY, "legacy/route.cbl": DISPATCH}
    if scenario != "missing_implementation":
        files["archive/settlement.cbl"] = WORKER
    for i in range(9):
        files[f"hint{i}.cbl"] = (f'IDENTIFICATION DIVISION.\nPROGRAM-ID. BANNER{i}.\n'
                                'PROCEDURE DIVISION.\nDISPLAY "reserve refund".\nGOBACK.')
    return {name: text.encode() for name, text in files.items()}


def choose_reply(pages, actions, *, initial_deferral):
    """Derive all search targets, read locations, and business assertions from sent pages."""
    if initial_deferral and not any(a.get("tool") == "grep" or "grep" in a for a in actions):
        return INSUFFICIENT
    start = next((p for p in pages if 'TO REFUND-ROUTINE' in p['source_text']), None)
    if not start:
        return INSUFFICIENT
    origin = start
    stage = "REFUND-ROUTINE"
    route = None
    targets = []
    for _ in range(2):
        match = re.search(r'MOVE "([A-Z0-9-]+)" TO ' + stage, origin['source_text'])
        if not match:
            return INSUFFICIENT
        if f"CALL {stage} USING " not in origin["source_text"]:
            return INSUFFICIENT
        literal = match[1]
        targets.append(literal)
        definition = "PROGRAM-ID. " + literal
        found = next((p for p in pages if definition in p["source_text"]), None)
        if not found:
            prior = next((a for a in reversed(actions) if a.get("tool") == "grep"
                          and a.get("target") == definition), None)
            if not prior:
                return {"grep": [definition]}
            hits = prior.get("matches") or []
            if hits:
                hit = hits[0]
                return {"read": [{"relative_path": hit["relative_path"], "start_line": max(1, hit["line"] - 1),
                                  "end_line": hit["line"] + 8}]}
            # A bounded or empty navigation result permits only a qualified gap.
            return (f'主程序在类型为 1-RESERVECREDIT 时扣准备金，应收为正才尝试后续处理。'
                    f'本轮尚未读到 {literal} 的实现，退款的跳过条件和最终应收待核。'
                    f'[{start["evidence_id"]}]')
        if stage == "REFUND-ROUTINE":
            route = found
            stage = "NEXT-ROUTINE"
            origin = found
        else:
            rule = found
    if not route or not all(term in start['source_text'] for term in
            ('IF ITEM-TYPE = "1-RESERVECREDIT"', 'SUBTRACT CREDIT-AMT FROM NET-DUE', 'IF NET-DUE > 0')):
        return INSUFFICIENT
    if ('IF POOL-AMOUNT < REMAINING-DUE' not in rule['source_text']
            or 'SUBTRACT POOL-AMOUNT FROM REMAINING-DUE' not in rule['source_text']):
        return INSUFFICIENT
    return (f'先判断项目类型。只有 ITEM-TYPE 等于 1-RESERVECREDIT 才从 NET-DUE 减去准备金 CREDIT-AMT。'
            f'其他类型跳过准备金。然后独立检查剩余应收，大于零才调用 {targets[0]}，否则跳过退款并保留当时应收。'
            f'[{start["evidence_id"]}] 按原文赋予两个调用目标的名称、参数相容并正常返回的路径，'
            f'{targets[0]} 按引用把剩余应收传给 {targets[1]}。[{route["evidence_id"]}] '
            f'只有 POOL-AMOUNT 严格小于当前 REMAINING-DUE 才扣退款，相等或更大时跳过。'
            f'最终应收等于进入时的应收减去实际生效的扣减额，两步都跳过时保持原额。100 先扣 20 后，如再扣 30，剩 50，'
            f'退款恰好 80 则仍剩 80。[{rule["evidence_id"]}]')


def replay(scenario):
    from test_conversation_workbench import ConversationWorkbenchTests
    from company_api import TransportResponse

    if scenario not in SCENARIOS:
        raise ValueError(scenario)
    with ExitStack() as cleanup:
        network = cleanup.enter_context(patch("socket.socket.connect", side_effect=AssertionError("unexpected network")))
        case = ConversationWorkbenchTests()
        cleanup.callback(case.doCleanups)
        case.setUp()
        files = originals(scenario)
        (case.source / "quota.cbl").unlink()
        for name, data in files.items():
            target = case.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        requests = []
        during = {}

        def respond(request):
            context = json.loads(json.loads(request.body)["messages"][-1]["content"])
            pages = [p for bundle in context.get("source_context", []) for p in bundle.get("pages", [])]
            actions = context.get("completed_actions", [])
            visible = [checked_page(p, files) for p in pages]
            rounds = [{"path": p["relative_path"], "lines": [p["start_line"], p["end_line"]],
                       "id": p["evidence_id"]} for p in visible]
            state = case.app.state()
            progress = state["job"]["progress"]
            if not requests:
                findings = progress.get("checked_findings") or []
                for finding in findings:
                    for citation in finding["sources"]:
                        if not any(p["id"] == citation["evidence_id"] and p["path"] == citation["relative_path"]
                                   and p["lines"][0] <= citation["start_line"] <= citation["end_line"] <= p["lines"][1]
                                   for p in rounds):
                            raise AssertionError("interim finding points outside verified source")
                during.update(job_status=state["job"]["status"], checked_findings=findings,
                              message_roles=[m["role"] for m in state["conversation"]["messages"]])
            if len(requests) >= 12:
                raise AssertionError("repeated model requests without bounded progress")
            answer = choose_reply(pages, actions, initial_deferral=scenario == "insufficient_then_grep")
            if scenario == "wrong_amount_then_checked" and isinstance(answer, str) and "最终应收等于进入时" in answer:
                answer = answer.replace("准备金 CREDIT-AMT", "准备金 " + WRONG_FIELD).replace("剩 50", "剩 1000")
            requests.append({"supplied_pages": rounds, "visible_search_steps": progress.get("source_grep_steps", []),
                             "grep_actions": [a for a in actions if a.get("tool") == "grep" or "grep" in a],
                             "reply": answer})
            text = json.dumps(answer, ensure_ascii=False) if isinstance(answer, dict) else answer
            return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": text},
                                                                  "finish_reason": "stop"}]}))

        case.transport = respond
        case.run_job()
        result = case.run_job(question=QUESTION, allow_network=True)
        conversation = result["conversation"]
        message = conversation['messages'][-1]
        refs = []
        for ref in message.get("evidence_refs", []):
            source, = case.app.evidence(ref["evidence_id"], conversation["id"], message["id"])["spans"]
            if source["integrity"] != "VALID":
                raise AssertionError("saved citation failed verification")
            refs.append(checked_page(source, files))
        network.assert_not_called()
        return {"files": {k: digest(v) for k, v in files.items()}, "requests": requests, "answer": message['content'],
                "status": message["status"], "stop_reason": message.get("stop_reason"), "while_waiting": during,
                "inline_citations": message.get("cited_evidence_ids", []),
                "citations": sorted(refs, key=lambda r: (r['relative_path'], r['start_line'], r['end_line'], r['evidence_id'])),
                "network_connections": network.call_count}
