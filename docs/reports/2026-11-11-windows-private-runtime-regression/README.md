# 真实Windows私有组件及完整源码回归（原样失败记录）

run42934769797、attempt1、main11a6f9be93492b5050c6895ade8c49b0d1c80417。四个私有SDK均实际加载官方SQLite3.54.1，DLL SHA256及sourceid与固定pin一致，八项内存功能全部通过；宿主SDK5856逻辑条目、启动器alias身份及target均before==after。该事实已真实验证，不是模拟PE或Mac结果。

5400个Windows poc测试恰好全部启动并执行一次：5396 passed、两个error方法、两项原有POSIX平台skip；raw errors共5条，含四处Path.rename的WinError32及一处类清理WinError32。原汇总检查器不修改白名单/时限，准确返回exit1/status failed。构建依赖未通过，Compile skipped，没有EXE或候选。

包含万文件验收的source0总2665.267秒（44分25.267秒），在原45分钟内全部执行结束，无timeout或runner中断；online10001完整分页业务清单实际passed1281.570秒，本地10001passed229.191秒，所有七项万文件金标/索引/交付检查均passed。真实全公司源码、模型时延与质量及目标设备启动仍未被这个中性数据和注入模型替代；不能说所有分析都快或已彻底无bug。

[SDK实证](sdk-verification-summary.json)、[独立精简审计](source-verification-summary.json)、[原检查器拒绝结果](WINDOWS-REGRESSION-SUMMARY.json)、原ZIP/API/digest/完整四job日志在本目录归档。4个文件替换错误属于020测试先决条件；清理错误经GC禁用/弱引用追踪确认是021测试自己只退出事务、未close只读查询连接。生产SDK/索引/导出没有被这些测试错误冒充回退；同样也不能把测试错误忽略后发布。

两项缓存fixture的Windows适配、一条显式closing及新增两生命周期回归已在/tmp形成可审阅提案；仓库旧测试未改，待用户依测试合同明确批准。随后全部旧回归和Windows构建必须重新执行，原失败和原POSIXskip保留。
