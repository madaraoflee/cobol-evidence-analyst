# 联合验收与发布流程

正式发布要求同一份 Windows ZIP 同时具备原生离线回归、真实模型问答、逐条人工评审与目标办公设备验收。缺任何一项就保留为候选。构建成功或填写完评分表不代表答案正确。

公司 Windows 设备优先使用[直接运行的验收包与步骤](office-acceptance-steps.md)：独立编译的向导带 Tk/运行时，冻结原应用 EXE，默认无调用计划、本机完整 76 样本回归、38 轮真实问答、逐项人工评审及本地门禁。无需使用者安装 Python、Git 或开发工具。`windows-office-acceptance.yml` 只编译辅助工具，在 Windows 启动它并复跑原 EXE；不将 CI 机器冒充公司设备，不调用公司模型或发布。

桌面主分支构建成功后，须通过 `workflow_dispatch` 手动运行 `windows-office-acceptance.yml`，生成与该次构建绑定的验收包；不会由 `workflow_run`、push 或 PR 自动触发。解析器只接受本仓库 main 的成功桌面 run，核对提交、attempt 与唯一候选 artifact；缺包、过期、重复或版本不符都会停止，不回退到旧 EXE。编译辅助工具前重算候选门禁，并核对同一验收合同及应用输入树。手动运行可通过 `app_commit`、`candidate_run_id`、`candidate_artifact_id` 指定应用提交、构建 run 和候选 artifact；全部留空时选择最新成功 main 构建。

离线合同保留原 17 案例、55 样本及其嵌入的 14 次独立默认 HTTP 问答，覆盖组字段与限定身份、歧义、明确的两条 IF 和 PIC 供证；另增 3 次越过交付目标仍交付首答及 3 次自然 batch 触发的回归，再增 CL 批次环境与长文件晚调用各 3 次，再增框架多页 CALL 与 COPY 参数连续供证 3 次，再增批次和在线业务概览各 3 次，合计 24 案例、76 样本。每次有独立 run、答案及证据哈希、请求和耗时，耗时计入原样本门槛；静态调用来源与未实现段落还增加了实际诊断闭合检查。新增慢响应是受控离线机制测试，不代表真实模型速度。公开默认答案还核对补全后的必要事实、实际正文与来源；CL 检查要求来自最终 EXE 的索引事实与实际供应原文相符，运行环境未知不阻断其他直接源码解释。38 轮真实模型题集及 114 次自动请求上限保持单独记录；组内裸字段、IN 限定别名和同名字段歧义也必须完成真实问答与人工评审，正式门禁要求全部回归覆盖标签。

`windows-desktop.yml` 仅通过 `workflow_dispatch` 手动运行，push 和 PR 不触发构建：固定版本 actionlint 校验 Actions 表达式 → 固定公开语料校验 → Python/浏览器回归 → MSVC 编译 → 启动最终 EXE 联合回归 → 候选门禁 → 上传白名单产物。失败会阻断候选上传；合成模型不能授权正式发布。候选产物含 ZIP、校验和、BUILD-INFO、逐样本原生报告、合同、候选策略与门禁结果。工作流语义检查使用 [actionlint 官方用法](https://github.com/rhysd/actionlint/blob/v1.7.7/docs/usage.md)，只在维护/CI 环境运行，不进入用户应用。

真实验收只在获准设备上执行，报告保留本地。维护者先准备已经审阅的案例 JSON，使用现有 `live-business-eval/v3` 案例结构；每项关键结论须包含 `gold.required_findings[].supporting_ranges`，记录路径、行段与源码 SHA。`reviewed_context.source_ranges` 提供相同版本的冻结源码范围。正式题集至少含一个三轮 `turns` 场景、一项 `split=holdout` 留出题和一项 `split=regression` 或 `public-development` 回归题；后两轮必须实际供应前序 user/assistant 历史。不能用单个容易的单轮题代替联合质量验收。这些要求在任何模型请求之前检查。

先冻结正式策略，不能在看到结果后修改速度目标。策略继承候选的 `source_commit` 与 `manifest_sha256`，并增加 `live_cases_sha256`、`live_limits`（`question_seconds`、`p95_question_seconds`、`min_evidence_recall`）和 `office_required_checks`。办公设备六项最低检查固定为 `desktop_start`、`source_import`、`conversation_recovery`、`cancellation_recovery`、`historical_evidence`、`company_api_ca`，策略只能增加，不能删减；模板自动带入全部六项且初始均未评分。阈值应基于实际模型基线确定；未测基线时不能宣称精度提升。真实大库规模与企业目标方言还须在实际验收记录中明确，单凭题集标签不能证明其成立。

以下命令默认只计划，不启动模型请求：

```powershell
python tools/desktop_live_acceptance.py --archive <最终WindowsZIP> --source-commit <完整commit> `
  --source <获准源码目录> --cases <冻结案例JSON> --output <新的本地验收目录> --plan
```

获得真实模型与材料发送授权后，维护者在 Windows 上执行同一最终 EXE：

```powershell
python tools/desktop_live_acceptance.py --archive <最终WindowsZIP> --source-commit <完整commit> `
  --source <获准源码目录> --cases <冻结案例JSON> --output <新的本地验收目录> `
  --profile-config <显式.env文件> --allow-network --request-budget <总请求上限> `
  --max-model-requests 5 --max-question-seconds 120 --source-format auto `
  --office-check desktop_start --office-check source_import --office-check conversation_recovery
```

总请求上限至少覆盖计划的轮数乘单轮预算。没有额外 ping，不自动重试失败或不确定的远程请求。使用公司 CA 时显式加 `--ca-file <证书文件>`，目标方言/COPY 编译配置使用现有 `--compilation-context <JSON>`；配置哈希绑定到评审，逐轮检查未改变。运行隔离配置，不继承开发者环境。报告记录 EXE 实际生成的索引文件数、字节数、快照及源码选项，明确这是索引快照而非全库实时状态。用户应用本身仍无需 Python、Git 或编译器；这些命令供维护者验收使用。

工具产生 `LIVE-ACCEPTANCE.json` 和未评分的 `HUMAN-REVIEW-TEMPLATE.json`。人工检查实际回答与证据后另存评分文件：每条 required finding 记录 met/omitted/incorrect，每条 forbidden claim 记录是否出现；填写可用性、追问连续性、评审人和时间。办公验收记录绑定 ZIP/EXE/commit、evaluation_run_id 和最终报告 SHA。改答案、模型、prompt、policy、gold、供证范围、用时或包文件都会使旧评审失效。报告中的实际历史与 prompt 哈希来自 EXE 发出的请求，不能用维护脚本当前代码代替。

唯一受支持的正式发布入口是以下工具，默认仅本地检查：

```powershell
python tools/publish_desktop_release.py --package <最终WindowsZIP> `
  --compiled-report <COMPILED-ACCEPTANCE.json> --manifest <ACCEPTANCE-MANIFEST.json> `
  --policy <冻结正式策略JSON> --live-report <LIVE-ACCEPTANCE.json> --live-cases <冻结案例JSON> `
  --office-report <本机重新生成的COMPILED-ACCEPTANCE.json> `
  --reviews <已完成评分JSON> --repo madaraoflee/cobol-evidence-analyst --tag <现有草稿tag> `
  --output <本地发布决定JSON>
```

检查完整通过且用户明确授权正式发布时，再加 `--publish`。该入口核对现有草稿目标 commit、tag、Windows 资产字节 SHA，拒绝顺带发布未经验收的其他平台包；只上传经过白名单筛选的哈希凭据，再转为正式版本。企业问题、回答、原始节录与评审人不上传。门禁失败时零 GitHub 调用。

仓库管理员仍可在 GitHub UI 或其他命令中绕过此工具；本地流程不冒充服务器访问控制。维护流程禁止直接使用 `gh release edit --draft=false` 绕过联合门禁。现有正式版本不会因为新候选构建成功而自动替换。

门禁现同时重算本机原生报告的全部案例，要求同一 EXE/合同、与 CI 不重叠的新运行身份和与真实报告相同的匿名设备观察。真实题集不可删掉旧语义、公式、调用链、异常与公开业务回归。逐题保留实际 EXE 的完整诊断并绑定评审身份；缺状态、缺停止原因、改诊断或用旧评分均失效。PARTIAL 必须逐题记录具体分类、原因、受影响关键结论及是否查清；仍有错误/遗漏的答案不能因“已查清”勾选而通过。原归档记录保持原版本，不用新重跑覆写历史结果。
