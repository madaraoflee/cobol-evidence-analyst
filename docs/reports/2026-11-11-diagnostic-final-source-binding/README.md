# 诊断结束时观测到源码变化却仍返回成功

BUG-20261111-013在维护诊断工具入库前发现；不影响已发布应用，不改动生产数据库策略。

真实3文件生产流程完成四种日志模式后，仅将源码绑定函数的三次观测注入为“初始、初始、变化”，即可复现原工具返回0、整体状态completed_diagnostic，同时最终source_unchanged_in_declared_scope=false。四个case的数据金标都通过，说明缺陷在最终状态判定，而非公式交付。见 [实际复现](boundary-reproduction.json)、[原始报告](BEFORE-DIAGNOSTIC-REPORT.json)、[日志](reproduction.log) 与 [原复现脚本](reproduce.py)。三文件及观测注入仅用于复现该控制流，不冒充真实源码变更或Windows速度验收。

修复将正常成功返回移到finally之后：最终观测若变化，先持久化失败状态，再返回非零。原异常继续传播，中断继续返回130，旧错误和未完成证据不被成功覆盖。新增同一真实3文件四模式的稳定观测正例与末次变化反例；原四项诊断测试仍须通过。修后完整源码回归、冻结工具200文件实测及Windows诊断分别另行记录，不使用本次单项复现替代。

修后5项诊断回归全部通过，最终完整5,294项Python及334项浏览器回归通过；最终源清单与 [完整回归](../2026-11-11-windows-inventory-cost/BUG-REGRESSION-REPORT.json) 绑定。最终工具200文件四模式同样通过，见 [同字节实测](../2026-11-11-windows-inventory-cost/final-mac-diagnostic/summary.json)。整体仍保留8条开放问题，不因诊断工具守卫已修复而关闭Windows持久化耗时或公司现场问题。
