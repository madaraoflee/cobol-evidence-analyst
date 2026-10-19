# 有界问答离线机制对照

本次只验证修复机制：相同中性源码、原问题和确定性模型脚本，直接运行两个版本的生产检索、供证、审核与持久化流程。没有调用真实模型，也没有替换生产 reviewer。

| 题目 | 版本 | 请求数 | 本地问答耗时 ms | 缺源码错误 | 999 错公式 | 正确算式/倍数 | 正文来源 | 有可用正文 |
|---|---|---:|---:|---|---|---|---|---|
| complete_source_denial | baseline | 2 | 84.146 | True | False | True | model | True |
| field_source_denial | baseline | 1 | 69.355 | True | False | True | model | True |
| parameter_source_denial | baseline | 1 | 66.358 | True | False | True | model | True |
| wrong_richer_revision | baseline | 2 | 83.899 | False | False | True | model | True |
| default_bounded_investigation | baseline | 9 | 211.576 | False | False | True | model | True |
| ignores_finalization_request | baseline | 9 | 210.33 | False | False | True | model | True |
| complete_source_denial | candidate | 1 | 73.888 | False | False | True | model | True |
| field_source_denial | candidate | 1 | 72.952 | False | False | True | model | True |
| parameter_source_denial | candidate | 1 | 74.845 | False | False | True | model | True |
| wrong_richer_revision | candidate | 2 | 88.741 | False | False | True | model | True |
| default_bounded_investigation | candidate | 6 | 166.027 | False | False | True | model | True |
| ignores_finalization_request | candidate | 6 | 156.985 | False | False | True | local_source_facts | True |

默认有界调查案例按相同规则响应收尾请求：当 reads_per_turn=0 时给出答案；另有 ignores_finalization_request 压力案例故意忽略该请求，记录预算停止后的真实交付结果。表中 model 表示脚本模拟的模型正文，local_source_facts 表示生产逻辑从已供应源码生成的局部事实，两者不可混为模型质量提升。

每题的源码 hash、原问题、逐请求实际供证范围/hash、脚本原始答复、最终正文、run_id、trace 摘要/hash 和生产状态见同名 JSON。源码均为脚本内中性合成样例。

耗时是单次本地编排测量，排除建索引且不包含真实模型服务延迟，不能据此宣称真实模型回答速度或业务质量提高。此集合属于有针对性的开发回归，不代表广泛业务分布，也不是保留评估集。

复跑：`python3 tools/compare_bounded_answers.py --baseline <baseline-tree> --candidate <candidate-tree> --output docs/reports/2026-10-19-bounded-answer-comparison.json`。独立测试保护最多 12 次请求、单进程 30 秒；若触发会明确记录，不能算成产品自然结束。

坏修订案例必须单独解读：两版最终都消除了 999；旧版采纳第二稿后才在最终审核改写，新版保留第一稿。这验证审核时机与草稿保留，不能宣传为此题最终公式准确率提升。输入/结果说明仍有局部缺口，JSON 保留真实 incomplete 状态。

压力案例中脚本忽略最后收尾请求；候选在 6 次请求后停止，并从问题明确点名、当前请求实际供应的 root.cbl 中保留局部源码事实。正文来源记录为 local_source_facts，model_answer_recorded=False，状态为 PARTIAL；这证明有界停止时仍保留可证明内容，不能据此称模型已经完成调查或全部业务路径已得到解释。最后计划、实际源码、正文及未完成边界均在 JSON 中保留。
