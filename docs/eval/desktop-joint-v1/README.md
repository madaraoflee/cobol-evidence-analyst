# 发布包联合回归集

这组检查验证实际桌面进程的默认 HTTP 导入、问答、恢复和证据流程。固定清单包含 17 个案例、55 个样本：一般案例重复 3 次，密集字段与大型仓库重复 5 次。它使用本地 HTTPS 假模型，只能证明指定机制和关键事实检查通过，不能证明真实模型的业务准确率；报告中的真实模型准确率和业务答案正确性保持 `null`。

`manifest.json` 固定必须执行的案例、断言、原始源码哈希和时间上限。实际发布门禁同时核对清单、应用提交、Windows ZIP、EXE 和构建信息身份。缺少案例、重复记录、语料缺失、断言失败、缺测或超时不能当作通过。`PARTIAL` 状态本身不算质量通过：每个答案仍须满足该案例的具体关键事实、供证和可见答案断言。完整业务质量与办公室设备验收另由发布门禁核对。

语义案例覆盖最初七个前端反例、条件覆盖与分支合并、输入变更影响、三级静态调用传参及正常返回。检查既读取最终进程创建的稀疏索引和语义 IR，也核对实际模型请求的源码文本、哈希、原始行范围、ClaimLedger 和工作台可见答案。运行器不导入应用的解析器或分析服务替代最终程序。调用语义工具的候选由假模型通过普通 Chat JSON 提出，仍经过工作台本地校验和默认工具执行路径。

公开样例复用 `../public-business-v1` 中冻结的源文件哈希和黄金要求，覆盖超限拒绝、账户更新、月息、文件映射、未实现费用段落，以及两项错误答案修订。源码保留在外部目录；未提供或哈希不匹配时失败，不跳过。假模型答文是受控测试输入，不计入真实模型评分。

大型样例由中性生成器创建 12,024 个文件、2,712,992 行，其中一个程序长 25,000 行。文件内容指纹与本轮既有大型基准相同。运行器通过桌面正常导入创建空白索引，不复制预建数据库。冷建索引与后续五次提问分开计时；每次提问使用新会话，并核对索引实际复用。时间上限是有余量的回归阈值，不是生产 SLA，也不能代替企业目标机器实测。

在 Windows 上运行实际交付 ZIP：

```powershell
python tools/desktop_acceptance.py --archive path/to/COBOLWorkbench-windows-x64.zip --source-commit COMMIT --public-source path/to/fixed-public-checkout --output path/to/new-acceptance-output
```

开发联调可使用源码进程，结果会明确标为 `source_python_http`，不能通过最终发布包验收：

```bash
python tools/desktop_acceptance.py --source-root . --source-commit COMMIT --public-source /path/to/fixed-public-checkout --output /path/to/new-preview
```

`--case CASE_ID` 用于调试子集，报告会记录 `selection_complete=false`。不得将不同子集、提交、包、模型或策略的报告拼接成一份完整通过结果。运行目录必须是新目录；先前样本不会自动复用。保留每个样本的实际运行 ID、答案哈希、请求证据和阶段耗时，以便定位下一次回归发生在哪一层。
