# 业务回答挑战集 v1

这组 8 题用于发现业务答案的具体错误，比较修改前后的质量、耗时与模型请求数。全部复用现有中性合成源码，不增加测试夹具，不需要公司源码或框架手册。题目和逐项参考判断本轮按原始源码另行审阅；源码已经用于开发，因此统一标记为 `development`，不能称为独立 holdout，也不能代替公司业务验收。所有预期都是静态条件推导，未执行 COBOL。

## 题目与输入分组

现有评测器一次运行只接受一个 source root，故分为 5 份可直接运行的 case 文件。`manifest.json` 记录完整映射，不能直接作为 `--cases` 输入。

| Case 文件 | 源码根目录（相对仓库） | 题目 ID / 检查重点 |
| --- | --- | --- |
| `exception-main.json` | `poc/fixtures/exception-flow-v4/main` | `nested-exception-alternatives`：加载失败、业务返回、算术异常的互斥路径；`cross-program-error-isolation`：A 失败而 B 成功的跨程序返回及汇总 |
| `context-main.json` | `poc/fixtures/call-context-v3/main` | `evaluate-first-match`：EVALUATE 首匹配及左侧错误优先 |
| `exception-overwrite.json` | `poc/fixtures/exception-flow-v4/regressions/programs` | `post-calculation-overwrite`：错误清零后仍被无条件赋值覆盖 |
| `copy-boundary.json` | `poc/fixtures/error-return-v5/copy-boundary/programs` | `copy-parameter-writeback`：BY CONTENT / BY VALUE 阻断写回 |
| `complex-main.json` | `poc/fixtures/complex-business-v2/main` | `rate-source-and-gating`：数据来源、查询条件、公式及失败门槛；`dynamic-target-boundary`：配置决定的动态目标及不能推断的部分；`validation-priority-and-final-result`：首个错误保留、后续步骤跳过及最终结果映射 |

每题包含 `required_findings`、禁止断言、允许未知、预期来源，以及人工供证的完整必要小文件。`supporting_ranges` 指向支撑每项判断的精确行段；它和 `reviewed_context.source_ranges` 均保存整个源文件 SHA-256。完整文件用于保留控制上下文，不把参考答案或 profile 注入模型。`expected_source_paths` 仅是定位诊断；路径齐全、引用齐全都不能代替答案评分。不要将 fixture 根目录的 README、profile 或其他答案资料加入模型证据。

## 运行方式

先使用工作台相同模型配置和 policy。`--profile workbench` 仅选取工作台参数默认值，不能自动证明实际模型、令牌上限和覆盖设置一致；核对评测报告记录的安全配置摘要即可，不输出密钥。显式 `--framework ""` 避免引入无关本机框架资料。

以下例子只生成计划，不调用模型。将 `task_repo` 和 `task_eval` 替换为实际的代码目录、独立评测结果目录；结果目录不得位于源码根目录内。

```sh
task_repo='/absolute/path/to/repository'
task_eval='/absolute/path/to/evaluation/baseline/exception-main'
python3 "$task_repo/poc/live_business_eval.py" \
  --source "$task_repo/poc/fixtures/exception-flow-v4/main" \
  --cases "$task_repo/docs/eval/quality-challenges-v1/exception-main.json" \
  --evaluation-dir "$task_eval" \
  --profile workbench --mode automatic --framework "" \
  --capture-context --plan \
  --review-template "$task_eval/review-empty.json"
```

实际运行时，在已授权的模型及费用范围内，将 `--plan` 改为 `--allow-network`。首组保留连接预检，已确认同一连接有效的其余组可用 `--no-preflight`，避免每组重复预检；修改前后需使用相同约定。按表运行另外四组；不要把正向 main、边界样本和故意错误的 regression 混在同一个 source root。

快速诊断顺序：先跑 8 题 automatic，人工逐项审阅；只对答错或明显缺项的题跑 `--case-id ID --mode paired-synthesis`，区分自动供证不足与模型合成失败。paired-synthesis 需先把对应 automatic 的 `captures/` 复制到独立 paired 结果目录，在同一源码、配置与问题上运行。它会分别生成自动证据与人工证据两支答案，不能把 gold 送给模型。使用独立目录能避免覆盖 automatic 的 `live-business-eval.json`。冻结证据缺失、哈希或问题变化时应停止这次配对，不能用临时拼凑证据代替。

`--mode reviewed-context` 可单独测试人工必要证据下的合成，但不与原自动答案直接当作严格的配对实验。严格配对来自上述 paired-synthesis 两支重合成；当前评测器在该模式记录整题两支的合计耗时，没有单独记录每支耗时。

## 质量与速度判定

先看关键项错误/遗漏和禁止断言，再比较有用性与速度。报告 `quality_status=complete` 只表示评分栏填全，不表示通过。建议报告每题关键项 `met / omitted / incorrect`，以及禁止断言是否出现；任一关键结论错误或禁止断言出现都单列失败，不能用其他题的高分抵消。

自动模式已记录每题 `elapsed_seconds`、`model_requests`、`usage`、`request_bytes`、本地阶段 `metrics` 和整体 p50/p95。保存基线与候选的完整报告、未评分表、填写后的评分表和冻结 captures。比较时保持源码哈希、问题、模型、policy、超时、输出上限及运行模式一致；首轮先一遍定位问题，确定有效改动后再交错复跑有代表性的题，避免把单次网络波动宣传成提速。

索引建立耗时在 `index_seconds`/`search_index_seconds`，不包含在 automatic 单题延迟里，需另报；不把 paired 两支合计耗时与 automatic 单题耗时相减。不能通过减少必要源码、降低输出完整度或省略错误处理来换取速度通过。

本目录只交付离线题目与金标准，不代表已连接模型、完成模型评测或人工评分。
