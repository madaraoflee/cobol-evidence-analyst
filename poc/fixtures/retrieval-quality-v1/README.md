# 离线检索与供证诊断反例

这些源码行是原创中性样本，标签依据是字面条件、计算和覆盖语句，尚未经过独立人工评审；它们属于开发回归集，不代表真实业务题集或保留评估集。

在仓库根目录运行：

```bash
python3 -m unittest poc.tests.test_retrieval_quality_eval -v
python3 poc/retrieval_quality_eval.py --output /private/tmp/retrieval-quality-report.json
```

工具不调用模型或网络。默认复用生产请求装载器，从最终序列化请求读取实际原文；A 为原始候选，B 加入 gold 充分原文后走同一装载器，C 只保护已召回的必要证据，D 直接装载最小充分原文。输入中不包含 gold 答案。A/B/C/D 的差别用于定位材料损失，生成质量仍为未评测。

本文件中的候选顺序是人为编写的失败轨迹，CLI 不执行生产检索，因此不能把分数解读为当前检索器实测质量。`evaluate_evidence` 可另接真实轨迹的候选与最终原文。必要项充分率使用全部返回候选；排名指标使用前 k 个唯一 span。`context_support_rate` 表示已召回必要证据在最终原文中的保留比例，不是答案的语义正确率或上下文纯度。

排名按去重后的原始 span、源码 hash 和包含实例计量，报告被去重的条数；相同内容位于不同文件或 COPY 实例时不合并身份。Recall 只描述已标注相关证据，原始行段可由多个新 chunk 联合覆盖。固定 k precision 与实际返回 precision 分开；未标注结果使 precision 显示上下界，MRR 或 nDCG 在无法确定时留空。空相关 gold 不记满分。必答项可以有替代的最小充分证据集合，关键项缺失单独报告。

默认比较序列化请求字节预算，明确标记 `token_budget_verified: false`。需要固定模型 token 对照时，通过 `production_loader(..., max_input_tokens=..., token_counter=..., tokenizer_id=...)` 注入与实际模型匹配的本地计数器；计数器接收全部最终消息，包含系统指令。工具不会把字符数、字节数或测试用分词器冒充真实模型 token。D 按原文字节选取一套较小充分集合；超预算标记只描述该集合，不证明所有替代集合都无法装入。
