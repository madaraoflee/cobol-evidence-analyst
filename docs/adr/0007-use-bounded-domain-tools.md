---
status: proposed
date: 2026-08-29
---

# Agent 使用受约束领域工具

在线 Agent 只调用版本化、只读、受快照、权限、关系白名单和预算约束的领域工具，不获得 raw Cypher、Qdrant query、SQL、Shell、任意文件读取或无界图扩展能力。LangGraph Runtime 注入不可由模型修改的调查上下文；工具以统一状态、Evidence/Fact refs、Boundary、Diagnostic、预算消耗和审计引用返回结果。

定位工具只产生候选；计算、来源、条件和执行路径由各自专项工具证明；只有确定性的 `obligation.assess` 可以改变证据义务状态。源码文本被标记为不可信数据，不能成为工具指令。工具只追加调查轨迹，不修改源码、IR 或派生事实索引。

这样做牺牲了一部分自由探索能力，也需要维护更多 JSON Schema 和 Contract Tests，但能够限制图爆炸与数据越权，区分候选和事实，稳定评测每类调查能力，并使 Agent 编排、图数据库和向量数据库可以独立替换。

## Considered Options

- **向 LLM 开放 raw Cypher、向量查询或文件读取**：原型灵活，但权限、预算、关系语义、结果完整性和评测无法可靠控制。
- **只提供一个通用 graph expansion 工具**：接口数量少，但问题关系白名单和完成条件会重新落入 Prompt。
- **为每个业务问题编写固定工作流而不保留 Agent 决策**：最可控，但面对多跳歧义、复合问题和部分边界时缺少必要的有限自主性。
