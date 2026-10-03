"""Disposable, offline browser-review fixture using the real workbench server.

Run from the repository root: python3 poc/tests/workbench_ui_fixture.py --port 8873
Only synthetic source and conversations are created in a temporary directory.
Questions deliberately exercise cancellation or a safe failure; no provider is called.
"""
from pathlib import Path
import argparse
import hashlib
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyze_source import analyze_source
from api_error_details import build_diagnostic
from company_api import APIClientError, CompanyAPIConfig, TransportResponse
from web_app import WorkbenchState, WEB_ROOT, create_server
from source_reading import _identify_page
from source_session import archive_evidence


ANSWER = """## 费用如何计算？

在这个**合成验收案例**中，程序先确认申请状态，再按有效数量计算费用。以下是预置的界面测试内容，没有调用模型，也不代表真实业务验收。

### 1. 先判断是否可以受理

仅在 `REQUEST-STATUS = 'READY'` 且数量大于零时进入计算。缺少必要条件时返回待补充状态，不更新原有结果。 [review-evidence]

| 条件 | 处理结果 | 需要核对的范围 |
| --- | --- | --- |
| 状态为 READY，数量有效 | 进入费用计算 | 当前程序 |
| 数量为零或负数 | 返回补充提示 | 输入校验 |
| 外部规则缺失 | 保留待确认项 | 尚未提供的依赖 |

### 2. 再计算并保存结果

基础费用等于有效数量乘以单价。程序使用定点小数保存计算结果；本例只验证阅读排版，不能据此判断生产中的精度或舍入规则。

```cobol
COMPUTE TOTAL-FEE = VALID-COUNT * UNIT-PRICE
IF TOTAL-FEE > LIMIT-AMOUNT
    MOVE 'REVIEW' TO REQUEST-STATUS
END-IF
```

### 3. 保留例外与证据边界

**外部审批规则尚未提供，不能确认所有异常路径。** 引用位置可以定位源文件，但旧引用失效时必须明确提示，不能继续显示已经无法核对的原文。

> 当前说明只覆盖已提供的合成程序。需要进一步确认的条件仍然保留在答案中。

### 接下来可以追问

可以继续询问数量来源、状态变化、失败时是否重复更新，以及每条结论对应的源码。下面的长段落用于验证连续阅读、换行和末尾保留。

""" + "\n\n".join(f"**核对项 {i}。** 先确认输入条件，再核对计算和保存步骤。发生异常时，已有答案与当前草稿应保持可见；技术信息折叠保存，不能代替明确的失败提示。" for i in range(1, 9)) + "\n\n**长答案末尾标记：已保留全部合成文本。**"


def offline_analyzer(source, output, **options):
    if not options.get("allow_network"):
        return analyze_source(source, output, **options)
    # Give the real cancel endpoint a deterministic window for browser review.
    for _ in range(80 if "停止" in (options.get("question") or "") else 12):
        options["check_cancel"]()
        time.sleep(.1)
    diagnostic = build_diagnostic("HTTP_ERROR", http_status=429)
    raise APIClientError("HTTP_ERROR", http_status=429, diagnostic=diagnostic)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8873)
    parser.add_argument("--web-root", type=Path, default=WEB_ROOT)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="cobol-ui-review-") as temporary:
        root = Path(temporary).resolve()
        source, output = root / "synthetic-review", root / "analysis"
        source.mkdir()
        (source / "fee-review.cbl").write_text(
            "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. FEE-REVIEW.\n"
            "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n"
            "       01 VALID-COUNT PIC 9(4) VALUE 2.\n"
            "       01 UNIT-PRICE PIC 9(4)V99 VALUE 12.50.\n"
            "       01 TOTAL-FEE PIC 9(8)V99.\n       PROCEDURE DIVISION.\n"
            "           COMPUTE TOTAL-FEE = VALID-COUNT * UNIT-PRICE.\n"
            "           STOP RUN.\n", encoding="utf-8")
        app = WorkbenchState(
            analyzer=offline_analyzer,
            config_provider=lambda: CompanyAPIConfig(base_url="https://offline.example.invalid/v1", chat_model="synthetic-review", api_key="synthetic-unused"),
            model_check_transport=lambda _: TransportResponse(200, '{"choices":[{"message":{"content":"synthetic"}}]}'),
            policy_provider=lambda: {}, folder_picker=lambda _: None,
            demo_output_root=root / "demo", state_path=root / "state.json")
        app.start({"source": str(source), "output": str(output), "allow_network": False})
        while app.job["status"] == "RUNNING":
            time.sleep(.02)
        assert app.job["status"] == "COMPLETED", app.job
        store = app.conversation_store
        source_text = (source / "fee-review.cbl").read_text(encoding="utf-8")
        page = {"relative_path": "fee-review.cbl", "start_line": 5, "end_line": 10,
                "source_sha256": hashlib.sha256(source_text.encode()).hexdigest(),
                "source_text": "\n".join(source_text.splitlines()[4:10])}
        page["evidence_id"] = _identify_page(page)
        archive_evidence(output / "structural-index.sqlite", [page])
        for question in ("合成案例 · 异常状态如何保留？", "合成案例 · 哪些程序受影响？"):
            other = store.create(app.project["snapshot_id"])
            store.append(other["id"], "user", question)
            store.append(other["id"], "assistant", "合成验收记录。尚未提供外部依赖，相关行为仍需确认。")
        conversation = store.create(app.project["snapshot_id"])
        store.append(conversation["id"], "user", "合成案例 · 费用如何计算，哪些条件需要人工复核？")
        store.append(conversation["id"], "assistant", ANSWER.replace("[review-evidence]", "[" + page["evidence_id"] + "]"), status="partial", details={
            "answer_truncated": True, "finish_reason": "length",
            "evidence_refs": [{"evidence_id": page["evidence_id"], "relative_path": "fee-review.cbl", "start_line": 5, "end_line": 10},
                              {"evidence_id": "review-evidence", "relative_path": "old-review.cbl", "start_line": 1, "end_line": 4}],
            "related_sources": [{"relative_path": "fee-review.cbl", "program_names": ["FEE-REVIEW"]}, {"relative_path": "review-rules.cpy", "program_names": []}]})
        app.conversation = store.get(conversation["id"])
        app._save_workspace()
        server = create_server(args.port, app=app, web_root=args.web_root)
        print(f"Synthetic review only: {server.origin}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
