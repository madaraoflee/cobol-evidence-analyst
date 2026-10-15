`01f2780` 的 Windows EXE 与便携验收包已构建、下载并独立核验，可以交付公司电脑操作。公司真实模型、目标 Windows 操作及人工业务评分尚未完成，正式发布仍被门禁阻止；正式版本保持 v2026.10.14.2。

应用与辅助工具都冻结于 `01f27806e3254315e5744ad962b4e0ffbfdf5a38`。[桌面流程38652222418](https://github.com/madaraoflee/cobol-evidence-analyst/actions/runs/38652222418)和[现场包流程38657542417](https://github.com/madaraoflee/cobol-evidence-analyst/actions/runs/38657542417)均成功。后者的 Artifacts 中下载 `Windows-office-acceptance-kit-01f27806e3254315e5744ad962b4e0ffbfdf5a38`（artifact 11938331857），解开外层后取得 `office-kit-delivery/COBOLWorkbench-office-acceptance-windows-x64.zip`，再完整解压该套件。无需安装 Python、Git、Java 或编译器。

| 产物 | SHA256 |
|---|---|
| 现场验收 ZIP（49,005,719字节） | `c30f39b34b917e730c1340d5f6c18d0a5398173ab5a73ce0331dd31bae8f40bd` |
| 原应用 ZIP | `6a307edd71075333a92191ee7c7e7ad2332016f5b8533212fb2f9c40f42f9d2e` |
| 原应用 EXE | `17091604073c503e8e31e5661b44a5b8be7d972d4f2b95a67945dabd54ff57c0` |
| 辅助工具 EXE | `ecc54ec1199b90d9ff2c0d59a12a3c7d948f9a8970af0ff7a61ae3a855bdc864` |
| OFFICE-KIT.json | `ada60d13e1405ceadc277e25c23c41eeea72a4f210525e7a05a6b42861b11abb` |

独立核验包括 ZIP CRC、两份 x64 PE、全部套件文件哈希、原应用 ZIP/EXE 字节一致、12,043份冻结源码、35轮真实题集和最多105次请求、辅助工具实际 Windows Tk 自检。原 EXE 已在桌面流程通过55主清单行及14范围probe，再由交付向导原样启动独立复跑全部样本，执行ID与主运行不同；新舍入相关性断言两轮均3/3通过，全部旧必需断言及预算保留。源代码完整回归Windows2716项（跳过2项）、浏览器166项通过；本机完整2716项无跳过通过。

同一EXE的两次大库冷导入为285.496、410.635秒，主问范围7.518–8.664秒；带范围probe为12.938–14.548秒。两次均在原门槛内，但只有本机合成接口、单次冷导入和顺序重复，不能作为公司模型、公司设备或稳定吞吐结果。两次55行均为24 ANALYZED、25 PARTIAL、6生命周期；它们是机制验收，不能当作49个自然语言答案已正确完整。[最终40项审计](2026-10-15-final-exe-01f2780.md)及其逐项JSON保留真实缺口，没有改写旧运行或评分。

公司电脑按[完整操作步骤](../office-acceptance-steps.md)执行：双击 `OfficeAcceptanceKit/START-ACCEPTANCE.cmd`，报告目录选包外新目录；先生成无网络计划，再做本机离线回归；随后选择获准的本地 `.env` 和 CA，在已有授权和预算内运行35轮真实问答（自动题集最多105请求）。窗口逐题显示实际回答、实际供证与金标准，35轮全部按finding评分，每个PARTIAL说明具体未知和受影响结论；实际操作后再确认六项设备检查。手动历史证据、重启和取消实验只在已有额外授权内进行。远程结果不确定时立即中断，不继续后续收费题、不自动重发，不宣称exactly-once。

配置、密钥、回答、源码节录与原始评审留在获准公司环境；不上传整个报告目录或企业源码。可先回传获准的门禁决定与错误代码。没有原始材料外移授权时，维护者须在获准环境执行发布核验。只有同包候选回归、同机回归、全部真实问答及供证/速度/逐finding质量、六项现场确认共同通过，才可由 `tools/publish_desktop_release.py` 重算门禁并正式发布。

本轮真实/收费调用为0，真实模型题集未执行、未评分、公司现场未验收。套件不会上传或发布。唯一发布入口已实际dry-run返回 `gate_rejected / publish_allowed=false / published=false`；没有创建新正式版、替换已发布版本或用CI结果代填现场表单。
