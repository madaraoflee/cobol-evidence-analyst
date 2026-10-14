# 固定公开源码的默认业务链验收

本批次验证真实公开源码进入默认 `run_business_chat` 后，供证、引用版本和有限本地核验能够保持正确公式，并定向修订错误公式及按段落名编造的手续费逻辑。采用 [CardDemo 固定提交 59cc6c2fd7ebd7ef7925cad552a01a4b8b6e4d5e](https://github.com/aws-samples/aws-mainframe-modernization-carddemo/tree/59cc6c2fd7ebd7ef7925cad552a01a4b8b6e4d5e)，源码保留在外部下载目录，未复制到代码仓库；8 个实际参与文件的 SHA-256 记录在 [manifest.json](manifest.json)。

[cases.json](cases.json) 包含逐项源码审阅的黄金要求、禁止新增的结论及真实行范围，覆盖超限拒绝、账户更新、月息、文件映射与未实现费用段落。题目已用于本轮开发，标记为 `public-development`，不能充当独立留出题或企业方言验收。

通过现有业务索引和默认业务对话入口运行 5 个正确答案分支，并分别注入除以 999 的错误月息公式及余额 1% 的错误手续费文案。全部回答由进程内 fake transport 提供；供证和引用 ID 来自默认链实际供应的源码。核验只接收真实供应证据，不把测试黄金要求送给模型请求。

```bash
python3 poc/public_business_acceptance.py --source-root /path/to/fixed-public-checkout --output /path/to/offline-results.json
PUBLIC_BUSINESS_SOURCE_ROOT=/path/to/fixed-public-checkout python3 -m unittest discover -s poc/tests -p test_public_business_acceptance.py -v
```

本轮 [offline-results.json](offline-results.json) 记录 7 个分支通过，每分支 1 次本地 fake 请求。正确公式保留；错误公式修订为原源码的除以 1200，独立成立的后续说明保留；错误费用计算修订为完整段落仅有 EXIT 的事实。超限检查后还有到期日检查，拒绝码 102 可被 103 覆盖；账户改写为显式 REWRITE 请求，不能据此宣布运行写入成功。文件映射说明未被伪装为已做语义核验。

报告同时记录实际供证指纹、源码哈希、完整编译上下文及指纹、索引 snapshot、局部规则断言、核验 ledger、请求数及离线耗时；下载 archive 与许可原文件哈希也已固定。每分支通过主评估器同源的 review identity 绑定实际 run/branch、答案、model/prompt/policy、gold、源码版本与供证指纹，版本标签取自实际运行的 trace。它没有真实模型准确率、编译运行结果、真实文件数据或企业目标方言通过率；后续真实模型验收应先运行基线，再确定质量目标。人工评分使用主评估器的身份绑定表单，表单填全与答案正确分别统计。
