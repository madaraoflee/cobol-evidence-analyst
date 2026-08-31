# M1：保费计算解释合成试点

> 核心判断：先证明 Agent 能把一个计算问题还原为完整、可复核的业务解释，再扩大到整个香港保险系统。

状态：`Active for demonstrable POC`。当前只实现足以闭合 `CALC-01` 的最小原创 COBOL fixture，不默认完成全部 26 个问题；实施主线见 [可演示 POC](../09-demonstrable-poc.md)。

本目录只描述原创合成场景，不包含或影射公司代码、DXC 文档、真实产品规则和生产数据。

## 当前状态

| 工作项 | 状态 | 退出条件 |
| --- | --- | --- |
| 试点问题与边界 | 已完成初稿 | 一页纸中的目标、非目标和用户结果得到确认 |
| 合成业务与系统蓝图 | 已完成初稿 | 业务公式、程序边界、数据来源和异常路径没有歧义 |
| 金标准问题集 | 已完成 v0 | 20–30 个问题覆盖计算、条件、调用、数据和拒答 |
| 金标准案例标注 | 进行中（1/26 已升级为可执行案例） | 每题拥有正确答案、最小证据路径、源码位置和禁止结论 |
| 原创 COBOL fixture | CALC-01 v1 已完成 | 8 个 Program、5 个 COPYBOOK 可被真实扫描、追踪和引用 |
| P1-B 调查工具 | 已完成 | 四工具在 6 步预算内生成 `SUPPORTED_WITH_BOUNDARIES` 证据结果 |
| 完整解析器 Bake-off | POC 后再评估 | 只有轻量提取器无法支持真实试点时才启动 |

## 文档顺序

1. [产品一页纸](./00-premium-analysis-pilot.md)
2. [合成系统蓝图](./01-synthetic-system-blueprint.md)
3. [金标准问题集 v0](./02-gold-questions-v0.md)
4. [金标准案例模板](./03-gold-case-template.md)
5. [首个案例：CALC-01 分期保费计算解释](./cases/CALC-01-instalment-premium.md)

`CALC-01` 已能被真实扫描、检索、调查和引用，结果见 [P1-B 可执行演示](../leadership-demo/p1b-executable-demo.md)。P3-A 受控 Agent 骨架已在离线假传输和真实结构索引上通过；下一步在公司批准环境连接真实 API，再从其余问题中选择三个标注问题、一个未预写问题和一个拒答问题。
