"""Offline fault injection through the real workbench and conversation store.

The provider is scripted; only the investigation clock advances. This fixture
does not measure real model latency or call an external service.
"""
from pathlib import Path
import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyze_source import analyze_source
from agent_policy import AgentPolicy
import business_chat
from company_api import CompanyAPIConfig, TransportResponse
from question_budget import QuestionBudget
import question_investigation
import repository_discovery
from web_app import WorkbenchState


def run_scenario(root):
    root = Path(root)
    source, output = root / "source", root / "output"
    source.mkdir(parents=True)
    (source / "entry.cbl").write_text(
        "IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRYPOINT.\n"
        "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
        "01 REQUEST-COUNT PIC 9(4) VALUE 2.\nPROCEDURE DIVISION.\nMAIN.\n"
        "IF REQUEST-COUNT > ZERO\n    CALL 'HELPER01' USING REQUEST-COUNT\nEND-IF.\nGOBACK.\n",
        encoding="utf-8")
    (source / "helper.cbl").write_text(
        "IDENTIFICATION DIVISION.\nPROGRAM-ID. HELPER01.\n"
        "DATA DIVISION.\nLINKAGE SECTION.\n01 INPUT-COUNT PIC 9(4).\n"
        "PROCEDURE DIVISION USING INPUT-COUNT.\nMAIN.\nDISPLAY INPUT-COUNT.\nGOBACK.\n",
        encoding="utf-8")
    config = CompanyAPIConfig("https://offline.example.invalid/v1", "scripted-offline", api_key="unused")
    clock_value = [0.0]
    clock = SimpleNamespace(monotonic=lambda: clock_value[0], time=lambda: 2000000000.0 + clock_value[0])
    records, reads, pending_plans = [], [], []
    current_question = [""]
    original_plan = question_investigation.build_question_investigation
    original_read = repository_discovery.read_repository_context

    def transport(request):
        payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
        pages = [page for context in payload["source_context"] for page in context["pages"]]
        call_page = next(page for page in pages if "CALL 'HELPER01'" in page["source_text"])
        helper_page = next(page for page in pages if "DISPLAY INPUT-COUNT" in page["source_text"])
        answer = ("ENTRYPOINT 调用的相关程序是 HELPER01。仅当 REQUEST-COUNT 大于零时，"
                  "才通过 CALL 将 REQUEST-COUNT 传入；数量为零时跳过这次调用。"
                  f"[{call_page['evidence_id']}]\n\n"
                  "HELPER01 的入口通过 USING INPUT-COUNT 接收参数，执行 DISPLAY INPUT-COUNT，"
                  f"然后 GOBACK 返回。[{helper_page['evidence_id']}]\n\n"
                  "这些是当前源码中的调用和处理步骤；未执行程序，不能据此确认某次运行的实际输出。")
        records.append({"question": current_question[0], "payload": payload, "answer": answer})
        if "用到的相关程序" in current_question[0]:
            clock_value[0] = 119.0
            for item in pending_plans:
                item["planned_actions"] = [{"tool": "read",
                    "arguments": {"relative_path": "entry.cbl", "start_line": 2, "end_line": 2},
                    "reason": "neutral_optional_followup"}]
        return TransportResponse(200, json.dumps({"choices": [{"message": {"content": answer},
                                                                  "finish_reason": "stop"}]}, ensure_ascii=False))

    def plan(*args, **kwargs):
        result = original_plan(*args, **kwargs)
        if "用到的相关程序" in current_question[0]:
            result = {**result, "can_answer": False, "planned_actions": []}
            if clock_value[0] >= 119:
                result["planned_actions"] = [{"tool": "read",
                    "arguments": {"relative_path": "entry.cbl", "start_line": 2, "end_line": 2},
                    "reason": "neutral_optional_followup"}]
            pending_plans.append(result)
        return result

    def read(*args, **kwargs):
        if "用到的相关程序" in current_question[0] and clock_value[0] == 119.0:
            reads.append({"started_at": clock_value[0], "stopped_at": 181.0})
            clock_value[0] = 181.0
            kwargs["check_cancel"]()
        return original_read(*args, **kwargs)

    def budget(*args, **kwargs):
        return QuestionBudget(*args, **{**kwargs, "clock": clock.monotonic, "wall_clock": clock.time})

    def analyzer(source, output, **options):
        clock_value[0] = 0.0
        current_question[0] = options.get("question", "")
        with patch.object(business_chat, "time", clock), \
                patch.object(business_chat, "QuestionBudget", side_effect=budget), \
                patch.object(question_investigation, "build_question_investigation", side_effect=plan), \
                patch.object(repository_discovery, "read_repository_context", side_effect=read):
            return analyze_source(source, output, transport=transport,
                                  **{**options, "framework_reference_path": "",
                                     "agent_policy": AgentPolicy(max_question_seconds=180)})

    state_path = root / "state.json"
    app = WorkbenchState(analyzer=analyzer, config_provider=lambda: config,
                         policy_provider=lambda: {}, state_path=state_path)
    results, durations = [], []
    for question in ("请解释程序 ENTRYPOINT 的处理步骤。", "用到的相关程序呢？"):
        started = time.monotonic()
        options = {"source": str(source), "output": str(output), "source_format": "free",
                   "question": question, "allow_network": True}
        if results:
            options["conversation_id"] = results[-1]["conversation"]["id"]
        job_id = app.start(options)["job_id"]
        while time.monotonic() - started < 20:
            job = app.get_job(job_id)
            if job["status"] != "RUNNING":
                if job["status"] != "COMPLETED":
                    raise AssertionError(job)
                results.append(job["result"])
                durations.append(time.monotonic() - started)
                break
            time.sleep(.01)
        else:
            raise AssertionError("Offline workbench timeout")
    restored = WorkbenchState(analyzer=analyzer, config_provider=lambda: config,
                             policy_provider=lambda: {}, state_path=state_path)
    return restored, {"test_kind": "offline_scripted_provider_and_virtual_deadline",
        "paid_requests": 0, "external_requests": 0, "provider_requests": len(records),
        "clock_injection": reads, "wall_seconds": durations, "requests": records, "results": results}
