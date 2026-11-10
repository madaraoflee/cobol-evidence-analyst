# Windows 在线万文件持久化耗时仍未闭环

BUG-20261111-012 保持 open。两项查询索引修复已通过源码回归，但不足以使实际 Windows 整轮回归完成，不能把此前的 Mac 结果当作 Windows 已验收。

[Windows run 42877684403](https://github.com/madaraoflee/cobol-evidence-analyst/actions/runs/42877684403) 绑定 main `a46bff1320b41226d15be72ba28e5aa77e29bfb4`、attempt 1，工作区干净。分组0、1、3成功；分组2从18:43:53到19:28:57 UTC达到原45分钟上限。在线10,001文件案例单项执行1871.306555秒仍未完成，记录为 `incomplete`；本地10,001文件案例实际通过，耗时182.463316秒。运行栈多次在 `capture → connection → db.commit`，也有准备读取与连接检查。未出现业务断言失败，不代表没有缺陷。

原检查器实际退出1：5,288项被发现及选择一次，4,671项通过、2项原有POSIX权限限制跳过、1项中断、614项未开始；构建被阻断，没有新EXE、原8候选附件或GLOBAL报告。BUG-20261111-011在本次真实中断中正确工作，未把中断项目误记为通过。

四个原始artifact ZIP均与GitHub API digest逐字节核对。完整报告、日志、metadata、原ZIP及未开始项目保留在 [原始证据归档](failed-windows-regression-evidence.zip)，摘要见 [独立审计](independent-source-audit.json)、[原汇总检查器](WINDOWS-REGRESSION-SUMMARY.json) 与 [资产完整性](artifact-integrity.json)。原发布工作流、79样本合同、测试成功白名单与门槛没有修改，见 [门槛字节核对](unchanged-windows-gates.json)；两条既有跳过装饰器见 [原平台限制](existing-posix-skip-policies.json)。

下一步仅增加独立、手动的Windows中性诊断：区分实际写事务、无事务提交、连接和文件检查的墙钟/CPU成本，同机比较DELETE/PERSIST/TRUNCATE/DELETE回滚日志方式，全部维持FULL同步。诊断不会构建应用、调用真实模型或读取企业源码，也不能代替原完整回归。模式对照必须核对完整公式金标、分页和导出、原发布结果不变以及恢复状态；不能因为速度数字好看就判定数据正确或直接改变生产默认。

SQLite官方将PERSIST及TRUNCATE描述为回滚日志提交方式的文件操作优化；具体平台性能仍须实测。参见 [PRAGMA journal_mode](https://www.sqlite.org/pragma.html#pragma_journal_mode) 和 [原子提交及持久回滚日志](https://www.sqlite.org/atomiccommit.html#section_7_6)。本次不关闭同步、不改变已完成分析的保存要求。

真实公司API、复杂业务语义、最终EXE及目标办公设备验收继续保持各自边界；本次诊断没有替代它们。

## 诊断入库前自检

初稿曾将 `runner.temp` 放在不支持该上下文的job级env中；官方actionlint v1.7.7在入库前拒绝，移到step env并让上传路径直接使用runner上下文后同版检查通过。保留 [初稿失败](diagnostic-preflight/workflow-lint-before.json)、[修后实际lint](diagnostic-preflight/workflow-lint.json) 及 [官方工具校验](diagnostic-preflight/integrity.json)。此问题未进入应用或已提交工作流。

冻结工具在Mac上实际执行200文件四种模式：每种均完整核对200份原文、分页与导出金标、会话meta原始字节/摘要/旧发布结果的受控回滚；每种7,531个连接均确认FULL和所请求模式。见 [smoke摘要](mac-diagnostic/SMOKE-SUMMARY.json) 和 [完整结果](mac-diagnostic/DIAGNOSTIC-REPORT.json)。该结果没有自动选出生产模式，Mac耗时也不能当成Windows优化证据。四项新增源码回归还实际执行3文件四模式金标与FULL检查，并验证错误数量会失败、中断会准确记录、旧输出不会被覆盖；输入较小以避免诊断工具自身拖慢每轮必跑回归。

本轮第一次完整源码执行被本机端口权限拒绝，无法启动若干HTTP测试；发现后仅终止本轮仍在运行的测试进程，保留 [未通过报告](PERMISSION-BLOCKED-REGRESSION.json) 和 [原始日志](permission-blocked-regression-evidence.zip)。它不是通过记录。随后在允许本机端口的环境以相同源码、全部原测试重新执行，结果须单独记录；没有把权限失败改成跳过。

## 最终诊断版本完整源码回归

最终固定源码实际执行5,294项Python与334项浏览器测试，全部通过；执行前后源码快照一致、source_regressions_passed=true、errors=[]。账本297条中289条在源码范围通过，8条继续开放，整体blocked。见 [逐条报告](BUG-REGRESSION-REPORT.json)、[绑定清单](SOURCE-SNAPSHOT.json) 与 [原始日志](final-source-regression-evidence.zip)。新增维护诊断最后来源变化误报按 [BUG-20261111-013](../2026-11-11-diagnostic-final-source-binding/README.md) 修复并另加正反例，因此更早的5293项执行仅作阶段证据 [旧报告](PRE-FINAL-GUARD-REGRESSION.json)，不代替此次最终5294项回归。

此时生产应用的提交方式仍未改变，Windows原失败仍未结案；本次通过只允许使用已验证的独立诊断工具继续定位，不能据此生成或发布新EXE。

最终修复源码变化守卫后的工具（SHA256 `11f5e3494fd5f680c95623d7aa8f0d5b4dfafc4a276ec7908cd44dd0dc957eee`）再次完整执行200文件四模式，10.367秒完成；各case连接FULL与实际模式、完整交付及回滚全部一致，源码绑定未变。见 [最终摘要](final-mac-diagnostic/summary.json)、[最终报告](final-mac-diagnostic/DIAGNOSTIC-REPORT.json) 和 [全部证据](final-mac-diagnostic/complete-diagnostic-evidence.zip)。前文旧工具smoke保留为阶段证据，不混作同一工具字节。
