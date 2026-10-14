# 最终 Windows EXE 联合验收交付

联合验收已进入真实自动构建和受支持的发布入口。冻结应用提交为 `44fcb5f9e769ae559c9cd6b5b12d396959613998`，Windows [构建记录](https://github.com/madaraoflee/cobol-evidence-analyst/actions/runs/38593469204) 已成功。最终 ZIP 内的 EXE 通过默认 HTTP 工作台完成 17 个案例、55 个样本；本次交付仍为候选，正式发布没有获准。

## 实际原生结果

| 检查 | 结果 |
| --- | --- |
| Windows 完整 Python 回归 | 2,506 项，2,504 通过；2 项 POSIX 权限专属测试跳过 |
| 固定公开业务语料 | 必须下载并核对 commit/文件哈希，未跳过 |
| 浏览器回归 | 166/166 通过 |
| MSVC 编译、原生自检 | 通过，JSON C 扩展启用，零模型自检调用 |
| 交付 EXE 联合回归 | 55/55 样本通过，约 547.81 秒 |
| 12,024 文件、2,712,992 行正常冷导入 | 422.217 秒，未使用预制索引 |
| 大库首次模型请求 | 4.543、4.602、4.574、4.623、4.665 秒 |
| 大库完整热问答 | 8.603、8.793、8.790、9.076、9.048 秒 |
| 2,000 基本字段密集记录完整问答 | 1.447、0.685、0.675、0.743、0.725 秒 |

以上问答走本机 HTTPS 合成接口，响应引用 EXE 真正供应的证据 ID；不是远程真实模型准确率测试，也不是企业目标设备 SLA。报告保留 `ANALYZED=9`、`PARTIAL=40`、生命周期样本无答案状态 6。`PARTIAL` 不能被机制断言通过掩盖，`real_model_accuracy` 与 `business_answer_correctness` 仍为 null。没有测得可比的旧 EXE/真实模型基线，不宣称准确率或速度提升比例。

## 已固定的反例与阻断规则

冻结 [案例合同](../eval/desktop-joint-v1/README.md) 覆盖七项语义反例、真引用下错误乘以 999 的修订、独立 MOVE 保留、条件化来源及后续覆盖、三级调用与影响切片、三轮追问和重启、原版本证据、慢响应头取消和整题截止、未知远程请求不重发、旧索引不暗中重建，以及公开超限/账户更新/月息/文件映射/未实现手续费段落。

构建上传前重新计算逐样本检查与真实耗时；缺题、调试子集、复制 run、缺时间、违反该案例关键结论、供证或完整性断言的答案、超时和旧包均失败。这里核验的是冻结案例的机制与完整性要求，整体业务正确性仍须真实模型和人工评审确认。源码 Python 预跑不能取得原生资格。ZIP、EXE、应用指纹、提交、合同哈希均精确绑定，复制后的产物再核验一次。

正式门禁额外要求同包真实模型报告、逐条人工评审和六项不可删减的现场检查。真实题集至少包含三轮场景、回归题和留出题；后续轮必须实际供应用户及助手历史。评分身份包括答案、用时、实际 prompt/history、policy、gold、源码/框架版本与供证指纹，旧评分不能附到新答案。发布工具默认仅本地检查；缺条件时零 GitHub 调用。只有显式授权及全部通过，才核对草稿提交、tag 与资产 SHA，上传脱敏哈希凭据并转正式状态。

首次工作流的无效 job env 上下文已由固定版本 actionlint 复现、修复并加入编译前门禁。实际 Windows 回归发现的测试日志句柄清理问题已隔离修复，并加入提前合同检查；未跳过该失败项。草稿查询已通过真实只读接口验证，使用 `gh release view` 定位 ID，避免公开 tag 接口无法读取草稿。

## 包与使用

最终 ZIP 为 `COBOLWorkbench-windows-x64.zip`，18,206,353 字节。

```text
ZIP SHA-256: 065f1ea39e1cdd6313b9eb2d826f987af1286ef56bf8de733450df86d2f947e0
EXE SHA-256: 38ccd5e9a4147e4107a18216722eaa59681e4e625e65bda714391d2ec9a412df
应用指纹:   57c4e2319e41549bc312d4846139645fb156a44b8cb0a2481a9f6a51c595f3ad
合同 SHA:  fbdc9ffe0475da02330a06db57e8efc97ff492d2197f11b91760c17567040844
Actions 外层: e556224aafb35bb23deb4effbc2f09e8e492ebaf1899af3fbcebb553471de347
```

本机候选保存在 `release/joint-acceptance/44fcb5f/candidate`，含 ZIP、校验和、BUILD-INFO、原生逐样本报告、合同、候选策略和门禁决定。下载/解压后，打开 `COBOLWorkbench/COBOLWorkbench/COBOLWorkbench.exe`，保留完整依赖文件夹；也可用外层 `OPEN-IN-BROWSER.bat`。应用无需使用者安装 Python、Git 或编译器，沿用现有本机配置与工作台。`release/current` 和正式日常下载入口没有被候选替换。

GitHub 候选 `v2026.10.15.1-rc.1` 保存为未发布草稿，冻结提交与上述应用提交一致；八项附件的大小和 SHA-256 已逐项核对。正式门禁对这一个 ZIP 的实际执行返回退出码 1，原因是 `REAL_MODEL_AND_OFFICE_ACCEPTANCE_REQUIRED`：`candidate_allowed=true`、`publish_allowed=false`。本机同时保存 `WORKFLOW-RECEIPT.json`、`DRAFT-RECEIPT.json` 和 `FORMAL-PUBLISH-BLOCKED.json`。原正式最新版本 `v2026.10.14.2` 保持原状。

维护者真实验收与门禁命令见 [联合发布流程](../joint-release-acceptance.md)。本轮没有调用真实公司模型、外传企业材料或正式发布。目标办公设备 GUI、真实公司接口/CA、实际企业方言与大库业务质量仍须取得同包验收记录；缺失时继续保持候选。
