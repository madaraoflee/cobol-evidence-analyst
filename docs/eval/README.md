# 业务回答真实评测：离线准备与费用边界

本目录给出可直接运行的评测计划与人工评分结构。`neutral-example.json` 只使用项目合成源码，是开发样例；`company-case-template.json` 是待填写的独立业务题模板，不能把前者改名为 holdout。`plans/` 中三份 JSON 是 2026-09-30 以工作台参数和占位模型配置生成的离线计划，未接入公司模型，也未评出业务质量。`unscored-review-template.json` 的空值不能算通过。

在项目根目录生成针对实际题集的无费用计划：

```bash
python3 poc/live_business_eval.py --source /path/to/approved/source-snapshot --cases /path/to/independent-cases.json --evaluation-dir /path/to/local-evaluation --mode automatic --profile workbench --capture-context --plan --review-template /path/to/reviews-empty.json
python3 poc/live_business_eval.py --source /path/to/approved/source-snapshot --cases /path/to/independent-cases.json --evaluation-dir /path/to/local-evaluation --mode reviewed-context --profile workbench --plan
python3 poc/live_business_eval.py --source /path/to/approved/source-snapshot --cases /path/to/independent-cases.json --evaluation-dir /path/to/local-evaluation --mode paired-synthesis --profile workbench --plan
```

计划模式不建语义侧库、不预检接口、不发模型请求。工作台 profile 为单次超时 60 秒、默认输出上限 8192 tokens（显式环境或文件设置仍优先）；每次普通请求最多 230,000 bytes。当前示例只有 1 个用户轮次，含可选预检时 automatic 最多 6 次请求，reviewed-context 最多 2 次，paired-synthesis 最多 3 次；预检关闭后分别为 5、1、2。真实题集按实际用户轮数重算，多轮历史不另计为新问题。失败尝试仍可能计费，脚本不自动重试；未提供单位价格，因此计划中的币种金额为 `null`。请求上限不是费用授权。

实际评测须先确定：获准使用的模型与单价、输入及输出计价单位、预检是否执行、题数及用户轮数、单轮与总费用上限、失败请求的计费处理、框架资料与源码快照版本、公司数据的允许使用范围，以及负责评分的 BA。获得明确批准后才可另行使用 `--allow-network` 运行。automatic 先以 `--capture-context` 保存最后作答轮真实原文；paired-synthesis 必须读取这个冻结 artifact。缺少 artifact、路径/hash/行范围变化或人工材料超出请求预算时，不生成“公平配对成功”结论。两支最终合成只改变证据集合，共享问题、背景、模型、提示和输出参数，不把 gold 或自动初稿送给任何一支。

人工评分 JSON 由 `--review-template` 导出，再填写 `reviewer`、`reviewed_at`、每项 required finding 的 `met|omitted|incorrect`、每项 forbidden claim 的 `present`、`usefulness`、`followup_continuity` 和备注。v2 表单逐个答案、逐个 paired 分支保存不可替换的 `identity`：run、case、turn、branch、问题/历史/答案 hash、model、prompt/request、policy、gold、源码/框架版本和最后作答轮实际供证指纹。保留这些字段原值；旧 v1 表单或任何不匹配的评分记为 `stale`，不附到新答案。

评分完成后，将其附到原归档报告；这一步完全离线，不重问模型，也不读取 live 源码：

```bash
python3 poc/live_business_eval.py --source /path/to/source-snapshot --cases /path/to/cases.json --evaluation-dir /path/to/reviewed-report --review-report /path/to/original/live-business-eval.json --reviews /path/to/filled-reviews.json
```

`review_completion` 统计表单是否填全，`correctness_counts` 独立统计 correct、incomplete、incorrect、pending 与未评分。完整表单中的遗漏仍是 incomplete，错误新增结论仍是 incorrect。`expected_paths_supplied` 依据最后作答轮真实供证记录计算，导航命中和检索候选不算实际供应。已归档答案继续绑定原版本证据；新一轮生成新 run，不能自动继承旧评分。业务正确性、关键条件、最终结果、框架解释、BA 可用性和追问承接仍需独立人工判定；离线机制与性能指标不能代替真实模型验收。固定公开源码的无网络验收入口见 [public-business-v1](public-business-v1/README.md)。
