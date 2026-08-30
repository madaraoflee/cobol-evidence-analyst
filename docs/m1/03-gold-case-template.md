# M1 金标准案例模板

> 核心判断：一个金标准案例必须规定“系统需要找到什么证据”，而不只是保存一段看起来正确的参考答案。

每个案例保存为独立 YAML 或等价结构。当前先用此模板进行人工标注，字段稳定后再决定机器可读格式。

```yaml
case_id: CALC-01
title: 分期保费计算解释
question: 分期保费最终是怎样计算出来的？

scope:
  corpus: synthetic-premium-v1
  repository_revision: pending
  entry_points:
    - SYNCL001
    - SYNP000

expected_answer:
  code_facts:
    - claim: pending
      evidence_ids: []
  business_inferences: []
  open_questions: []

required_evidence:
  entities: []
  relation_paths: []
  source_ranges: []
  control_conditions: []
  data_sources: []

forbidden_claims:
  - 不得把合成规则描述成真实公司、DXC 或香港监管规则
  - 不得给出证据中不存在的程序、字段、费率或源码位置

expected_behavior:
  answer_mode: supported
  must_abstain_on: []

evaluation:
  parser_checks: []
  retrieval_checks: []
  answer_checks: []
  reviewer: pending
  review_status: unreviewed
```

## 标注方法

先由分析人员不借助 Agent 完成一次调查，记录实际阅读顺序；再把“阅读过但最终无关”的内容剔除，得到最小完整逻辑闭包。第二名复核者应尝试只依赖这些证据重建答案：若无法重建，说明证据不完整；若仍包含大量无关代码，说明证据不够精确。

对于拒答案例，`expected_answer.code_facts` 可以说明源码能够证明的有限部分，但 `expected_behavior.answer_mode` 必须设为 `abstain`，并明确禁止系统跨越的证据边界。
