# 交互延迟：成熟项目参考与本仓库落点

本轮判断是先隔离导入、调查和远程等待的成本，再让每道问题按实际证据缺口推进。大型项目提供的是可验证的机制参考；本项目继续使用 Python 模块化单体、SQLite 索引和自己的 COBOL 语义、来源捕获及结论核验。

研究日期：2026-10-14。下面只引用官方文档及原仓库源码，代码引用固定到实际读取的提交。

| 参考机制 | 原始依据 | 本仓库的适配与边界 |
| --- | --- | --- |
| 查询时间预算与输出数量预算分开 | [Zoekt `SearchOptions`，固定提交 `27517a30`](https://github.com/sourcegraph/zoekt/blob/27517a30ae6df16c6e768623847fdab8f6a3bbcf/api.go#L960-L1015) 同时定义实际搜索的匹配数、墙钟时间限制，以及排序后的展示限制。[Sourcegraph 搜索配置](https://sourcegraph.com/docs/admin/search) 说明索引与搜索、按需磁盘页访问及资源约束。 | 保留 FTS5/BM25；索引迁移必须由更新操作发起。交互预算必须覆盖实际计算与网络等待，不能把最终答案截短当作计算变快。暂不引入分片服务或新的搜索引擎。 |
| 问题相关排序、缓存、预算内上下文 | [aider 仓库地图说明](https://aider.chat/docs/repomap.html)；[固定提交 `5dc9490b` 的 `get_ranked_tags_map`](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py#L576-L706) 缓存地图、记录构建耗时并按 token 预算选择可呈现范围。 | `business_map` 负责定位，`EvidenceContext` 负责供应。问题实体、来源版本和义务决定保留哪些材料；避免同一段源码以全文、节录、原始 IR 多次供应。导航摘要不能代替核验所需源码。用户明确指定单程序时，现有全文供应能力继续保留。 |
| 先局部流，再由具体起点/终点约束跨过程流 | [CodeQL 数据流说明](https://codeql.github.com/docs/writing-codeql-queries/about-data-flow-analysis/)；[官方 JavaScript/TypeScript 数据流指南](https://codeql.github.com/docs/codeql-language-guides/analyzing-data-flow-in-javascript-and-typescript/) 明确全局流成本较高，应约束 sources/sinks。 | `FactStore` 已能按程序读取，`SemanticService` 已有切片和摘要。应优先复用这些事实，围绕提问字段与实际输出构建流；跨调用只扩大到未解决义务。保守未知效果、分支条件和后续覆盖不能为了缩小图而消失。没有把 CodeQL 本身作为 COBOL 解析器引入。 |
| 步骤超时、迭代上限、持久恢复分别建模 | [LangGraph 固定提交 `ccb851ef`：步骤超时与检查点](https://github.com/langchain-ai/langgraph/blob/ccb851effb7b4c6255add86dbeed577061475aff/libs/langgraph/langgraph/pregel/main.py#L737-L754)，[实际调度与迭代边界](https://github.com/langchain-ai/langgraph/blob/ccb851effb7b4c6255add86dbeed577061475aff/libs/langgraph/langgraph/pregel/main.py#L3048-L3102)；[官方持久化说明](https://docs.langchain.com/oss/python/langgraph/persistence)。 | 继续扩展已有 `AgentPolicy`、`RunStore` 和默认 `business_chat`。整个问题、单个步骤和模型次数是不同预算。超时应保留已验证材料与待执行动作；远程请求状态不明时不能宣称已取消，也不能自动重复收费请求。本轮不以接入 LangGraph 为前提。 |

## 已复现的隐性索引重建

基线为 `b8b63899080e2fe52f18c24159da21254b5f11c0`。`indexed_chat.try_indexed_question` 遇到解析器版本或源码选项不匹配时返回 `None`，`analyze_source` 随后进入普通导入，调用 `build_business_index`。因此，升级后用户只是问一个问题，也可能在等待整个源码库重建。检索 schema 不兼容也存在相同回退。

回归先建立正常业务索引，再把其 `parser_version` 改为旧版本，最后沿默认 `analyze_source(..., analysis_mode="business", reading_strategy="retrieval")` 发起问题。修复前确实调用了全库构建；测试失败于 `Unexpected full rebuild`。这证明一个能解释长等待的机制，尚不能证明用户现场只发生了这一种情况。

修复后，同一输入返回 `SOURCE_INDEX_UPDATE_REQUIRED`，分别记录解析器、索引类型、源码选项、编译上下文或检索版本需要更新的原因，明确引导“更新源码”。本次问答不扫描源码、不发模型请求、不改变 SQLite 内容，也不把上次答案显示为本次答案。主动更新完成后继续复用新索引。没有索引时的首次导入、另选源码目录和选取尚未索引的入口仍遵循原有入口语义。

`test_index_upgrade_question_guard.py` 同时覆盖 COPY 库顺序、条件编译、框架版本和移除显式编译配置。旧版行为会忽略“显式配置被移除”的变化，新检查将其作为编译身份变化处理，避免伪装成可复用索引。

## 截止时间与恢复的独立审阅

新增 `test_question_deadline_recovery_audit.py`，在真实默认链上使用进程内 fake transport 和可控时钟，复现并修复了以下边界：远程请求超时后同问题再次进入时自动重发；预算或接口超时设置改变后绕过未知请求保护；响应已落盘但时间预算到达时丢弃正文；工具执行中截止却把调查标为完成；已记录响应重放时遗漏原请求的 trace；下一次请求尚未发送便把错误写到上一个已完成请求；trace 已保存但最终结果尚未写入时中断导致未知请求被藏在完成状态。

另验证接口 `timeout_seconds=120/300` 不会在首轮直接禁用中文问题的源码词项补查。接口超时是上限，不能等同于模型通常需要的合成时间。假模型返回真实证据 ID 及错误的 `IN-AMOUNT * 999` 时，即使已到调查截止时间，已收到的正文仍进入 ClaimLedger 并修订为源码实际的 `IN-AMOUNT * 2`。

本轮定向验证命令：

```sh
python3 -m unittest poc.tests.test_question_deadline_recovery_audit poc.tests.test_quality_trace poc.tests.test_run_store poc.tests.test_index_upgrade_question_guard poc.tests.test_request_fit_latency
```

结果：36 项通过，1.949 秒（本机离线运行）。另一次入口、编译配置、默认问答、工作区恢复及对话工作台组合为 91 项通过，4.794 秒。这些是机制测试耗时，不是用户仓库或真实模型的延迟数据。源码定位超时恢复、取消优先和按预算二分裁剪保持独立覆盖；导航裁剪不删除测试中已供应的公式原文。

## 验收边界

这份报告记录参考与已定位机制，不声称真实模型准确率或线上延迟已有某个提升比例。整体发布仍需分别核对：导入与提问分离、冷/热大型语义分析、慢连接与无响应、默认答案错误公式拦截、真实供应证据、多轮焦点、编译产物。企业源码和收费模型请求未用于本次研究。
