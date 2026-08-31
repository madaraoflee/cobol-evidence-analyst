# COBOL Evidence Analyst

核心判断：本项目首先要做成一个“以源码证据为底座的 COBOL 业务逆向分析 Agent”，而不是普通代码聊天机器人，也不是一步到位的自动化 SDLC 平台。

当前主线已经切换到 **可演示 POC**：先让一台 Windows 电脑能够读取一个源码文件夹，由单个受控 Agent 调查代码并返回带行号证据的业务回答。大模型只允许通过公司 API Key 调用，不安装 Ollama 或任何本地模型运行时。完整 IR、图服务和企业级核验契约保留为验证成功后的目标架构，不进入首版实现。

## 项目文档

- [总设计说明书](./DESIGN.md)
- [领域语言](./CONTEXT.md)
- [产品章程](./docs/00-product-charter.md)
- [架构基线](./docs/01-architecture-baseline.md)
- [路线图与里程碑](./docs/02-roadmap-and-milestones.md)
- [评估策略](./docs/03-evaluation-strategy.md)
- [Agent 框架：混合 Code RAG](./docs/04-agent-framework.md)
- [架构设计与实施流程](./docs/05-design-process.md)
- [问题分类与调查策略矩阵](./docs/06-question-investigation-matrix.md)
- [统一 IR 与关系 Schema](./docs/07-unified-ir-and-relations.md)
- [Agent 调查工具契约](./docs/08-agent-tool-contracts.md)
- [可演示 POC：Windows 文件夹到业务回答](./docs/09-demonstrable-poc.md)
- [复杂 COBOL Agent 可行性复核](./docs/10-poc-feasibility-assessment.md)
- [交互式领导演示 UI](./docs/leadership-demo/prototype.html)
- [领导演示包说明与三态截图](./docs/leadership-demo/README.md)
- [CALC-01 P1-B 可执行演示结果](./docs/leadership-demo/p1b-executable-demo.md)
- [2026-08-30 P1-B 项目进度报告](./docs/reports/2026-08-30-p1b-progress-report.md)
- [2026-08-31 P3-A 项目进度报告](./docs/reports/2026-08-31-p3a-progress-report.md)
- [P1-B 领导审阅 Word 报告](./docs/reports/COBOL-Agent-P1B-Progress-Report.docx)
- [POC 实现：离线代码库画像工具](./poc/README.md)
- [2026 RAG、向量与图框架选型报告](./docs/research/2026-rag-vector-framework-review.md)
- [M0 架构复检记录](./docs/reviews/2026-08-28-m0-architecture-recheck.md)
- [解析器验证计划](./docs/research/parser-bakeoff-plan.md)
- [Skill 选型记录](./docs/research/skill-selection.md)
- [POC 开发与评测 fixture：合成保险试点](./docs/m1/README.md)
- [ADR-0001：证据优先的只读 MVP](./docs/adr/0001-evidence-first-read-only-mvp.md)
- [ADR-0002：解析器中立的类型化多图](./docs/adr/0002-parser-neutral-typed-multigraph.md)
- [ADR-0003：先采用模块化单体](./docs/adr/0003-modular-monolith-first.md)
- [ADR-0004：通用内核与私有企业知识分离](./docs/adr/0004-separate-generic-core-and-private-enterprise-knowledge.md)
- [ADR-0005：采用混合 Code RAG](./docs/adr/0005-hybrid-code-rag.md)
- [ADR-0006：LangGraph + Qdrant + Neo4j 目标实现栈](./docs/adr/0006-adopt-langgraph-qdrant-neo4j-target-stack.md)
- [ADR-0007：Agent 使用受约束领域工具](./docs/adr/0007-use-bounded-domain-tools.md)

## 当前状态与下一步

P3-A 的可运行骨架已完成：公司 OpenAI-compatible API 能力探测会验证 Chat、Tool Calling 完整回传、严格 JSON 和可选 Embedding；Agent 只能调用四个只读工具，最多 6 次，并强制快照、Evidence ID 范围、Hash、引用和结果包契约。CALC-01 已在 4 次真实工具调用内完成离线端到端闭环，两类超出源码快照的问题会明确拒答；当前 70 项自动测试全部通过。

下一步不是扩张工具，而是在公司批准环境对真实 API 执行 capability probe 和 CALC-01 验收，然后增加独立的 Claim 语义支持核验。在该核验完成前，系统即使通过引用完整性与词面锚定，也只返回 `PARTIAL`，不把模型的自然语言解释写成“已完全证明”。真实公司 API 尚未在本项目环境中调用。
