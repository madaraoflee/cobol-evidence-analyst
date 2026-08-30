# COBOL Evidence Analyst POC 项目进度报告

领导审阅版：[COBOL Agent P1-B Word 报告](./COBOL-Agent-P1B-Progress-Report.docx)

日期：2026-08-30  
阶段：`P1-B — bounded investigation tools`  
结论：`GO — 可以进入公司 API Adapter 与受控 Agent 循环开发`

## 管理层摘要

本轮已经证明最关键的底层假设：不依赖大模型、不预写代码答案，仅凭本地 COBOL/COPYBOOK 索引，系统能够找到计算公式、调用关系、控制表边界和可复核源码位置。四个只读调查工具已经完成，并在一个完全原创的香港保险风格合成流程上以 6 次工具调用形成证据闭环。

这还不是最终 AI 对话产品。当前完成的是 Agent 的“眼睛和调查工具”；下一阶段才把公司 OpenAI-compatible API 接到这些工具上，让模型负责理解自然语言、选择下一步调查动作和组织中文业务回答。代码事实仍由本地工具提供，模型不能执行 SQL、Shell、任意文件读取或修改源码。

## 本轮可验证交付

| 交付物 | 结果 |
| --- | --- |
| 原创保险代码库 | 8 个 COBOL Program、5 个 COPYBOOK，共 13 个文件 |
| 结构事实 | 261 个 CodeUnit、104 个 Symbol、416 条 Relation、241 个 EvidenceSpan |
| `search_code` | 精确符号优先，再使用 FTS5；达到预算时返回 `PARTIAL` |
| `inspect_symbol` | 查看精确定义、写入点、读取点、调用、PERFORM、条件和外部表 |
| `trace_relations` | 只允许白名单关系，最多 3 跳和 200 条边；动态调用保持 unresolved |
| `read_evidence` | 只能按已发现的 Evidence ID 读取，校验 Snapshot Hash，并标记源码为不可信数据 |
| 六步演示 | 取得最终公式、年化组成、15 条入口路径/边界关系和 12 段完整性通过的源码证据 |
| 自动测试 | 22 / 22 通过，用时约 0.1 秒；整个过程无网络调用 |

可执行演示记录见 [CALC-01 P1-B 演示结果](../leadership-demo/p1b-executable-demo.md)。

## 已证明的业务效果

系统从源码识别出：

```text
分期保费 = 年化应缴保费 × 缴费频率因子

年化应缴保费 = 基础保费
               + 职业风险加费
               + 附加保障保费
               - 高保额折扣
               + 年度保单费用
```

两个计算语句都明确使用 `ROUNDED`。系统还确认缴费因子来自 `SYN_MODE_FACTOR`，产品计算器名称来自 `SYN_CALC_ROUTING`，并识别了入口 `SYNP000` 到加载、路由、附加保障、调整和结果映射程序的字面量调用。

同样重要的是，它没有把动态 `CALL LK-CALCULATOR-PROGRAM` 猜成某一个确定程序。因为当前只有程序源码、没有控制表记录和运行输入，所以工具保留动态调用边界；这正是后续 Agent 应该向用户说明“能够确认什么、不能确认什么”的行为。

## 当前成熟度

| POC 阶段 | 当前状态 | 说明 |
| --- | --- | --- |
| P0 代码库画像 | 已完成公开实现 | 真实公司代码库聚合画像仍需在公司环境运行 |
| P1-A 结构索引 | 已完成 | Program、Paragraph、Field、CALL、PERFORM、基础数据/控制关系和 EvidenceSpan |
| P1-B 调查工具 | 本轮完成 | 四工具、白名单、预算、状态、边界和函数 Schema 已通过测试 |
| P2 单流程深分析 | 合成语料通过 | CALC-01 可取得证据闭包；真实 DXC 流程尚未验证 |
| P3 Agent 可对话 | 下一阶段 | 公司 API Adapter、6 步循环、回答与引用核验尚未实现 |
| P4 领导演示版 | UI 概念稿已存在 | 本轮校准为真实 fixture 数字；后续连接真实 Agent 运行结果 |

## 架构判断

现在不应加入 Qdrant、Neo4j、LangGraph 或本地模型。现有 Python 标准库、SQLite/FTS5 和受限工具已经足以验证第一条真实业务闭环。只有公司真实代码规模和金标准暴露明确瓶颈时，才增加 Embedding、图服务或可恢复状态机。

存储层仍保持可替换方向：公司若只禁止自行安装软件，可以使用 Python 已包含的 `sqlite3`；若明确禁止 SQLite，后续增加 `FileIndexStore`，用 JSONL、压缩倒排索引和内存邻接表实现同一工具契约。该备用方案不阻塞当前 POC。

## 风险与处理

| 风险 | 当前处理 |
| --- | --- |
| 公司 API 不支持 Tool Calling | 使用严格 `action + arguments` JSON 回退，仍执行相同四工具 |
| 公司 API 没有 Embedding | 继续采用精确符号、FTS5 和结构关系；Embedding 不是 POC 前置条件 |
| COPYBOOK 未展开 | 当前按名称发现共享字段并显式保留 unresolved；下一步增加参数位置映射，不伪造 resolved |
| DXC/Smart COBOL 方言差异 | 真实试点发现一条、增加一条可测试规则；不先造完整编译器 |
| 上万程序导致图扩展过宽 | 工具固定深度、边数、证据字符和调用次数预算，并显式返回截断状态 |
| 私密源码进入外部服务 | 索引与追踪留在公司电脑，只向公司 API 发送当前问题需要的 EvidenceSpan；Key 不落盘 |

## 下一阶段实施顺序

1. 实现公司 API capability probe，仅检查 Chat、Tool Calling、严格 JSON 和可选 Embedding，不记录 API Key。
2. 实现最多 6 次调用的单 Agent 循环；连续两步没有新证据即停止。
3. 固定四段回答格式：结论、代码怎样实现、源码依据、不能确认。
4. 加入引用完整性检查，禁止回答引用未读取的 EvidenceSpan。
5. 将现有浅蓝领导演示页面连接到真实本地运行结果，并保留“合成数据 / API 是否连接”的醒目标识。
6. 在公司 Windows 环境对一个获准业务流程运行聚合画像和金标准测试，再决定是否增加 Embedding 或文件型备用存储。

下一阶段退出门槛不是“聊天界面能说话”，而是：对 CALC-01 的自然语言问题，Agent 能在 6 步内调用真实工具，生成与当前证据结果一致的中文回答；当询问费率商业原因或实际控制表值时能够拒答。
