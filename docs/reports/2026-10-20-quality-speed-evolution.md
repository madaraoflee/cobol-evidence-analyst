# 质量与速度进化：先消除供证损失和重复分析

当前系统采用 Python、SQLite FTS5/BM25、CALL/COPY 关系、自有调查循环与检查点，没有接入 LangChain、LangGraph 或 LlamaIndex 运行时。判断技术是否值得采用，应看它能否补上可测量的缺口。本轮的具体缺口是：正确源码页已命中，但关键行在裁剪时丢失；相同 COPY 源码在同一问题中反复做词法分析；精确标识符判断反复构造整页词元列表。修复这些环节能够直接改善供证与本地耗时，整体更换框架暂时没有相应收益证据。

## 最新框架核对及取舍

以下为 2026-10-20 核对的官方主包稳定发布；日期按北京时间。各项目版本号不可横向比较，CLI、子包和主包也不是同一版本序列。

| 项目 | 核对版本 | 适合借鉴的能力与当前决定 |
| --- | --- | --- |
| [LangChain](https://github.com/langchain-ai/langchain/releases/tag/langchain==1.4.6) | 1.4.6，10 月 15 日 | [统一模型、工具与 Agent 中间件](https://docs.langchain.com/oss/python/langchain/overview)。现有模型接口已经可用，本轮无需迁移。 |
| [LangGraph](https://github.com/langchain-ai/langgraph/releases/tag/1.2.16) | 1.2.16，10 月 17 日 | [状态图、节点缓存、执行与恢复](https://docs.langchain.com/oss/python/langgraph/use-graph-api)。借鉴按确定输入复用工作；未来调度复杂度显著增加时，再评估迁移调查调度层。 |
| [Deep Agents](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.28) | 0.7.28，10 月 17 日 | [规划、子代理、文件上下文与技能](https://docs.langchain.com/oss/python/deepagents/overview)。不能直接补齐 COBOL 语义；多代理还可能增加调用和上下文成本。 |
| [LlamaIndex](https://github.com/run-llama/llama_index/releases/tag/v0.14.25) | 0.14.25，9 月 22 日 | [输入节点及转换组合的哈希缓存](https://developers.llamaindex.ai/python/framework/module_guides/loading/ingestion_pipeline/)。本轮借鉴该模式，缓存不可变源码的词法结果。 |
| [Haystack](https://github.com/deepset-ai/haystack/releases/tag/v3.4.0) | 3.4.0，10 月 15 日 | 多路检索与上下文组织；[3.3 的窗口读取优化](https://haystack.deepset.ai/release-notes/3.3.0)说明减少重复访问也有价值。本轮延续分路检索，修复线索在最终裁剪阶段丢失。 |
| [DSPy](https://github.com/stanfordnlp/dspy/releases/tag/3.4.1) | 3.4.1，10 月 13 日 | [GEPA](https://github.com/stanfordnlp/dspy/blob/main/docs/docs/api/optimizers/GEPA/overview.md)可用于评测驱动的提示优化。需要有代表性的保留题与模型调用预算，本轮不引入。 |

以上是设计借鉴，没有复制第三方实现或增加运行依赖。LangChain、LangGraph、Deep Agents、LlamaIndex、DSPy 的仓库采用 MIT，Haystack 采用 Apache-2.0；若以后直接引入实现，仍需固定版本并保留对应许可。

系统已经具有内容哈希、解析器/IR 版本和编译上下文约束的语义缓存（`poc/semantic_scope.py`），持久检查点与依赖失效（`poc/run_store.py`），隔离解析器适配与 IR 对照（`poc/parser_adapter.py`），以及召回、最终装载分开计分的评测（`poc/retrieval_quality_eval.py`）。这些能力应继续增强，无需重复建设。当前 embedding 相关代码属于可选接口能力探测，不等于已经启用向量检索。

## 已实现的改动

### 保留触发检索的线索，避免裁掉关键源码

`poc/repository_discovery.py` 现在把候选所属检索通道的词语和完整标识符带到裁剪阶段。选择中心行时，完整标识符优先，其次是当前通道线索，最后才是原问题的其他词。显式行号读取仍按指定范围取原文；源码哈希、行号和证据身份核验继续执行。

一个可复现的反例是：新线索出现在第 4 行，计算公式在第 5 行，而原问题的多个普通词集中在第 256 行。旧逻辑会在 512 字符预算下截取后者，虽然找到正确文件，实际请求仍缺计算公式。新增三种对照分别覆盖完整字段名、普通英文线索、较长标识符中的子串干扰；修复后三例的原文行段召回及最终请求材料充分性均从 0 变为 1，原有三例保持为 1。

这是六个开发反例的离线检查，不是独立业务题的正确率。脚本实际运行生产检索，再经过 `production_loader` 的请求预算与序列化路径；没有生成或评分模型回答。目标行段、源码哈希及前后指标见[裁剪质量对照](2026-10-20-retrieval-focus.json)。

### 降低精确标识符扫描开销

同一模块先快速排除不可能含完整标识符的页面，再逐个检查词元并在命中时停止，避免每次生成整页词元列表。最终仍验证完整词元边界，大小写和句末点语义保持一致。候选数量、SQL 排名与最终预算没有为加速而缩减。

### 复用不可变 COPY 词法结果

`poc/procedure_expansion.py` 和 `poc/source_session.py` 在单个问题会话中增加有界 LRU 缓存，最多 64 条、按保留对象大小估算最多 16 MiB。缓存身份包含相对路径、捕获内容哈希、实际编码、词法版本及完整编译上下文指纹。

缓存只保存冻结的物理源码分析结果。每次 COPY 的宿主、包含链、替换轨迹和条件处理仍单独生成，因此不同程序的 `COPY REPLACING` 不会共享展开含义。源码或编译上下文变化不会复用旧词法结果；失败及带词法问题的源码不入缓存。命中仍检查取消，关闭会话即释放；只有捕获能力的旧来源接口继续使用原路径。此缓存不跨问题持久保存，也不缓存业务答案。

## 验证方法

集成基线为 `7bfad65a11574e86831d73f520a241330aa8089b`，已包含另一条进化线公开提交的 `1b699e3`。检索基线直接冻结该提交中的模块，其余模块相同；COPY 性能对照在同一实现中关闭或启用词法缓存，各自建立独立语义侧库。

48 个相关模块的 523 项源码测试通过，网络连接被替身阻断，三个产品文件的测试前后哈希相同，原始记录见[回归记录](2026-10-20-quality-speed-tests.json)。缓存专用测试随后补足到 10 项，覆盖共享 COPY 的宿主替换隔离、编译条件、身份变化、取消、失败、缓存上限、关闭清理与真实语义缓存复用。最终由独立审查代理禁用网络复跑裁剪、缓存与原自适应检索共 27 项，全部通过；另有 8 项 Windows 工作流源码检查通过，两个工作流继续只有手动入口。专用测试与广回归有重叠，不将次数相加冒充独立用例数。

检索计时与 COPY 计时分别串行执行，避免本任务内的性能进程互相竞争。计时来自本机合成源码，不含模型响应、办公室机器或 Windows EXE；“冷”指清除对应 Python 缓存或新建语义侧库，不表示清空操作系统磁盘缓存。

三个重复样本的暖运行中位数如下，检索耗时包含轻量观测钩子：

| 场景 | 基线 / 关闭词法缓存 | 修复 / 开启词法缓存 | 对照范围 |
| --- | ---: | ---: | --- |
| 24 文件、76 页，多线索检索 | 35.43 ms | 16.04 ms | 2.21 倍，完整返回结果一致 |
| 160 文件、1,476 页，多线索检索 | 493.30 ms | 72.66 ms | 6.79 倍，完整返回结果一致 |
| 单程序引用 2,000 行 COPY，重复语义分析 | 77.10 ms | 22.38 ms | 词法调用 2,010 → 0 |
| 四程序共享 2,000 行 COPY，重复语义分析 | 303.46 ms | 86.14 ms | 词法调用 8,040 → 0 |

检索共比较两种规模、五种场景的完整返回对象、原文、哈希、行号及证据 ID，十组均一致。明确改变裁剪结果的质量反例单列，不混入等价速度比较。其他场景耗时接近；完整标识符单查询的小/中语料中位数分别为 5.49 → 5.86 ms、10.97 → 11.64 ms，因此不能宣称所有问题都更快。细节见[检索速度记录](2026-10-20-retrieval-speed.json)。

COPY 冷运行的单程序中位数为 1,604.46 → 1,601.17 ms，基本相当；四程序为 4,430.78 → 4,221.88 ms，词法调用从 8,040 降到 2,040。两种模式的派生内容、源码映射、IR 指纹、范围前沿一致，缓存估算保留约 0.45 MB，见[COPY 缓存记录](2026-10-20-copy-source-cache.json)。这些收益只适用于本地对应步骤，不能相乘或外推为整次问答提速。

复跑命令：

```bash
python3 -m unittest poc.tests.test_retrieval_focus poc.tests.test_adaptive_retrieval poc.tests.test_expansion_source_cache -v
git show 7bfad65:poc/repository_discovery.py > /tmp/repository_discovery_before.py
python3 tools/evaluate_retrieval_routes.py --module /tmp/repository_discovery_before.py --output /tmp/retrieval-before.json
python3 tools/evaluate_retrieval_routes.py --output /tmp/retrieval-after.json
python3 tools/benchmark_retrieval_routes.py --baseline-ref 7bfad65 --output /tmp/retrieval-speed.json
python3 tools/benchmark_copy_source_cache.py --repeats 3 --output /tmp/copy-cache.json
```

## 下一步应如何进化

下一轮质量投入应先扩展有人工判定的保留题，覆盖中文问题与英文源码词不重合、同名歧义、跨程序返回、条件优先级及早退；以关键证据是否成套进入请求、错误证据比例和最终解释是否受原文支持为判断依据。只有这些对照显示词面召回仍是主要缺口，再增设可选向量或重排通道。向量相似度不能证明业务因果。

随后用已有隔离解析器接口扩大原生前端影子对照，逐项核验 `REDEFINES`、`OCCURS`、动态 CALL、COPY REPLACING 和 SQL/CICS 的来源映射与未知边界。性能方面可继续减少检查点全量历史读取、批量读取关联证据，并测量各步骤耗时后再并行独立任务。此次没有触及另一条开发线正在修改的问答与回答生成模块。

如果以后大量并行调查分支、人工中断和多进程恢复使自有调度器成为主要维护成本，再评估用 LangGraph 承担调度层；源码索引、解析、证据身份和评测继续保留。

## 并行交付

改动位于独立工作区与 `codex/adaptive-evidence-retrieval` 分支，通过现有 [PR #11](https://github.com/madaraoflee/cobol-evidence-analyst/pull/11) 交付。只把另一条开发线已经发布到远端主分支的修改合入隔离分支，未编辑主工作区。按本会话 `AGENTS.md` 的当前阶段要求，仅发布源码，未构建应用、安装包、验收可执行文件或 release，未触发 Windows 工作流。
