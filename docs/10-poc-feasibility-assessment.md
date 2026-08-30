# 复杂 COBOL 代码理解 Agent：POC 可行性复核

> 核心判断：项目可行，但产品形态必须是“确定性代码事实层 + 混合检索 + 有界 Agent + 证据回答”，不能是把源码任意切块后只做向量 RAG。当前结论为 **Conditional GO**：足以开始一个真实纵向 POC；是否能扩展到整个 DXC Smart COBOL 企业代码库，必须由公司环境中的方言覆盖率和私有金标准决定。

状态：Conditional GO — ready for P1 structural index  
日期：2026-08-29  
范围：已下载到 Windows 文件夹的 COBOL 与 COPYBOOK，只读分析

## 1. 为什么技术上可行

COBOL 的 Program、Section、Paragraph、Data Item、CALL、PERFORM、IF/EVALUATE、算术语句和文件操作具有相对稳定的语法结构，适合先被转换成可查询事实。IBM 官方文档明确区分了静态/动态 CALL，并说明 PERFORM 的 inline 与 out-of-line 控制语义；公开的 ProLeap 解析器也已经能生成 AST/ASG、执行 COPY/REPLACE 预处理并提供部分变量访问与控制/数据语义。这证明“从 COBOL 恢复结构与关系”不是理论设想。

大模型不需要一次阅读整个代码库。全库先离线建立结构索引；每个问题只围绕命中的字段、Paragraph、Program 和有限关系路径做深分析，再把最小证据包发送给公司模型 API。代码量增加主要影响离线索引时间与存储，不要求单次问答上下文等比例增加。

## 2. 能力边界判断

| 目标能力 | POC 判断 | 需要的确定性事实 | 必须保留的边界 |
| --- | --- | --- | --- |
| 全库 Program/COPYBOOK 画像 | 可实现 | 文件 Hash、编码、Program-ID、COPY/CALL/PERFORM 计数 | 无扩展名成员、重复 Program 名和未知编码需报告 |
| 字面量 Program Calling Chain | 可实现 | CALL literal、Program/ENTRY 定义、参数位置 | “存在调用边”不等于生产时必然执行 |
| 动态 CALL identifier | 部分实现 | 目标字段赋值、候选 Program、路由条件和表读取 | 没有运行值或路由表内容时只能输出候选集合 |
| Paragraph/Subroutine 路径 | 可实现 | PERFORM、PERFORM THRU、GO TO、Paragraph 范围与返回点 | 循环以 SCC/循环段压缩，不能无界展开 |
| IF/EVALUATE/88 条件影响 | 可实现 | Condition、分支入口、CONTROL_DEPENDS_ON、字段来源 | 静态分析能证明条件关系，不能断言某次运行走哪一支 |
| 计算与 Data Logic | 可实现到受支持语法范围 | Expression Tree、PIC/scale、READS/WRITES、ROUNDED、覆盖写入 | REDEFINES、别名、数组下标、方言函数未解析时只能给部分公式 |
| COBOL File I/O | 对象级可实现 | SELECT/FD、READ/WRITE/REWRITE/START、record、file-status | 没有 DDS/DB File 定义与真实数据时不能证明字段结构或生产值 |
| EXEC SQL | 表级可实现，列级视资料而定 | SQL 文本、SELECT/UPDATE、host variable、表/列引用 | 动态 SQL、DDL 缺失和框架封装必须标成外部边界 |
| 跨 Program 参数流 | 可实现但成本较高 | CALL USING、LINKAGE/ENTRY USING、ordinal、passing mode、读写方向 | BY REFERENCE、副作用和多入口歧义必须显式表示 |
| 业务语言回答 | 可实现 | 每个事实绑定 EvidenceSpan，业务解释与代码事实分层 | 商业原因、监管原因和生产配置不能从源码中猜测 |

## 3. 上万程序不会推翻方案

规模问题不能靠“换一个更大的向量库”解决，而要靠分层计算：

1. L0 Inventory 覆盖全库：Hash、编码、文件和粗粒度结构；
2. L1 Structural Index 覆盖全库：Program、Paragraph、Field、COPY、字面量 CALL、PERFORM、FTS5；
3. L2 Semantic Retrieval 只在公司批准 Embedding API 后启用，向量只用于入口候选；
4. L3 Deep Analysis 只分析当前问题命中的程序及有限依赖，恢复表达式、条件、I/O 和最多三跳关系；
5. 文件变更后按 Hash 增量失效，不重做整个代码库；
6. 调用环和递归先压缩为强连通分量，避免路径无限展开。

因此第一版仍应保持 Python 模块化单体和 SQLite。Neo4j、Qdrant、LangGraph 等目标组件只有在 POC 基准证明必要时才引入。

## 4. 真正的最大风险

最大风险不是模型能力，而是公司方言和缺失制品：

- DXC Smart COBOL 可能通过宏、表驱动路由、动态 Program 名或框架 Subroutine 隐藏真实关系；
- 当前只有 COBOL/COPYBOOK，没有 DDL/DDS、Job Schedule、DB File、Item/Control Table 实际内容和运行日志；
- REDEFINES、OCCURS DEPENDING ON、BY REFERENCE、副作用和 COPY REPLACING 会增加数据流歧义；
- “OpenAI-compatible”不保证 Tool Calling、strict JSON Schema、Embedding 和流式响应都可用，必须逐项探测。

这些风险不会阻止 POC，但会决定回答是 Confirmed、Candidate、Unresolved 还是 Unavailable。系统的可信度来自诚实缩小结论，而不是强行回答。

## 5. 开发进入条件

当前可以进入开发。第一项不是接向量数据库，而是完成 P1 Structural Index：

    Windows 源码快照
      → 固定列与编码标准化
      → Program / Paragraph / Field / COPY 抽取
      → CALL / PERFORM / IF / I/O / 计算语句识别
      → CodeUnit / Symbol / Relation / EvidenceSpan
      → SQLite + FTS5 + Hash 增量更新

首个纵向业务切片继续使用 CALC-01 分期保费。通过条件是：不依赖预写答案，能够从合成源码恢复最终写入、计算组成、关键条件、Program/Paragraph 路径、错误传播和精确行号。进入公司环境后，用相同接口连接获准的真实 Windows 文件夹，再增加 DXC 方言规则。

## 6. 当前证据

现有 repo_inventory 已实现只读扫描、CP950 识别、快照 Hash、默认去标识符聚合报告和 Windows 启动脚本；本次复核运行 5 项测试全部通过。这意味着 P0 的离线入口已经存在，下一步可以直接构建 L1，而不是重新从项目脚手架开始。

公开依据：

- [IBM：CALL statement](https://www.ibm.com/docs/en/cobol-zos/6.4.0?topic=statements-call-statement)
- [IBM：PERFORM statement](https://www.ibm.com/docs/en/cobol-zos/6.3.0?topic=statements-perform-statement)
- [IBM i：dynamic program call](https://www.ibm.com/docs/en/i/7.6.0?topic=calls-performing-dynamic-program-using-call-literal)
- [ProLeap COBOL Parser](https://github.com/uwol/proleap-cobol-parser)
- [OpenAI Docs：Function calling strict mode](https://developers.openai.com/api/docs/guides/function-calling#strict-mode)
