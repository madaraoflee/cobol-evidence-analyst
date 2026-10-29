# 跨对话修复整合覆盖核对

核对日期：2026-10-29。

**本次已经把此前漏在 adaptive 分支的源码能力纳入整合工作树，并保留本轮累计修复。** 这个结论限于下列可访问的 Git 分支、历史工作树、对话结论和实际源码，不表示所有未知缺陷已经消失，也不直接证明任何既有安装包包含这些代码。

## 核对基线与范围

本轮累计修复基线为 `d993031fae8559a14bc162511cc53d46640f1df6`，历史 adaptive 分支头为 `0e082244a28283094b474e3581ca642c7e64e57b`。写入本报告时，主工作树 HEAD 仍为 `d993031`，正在完成这两个来源的合并；本报告没有创建提交、构建程序或发布版本。最终合并提交及运行包来源应以最终发布记录为准。

检查包含：

- 当前仓库全部本地分支与已保存的 `origin/*` 远端跟踪分支，不包含符号引用 `origin/HEAD`；本次覆盖核对没有联网 fetch，因此不推断远端尚未同步的变化。
- 历史工作树 `/Users/madara/.codex/worktrees/adaptive-evidence-retrieval/工作 - Cobol 业务分析`，HEAD 为 `0e08224`。
- 历史工作树 `/Users/madara/.codex/worktrees/business-quality-speed/工作 - Cobol 业务分析`，HEAD 为 `f4d3a88`。
- 可访问历史对话「评估并改进馨弦系统质量」与「优化馨弦系统回答质量与速度」的近期结论。

两个历史工作树的 `git status --short`、未暂存 diff 与暂存 diff 均为空，没有发现滞留在这两个工作树中的未提交修复。

## 分支提交覆盖

对每个引用执行 `git merge-base --is-ancestor <ref> d993031`，并检查 `git rev-list --count d993031..<ref>`。共核对 23 个分支引用：除 adaptive 的本地与远端两个引用外，其余 21 个引用均已被 `d993031` 的祖先历史涵盖，独有提交数为 0。下表合并显示同头的本地和远端引用。

| 分支 | 已核对引用 | 分支头 | 相对 d993031 |
| --- | --- | --- | --- |
| main | 本地、origin | d993031 | 基线本身 |
| codex/baseline-20261007 | 本地、origin | 7dd3e57 | 已包含 |
| codex/bounded-answer-quality | 本地、origin | ea341ba | 已包含 |
| codex/business-behavior-quality-speed | 本地、origin | f4d3a88 | 已包含 |
| codex/source-role-recovery | 本地、origin | f4d3a88 | 已包含 |
| codex/windows-quality-speed | 本地、origin | f4d3a88 | 已包含 |
| design/refine-workbench-20261003 | origin | 73a7182 | 已包含 |
| fix/cobol-business-detail | origin | 1674346 | 已包含 |
| fix/cobol-complete-working-set | origin | a225857 | 已包含 |
| fix/cobol-evidence-batch1 | origin | 3ac40e4 | 已包含 |
| fix/cobol-postrelease-answer-recovery | origin | 4f7d07a | 已包含 |
| fix/default-business-recovery-20261001 | origin | c63554e | 已包含 |
| fix/detail-intent-mac-20261001 | origin | 4eea535 | 已包含 |
| fix/file-field-impact-evidence-20261001 | origin | 0f91a9a | 已包含 |
| fix/safe-api-error-details-20261001 | origin | 9355b44 | 已包含 |
| codex/adaptive-evidence-retrieval | 本地、origin | 0e08224 | 本次补入 |

adaptive 在整合前并非只有提交编号不同：`d993031...0e08224` 为主线独有 37 个、分支独有 10 个提交；`git cherry -v d993031 0e08224` 中 7 个普通提交没有等价补丁，另外 3 个为合并提交。直接比较两端源码还发现生产模块和回归用例确实缺失；将历史测试在内存加载并导入主线源码后，也复现了精确检索被噪声挤掉、后续检索线索预算耗尽、语义未知被误改为源码已提供，以及框架限制和证据引用丢失等问题。因此本次进行了功能整合，没有仅凭分支名称宣称已包含。

历史质量对话的最后结论是 PR #11 保留未合并、暂不调用模型；这与上述发现相符。速度对话提及的 `6408059`、`b636fbd`、`3628a05`、`845143c` 均为当前基线祖先。对话中的旧候选安装包不能据此视为已经包含本次合并。

## adaptive 功能覆盖

以下 5 个历史新增生产模块已存在，分支引入的函数均保留，并核对了实际调用入口：

| 模块 | 已整合能力 |
| --- | --- |
| `poc/answer_preview.py` | 正文流式前缀展示，避免把协议内容或保护值直接暴露到预览 |
| `poc/answer_reference_normalization.py` | 根据实际供应材料规范引用；合并时补入 COPY 实例与替换来源身份 |
| `poc/answer_sql_boundaries.py` | 核对 SQL 回答中的无依据断言；合并时保留本轮多实例来源绑定 |
| `poc/semantic_answer_review.py` | 基于实际供应源码和框架资料构造独立审查，并验证审查结果 |
| `poc/semantic_risk_identity.py` | 稳定识别具体风险、避免把同类计数误认为没有变化；补入 COPY 实例身份 |

对该分支涉及的 22 个 Python 生产模块逐项比较了新增及修改的函数定义，未发现对应定义在当前树中丢失。调用链检查包括 `business_chat` 的审查状态保存、恢复、修订与复审，最终引用规范化，流式正文前缀，以及框架定义与适用限制成组保留。函数存在检查与源码差异审查共同用于覆盖核对，不能代替运行回归。

已有模块中的整合重点为：独立搜索线索轮转和精确标识排序、检索裁剪保留命中位置、共享 COPY 词法缓存、一般程序解释的业务意图识别、保留有效业务叙述、框架原文与限制共同供应、语义未知与物理源码缺失的区分，以及审查进度和风险身份的恢复。3 个历史工具 `benchmark_copy_source_cache.py`、`benchmark_retrieval_routes.py`、`evaluate_retrieval_routes.py` 均完整保留。

## 历史测试保留情况

18 个历史 Python 测试文件全部存在。历史共有 203 个 `test_` 方法；本报告写入时这 18 个文件共有 210 个方法，其中 201 个历史方法名原样保留。其余 2 个方法经过有意改名、拆分以适配主线已经支持的 COPY 能力，原因见表后说明。这里统计方法定义，不把子用例数量当作测试方法数量，也不把存在数量当作通过数量。

| 测试文件（均位于 poc/tests） | 历史方法 | 当前方法 | 原方法名保留 |
| --- | ---: | ---: | ---: |
| test_adaptive_retrieval.py | 11 | 11 | 11 |
| test_answer_boundary_delivery.py | 10 | 10 | 10 |
| test_answer_preview_progress.py | 6 | 6 | 6 |
| test_answer_reference_normalization.py | 13 | 14 | 13 |
| test_answer_sql_boundaries.py | 15 | 16 | 15 |
| test_business_explanation_intent.py | 7 | 8 | 7 |
| test_claim_prose_preservation.py | 10 | 10 | 10 |
| test_expansion_source_cache.py | 10 | 10 | 10 |
| test_framework_copy_assignments.py | 10 | 12 | 8 |
| test_framework_reference_constraints.py | 10 | 10 | 10 |
| test_framework_reference_delivery.py | 8 | 8 | 8 |
| test_framework_wire_evidence.py | 4 | 4 | 4 |
| test_retrieval_focus.py | 6 | 6 | 6 |
| test_semantic_answer_review.py | 31 | 31 | 31 |
| test_semantic_risk_identity.py | 14 | 14 | 14 |
| test_source_availability_semantic_absence.py | 4 | 4 | 4 |
| test_source_bound_semantic_review.py | 23 | 25 | 23 |
| test_workbench_progress_wait.py | 11 | 11 | 11 |
| 合计 | 203 | 210 | 201 |

`test_framework_copy_assignments.py` 的两项调整有明确行为依据：

- 原 `test_copy_change_invalidates_layout_even_when_caller_binding_cache_reuses` 改为 `test_copy_change_invalidates_binding_even_when_caller_bytes_are_unchanged`。主线已经把 COPY 依赖纳入调用绑定缓存失效；新测试同时确认无变化时复用、COPY 变化而调用方字节不变时重建，保留了原来不能沿用过期布局的要求。
- 原 `test_nested_replacing_missing_and_ambiguous_copies_remain_uncovered` 拆为普通嵌套 COPY、单词级 REPLACING、缺失和歧义 COPY 三项。主线已经支持前两类，不能重新套用旧分支的一律不支持预期；更新后仍要求全部物理来源、保留替换轨迹，并拒绝缺失和歧义输入。

`web_progress_wait.test.cjs` 也已保留。其断言仅调整为准确定位当前阶段标题，并检查主线已有的活动历史，避免把历史中的 Writing 误当成当前仍在撰写。

## 本轮累计修复保留与缓存判断

与 `d993031` 比较，核对负责范围得到以下结果：

- `source_session.py` 原有顶层函数 AST 未变；新增词法缓存没有替换源码捕获、编译配置指纹、局部刷新编译上下文、COPY/程序角色变化处理、概览重算和旧归档兼容逻辑。
- `source_availability.py` 在本轮派生页排除逻辑之上新增语义内容缺失保护；带 `copy_instance` 或 `replacement_trace` 的页面仍不能证明原文件完整供应。
- `analysis_continuation.py` 保留本轮 COPY 身份和有效语义进度识别，仅增加稳定风险身份进入复核状态。
- `complete_working_set.py` 与 `d993031` 无差异：稀疏解析因长度预算遗漏事实时，仍区分物理全文完整与依赖闭包未完成。
- `business_synthesis.py` 保留保存的业务要求、当前问题限制、字段和专属范围参数，同时增加历史分支的 SQL/引用复核与一般业务解释入口。新旧意图测试发现的局部问题被扩大为概览的交互问题已修复。
- `repository_discovery.py` 同时保留主线对长中文词组的预算平衡，以及历史分支对独立搜索建议的轮转、精确标识优先和命中位置裁剪。

无需升级持久 `SEARCH_VERSION`：与 `d993031` 的 AST 比较显示 `_identifier_tokens`、`_tokens`、`ensure_repository_search`、`_verified_pages`、`_fast_snapshot` 均未变化。本次改变查询选择与排序，没有改变持久分词、索引记录、物理页或快照格式；已有索引可以直接采用新检索过程。

COPY 词法缓存只存在于 `QuestionSourceSession` 的内存字典，关闭会话即清空，没有跨会话持久缓存需要迁移。键包含相对路径、源码 SHA-256、编码、编译上下文指纹和 `LEXICAL_SOURCE_VERSION`；调用实例的展开结果与替换轨迹不作为共享词法值复用。

## 验证边界

冲突整合后，负责范围的两组新旧专项回归共 234 项通过：业务意图、保存要求、SQL/引用/风险 124 项；源码会话、词法缓存、检索、可用性、调查进度及读取身份 110 项。该结果是专项检查记录；最终整树回归、Windows 运行包的来源提交及验收结果应以本次发布的对应记录为准。

随后全量回归发现一个合并交互问题：一般程序解释入口把“读取不存在的表时会发生什么”误扩展为业务概览。先用原失败用例复现，再将 `what happens when/if` 这类明确场景问法保留为局部解释，并补入原问法与条件变体；相关 6 个模块的 57 项回归通过。此项没有减少源码供应或改变检索过程。

另一个原引用回归暴露了相同入口的范围问题：“订单金额怎样检查并决定结果，面向新人解释”被当作整份程序概览，进而触发完整工作集的重复语义分析跳过。只在内存纠正意图即可使原引用测试通过，确认不是引用字段改名或检索排序退化。随后补入具体检查/判断问法的中英文意图回归，修正新人语气扩大问题范围的行为；原 `test_prompt_source_references.py` 未修改，连同业务意图、保存要求、范围恢复、完整工作集的 112 项回归通过。

本报告不覆盖无法访问或尚未同步的其他仓库、分支、工作树和对话，也不表示已消除所有未知缺陷。源码覆盖、离线回归与真实模型回答质量、目标设备验收是不同证据；这里没有调用真实模型，也没有构建或提交发布。
