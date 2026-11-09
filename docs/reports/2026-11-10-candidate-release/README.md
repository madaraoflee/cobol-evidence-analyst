# TLS 修复后的 Windows 候选与逐次回归

用户于 2026-11-10 明确要求生成包含本次 TLS 修复的 release 候选，供公司复测剩余六项，并要求生成前完整验证全部已登记旧缺陷，未验证项目继续保留。此次授权适用于候选构建与交付，不表示正式联合验收已经完成。

应用冻结提交为 `45add3726904c5e90fff740b3da49cef42bbfec0`。相对上一候选 `v2026.11.09.1-rc.1` 的应用变化仅为已经批准的 TLS 诊断说明和兼容注释，并增加对应回归；此前累计分析、上下文压缩、失败恢复及独立在线链路原样保留。本轮未再修改应用源码、测试、缺陷登记或最低基线。

## 生成前的新一轮完整验证

在独立且干净的固定提交目录运行 `tools/verify_bug_regressions.py`，实际执行全部 5,140 项 Python（含维护工具）和 310 项浏览器测试，零失败、错误、跳过或预期失败。258 条缺陷逐条核对为 252 条 passed、原有 6 条 unverified；`source_regressions_passed=true`、`all_recorded_bugs_verified=false`，总状态继续为 blocked。该非零状态来自未关闭的现场记录，不是跳过测试或改写门禁结果。

独立核验每个测试 ID 恰好执行一次、全部 Git 历史最低基线和缺陷映射保留、831 文件源码快照与冻结提交一致，以及八份公开输入的冻结哈希。原始全部分片、日志、浏览器事件和快照保存于 `SOURCE-REGRESSION-REPORTS.zip`；逐条结果见 `BUG-REGRESSION-REPORT.json`。该次结果独立于之前的修复回归，未复用旧结果冒充重新运行。

## 六条保留未验证

| 编号 | 尚待公司/真实模型验证的内容 |
|---|---|
| BUG-20261007-901 | 保存前后审批状态、默认路径及条件的真实回答准确性 |
| BUG-20261007-902 | 动态调用失败、金额计算阶段和回写的真实回答准确性 |
| BUG-20261106-901 | 公司电脑超过一小时启动的分阶段耗时与根因 |
| BUG-20261106-902 | 公司 HTTP 500 的安全错误码和关联记录；额度限制仍是待核实线索 |
| BUG-20261106-903 | 公司 TLS 通信失败的实际根因 |
| BUG-20261106-904 | logical view/subroutine 等复杂扣费链路及多轮业务回答完整性 |

候选的离线测试不证明真实模型准确率或公司设备体验。本轮未调用公司模型、发送公司源码或修改企业网络策略，也未关闭上述六项。公司复测应使用同一候选 ZIP，并保留原问题、追问、完整回答及引用；启动问题记录窗口出现、资料恢复和可提问三个阶段，接口问题保留安全错误详情。无需公开上传公司源码、密钥或完整请求。

## Windows 构建与交付

手动构建 run 为 [42656491361](https://github.com/madaraoflee/cobol-evidence-analyst/actions/runs/42656491361)，绑定 main 上的上述固定提交。最终 EXE 已完成 25 案例、79 离线样本及 55 项连续性检查。唯一候选 artifact 为 `13695606798`，外层 SHA-256 为 `ee24739f2e0ad3e1d106a5bc8e1c18a2673cd231f11d289403d7fa4f57d3fe75`。下载后重新校验全部八个附件、源码提交、EXE 与 ZIP 绑定，包内前端资源归一换行后与冻结提交逐字节一致，TLS 新文案已包含。候选门禁允许交付，正式门禁仍为 `REAL_MODEL_AND_OFFICE_ACCEPTANCE_REQUIRED`。详细结果见 `windows-candidate-verification.json`。

Windows 源码回归已独立核验：四分片覆盖当前全部 5,139 项应用测试且每项恰好一次，5,137 通过、2 项既有 POSIX 权限专用测试跳过。两项在本轮同提交本地完整回归中均通过，未记作 Windows 通过。新增两项 TLS 测试在 Windows 实际通过。五份 artifact 的来源、大小和 SHA 全部匹配，原始分片及汇总保存在 `WINDOWS-REGRESSION-REPORTS.zip`，独立核验见 `windows-regression-crosscheck.json`。

## 发布后复核

[v2026.11.10.1-rc.1](https://github.com/madaraoflee/cobol-evidence-analyst/releases/tag/v2026.11.10.1-rc.1) 已按 Pre-release 发布，标签精确指向应用提交 `45add3726904c5e90fff740b3da49cef42bbfec0`。发布后的八个附件大小及 SHA-256 均与本地已经核验的字节相同；正式 latest 仍为 `v2026.10.14.2`。完整回执见 `github-release.json`，独立同包核验见 `candidate-independent-verification.json`。

Windows ZIP SHA-256：`c4631c7f9a5d61d31454b1f7f226b7e205203ec649a551777cf75318e0bf581b`。候选不代表公司真实验收完成，六条记录继续未验证。本轮记录提交只追加本目录证据，不改变候选源码、不重建应用，也不使用上个版本的辅助验收包冒充本包。
