# P3-A 项目进度报告

日期：2026-08-31

核心判断：P3-A 的可运行骨架已经闭环，但 P3 还不能宣布完成。现在系统已能安全地探测公司 OpenAI-compatible API、在最多六步内调度四个只读工具、从真实结构索引动态收集证据并生成固定四段回答；但真实公司端点尚未验收，独立的 Claim 语义支持核验也尚未接入。

## 已完成的实现

[`company_api.py`](../../poc/company_api.py) 现在会行为化探测基本 Chat、原生 Tool Calling、`role=tool` 结果回传、严格 JSON 和可选 Embedding。`/models` 只作为信息项，不再因公司网关未暴露模型列表而错误阻断 Agent。客户端默认断网，生产连接只允许 HTTPS，拒绝重定向，限制请求字节、响应字节和模型输出 token。自定义环境映射不会偷用宿主进程中的 Key，报告和错误也不记录 Key、Base URL、模型名、Prompt 或远程响应正文。

[`agent_loop.py`](../../poc/agent_loop.py) 把模型限定为“规划器和表达器”。可执行工具集合固定为 `search_code`、`inspect_symbol`、`trace_relations`、`read_evidence`，每轮只接受一个 action，最多调用六次，连续两次无新实体或证据后停止。应用侧会校验工具参数、契约版本、快照一致性、状态、截断标记和 Evidence 定位字段。`read_evidence` 只能读取当次调查前面已发现的 ID，且只有真正返回、Hash 有效、未截断的 span 才能成为最终引用。

[`investigation_tools.py`](../../poc/investigation_tools.py) 增加了本地快照覆盖报告。每个回答都会明示已索引的资产类型，以及是否缺少 DDL/DDS、Job/JCL、数据库记录、控制表值、运行参数和日志。这些覆盖信息是编排器本地元数据，不是模型可调用的第五个工具。SQLite 只读连接现在也会在每次调用后显式关闭。

[`run_agent.py`](../../poc/run_agent.py) 是新的完整入口。它每次先执行 capability probe，再选择 `NATIVE_TOOL_CALLING` 或 `VALIDATED_JSON_FALLBACK`，然后绑定本地结构索引执行一次调查。缺少 `--allow-network` 时不会发送请求，且以非零退出码标记 `NOT_READY`。

## 离线验收结果

当前 70 项自动测试全部通过，不访问真实网络。API 测试使用注入的内存传输，覆盖 Key 隔离、HTTP/重定向拒绝、请求上限、Tool Calling 完整回传、JSON fallback、非有限 Embedding 向量拒绝和离线退出码。Agent 测试覆盖越权工具、额外参数、多 action、六步上限、无进展停止、任意 Evidence ID、伪造/越权工具结果、源码夹带、快照异常和未声明代码锚点。

CALC-01 端到端测试每次重建真实 fixture 索引，再执行四个工具调用：

1. 搜索 `OUT-INSTALMENT-PREMIUM`；
2. 从搜索结果动态取得计算 Program，检查公式与外部表；
3. 从 incoming `CALLS` 动态取得入口 Program，追踪字面量与动态调用；
4. 从前三步动态收集 6 个 Evidence ID，读取并核验源码。

该测试不硬编码 Evidence ID，因为 ID 会随文件 Hash 和行范围变化。它同时验证两类必须拒答的问题：缴费因子的商业/精算原因，以及某日生产环境实际使用的动态 Program 和控制表值。这两项目前验证的是结构化拒答路径，不是真实公司模型的决策质量。

## 为什么当前最高是 PARTIAL

现有校验能证明“这个引用属于同一快照、源文件 Hash 正确、代码锚点和数字字面量存在”，但不能证明任意自然语言句子的逻辑与源码一致。例如，正向和否定句可能包含完全相同的代码锚点。因此运行时会自动加入 `semantic_claim_support_not_checked` 边界，把模型声称的 `supported` 降为 `partial`。

要解除这个降级，下一切片需要增加独立 Claim Support Checker，并用正向、否定、条件、数值、路径和拒答对抗样本验证它。在此之前，保留 `PARTIAL` 比提前宣称 `SUPPORTED` 更符合本项目的证据优先原则。

## 下一个退出门槛

P3 的下一个决策点是在公司批准的 Windows 环境中：

1. 对真实 API 执行 capability probe，确认原生 Tool Calling 或 JSON fallback 模式；
2. 对合成 CALC-01 执行一次真实模型调查，核对工具步数、引用、边界和成本；
3. 把同一运行器连接到获准的试点索引，不导出真实源码或日志；
4. 实现独立 Claim 语义支持核验，在对抗评测通过后才允许返回 `SUPPORTED`。

公司 API 实际调用和真实 DXC 代码库验收仍只能在公司批准环境中完成。
