#!/usr/bin/env python3
"""Compare production answer paths offline with shared neutral scripted inputs.

No reviewer is replaced. The only substituted boundary is the model transport;
all network sockets are blocked. Latency measures local orchestration, not a
remote model. Run against two source trees with --baseline and --candidate.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

SCRIPT_ID = "bounded-answer-offline-contrast-v1"
SOURCE = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. TOTAL-RULE.\nDATA DIVISION.\n"
          "WORKING-STORAGE SECTION.\n01 BASE-AMOUNT PIC 9(5) VALUE 10.\n"
          "01 TOTAL-AMOUNT PIC 9(6).\nPROCEDURE DIVISION.\nMAIN.\n"
          "COMPUTE TOTAL-AMOUNT = BASE-AMOUNT * 2.\nGOBACK.\n")
CASES = {
    "complete_source_denial": {"question": "root.cbl 的金额如何计算？", "denial": "完整源码本轮没有提供"},
    "field_source_denial": {"question": "root.cbl 的金额如何计算？", "denial": "字段处理源码未提供"},
    "parameter_source_denial": {"question": "root.cbl 的金额如何计算？", "denial": "参数处理源码未提供"},
    "wrong_richer_revision": {"question": "请完整解释 root.cbl 的金额计算、输入来源、输出和异常处理。"},
    "default_bounded_investigation": {"question": "root.cbl 的金额如何计算？"},
    "ignores_finalization_request": {"question": "root.cbl 的金额如何计算？"},
}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def git_head(root):
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def run_one(source_tree, case_id):
    sys.path.insert(0, str(source_tree / "poc"))
    from business_chat import run_business_chat
    from business_index import build_business_index
    from company_api import CompanyAPIConfig, TransportResponse
    from repository_discovery import ensure_repository_search
    from agent_policy import AgentPolicy

    case = CASES[case_id]
    requests, events = [], []
    with tempfile.TemporaryDirectory(prefix="answer-contrast-") as folder:
        root = Path(folder)
        source = root / "source"
        source.mkdir()
        (source / "root.cbl").write_text(SOURCE, encoding="utf-8")
        helpers = {}
        for number in range(1, 9):
            name = f"helper-{number}.cbl"
            body = (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. HELPER-{number}.\nDATA DIVISION.\n"
                    "WORKING-STORAGE SECTION.\n01 HELPER-RESULT PIC 9.\nPROCEDURE DIVISION.\nMAIN.\n"
                    f"MOVE {number} TO HELPER-RESULT.\nGOBACK.\n")
            helpers[name] = body
            (source / name).write_text(body, encoding="utf-8")
        database = root / "index.sqlite"
        index_started = time.monotonic()
        with redirect_stdout(io.StringIO()):
            build_business_index(source, database, source_format="free", verify_content=True,
                                 framework_reference_path="", quiet=True)
            ensure_repository_search(database, source)
        index_ms = (time.monotonic() - index_started) * 1000

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            number = len(requests) + 1
            pages = [page for bundle in payload["source_context"] for page in bundle["pages"]]
            page = next(page for page in pages if page["relative_path"] == "root.cbl")
            cite = "[" + page["evidence_id"] + "]"
            good = "基础金额初值为10，金额按基础金额乘以二得到。" + cite
            if number > 12:
                raise AssertionError("Independent comparison request watchdog exceeded")
            if "denial" in case:
                answer = good + " root.cbl 的" + case["denial"] + "，无法完整解答。"
            elif case_id == "wrong_richer_revision":
                answer = ("金额计算为 TOTAL-AMOUNT = BASE-AMOUNT * 2。" + cite if number == 1 else
                          "输入来源是基础金额，输出金额为 TOTAL-AMOUNT = BASE-AMOUNT * 999。" + cite +
                          " 条件和异常处理仍需核对。")
            elif (case_id == "default_bounded_investigation"
                  and payload.get("investigation_budget", {}).get("reads_per_turn") == 0):
                answer = good
            elif number <= 8:
                helper = f"helper-{number}.cbl"
                answer = json.dumps({"read": [{"relative_path": helper, "start_line": 1,
                                              "end_line": len(helpers[helper].splitlines())}]})
            else:
                answer = good
            requests.append({"number": number, "original_question": payload["question"],
                "model_reply": answer, "model_reply_sha256": digest(answer),
                "request_sha256": hashlib.sha256(request.body).hexdigest() if isinstance(request.body, bytes) else digest(request.body),
                "budget": payload.get("investigation_budget"),
                "answer_review": payload.get("answer_review"),
                "question_investigation": payload.get("question_investigation"),
                "supplied_pages": [{key: page.get(key) for key in ("evidence_id", "relative_path", "source_sha256",
                    "start_line", "end_line", "span_truncated")} | {"source_text_sha256": digest(page["source_text"]), "source_text": page["source_text"]}
                    for page in pages]})
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": answer},
                                                                      "finish_reason": "stop"}]}, ensure_ascii=False))

        started = time.monotonic()
        try:
            with patch.object(socket, "create_connection", side_effect=AssertionError("Network forbidden")), \
                 patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")), \
                 patch.object(socket.socket, "connect_ex", side_effect=AssertionError("Network forbidden")):
                result = run_business_chat(case["question"], database, source,
                    CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-script", api_key="offline-only"),
                    framework_reference_path="", transport=transport, progress=events.append, allow_network=False)["agent_result"]
            elapsed_ms = (time.monotonic() - started) * 1000
            trace_path = result.get("metrics", {}).get("quality_trace_path")
            trace = json.loads(Path(trace_path).read_text()) if trace_path and Path(trace_path).exists() else {}
            final = result.get("answer", "")
            false_absence = bool(re.search(r"(?:完整源码本轮没有提供|字段处理源码未提供|参数处理源码未提供)", final))
            bad_formula = "999" in final
            policy = AgentPolicy().to_dict()
            checks = {"false_source_absence": false_absence, "wrong_formula_999": bad_formula,
                      "has_correct_formula_or_factor": bool(re.search(r"乘以二|翻倍|BASE-AMOUNT\s*\*\s*2", final)),
                      "candidate_revision_exercised": case_id == "wrong_richer_revision" and len(requests) > 1,
                      "selected_first_draft": trace.get("final", {}).get("final_answer_round_id") == "round-1",
                      "answer_available": result.get("answer_available", trace.get("final", {}).get("answer_available")),
                      "answer_origin": result.get("answer_origin", trace.get("final", {}).get("answer_origin")),
                      "model_answer_recorded": result.get("model_answer_recorded"),
                      "default_finite_request_limit": policy.get("max_model_requests"),
                      "within_six_requests": len(requests) <= 6}
            return {"case_id": case_id, "question": case["question"], "script_id": SCRIPT_ID,
                "source_sha256": digest(SOURCE), "source_text": SOURCE, "model_requests": len(requests),
                "elapsed_ms": round(elapsed_ms, 3), "index_setup_ms": round(index_ms, 3),
                "status": result.get("status"), "stop_reason": result.get("stop_reason"),
                "answer_origin": checks["answer_origin"], "answer_available": checks["answer_available"],
                "model_answer_recorded": checks["model_answer_recorded"],
                "run_id": result.get("run_id") or trace.get("run_id"), "snapshot_id": result.get("snapshot_id"),
                "answer": final, "checks": checks, "requests": requests,
                "trace": {key: trace.get(key) for key in ("run_id", "schema_version", "question", "source_snapshot",
                    "source_availability_repairs", "draft_reviews", "question_investigation", "question_investigation_rounds", "rounds", "final")},
                "trace_sha256": digest(json.dumps(trace, ensure_ascii=False, sort_keys=True)),
                "claim_ledger": result.get("claim_ledger"), "answer_completion": result.get("answer_completion"),
                "metrics": {key: result.get("metrics", {}).get(key) for key in
                    ("model_requests", "elapsed_seconds", "convergence_stop_reason", "delivery_target", "policy",
                     "tool_calls", "question_investigation", "timing_seconds")},
                "progress_event_count": len(events)}
        except Exception as error:
            return {"case_id": case_id, "question": case["question"], "script_id": SCRIPT_ID,
                    "source_sha256": digest(SOURCE), "model_requests": len(requests), "requests": requests,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                    "error": type(error).__name__ + ": " + str(error), "independent_watchdog": "watchdog" in str(error)}


def compare(args):
    rows = []
    manifests = {}
    for label, tree in (("baseline", args.baseline), ("candidate", args.candidate)):
        manifest = {str(path.relative_to(tree)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted((tree / "poc").glob("*.py"))}
        manifests[label] = {"production_python_files": manifest,
                            "sha256": digest(json.dumps(manifest, sort_keys=True))}
    for label, tree in (("baseline", args.baseline), ("candidate", args.candidate)):
        for case_id in CASES:
            command = [sys.executable, str(Path(__file__).resolve()), "--run-one", case_id, "--source-tree", str(tree)]
            started = time.monotonic()
            try:
                completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=True)
                row = json.loads(completed.stdout)
            except subprocess.TimeoutExpired:
                row = {"case_id": case_id, "error": "Independent 30-second process watchdog", "independent_watchdog": True}
            row.update(variant=label, source_tree=str(tree.resolve()), commit=git_head(tree),
                       process_elapsed_ms=round((time.monotonic() - started) * 1000, 3))
            rows.append(row)
            print(f"{label}/{case_id}: calls={row.get('model_requests')} errors={row.get('error', row.get('checks'))}", file=sys.stderr)
    report = {"schema_version": 1, "experiment_id": SCRIPT_ID,
              "method": "Shared neutral source/question/script; production indexing, retrieval, supply, review, persistence; offline model transport only.",
              "limitations": ["Scripted mechanism verification, not measured real-model answer quality or remote-model speed.",
                  "The six development cases are targeted regressions, not a representative benchmark or held-out evaluation.",
                  "Latency is one local run per case, excludes index setup, and includes no model service delay.",
                  "Independent 12-request/30-second harness watchdogs are not product convergence guarantees."],
              "code_manifests": manifests, "rows": rows}
    report["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report["source_integrity_checks"] = {
        "same_root_source_hash_all_cases": len({row.get("source_sha256") for row in rows if row.get("source_sha256")}) == 1,
        "candidate_manifest_stable": manifests["candidate"]["production_python_files"] == {
            str(path.relative_to(args.candidate)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((args.candidate / "poc").glob("*.py"))}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = ["# 有界问答离线机制对照", "", "本次只验证修复机制：相同中性源码、原问题和确定性模型脚本，直接运行两个版本的生产检索、供证、审核与持久化流程。没有调用真实模型，也没有替换生产 reviewer。", "",
             "| 题目 | 版本 | 请求数 | 本地问答耗时 ms | 缺源码错误 | 999 错公式 | 正确算式/倍数 | 正文来源 | 有可用正文 |", "|---|---|---:|---:|---|---|---|---|---|"]
    for row in rows:
        check = row.get("checks", {})
        lines.append(f"| {row['case_id']} | {row['variant']} | {row.get('model_requests', '—')} | {row.get('elapsed_ms', '—')} | {check.get('false_source_absence', '—')} | {check.get('wrong_formula_999', '—')} | {check.get('has_correct_formula_or_factor', '—')} | {check.get('answer_origin', '—')} | {check.get('answer_available', '—')} |")
    lines += ["", "默认有界调查案例按相同规则响应收尾请求：当 reads_per_turn=0 时给出答案；另有 ignores_finalization_request 压力案例故意忽略该请求，记录预算停止后的真实交付结果。表中 model 表示脚本模拟的模型正文，local_source_facts 表示生产逻辑从已供应源码生成的局部事实，两者不可混为模型质量提升。", "", "每题的源码 hash、原问题、逐请求实际供证范围/hash、脚本原始答复、最终正文、run_id、trace 摘要/hash 和生产状态见同名 JSON。源码均为脚本内中性合成样例。", "",
              "耗时是单次本地编排测量，排除建索引且不包含真实模型服务延迟，不能据此宣称真实模型回答速度或业务质量提高。此集合属于有针对性的开发回归，不代表广泛业务分布，也不是保留评估集。", "",
              "复跑：`python3 tools/compare_bounded_answers.py --baseline <baseline-tree> --candidate <candidate-tree> --output docs/reports/2026-10-19-bounded-answer-comparison.json`。独立测试保护最多 12 次请求、单进程 30 秒；若触发会明确记录，不能算成产品自然结束。", ""]
    lines.extend(["坏修订案例必须单独解读：两版最终都消除了 999；旧版采纳第二稿后才在最终审核改写，新版保留第一稿。这验证审核时机与草稿保留，不能宣传为此题最终公式准确率提升。输入/结果说明仍有局部缺口，JSON 保留真实 incomplete 状态。", ""])
    pressure = next(row for row in rows if row["variant"] == "candidate" and row["case_id"] == "ignores_finalization_request")
    if pressure.get("answer_available") and pressure.get("answer_origin") == "local_source_facts":
        lines.append(f"压力案例中脚本忽略最后收尾请求；候选在 {pressure['model_requests']} 次请求后停止，并从问题明确点名、当前请求实际供应的 root.cbl 中保留局部源码事实。正文来源记录为 local_source_facts，model_answer_recorded={pressure.get('model_answer_recorded')}，状态为 {pressure.get('status')}；这证明有界停止时仍保留可证明内容，不能据此称模型已经完成调查或全部业务路径已得到解释。最后计划、实际源码、正文及未完成边界均在 JSON 中保留。")
    elif pressure.get("answer_available"):
        lines.append(f"压力案例最终请求数为 {pressure.get('model_requests')}，正文来源为 {pressure.get('answer_origin')}，状态为 {pressure.get('status')}。是否完成业务调查仍须结合 JSON 中的 answer_completion 与 trace 判断，不能只凭有限请求和存在正文判定。")
    else:
        lines.append(f"压力案例最终请求数为 {pressure.get('model_requests')}，未保留可用正文；有限退出不能等同于业务问题已完成。JSON 保留最后调查计划、实际供应源码和诊断结果，不能将该边界记为质量通过。")
    lines.append("")
    args.output.with_suffix(".md").write_text("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--output", type=Path, default=Path("docs/reports/2026-10-19-bounded-answer-comparison.json"))
    parser.add_argument("--run-one", choices=CASES)
    parser.add_argument("--source-tree", type=Path)
    args = parser.parse_args()
    if args.run_one:
        print(json.dumps(run_one(args.source_tree.resolve(), args.run_one), ensure_ascii=False))
    elif args.baseline and args.candidate:
        compare(args)
    else:
        parser.error("provide --baseline and --candidate")


if __name__ == "__main__":
    main()
