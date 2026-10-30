# 全任务修复整合与候选交付核验

本次按整个项目可访问的主线、分支、工作树和历史任务核对修复。累计修复基线 `d993031fae8559a14bc162511cc53d46640f1df6` 已推送 GitHub；此前遗漏的 `codex/adaptive-evidence-retrieval`（`0e082244a28283094b474e3581ca642c7e64e57b`）纳入本次整合。其他 21 个已核对分支引用均已被该基线包含，两个历史工作树没有未提交修复。详细范围与历史测试保留见 [覆盖记录](all-fixes-coverage.md)。

配置修复覆盖重复 JSON 键、无效配置路径、安全错误及配置保存失败的内存回滚；索引与原文修复覆盖编译配置指纹、COPY 身份、过期导航、稀疏解析遗漏和真实供应全文判断。Bitbucket Server/DC 修复路径识别、同提交本地缓存字节校验与重新归档；Cloud 在本地缓存不可读时重新取得材料。任何物理全文判断都不自动代表依赖闭包完整或真实运行语义已验证。

历史分支补入独立检索线索轮转、精确标识优先、命中处裁剪、共享 COPY 词法缓存、框架规则与限制成组保留、正文引用规范化及独立审查→修订→复审。整合继续保留已有自定义回答要求、语言续查、恢复状态、流式显示、长轮询回退、严格请求预算和证据实例追溯。版本升为 `question-evidence-v67`、`business-chat-v55`、`framework-semantics/v1.10`，旧持久状态不能冒充新规则复核结果。

首轮整树 4,606 项 Python 测试发现 3 个合并交互失败，原记录保存在 [首轮结果](first-integration-regressions.json)：具体条件问题和带新人语气的具体判断问题被误扩为业务概览；机器引用编号被计入正文长度，多触发一次复核。均按根因修复，原失败测试保持不变；新增审稿请求还补齐当前用户回答要求。最终整树 4,612 项 Python、272 项网页及 1 项工具测试全部通过；四分片测试清单和测试源码哈希核对一致，详见 [最终源码回归](final-source-regressions.json)。同一源码 HTTP 补充预演 22 项通过，合成请求 15 次。冻结提交及 Windows 原生验收以候选包记录为准。

本机检索探针的 6 个合成案例均找回指定原文并通过加载充分性检查（[结果](retrieval-routes.json)），只代表这些指定问题，不是生产问答准确率。共享 COPY 的 1,000 行、1/4 调用方测试中，缓存开关的源码映射与语义输出完全一致；4 调用方的热路径中位耗时由 0.133 秒降至 0.045 秒，冷路径没有改善（[结果](copy-cache.json)）。这不是实际大库或真实模型的性能承诺。

最终 Windows ZIP 仍须独立通过原生 79 样本及同包补充检查。源码预演只帮助发现问题，不代替原生运行。当前没有新的真实模型、人工评分或目标办公设备验收；正式发布仍须经 `tools/publish_desktop_release.py` 的同包联合门禁，缺项只能交付候选，不替换现有正式版本。

## 已交付 Windows 候选

[下载 v2026.10.30.1-rc.1](https://github.com/madaraoflee/cobol-evidence-analyst/releases/tag/v2026.10.30.1-rc.1)。最终源码提交为 `e9852b590498955e802f80705931d3e8ead32170`，包含完整合并 `3b1d445` 及 Windows 测试夹具修复。GitHub 已确认 [历史 PR #11](https://github.com/madaraoflee/cobol-evidence-analyst/pull/11) 以该完整合并提交进入主线。

[Windows run 41053545367](https://github.com/madaraoflee/cobol-evidence-analyst/actions/runs/41053545367) 已成功：4,615 项 Python 测试恰好运行一次，其中 4,613 项通过、2 项仅适用非 root POSIX 权限语义的测试按预期跳过；网页 272 项通过。最终 EXE 通过 79 个原生样本，另通过 22 项同包补充检查、15 次合成请求，其中独立审稿链准确执行生成、审稿、修订、复审四步。

下载后的 ZIP、EXE、BUILD-INFO、合同和所有报告已重新校验。8 个上传资产的 GitHub SHA-256 与本地字节一致，见 [发布收据](release-receipt.json)。ZIP SHA-256 为 `956ba29ab4752f69d8d8d50dd4d47c294484108184db2603c732353bf5df1f59`。候选门禁重算为 candidate_allowed=true、publish_allowed=false，原因是仍缺同包真实模型、人工及目标设备验收；Release 明确为 prerelease，latest 正式版仍为 `v2026.10.14.2`。

同次 Windows 大仓库合成案例冷索引用时 408.275 秒；首个完整问题 51.337 秒（含额外范围探测），后续四次约 25.06–25.77 秒，均通过冻结门槛。这些是指定合成案例的实际耗时，不是生产大库或真实模型的性能承诺。

办公室验收工具已绑定 app_commit=e9852b5、candidate_run_id=41053545367、candidate_artifact_id=12977014535 手动启动 [run 41059408541](https://github.com/madaraoflee/cobol-evidence-analyst/actions/runs/41059408541)，现已成功完成：独立助手编译和实际 Tk 启动通过，冻结同一 EXE 的 79 样本重新运行全部通过，报告运行身份与原报告不同。包内应用字节、源码提交及合同与候选一致；[办公室核验记录](office-verification.json) 记录六项上传资产的哈希。办公室工具和报告已补充到同一个候选 Release，原应用报告未覆盖。CI 构建工具不等于目标办公设备验收。
