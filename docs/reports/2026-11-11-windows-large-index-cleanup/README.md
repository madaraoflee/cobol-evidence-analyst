# BUG021：万文件测试的只读连接未显式关闭

真实Windows分片0在原45分钟内完整执行，在线10001分析实际通过21分21.570秒，本地10001通过229.191秒；但最终类清理index.sqlite被未释放句柄阻止，整轮及EXE构建仍失败。

动态追踪关闭GC，仅保留弱引用及字符串/整数、没有追踪器强持有connection或frame。setUpClass中的4个生产索引/导出连接均明确close、存活open=0；执行唯一real_index方法后，唯一存活open来自测试自身with sqlite3.connect(self.database)，context_exit=1、close=0。相同10001fixture只将该查询连接包closing后open=0，原断言全通过，无需GC。

因此最小提案只修改测试的连接管理，不修改生产索引/导出或数据格式，不用gc.collect隐藏泄漏。将与020的Windows路径测试夹具迁移合并为一次具体审批；未批准前仓库原测试仍保持。

2026-11-11 用户批准合并提案，已在原查询连接上应用显式关闭。真实 Windows 重跑结果将另行记录。
