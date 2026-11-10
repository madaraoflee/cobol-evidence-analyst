# 中断测试误记通过的回归报告修复

BUG-20261111-011：Windows attempt2 的在线万文件测试被45分钟步骤上限中断，异常栈明确为 `KeyboardInterrupt`，但旧 `TimedResult.stopTest` 仍输出了该方法 `passed`。旧总报告实际为 `runner_error`，总门禁没有通过；错误是逐条结果及日志误标，不能用该条 `END passed` 声称万文件测试自然结束。

原因是 `startTest` 在方法尚未执行时就默认 `passed`。现在初值为 `incomplete`，只有 unittest 的 `addSuccess` 回调能确认通过，且不会覆盖已经记录的失败。原失败、错误、跳过、预期失败、意外成功和子测试分类保留；未修改旧测试、成功结果白名单、时间或请求限制、平台跳过条件。

`before.json` 与 `after.json` 使用同一极小真实 `KeyboardInterrupt` 复现，修后明确记录 `incomplete`。新增3项回归通过；原14项runner合同、17项分片收集合同和25项缺陷门禁合同全部通过。新回归还通过实际子进程执行验证总报告为 `runner_error`，并确认原分片收集器拒绝该结果。完整源码门禁仍由主任务执行，这些定向结果不能替代完整验证。
