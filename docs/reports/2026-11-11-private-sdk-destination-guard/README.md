# BUG019：新别名物化的destination链接保护

这是018未入库补丁在交叉审查中发现的新边界，不是已发布应用的故障。WindowsPath比较忽略大小写，原排除名单却区分大小写，可能留下大写拼写的链接。copy2的follow_symlinks=False只控制源；一旦destination仍是链接，它会尝试沿链接写。中性临时fixture实际确认destination与host假SDK同file、copy后仍为link且host ctime改变；内容相同、mtime会被copy2复原。没有执行真实SDK写入或声称真实Windows损坏。

修复对排除名单统一casefold；物化内容来自已经验证SHA的私有python.exe缓存字节。destination首先通过原no_links并确认不存在，再以xb独占创建，写已打开的句柄，不再调用目标路径的copy2/copyfile/copystat/chmod。已存在的普通文件、残留link、检查后插入的link均失败且不修改host或外部目标。

新增5项回归验证大小写、残留link、guard后与非存在检查后的两处注入窗口、已存普通文件不覆盖；原24工具测试和018前10方法AST/断言保持。39项同进程0fail/error/skip。before/after源与中性证据、完整IDs/哈希在本目录；原始审查也保留。完整全部旧缺陷/源码测试仍需本轮门禁，Windows真实SDK复制和最终EXE另行验证，不能由该fixture冒充。

随后[完整5,401/338源码门禁](../2026-11-11-private-sdk-path-compatibility/BUG-REGRESSION-REPORT.json)全部完成，无跳过/失败；source snapshot未变。该结论不替代Windows真实SDK及EXE。
