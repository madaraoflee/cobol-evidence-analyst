# BUG018：私有SDK准备误拒绝标准启动器别名

真实Windows main构建run42928782157的四个分片都在准备阶段失败；源码回归、万文件用例及EXE编译未开始。官方归档1450919字节和固定SHA3完全匹配。prepare在完整源码SDK树扫描尚未返回时拒绝某个链接，因此source_before尚未赋值、source_unchanged为null，不能把它误读为顶层路径发生变化。

两轮独立只读诊断均保留原ZIP、API、GitHub digest与完整观察，不改应用、tools、旧测试或账本的Git对象，不复制SDK或构建。第一轮run42929957675明确反证：source、pin、输出、归档、receipt及Actions环境文件的祖先均无symlink/junction/reparse。第二轮新head/run42930498426进一步完整枚举SDK内部5856条、enumeration_errors=[]，唯一reparse为根python3.exe，tag0xA000000C、attrs0x420，symbolic link而非junction；目标同SDK普通python.exe，withinSDK且samefile=true。关键本体python.exe/python313.dll/_sqlite3.pyd/sqlite3.dll均ordinary；python3.13.exe不存在。

这解释了原严格整树扫描为什么提前退出；不能归因于SQLite3.54.1 ABI、下载、顶层junction或旧业务分析缺陷。官方构建脚本也明确创建python3.exe→python.exe别名：[上游安装模板](https://github.com/actions/python-versions/blob/main/installers/win-setup-template.ps1)。修复方案只针对这个实际证明的标准别名，不删除条目、不修改原安装、不普遍允许链接。

最小补丁已实现并通过源码验证：prepare默认仍拒所有原有链接/重解析点；显式新选项才能接受顶层python3.exe已验证指向同SDK普通python.exe的情况。源前后清单保留原alias身份、目标及hash，私有副本明确从已验证的普通python.exe复制生成普通python3.exe，不跟随任意alias读取外部内容。其余文件完整保留，未知/嵌套/错误目标/发生变化的链接仍失败。自动工作流只有确认当前解释器与所选SDK相同才启用该窄选项；显式source_base仍默认严格。结构化拒绝位置及原始受控诊断单独保存，原错误文案/拒绝条件/旧断言保持。

原始失败和两次正反诊断均在本目录独立归档，原before工具按main55a6的Git对象字节保留。新完整回归已执行5,401项Python和338项浏览器，全部完成、无skip/fail/error，执行前后源码清单一致。真正Windows复制/加载、长测或EXE尚未完成，不能提前记通过；原9条加018继续open，019单独在源码范围修复。

[完整逐条缺陷报告](BUG-REGRESSION-REPORT.json)：source_regressions_passed=true、errors=[]；账本303条中293条源码范围验证，10条unverified，总门禁blocked。源码快照`493835d0dd209ae342de74575599b34faf04d65c85f35043f62a0147cbdc1d56`；原始日志在[归档](full-regression-evidence.zip)。

在此补丁首次上传前，又修复了[019目标链接写保护](../2026-11-11-private-sdk-destination-guard/README.md)，源alias按casefold排除、目标xb独占创建，不沿已存或后来插入的链接写入；原24及首10方法/断言保持。
