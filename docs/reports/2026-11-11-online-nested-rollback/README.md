# 在线连接缓存的嵌套回滚防护

BUG-20261111-015在新在线连接管理代码入库前发现。原SQLite触发器RAISE(ROLLBACK)会取消整笔事务；嵌套清理随后尝试ROLLBACK TO已不存在的savepoint，覆盖原IntegrityError为OperationalError。调用者若捕获内层错误后继续写，外层还可能把后续写入提交。见 [实际修前复现](before.log)。这不代表既有已发布版本或公司资料发生了此问题。

修复须保留原异常，将整笔外层事务标为不可提交；退出时回滚并拒绝成功提交，随后允许干净重开。正常savepoint回滚仍允许外层按原意继续。新回归及完整验证待补；当前条目不算通过。

修后真实trigger及INSERT OR ROLLBACK均保留原错、强制外层回滚并允许下笔独立事务干净重开；rollback/close/注解API故障也不覆盖原异常。对应三项精确回归和全部5,329+334已在 [最终固定源码门禁](../2026-11-11-online-managed-storage/BUG-REGRESSION-REPORT.json) 通过。本地核心与共享组件未变，Windows新组件/EXE仍待批准和验证。
