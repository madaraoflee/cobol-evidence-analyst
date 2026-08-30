# 2026 RAG、向量与图框架选型报告

> 核心判断：本项目应采用“多路混合召回 → 重排序 → 确定性代码图扩展 → 证据核验”的 Code RAG，而不是普通向量 RAG，也不是让 LLM 从源码自动抽取知识图谱的通用 GraphRAG。

状态：`Target options report — POC uses company API plus SQLite only`  
调研日期：2026-08-28  
当前轻量实现见：[可演示 POC](../09-demonstrable-poc.md)  
适用范围：目标能力选项研究；当前公司环境只允许公司 API Key，不允许在工作电脑自托管模型或额外服务

约束更新（2026-08-29）：本报告中的 Qwen、BGE-M3、Qdrant、Neo4j 和 LanceDB 仅保留为历史候选，不是当前 POC 依赖。当前实施以 SQLite + 公司 API 为准。

## 1. 选型原则

“最新”只代表候选能力更丰富，不代表可以绕过验证。本项目按四个不可交换的职责选技术：

| 职责 | 必须解决的问题 | 选型原则 |
| --- | --- | --- |
| 候选召回 | 用户说业务语言、源码使用缩写时，先找到可能的入口 | 精确符号、稀疏词汇和稠密语义并行，不押注单一向量 |
| 关系证明 | 证明调用、条件、字段流、参数和外部数据关系 | 关系来自解析器与静态分析，不来自 LLM 猜测 |
| 调查编排 | 在预算内决定下一步查什么，并能重放和中断 | 有限状态、结构化工具输入输出、可持久化轨迹 |
| 回答生成 | 把最小证据闭包翻译成业务说明 | 只读取证据包，逐条核验，证据不足时拒答 |

## 2. 当前框架比较

| 候选 | 当前能力判断 | 本项目决定 |
| --- | --- | --- |
| [**Qdrant**](https://qdrant.tech/documentation/search/hybrid-queries/) | 原生支持命名稠密/稀疏向量、Query API、多阶段 prefetch、RRF/DBSF 融合及 ColBERT 式 multivector | **选为目标检索引擎**；最符合多路召回和后续 late-interaction 试验 |
| [**Neo4j + neo4j-graphrag**](https://neo4j.com/docs/neo4j-graphrag-python/current/user_guide_rag.html) | 属性图适合多跳代码关系；VectorCypherRetriever 可把相似入口与 Cypher 扩展连接，也可接外部 Qdrant | **选为目标代码图引擎**；只使用检索适配能力，不使用 LLM 知识图谱抽取代码事实 |
| [**LangGraph**](https://docs.langchain.com/oss/python/langgraph/overview) | 低层、可持久化的有状态 Agent 运行时，支持 durable execution、人工中断和流式轨迹 | **选为调查状态机运行时**；业务状态、工具契约和证据模型保持框架无关 |
| [**LanceDB**](https://docs.lancedb.com/search/hybrid-search) | 嵌入式部署简单，支持向量、全文、混合检索和重排序 | 保留为单机轻量剖面，不作为目标架构 |
| [**pgvector**](https://github.com/pgvector/pgvector) | 可在 PostgreSQL 内结合 HNSW/IVFFlat、全文检索、RRF 和外部重排序 | 若公司只批准 PostgreSQL，则作为首选降级方案 |
| [**Weaviate**](https://docs.weaviate.io/weaviate/concepts/search/hybrid-search) | 原生 BM25 + 向量混合检索和融合策略 | 能力足够，但相对当前任务没有胜过 Qdrant 的决定性收益 |
| [**Milvus**](https://milvus.io/docs/multi-vector-search.md) | 强大的多向量混合检索与大规模部署能力 | 单代码库阶段运维成本过高，暂不采用 |
| [**Microsoft GraphRAG**](https://microsoft.github.io/graphrag/index/overview/) | 面向非结构化文本，用 LLM 抽取实体、关系、声明和社区摘要，支持 local/global/DRIFT 查询 | 不用于代码事实层；未来可试验高层业务主题总结 |
| **LlamaIndex / Haystack / LangChain** | 提供大量 RAG 组件、连接器和通用抽象 | 可按需使用局部组件，不成为产品架构边界或事实模型 |

## 3. 推荐目标栈

| 层 | 目标技术 | 责任边界 |
| --- | --- | --- |
| 产品形态 | Python 3.12+ 模块化单体 | 一个部署单元，内部模块清晰；不是微服务集合 |
| 调查编排 | LangGraph | 承载受限状态机、检查点、人工中断和运行轨迹 |
| 混合检索 | Qdrant，自托管 | 保存结构化代码单元的稀疏、稠密及可选 multivector；执行多阶段融合 |
| 代码事实图 | Neo4j Community，自托管 | 保存版本化 Program、Paragraph、Statement、Field 及类型化关系，执行 Cypher 路径查询和程序切片 |
| 源码与 IR | 本地只读快照 + 版本化 IR 文件/元数据 | 作为可重建事实源；Qdrant 和 Neo4j 都是派生索引，不是唯一真相 |
| 稠密向量 | Qwen3-Embedding-0.6B 基线 | 中文、英文、代码之间的语义入口映射；4B 只在基准收益足够时升级 |
| 重排序 | Qwen3-Reranker-0.6B 基线 | 对融合后的少量候选做 query-document 相关性重排 |
| 挑战模型 | BGE-M3 | 验证单模型 dense + sparse + multivector 是否优于基线 |
| 回答模型 | 公司批准的本地或企业 LLM Gateway | 只接收最小证据包；模型可替换 |

这不是把产品绑定到三个框架。统一 IR、`entity_id`、`snapshot_id`、Evidence Package 和 Agent Tool Contract 才是架构边界；LangGraph、Qdrant、Neo4j 都位于适配器之后。

## 4. 在线检索主路径

1. **理解问题**：识别问题类型、业务概念、可能的源码符号、时间/产品范围，并生成最多三个互补查询；不让模型生成代码事实。
2. **锚定与并行召回**：问题中显式出现的程序、Paragraph 或字段名先由 Neo4j 精确验证，命中后成为锚定种子；其余业务词、别名和描述进入 Qdrant 稀疏词汇与 Qwen3 稠密向量并行召回。代码单元按 Program、Paragraph、字段定义、SQL 块和有边界的语句窗口建立，不做任意 token 切块。
3. **秩融合**：Qdrant 内默认用 RRF 合并稀疏与稠密结果，保留每条候选来自哪个通道及命中原因；不直接相加 BM25 和余弦分数。
4. **重排序与种子合并**：用 Qwen3-Reranker 对前 30–50 个非锚定候选重排，应用层再与锚定种子合并，保证已验证精确符号不被语义分数压低，最终保留前 5–10 个图调查种子。具体数字由私有问题集校准。
5. **图引导扩展**：以种子 `entity_id` 进入 Neo4j，只沿当前问题需要的 `CALLS/PERFORMS/READS/WRITES/FLOWS_TO/PASSES_AS` 等关系扩展，并执行反向数据切片或控制依赖提取。
6. **证据预算**：按“答案覆盖增益 / token 成本”选择证据，防止 lost-in-the-middle；超预算时压缩重复上下文，而不是截断关键路径。
7. **生成与双层核验**：回答模型只解释证据包；先确定性检查快照、哈希、实体、关系路径和引用完整性，再检查每条 claim 是否被证据支持或扩大了适用范围，最终输出代码事实、业务推断或待确认项。

最新代码检索研究支持这条路径：DraCo 表明私有仓库检索需要数据流上下文；CodeRAG 指出单一路径查询和缺少重排序会限制仓库级检索；RepoDistill 进一步说明图检索之后仍需做上下文预算与压缩。这些研究主要面向代码补全或通用代码任务，不直接证明 COBOL 业务问答效果，因此只用于形成架构假设，最终仍以公司内金标准问题为准。

## 5. 两个存储如何保持一致

同一 IR 实体在两个派生索引中共享不可变键：

| 字段 | 用途 |
| --- | --- |
| `snapshot_id` | 隔离不同源码版本，禁止跨版本拼接证据 |
| `entity_id` | 在 Qdrant 候选与 Neo4j 节点之间无歧义跳转 |
| `file_hash` / `source_range` | 回到可复核的原始源码 |
| `entity_type` / `dialect` | 检索过滤和方言诊断 |
| `derivation` / `relation_status` | 区分语法事实、静态推导、候选关系、未解析边界和人工确认 |

索引构建采用“先固定版本化原始源码，再生成统一 IR，最后幂等写入两个存储”的顺序。原始源码是最终证据，IR 是规范化派生事实，Qdrant 和 Neo4j 是查询投影。任何一侧失败或与 IR 哈希不一致，快照状态保持 `INCOMPLETE`，不能用于正式回答。

## 6. 为什么不直接采用通用 GraphRAG

Microsoft GraphRAG 的核心流程是让 LLM 从非结构化文本中抽取实体、关系和声明，再构建社区摘要。这适合长文档中的主题和全局问题，但 COBOL 的 `PERFORM THRU`、动态 `CALL`、字段覆盖、COPY 展开和跨程序参数映射不能靠语言模型抽取来定案。

本项目使用的是“graph-guided RAG”，不是“LLM-extracted graph RAG”：图的边来自解析器和静态分析，LLM 只选择查询方向并解释已证明的路径。未来到了 M7，可以把通用 GraphRAG 作为业务主题发现的实验通道，但其结果只能是业务推断。

## 7. 模型策略

Qwen3 Embedding/Reranker 系列兼顾多语言、长文本与代码检索，适合作为中文业务问题到英文/缩写 COBOL 的当前基线。选择 0.6B 而不是直接使用 8B，是为了让公司内 CPU/小型 GPU 环境能够建立可复现基准；只有 4B 在 Recall、MRR 或 nDCG 上的增益超过资源成本，才升级默认模型。

BGE-M3 同时提供 dense、sparse 和 ColBERT-style multivector 表示，适合作为结构性挑战者。late interaction 只对小候选集做第二阶段重排，不对整个仓库直接 MaxSim 搜索；否则存储和内存成本会过早放大。

## 8. 必须通过的基准

技术选型在 M3 前仍是 `Proposed`。用 20–30 个公司内金标准问题做以下消融：

| 实验 | 要回答的问题 |
| --- | --- |
| 精确符号 | 代码自身命名能覆盖多少问题 |
| 符号 + 稀疏 | 词汇匹配带来多少 Recall@20 增益 |
| 再加 Qwen3 dense | 中文业务表述是否稳定找到命名不同的代码 |
| 再加 Qwen3 reranker | MRR/nDCG 与 Top-5 命中是否改善 |
| Qwen3 对比 BGE-M3 | 多向量表示是否值得额外成本 |
| 检索对比检索 + 代码图 | 完整证据路径命中率是否显著提高 |
| 无/有证据预算 | 答案证据覆盖、噪声和延迟如何变化 |

同时记录索引时间、磁盘、峰值内存、p50/p95 查询延迟和失败模式。若 Qdrant + Neo4j 的收益不能超过双存储成本，则降级到 LanceDB 或 PostgreSQL/pgvector 适配器，而不是维护“看起来先进”的架构。

## 9. 私有部署边界

Qdrant、Neo4j、向量模型和 Agent 都必须部署在公司批准环境；源码、向量、索引、Prompt、轨迹和缓存都按源码敏感数据处理。Qdrant 自托管默认部署不能直接暴露网络，必须配置认证、TLS/网络隔离和最小权限。任何外部 LLM 调用都由 Gateway 做策略检查和最小证据裁剪。

## 10. 主要资料

- [Qdrant：Hybrid and Multi-Stage Queries](https://qdrant.tech/documentation/search/hybrid-queries/)
- [Qdrant：Multivector Representations](https://qdrant.tech/documentation/tutorials-search-engineering/using-multivector-representations/)
- [Qdrant：Security](https://qdrant.tech/documentation/tutorials-operations/secure-qdrant/)
- [Neo4j GraphRAG for Python](https://neo4j.com/docs/neo4j-graphrag-python/current/)
- [Neo4j：VectorCypherRetriever](https://neo4j.com/docs/neo4j-graphrag-python/current/user_guide_rag.html)
- [LangGraph Overview](https://docs.langchain.com/oss/python/langgraph/overview)
- [Qwen3 Embedding Technical Report](https://arxiv.org/abs/2506.05176)
- [Qwen3 Embedding official repository](https://github.com/QwenLM/Qwen3-Embedding)
- [BGE-M3 official documentation](https://github.com/FlagOpen/FlagEmbedding/blob/master/docs/source/bge/bge_m3.rst)
- [Microsoft GraphRAG Overview](https://microsoft.github.io/graphrag/index/overview/)
- [DraCo: Dataflow-Guided Retrieval Augmentation for Repository-Level Code Completion](https://aclanthology.org/2024.acl-long.431/)
- [CodeRAG: Finding Relevant and Necessary Knowledge for Retrieval-Augmented Repository-Level Code Completion](https://aclanthology.org/2025.emnlp-main.1187/)
- [RepoDistill: Distilling Repository Knowledge through Compression-Aware Budget Allocation and Policy Optimization](https://aclanthology.org/2026.findings-acl.217/)
