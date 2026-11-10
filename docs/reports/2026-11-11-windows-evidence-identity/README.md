# Windows供证夹具的原文字节身份修复

Windows run `42863059793` attempt 2 的0号分组发现 `test_repository_formula_inventory` 四失败、一错误。应用正确拒绝了错误来源哈希：测试helper按输入LF字符串计算SHA，而 `write_text` 在Windows实际写出CRLF文件，索引按真实字节记录了另一SHA。

在Mac强制CRLF写入后精确复现同样四失败、一错误，见 `before-all16-forced-crlf.log`。只将helper改为对写后 `read_bytes()` 计算原文SHA，显示文本仍按规范换行读取；原16项测试方法的AST、全部断言、输入及gold未改，应用来源校验未改。修后16项全部通过，见 `after-all16-forced-crlf.log` 及 `contract-preservation.json`。

新增两个回归分别验证LF/CRLF拥有各自真实原文字节身份，以及错误哈希在两种换行下都仍被拒绝。该修复登记为 `BUG-20261111-008`，不以放松身份校验或改预期来让Windows变绿。

完整本轮回归和新的Windows构建结果另行提供。先前main构建的失败尝试保持原样，不冒充候选验收通过，也不与新尝试的部分结果拼接。

修复后已重新执行全部5,281项Python和334项浏览器测试，全部通过，没有失败、跳过或预期失败，执行期间源码与合同快照未变。292条记录中285条通过源码回归，原7条仍开放，因此报告如实保持 `blocked` / `source_regressions_passed=true` / `all_recorded_bugs_verified=false`。见 [逐条结果](BUG-REGRESSION-REPORT.json)、[汇总](verification-summary.json) 及 [全部原始日志](full-regression-evidence.zip)。

Windows已有两条历史POSIX权限专用用例按原有decorator不适用于Windows；这些用例在本轮Mac完整回归实际通过，不能将Windows skip说成通过。新的Windows同包验证将绑定新的合并提交，不能复用失败构建的部分结果。
