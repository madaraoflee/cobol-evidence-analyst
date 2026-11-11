# 在线长调查的持久化与连接生命周期修复（Windows组件待批准）

已确认的Windows万文件调查仍未完成最终验证。本次在线专属源补丁完整通过5,337项Python与338项浏览器回归；当前Windows解释器SQLite3.50.4不满足新WAL安全门禁，仍保留旧路径，不能把Mac通过或中性200文件实验写成Windows问题已结案。本次没有生成或发布新EXE，共享运行时未替换。

## 判断与对照

最早两项索引缺陷导致重复扫描，已经另按009/010修复。随后真实 [run42877684403](https://github.com/madaraoflee/cobol-evidence-analyst/actions/runs/42877684403) 仍在原45分钟分片门槛中断；在线10,001文件案例运行1871秒仍未完成，栈多次位于capture后的db.commit。没有出现业务断言失败不等于没有bug，也不能把未完成条目记通过。

保留同一中性输入及FULL同步的对照：

|环境与受控方式|200文件耗时|有行变化事务提交耗时|实际结果|
|---|---:|---:|---|
|Windows runner.temp，DELETE两次|6.077/5.931秒|0.999/0.960秒|完整交付；与默认临时目录不是同一机器|
|Windows默认临时目录，DELETE两次|37.123/38.022秒|28.162/28.977秒|提交占76%–81%，单笔约28毫秒|
|同轮默认目录，PERSIST/TRUNCATE|42.066/36.053秒|33.897/28.347秒|没有简单切回滚模式即可解决的证据|
|另一同机顺序实验，DELETE两次|52.003/51.214秒|约41.258/40.596秒|基线漂移约1.5%|
|同一轮，激活连接生命周期的WAL/FULL|17.472秒|8.634秒|完整数据/配置/回滚通过，仍是单actor临时实验|

后一次同机受控WAL耗时较两次DELETE平均减少66.1%，提交时间减少78.9%。这不是用不同机器的最快值拼接。两次默认目录实验明确记录C盘，runner.temp是D盘；早先失败run未记录实际temp路径，所以不能把所有差异都归为盘或断言历史失败一定发生在C盘。全部原始ZIP、API digest及源绑定分别留在 [D盘对照](windows-rollback-runner-temp/README.md)、[默认目录对照](windows-rollback-default-temp/README.md) 和 [激活连接对照](windows-active-keeper/README.md)。Windows actor审计是静态调用链核对，artifact没有逐SQL线程ID动态轨迹；最终keeper关闭后的独立重启当时也未增加，不能冒充已验证。

不支持的方案同样保留：仅关闭时不checkpoint、但频繁重开连接，Mac200文件WAL反而约60秒；仅PRAGMA的未激活keeper也约60秒。完成真实schema读取后生命周期才有效。原样证据见 wal-no-keeper/raw-evidence.zip、wal-unactivated-keeper/raw-evidence.zip、wal-active-keeper/raw-evidence.zip，不挑选只支持优化的结果。

## 在线源修复

新online_sqlite_store只服务独立在线状态；本地结构索引、目录和业务功能代码不使用它。固定且支持所需API的SQLite可复用有界锁保护的WAL连接，始终FULL/自动checkpoint1000，真实schema读取激活，无永久外部进程。旧引擎与缺少API的源码环境默认保留逐次DELETE，显式unsafe WAL拒绝，不能偷偷切换。原超时15/30秒、所有来源、版本记录、动作、待办和旧发布结果保持。

Inventory采用明确可释放、可懒重开的连接。OnlineState默认idle仍逐次DELETE；只有安全引擎且真实analysis lease期间使用受管理连接。最后lease在最终完成状态发表前让SQLite正常checkpoint/清理，不手删WAL/SHM，保持旧测试的精确单文件布局。并发lease、异常、中断和锁超时均有专属回归。共享HTTP文件只增加在线清理失败时释放socket的finally；local online_app=None路径保持原逻辑。

原生产默认的阶段性200文件验证只需5次库存连接（旧观测7,531次），全部分页/导出/原meta/摘要/旧发布回滚保持，见production-smoke/RESULT.json；其源码阶段哈希不能代替最终全量源码清单。最终源码实际执行原10,001文件在线案例，完整通过110.510秒；该数字来自Mac中的完整门禁，并非Windows或真实模型速度承诺。

新代码入库前还发现并修复 [015嵌套整事务回滚](../2026-11-11-online-nested-rollback/README.md) 、[016释放锁超时](../2026-11-11-online-lease-release/README.md) 和 [017收尾错误覆盖新分析](../2026-11-11-online-result-cleanup/README.md)。017保留runner已经返回且通过原校验的新检查点、回答和安全API诊断，单独显示受控存储收尾错误；answered不会提前发布，未知请求不自动重发。它们未进入已发布应用；原before临时JSON被覆盖的缺口在016明确记录，使用精确原源SHA的新重放不冒充原始JSON恢复。

## 旧回归与仍开放的范围

最终 [逐条缺陷报告](BUG-REGRESSION-REPORT.json) 的source_regressions_passed=true、errors=[]；5,337项Python及338项浏览器全部完成，无跳过。执行前后 [源清单](SOURCE-SNAPSHOT.json) 一致（7217d1e635399430cc6aba2b591f1515d5cff33cf6f2493cfa3619176a073ce2），原始日志在 [完整归档](full-regression-evidence.zip)。新增43个Python与4个浏览器测试编号，所有原编号及断言保留；两项原在线测试仅补资源释放及确保新连接能被测量，输入/金标/旧断言不削弱，见fixture-resource-changes.json及existing-assertion-audit.json。本次未修改本地回归测试或时限/成功白名单。017新增8项Python及4项浏览器测试，既有断言不改。之前5,329/334第一轮结果及29项独立合同/证据审计保存在[first-gate-before-result-cleanup](first-gate-before-result-cleanup/independent-evidence-and-contract-audit.json)，不能代替本轮最终源码验证。

[最终36项独立审计](independent-final-evidence-and-contract-audit.json)再次核对全部旧合同、源码绑定与证据一致性，issues=[]。账本301条中292条在源码范围验证通过，9条继续开放，总门禁仍blocked。开放项是原7条（包含首轮失败无回答锚点时普通追问不继承的已知本地缺陷），以及012的Windows长测、014的构建组件风险。原7条不全是现场问题，不能写成都只等公司环境。

官方确认当前Windows3.50.4位于WAL-reset影响范围，本地结构索引也共享该组件。不能直接把临时singleactor方案投产。[具体共享组件例外方案](../2026-11-11-sqlite-runtime-decision/PROPOSAL.md)待用户明确批准：保持CPython同版，仅应用私有SQLite升级到已校验官方3.54.1，并验证全体调用者；不改用户电脑全局Python。SDK草稿只在/tmp做合成单测和真实归档静态检查，尚未装载/替换/构建。未获批准前保留旧runtime，不用此处源码通过替代Windows/最终EXE/公司模型、人工业务和目标设备验收。
