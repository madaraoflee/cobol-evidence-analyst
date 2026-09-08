# COBOL Evidence Analyst

**当前产品进度：P2 单业务流程深分析进行中，尚未完成完整业务问答和界面交付。[任务计划与进度总表](./docs/12-task-plan-and-progress.md)已更新：T01 跨程序错误返回在受支持源码模型内通过，下一项 T02 正常业务计算待开始。**

核心判断：本项目首先要做成一个“以源码证据为底座的 COBOL 业务逆向分析 Agent”，而不是普通代码聊天机器人，也不是一步到位的自动化 SDLC 平台。

当前主线已经切换到 **可演示 POC**：先让一台 Windows 电脑能够读取一个源码文件夹，由单个受控 Agent 调查代码并返回带行号证据的业务回答。大模型只允许通过公司 API Key 调用，不安装 Ollama 或任何本地模型运行时。完整 IR、图服务和企业级核验契约保留为验证成功后的目标架构，不进入首版实现。

## 项目文档

- [任务计划与进度总表（当前进度入口）](./docs/12-task-plan-and-progress.md)
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
- [2026-09-07 P3-B 项目进度报告](./docs/reports/2026-09-07-p3b-progress-report.md)
- [2026-09-08 P3-C 业务链事实与完整性检查](./docs/reports/2026-09-08-p3c-business-chain-progress.md)
- [2026-09-08 P3-D 复杂调用链与错误传播](./docs/reports/2026-09-08-p3d-cross-program-error-flow.md)
- [2026-09-08 P3-E 重复调用上下文与可行性验证](./docs/reports/2026-09-08-p3e-call-context-feasibility.md)
- [2026-09-08 P3-F 调用异常与溢出路径验证](./docs/reports/2026-09-08-p3f-exception-path-feasibility.md)
- [2026-09-08 T01 跨程序错误返回闭环验收](./docs/reports/2026-09-08-t01-interprogram-error-returns.md)
- [P1-B 领导审阅 Word 报告](./docs/reports/COBOL-Agent-P1B-Progress-Report.docx)
- [POC 实现：离线代码库画像工具](./poc/README.md)
- [办公室电脑使用手册：从安装到 Agent 调查](./docs/11-office-usage-guide.md)
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

## 技术进展记录（不等于产品里程碑已通过）

以下 P3-A～P3-F 是历史研发切片编号，不等于原 P3 自主业务问答里程碑已完成。用户可见效果、验收门槛与任务顺序以[进度总表](./docs/12-task-plan-and-progress.md)为准。

P3-A 的可运行骨架已完成：公司 OpenAI-compatible API 能力探测会验证 Chat、Tool Calling 完整回传、严格 JSON 和可选 Embedding；只有原生工具闭环或严格 JSON 探测通过，运行器才会启动 Agent。Agent 只能调用四个只读工具，最多 6 次，并强制快照、Evidence ID 范围、Hash、引用和结果包契约；非 Evidence 结果会被重建为安全投影。读取源码后调查范围立即关闭，只能完成回答或拒答。

P3-B 已接入独立 Claim 核验的第一个可执行切片：模型提交结构化 `COMPUTE` 断言，本地核验器直接解析单段完整、Hash 有效的源码，逐项核对目标字段、算式 token 顺序和 `ROUNDED`，再由本地模板生成中文陈述。通过核验的回答可返回 `SUPPORTED_WITH_BOUNDARIES`；它只证明该语句的写法，不证明最终值、实际执行或精度规则。CALC-01 的四次真实工具调用离线闭环已接入这条路径；普通自然语言陈述仍为 `CITATION_VERIFIED_ONLY`，不匹配或超出支持语法的结构化断言会保留原因并拒绝升级。

P3-C 已补齐多行条件、程序范围内的 COPY 字段定义绑定，以及 SQL 宿主变量的直接读写。字段现在能沿“声明位置 → SQL 写入 → 公式读取”追踪，同名字段不会跨程序混绑；这仍不等于 CALL 参数传递或完整值流证明。旧索引重新运行构建命令时会按解析器版本自动更新。

P3-D 已实现受支持的 CALL USING/LINKAGE 位置与布局对应，区分 REFERENCE、CONTENT、VALUE，并提供错误状态、查询分支、委托清零和后续覆写审查。新增复杂样例含 14 个主程序、4 个 COPY、五层调用链及配置型动态调用；主场景得到 205 条参数/成员对应与 92 条可能回写关系。另有独立反例和 35 个明确未执行的业务案例期望。配置目标仍未确认，完整控制流与业务回答仍未证明。

P3-E 已加入完整静态调用点链上下文。同一包装程序被调用两次、再进入同一共享子程序时，参数和本地错误观察引用分别绑定到当前链；内容复制或缺失的链条不会升级为入口回写候选。新样例 7 个程序、8 个调用点展开为 12 个静态上下文，48 项明确的源码结构验收通过；这不是运行时实例隔离或完整错误执行证明。

P3-F 已实现显式语法子集的局部异常控制流与有界状态路径：区分调用正常/失败、计算正常/溢出，跟踪错误后的输出直到模型退出，并能检出清零后再次覆写。新样例 5 个程序展开为 8 个静态上下文，三项安全事件预期及单独的非零覆写反例均符合验收；隐式作用域、循环和跨程序真实执行仍未证明。

T01 已在明确支持范围内组合实际子程序源码路径与调用方返回：状态 21 经六步参数记录到达入口和汇总；30 项主检查、6 项复制隔断检查通过，调用方覆写 7 的副本触发 5 项安全预期失败。未绑定 LINKAGE、别名、递归和未决目标不伪装成完整结果；全量 438 项测试通过。可查看 [T01 报告](./docs/reports/2026-09-08-t01-interprogram-error-returns.md)或 [本地案例结果](./.poc-data/error-return-v5/result.md)。

完整业务分析仍是最终目标。原 CALC-01 保留缺口作为回归反例，Agent 始终另行标明问题完整性未核验。下一项是 T02 正常业务计算闭环，尚未开始；随后补数组循环、共享工作区、配置与真实模型完整回答。当前未调用公司 API、未执行 COBOL，也未接通界面。
