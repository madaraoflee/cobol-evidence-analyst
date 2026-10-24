# 业务答案的条件与赋值绑定核验

> 历史调查记录（2026-10-23）：下文版本、哈希、耗时和回滚补丁只对应当时快照，不代表当前代码。旧报告中的源码更新重试、搜索缓存缺页两项问题已在本次整合中复现并修复；当前源码与完整回归见[2026-10-24 整合记录](../2026-10-24-pending-integration/verification.json)。日志中的本机目录已规范化，原始测量数值保留。历史回滚补丁不得作为当前版本的回滚方案。

本轮使用公开合成源码及本地固定响应，先诊断三个当前问题，再只修复其中最优先的一项。没有使用公司源码或私有框架资料，没有真实模型请求、费用、应用构建、安装包或发布。

## 当前源码与运行范围

- 仓库 `cobol-evidence-analyst`，分支 `main`，基线提交 `a82ac474e9708a18dab00acc2e078716aed61c07`。
- 开始时已有 6 个已跟踪文件修改和 8 个未跟踪文件，涉及业务叙述、交接解释和题内缓存。本轮没有编辑这些文件；期间其他工作继续修改了其中部分文件。[开始时文件哈希与环境记录](source-state.json)区分提交版本和工作树版本，最终验证使用独立冻结快照。
- 当前工作树标识为 pipeline `question-evidence-v47` / prompt `business-chat-v41`；这些包含既有未提交修改，不能当作已安装版本。
- 网页入口为 `poc/web_app.py`，默认使用 `business` / `retrieval`；桌面入口为 `poc/desktop_app.py`。本机监听端口检查没有发现工作台；进程列表读取受主机限制，未取得运行实例的版本，也未取得 Windows 制品版本和哈希。
- 当前 `AgentPolicy` 默认最多 6 次模型请求、5 轮调查、2 次无新证据修订；整题与本地阶段时限默认 `None`，显式限制仍有效。历史文档中的默认 180 秒不能代表当前代码，本轮不改预算或恢复旧默认。
- 两个 Windows 工作流只有 `workflow_dispatch` 手动入口。本轮未触发工作流。

默认链路为：自然语言与历史焦点 → `indexed_chat.try_indexed_question` → `business_chat.run_business_chat` → SQLite 检索及本题源码版本采集 → 提供原文、条件和赋值事实 → 草稿与最终出口的 `review_answer_claims` → 保存答案、引用及会话 → 页面展示。本轮修复位于两个出口共用的断言核验层，不改检索、数据库或页面架构。

## 已复现问题及优先级

| 优先级 | 问题及期望 | 当前实际与证据 | 本轮范围 |
| --- | --- | --- | --- |
| P1 | 条件与赋值必须作为一个关系核对。源码大于零乘 2、否则乘 3，不能接受相反解释。 | 同一原文已供应完整条件及两支公式，错误答复仍原样交付；局部核验为 3 条 supported。缺口在答案的条件与公式未绑定，并非缺源码。[原始核验](branch-before.json)、[实际问答出口](answer-path-before.json)。 | 修复及回归。 |
| P2 | 中断后源码改变，明确重试应能进入新版本的有限调查。 | 乘 2 改为乘 3 后，两次明确重试都返回 `RESUME_SOURCE_VERSION_CHANGED`；旧调查仍为 running。换对话才取得新公式。没有复用过期答案，但恢复无出口。[重放记录](source-retry-before.jsonl)。 | 已复现，保留待修。 |
| P3 | 显式更新应发现损坏的搜索页或 FTS 缓存，并修复对应文件。 | 合成故障注入移除尾部 FTS 行，源码及哈希仍在；更新返回 `cached_files=1`、`updated_files=0`、`full_text_complete=true`，必要的返回 90 赋值未供应。部分搜索页缺失也未触发原文补读。[重放记录](search-gap-before.txt)。 | 已复现，保留待修；不是现场事故结论。 |

P2 的调用入口在 `web_app.py` 的重试分支，`business_chat.py` 在版本变化时返回却未使旧调查退出可恢复状态，`RunStore.unfinished` 下次继续选中它。P3 的 `ensure_repository_search` 缓存复用检查未核对页数和 FTS 行完整性；不能因此要求用户管理搜索词或声称用户没有导入代码。

开始时另见一个已有回归：`test_business_boundary_contract.py` 中 full/compact 两个子测试因 `business_analysis_brief` 缺失而报错。期间并发工作更新了业务说明相关文件，本轮后续复跑该模块的 2 项测试已通过。本轮没有修改该区域，不将这一变化算作本轮修复成果，也不将历史失败冒充当前状态。

## 同一问题的源码依据与前后对照

问题：**OUT-AMOUNT 如何计算？** 固定条件、例外、引用及禁止断言见[回归问答集](cases.json)。

| 证据位置 | 可直接核对的事实 |
| --- | --- |
| 合成 `rule.cbl:5–6` | 输入与结果字段的声明；不证明运行输入有效。 |
| `rule.cbl:9–10` | `IF IN-AMOUNT > ZERO` 的真分支执行 `OUT-AMOUNT = IN-AMOUNT * 2`。 |
| `rule.cbl:11–13` | ELSE 假分支执行 `OUT-AMOUNT = IN-AMOUNT * 3`。 |
| `rule.cbl:14` | 本例随后 GOBACK；不能外推到其他程序或源码版本。 |

在数值有效且正常运算、无接收字段转换错误或精度损失的本例路径上，输入 4 应得 8，输入 −4 应得 −12。倒置答案分别给出 12、−8；只测 0 无法发现倒置。这些是独立按源码指定的对照，不依靠模型自评分，也没有执行 COBOL 二进制程序。

修改前固定错误答复：

> 在 IN-AMOUNT > ZERO 的条件下，OUT-AMOUNT = IN-AMOUNT * 3；否则 OUT-AMOUNT = IN-AMOUNT * 2。

实际问答入口原样保留该错误，状态为 PARTIAL / request_budget；单独局部核验仍将三条断言标为 supported。PARTIAL 本身并没有纠正错误业务解释。

修改后，实际问答入口输出以下两项带条件的源码结论（完整正文及引用见[修复后交付](controlled-after-answer.json)）：

> 在源码控制条件 `IF IN-AMOUNT > ZERO` 下，`OUT-AMOUNT = IN-AMOUNT * 2`（rule.cbl:10）。
>
> 在源码控制条件 `条件不成立：IF IN-AMOUNT > ZERO` 下，`OUT-AMOUNT = IN-AMOUNT * 3`（rule.cbl:12）。

对应证据 ID 均为 `ev_page_30a76d5dee7307a67175af1a`，文件、源码 SHA256 和实际行范围保存在前后记录中。源码事实本来就含真/假分支；本轮使自然语言中明确的条件与赋值组合使用同一绑定关系，再复用已有局部纠错。核验版本由 `answer-claim-ledger-v12` 升至 `v13`。

只有 `poc/claim_ledger.py` 这个生产文件被本轮修改；新增测试为 `poc/tests/test_claim_prose_branch_binding.py`。没有改 pipeline 版本、请求预算、检索或数据库。支持范围限定为明确的条件加单个赋值，以及同一行、同一引用范围内分号后的“否则／otherwise”。重复条件、跨程序、跨版本、缺失外层守卫、未闭合块及否定、假设、待核表述不强行纠错；这些组合保留未核验。一般自然语言、独立表格、跨句省略和任意复杂路径不因本轮升级为完整语义证明。

为排除并发工作影响，另冻结了 675 个源文件，并在该快照中只替换 `claim_ledger.py` 做受控前后对照。[文件清单及哈希](validation-source-manifest.json)绑定该快照；测试期间快照内容未变化。

[输入核对](controlled-comparison.json)确认两次的物理证据 ID、原文、文件哈希、行范围及固定回复相同。各自临时工作区产生的导航 group ID 不同，因此不宣称整个请求逐字节相同。

| 对照项 | 修复前 | 修复后 |
| --- | --- | --- |
| 注入倒置答案中，条件与公式正确对应 | 0 / 2 分支 | 2 / 2 分支 |
| 局部核验对原始错误答复的记录 | 3 supported，错误原样输出 | 1 supported、2 contradicted，按实际分支修正 |
| 正确答复 | 原文保持 | 原文保持 |
| 独立 12 项反例 | 7 / 12 通过 | 12 / 12 通过 |
| 相同新增回归集 | 14 项中产生 26 条失败记录，含子测试 | 14 / 14 通过，包含实际问答入口集成 |
| 同题脚本响应次数 / 请求字节数 | 1 / 34,127 | 1 / 34,127 |
| 外部模型请求 / 新增费用 | 0 / 0 | 0 / 0 |
| 单次本地机制重放耗时 | 0.166 秒 | 0.104 秒 |
| 实际问答状态 | PARTIAL / request_budget | PARTIAL / request_budget |

耗时仅记录一次本地源码机制加固定响应，存在正常波动，不能当作真实模型等待或性能提升证明。原始错答的 `all_claims_supported` 从 true 改为 false 表示两条组合被否定，不等于修复后的所有自然语言已形式化证明。由于此次用单请求预算隔离机制，正确的局部修复也没有把 PARTIAL 升级为完整调查。

受控记录：[核验前](controlled-before-branch.json)、[核验后](controlled-after-branch.json)、[交付前](controlled-before-answer.json)、[交付后](controlled-after-answer.json)、[独立反例前](controlled-before-independent.json)、[独立反例后](controlled-after-independent.json)。固定响应只检验应用是否正确处理可证明的分支错配，不代表真实模型生成质量。

## 重放与验收边界

从仓库根目录执行以下脚本，均只使用临时合成源码及固定响应：

```sh
python3 docs/reports/2026-10-23-branch-binding/reproduce_branch_binding.py
python3 docs/reports/2026-10-23-branch-binding/reproduce_answer_path.py
python3 docs/reports/2026-10-23-branch-binding/reproduce_source_retry.py
python3 docs/reports/2026-10-23-branch-binding/reproduce_search_gap.py
```

既有回归覆盖不从零重建：

| 业务回归目标 | 既有测试入口 |
| --- | --- |
| 跨文件调用与 COPY、代码依据 | `test_evidence_answer_relationships.py`、`test_claim_copy_fragments.py` |
| 空输入 I/O 前提、DISPLAY、返回 90、异常及数值有效性 | `test_semantic_risk_review.py`、`test_numeric_semantics.py` |
| 条件、后续赋值与局部证据限制 | `test_claim_ledger_boundaries.py` |
| 连续追问、证据闭合及等长源码变化 | `test_conversation_evidence_closure.py`、`test_source_versions.py` |
| 证据不足、漏检及上下文裁剪 | `test_retrieval_evidence_regressions.py`、`test_retrieval_quality_eval.py` |
| 中断重放、请求结果未知及用户预算 | `test_source_answer_replay.py`、`test_run_store.py`、`test_question_budget_delivery.py` |

开始时 9 个定向模块共 108 项通过，详见[基线测试记录](baseline-tests.txt)。最终在冻结快照运行 23 个相关模块，**289 项通过，无失败、错误或跳过，耗时 11.611 秒**；全局阻止网络连接。[最终测试统计](validation-results.json)、[逐项结果](validation-tests.txt)。其中包括新增 14 项，以及跨文件、COPY、返回码、数值、历史焦点、等长源码更新、预算、裁剪和中断恢复等现有回归；不是全库测试。独立设计的 12 项反例另外运行并全部通过。

追问一致性由既有多轮证据闭合等机制测试覆盖，本轮没有新增真实模型多轮答题评分。历史边界已有相应防护且本轮相关测试通过，不把旧错误未经复核列为新缺陷。通过并不说明这些语义已全部形式化验证；P2、P3 正说明既有回归还有空白。

可以单独重跑本轮新增回归及独立反例：

```sh
python3 -m unittest discover -s poc/tests -p 'test_claim_prose_branch_binding.py'
python3 docs/reports/2026-10-23-branch-binding/independent_guard_probe.py
```

实现、新测试及 5 个重放脚本共 7 个 Python 文件通过 Python 3.10 语法解析检查，差异空白检查通过。这些都不是应用构建或安装包验证。

本轮不具备真实模型答题评分或用户等待时间、调用成本的真实对照。实际外部请求和新增费用均为 0；离线耗时仅为源码机制及脚本响应的本地运行时间。未执行最终 Windows 安装包业务验收，没有制品版本/哈希，也没有发布资格结论。

## 历史补丁归档

原始调查的最小补丁已归档，不随当前源码提交。它只对应当时的基线，不能撤销后续整合修复；当前变更以 Git 提交差异为准。
