# 待用户决定的测试夹具迁移

提案只改3个既有测试方法的准备/清理方式并新增2个生命周期回归；生产代码、SQLite共享方式、数据格式、时限及跳过规则保持。全部旧assert/fail AST及出现次数保留，见assertion-preservation.json；完整可审阅补丁APPROVAL-PROPOSAL.diff，3份文件也已独立快照，不随后续/tmp编辑改变。

两项在线缓存测试：可触发平台继续原真实改名/符号链接/换inode；Windows先实际证明OS以WinError32阻止替换且原inode/数据/integrity不变，再对同一真实缓存连接仅注入受控路径观察变化，仍必须在任何SQL和新连接前拒绝、旧连接关闭、目标不变。没有skip/xfail/提前返回算过。

一项万文件索引查询：只用closing显式释放现有只读SQLite连接，SQL、数据及所有旧断言保持。真实Windows清理报index.sqlite占用；GC禁用、无强追踪器的动态追踪确认4个生产索引/导出连接已close，唯一未close来自该测试自己的with sqlite3.connect。

新增两项真实库存SQL删除/版本更新/close后实际rename恢复回归；15项Mac提案检查和受控Win错误模拟已通过，额外索引owner追踪与closing对照也已完成首轮，非真实Windows适配后验收。后续还要完整所有旧回归及真实Windows/EXE，原失败不改成通过。

项目docs/bugs/README.md明确要求“测试确需调整时，记录原因、保留等价或更强的覆盖，并由用户明确决定历史合同的迁移。”这里只申请此测试范围；组件升级和生成EXE已获授权，无需重复批准。仓库原测试目前尚未修改。
