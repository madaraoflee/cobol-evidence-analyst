# 详细业务回答与 Windows 验证版

本轮把默认体验恢复为充分展开的业务解释：先给结论，再讲输入、条件、处理顺序、字段变化、异常和结果，并附源码依据。取消了按段数压缩详细答案的做法；提速主要来自先完成必要取证、复用相同证据，以及取消仅因局部限定措辞触发的整篇重写。

冻结产品源码为 `9cb884f8c89ebc928d56179ce5cb2874f38508a6`。结构证据补充了调用实参和传递方式、明确归属的 COPY 下级字段、SQL INTO 输出及 EVALUATE 分支优先级。字段归属按文件版本和引用实例绑定，同名、冲突、截断和改写式 COPY 保留为未绑定；新增观察有全局体积上限。以上步骤均在本地执行，不增加模型校验请求。

## 实际回答与耗时

本轮使用已配置的同一模型、公开合成源码、空框架资料输入，完整运行 14 题。独立代理按源码审读主问题及附加正文；全部 82 个运行模块与冻结提交匹配，11 组运行前后源码和题目哈希一致。题目属于开发集，评分是代理复核。

| 范围 | 主问题通过 | 整篇通过 | 题目耗时合计 | 模型请求 |
|---|---:|---:|---:|---:|
| 上一交付的原 8 题 | 7/8 | 6/8 | 307.224 秒 | 12 |
| 本轮相同 8 题 | 8/8 | 8/8 | 238.483 秒 | 11 |
| 本轮全部 14 题 | 14/14 | 11/14 | 485.621 秒 | 22 |

原 8 题本轮总耗时下降 22.4%，请求体积由 893,590 降至 852,530 字节。这是单次运行的观测；全部 14 题有 53 个必要项、36 条禁止断言通过，14 题均保留详细解释。整篇评分仍记录 3 个失败：两题把 SAVE 前的审批状态延用到返回后路由；另一题在动态调用失败的附加说明中混淆了计算字段所属阶段。另有 3 题出现局部术语或续写展示问题。当前包定位为验证版。

原始运行和逐项审读保存在 `.build/quality-syntax/detailed-field-evidence/`，全文评分见 `reviews/agent-review.json`。此前所有尝试保留；较早结果见 [历史验证记录](2026-10-07-quality-speed-windows.md)。对原版 `35eed48` 补做的统一全文审读为 5 pass / 1 unclear / 2 fail，另存于 `.build/quality-syntax/reviews/baseline-full-answer-corrective-review.json`，不覆盖旧报告。

独立的 16,384 token 预算试验耗时 25.495 秒、1 次请求，但实际只用了 5,296 个 completion token，且仍有错误的附加路由，因此没有调整产品输出上限。该试验与完整评测末段有一次请求重叠（validation 组末尾约 0.35 秒、probe-copy 和 probe-complex 开头）；耗时数据按观测记录，未据此作受控因果比较。试验在 `.build/quality-syntax/detailed-budget-trial/` 单列。

## Windows 交付与检查

本地完整回归通过 2,083 项，耗时 72.140 秒。Windows 原生构建使用同一冻结源码：[构建记录](https://github.com/madaraoflee/cobol-evidence-analyst/actions/runs/37558697415)。

Windows 完整回归共 2,083 项，其中 2 项按 POSIX 条件跳过，结果通过（380.292 秒）；MSVC 编译、源码浏览器入口和编译后二进制自检通过，自检模型调用数为 0。实际打包合并提交为 `d1e0ae42fb962c69dd5f4d8ae6775f13d524048c`。ZIP 完整性、包内外构建记录、x64 PE 标识和 166 项文件排除检查通过。

验证包：`release/windows-x64/run-37558697415/COBOLWorkbench-windows-x64.zip`（16,678,709 字节）。SHA-256：`b475e85683b2c00148d7b9525e9aaed81f13e95242ce9264c6d1c0d63681392b`。同目录的 `LOCAL-VERIFICATION.json` 已关联本轮业务评审，状态为 `engineering_verified_quality_evaluated_with_open_findings`。

使用时关闭旧版，解压完整 ZIP，保留 EXE 和依赖目录一起运行 `COBOLWorkbench.exe`；升级已有 Windows 安装时，沿用该电脑用户数据目录中的 API 配置和历史记录。
