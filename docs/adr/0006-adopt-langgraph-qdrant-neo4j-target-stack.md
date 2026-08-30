---
status: proposed
date: 2026-08-28
---

# ADR-0006：采用 LangGraph、Qdrant 与 Neo4j 作为目标实现栈

## Context

ADR-0005 已确定产品采用混合 Code RAG，但尚未回答三个实现问题：如何编排可恢复的调查流程、如何组合词汇与语义召回、如何执行可证明的多跳代码关系查询。

当前约束是单个私有 COBOL/IBM i 代码库、公司内自托管、只读分析和强证据要求。用户问题以中文业务语言表达，源码主要是英文缩写；一个答案又可能跨越 `PERFORM/CALL`、字段读写、控制条件、COPYBOOK、SQL 和文件。因此单一全文索引、单一向量库或由 LLM 抽取的知识图谱都不足以同时解决入口发现和关系证明。

最新官方能力和代码检索研究显示，多路召回、重排序、数据流/图引导扩展及上下文预算应成为同一条管道；但双存储会增加部署、一致性和资源成本，所以决定必须先保持可逆并接受基准验证。

## Decision

采用以下目标实现：

- 使用 **LangGraph** 承载一个显式、有限、可检查点恢复的调查状态机。项目自己的 `InvestigationState`、工具契约、预算和停止条件是架构边界。
- 使用自托管 **Qdrant** 保存结构化代码单元的稀疏、稠密和可选 multivector，执行并行召回、多阶段 prefetch 与 RRF 融合。
- 使用自托管 **Neo4j Community** 保存精确符号和类型化代码事实图，执行调用、控制、数据流、参数、SQL 和文件路径查询。
- 使用 **Qwen3-Embedding-0.6B** 与 **Qwen3-Reranker-0.6B** 作为首个本地基线；使用 **BGE-M3** 作为 dense/sparse/multivector 挑战者。
- 代码图只能由解析器、静态分析、方言规则或人工确认生成。不得使用通用 GraphRAG 的 LLM 抽取器创建确定性的 `CALLS`、`FLOWS_TO`、`WRITES` 等边。
- Qdrant 与 Neo4j 都是从版本化 IR 生成的派生索引，通过 `snapshot_id + entity_id` 连接。任一索引构建失败时，快照不能进入正式回答路径。
- 在 M3 前完成检索质量、完整证据路径、延迟、内存、磁盘和运维复杂度消融；通过前本 ADR 保持 `Proposed`。

## Consequences

### Positive

- 稀疏、稠密、重排序和可选 late interaction 可以独立消融，不再把“向量是否有用”当成笼统问题。
- 检索相关性与代码关系证明由不同引擎负责，向量相似度不会被误当成执行或数据流事实。
- LangGraph 提供恢复、人工中断和运行轨迹，同时不迫使事实模型依赖 Agent 框架。
- 所有组件可在公司批准环境自托管，源码和索引不需要进入公共云服务。

### Negative

- Qdrant 与 Neo4j 增加两个运行时、备份、安全配置、版本升级和观测面。
- 同一实体必须跨索引保持键和快照一致，需要幂等构建、完整性检查和失效传播。
- Qwen3/BGE-M3 的真实 COBOL 与公司术语效果未知，必须建立私有金标准集。

### Neutral

- 产品仍是模块化单体；两个数据引擎不意味着拆成微服务。
- `neo4j-graphrag` 可以作为查询适配器使用，但不拥有图模型和事实生成流程。
- 后续更换 Qdrant、Neo4j 或 LangGraph，不应改变 IR、工具契约、证据包和评测语义。

## Alternatives Considered

**SQLite FTS5 + 应用内图遍历**

部署最简单，适合极小原型；但会推迟验证多阶段混合检索和复杂多跳查询，迁移成本会落在已经形成的产品接口上，因此不再作为目标实现。

**LanceDB**

嵌入式、混合检索和重排序体验良好，适合个人电脑轻量剖面；但仍需单独解决类型化图查询，故保留为降级适配器。

**PostgreSQL + pgvector**

是公司只批准一种数据库时最稳妥的方案，能够结合全文检索和向量索引；但多向量、多阶段检索和图遍历都需要更多应用层实现，暂列第二选择。

**Weaviate 或 Milvus**

两者都支持现代混合/多向量检索，但对当前单代码库没有足以抵消平台和运维复杂度的优势。

**Microsoft GraphRAG 或 LlamaIndex PropertyGraphIndex**

适合从非结构化文档抽取主题关系，但 LLM 抽取不能作为 COBOL 控制流、调用和字段流的确定性来源。未来只在 M7 业务主题发现中作为实验能力。

**完整 LangChain/LlamaIndex 应用框架**

连接器丰富，但通用抽象容易侵入状态、文档节点和事实模型。当前只按需使用独立组件。

## References

- [2026 RAG、向量与图框架选型报告](../research/2026-rag-vector-framework-review.md)
- [Qdrant Hybrid and Multi-Stage Queries](https://qdrant.tech/documentation/search/hybrid-queries/)
- [Neo4j GraphRAG for Python](https://neo4j.com/docs/neo4j-graphrag-python/current/)
- [LangGraph Overview](https://docs.langchain.com/oss/python/langgraph/overview)
- [Qwen3 Embedding](https://github.com/QwenLM/Qwen3-Embedding)
- [BGE-M3](https://github.com/FlagOpen/FlagEmbedding/blob/master/docs/source/bge/bge_m3.rst)
- [DraCo](https://aclanthology.org/2024.acl-long.431/)
- [CodeRAG](https://aclanthology.org/2025.emnlp-main.1187/)
- [RepoDistill](https://aclanthology.org/2026.findings-acl.217/)
