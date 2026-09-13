# framework-flow-v1：框架控制与请求处理夹具

这是原创、中性、非金额业务样例，用来检查“框架注入的控制流程是否被看到，以及缺失证据是否被如实保留”。它不是生产框架的实现，也不是经过编译运行验证的业务系统。

`source/programs/BATCHJOB.cbl` 只处理一次读取返回的记录：初始化 → 首次读取 → 检查 I/O 状态 → 验证请求状态 → 根据记录处理决定请求更新 → 关闭访问。它是单记录的控制流程切片，不包含整批循环、断点续跑、事务提交或重启协议。`source/programs/SCREENJOB.cbl` 接收请求标识，读取并验证请求，尝试保存，最后选择下一画面。

两个入口的控制逻辑分别来自 `COPY BATCHCTL` 和 `COPY SCREENCTL`。必须读取控制 COPY 中实际的 `PERFORM` 与 `IF`；不能只看 section 编号猜流程。相同的 `4000` 前缀在批处理里指向关闭访问，在联机入口里指向下一画面。`ACCESSAREA` 是两个入口使用的数据 COPY，不是可运行程序。

`ACCESS-STATUS` 是访问接口返回状态；`RECORD-DECISION` 是是否处理记录的决定。访问返回 `OK` 并不意味着业务允许更新：请求状态不是 `PENDING` 时，业务段会写入 `SKIP`。`JOB-STATUS` / `SCREEN-STATUS` 汇总错误；不能把这三种状态合成同一个“返回码”。

## 运行结构审查

在仓库根目录运行（输出目录必须是新的或空的，且不能与源码目录重叠）：

```sh
python poc/run_project_poc.py \
  --source poc/fixtures/framework-flow-v1/source \
  --output .poc-data/framework-flow-v1 \
  --entry BATCHJOB \
  --profile poc/fixtures/framework-flow-v1/profile.json \
  --question "这条请求为什么会被跳过？"
```

使用 `--entry SCREENJOB` 和另一个输出目录审查联机入口。离线启动器记录问题并生成结构证据报告，不会调用模型自动回答问题，不会执行业务代码，也不会修改输入源码。

## 独立验收预期

- 原始程序应保留对数据 COPY 和控制 COPY 的引用；展开后的控制 `PERFORM` 必须同时能回溯到原始 COPY 行及宿主的包含位置，不能把生成文件行号当作原始证据。
- 批处理应观察到 `4000-CLOSE`，联机应观察到 `4000-NEXT-SCREEN`；配置的角色只是声明，观察到 section 存在不等于证明其完整语义或运行顺序。
- 两个入口都调用外部 `DATAACCESS`。其实现刻意不提供，所以数据库写入、I/O 返回值、游标状态、锁和事务结果均未闭环；`ON EXCEPTION` 也不能替代普通返回后的状态检查。
- `profile.json` 中 `FIRST`、`NEXT`、`GET`、`SAVE`、`CLOSE` 的说明是带版本的人工声明。`NEXT` 特意只在声明中存在；不能把声明当作已经到达某次调用的操作值。`FIRST` 声明读首条记录，也不证明实际接口会这样做。
- 配置要求的 `definitions/REQUESTS.lf` 刻意缺失。报告应指出缺失，不得推断逻辑文件的键顺序、筛选范围或生成关系。
- 删除任一控制 COPY、增加同名 COPY 或加入不支持的替换语法时，应显式报告边界，不得继续宣称完整框架控制流程。

这些验收点针对静态结构、来源关联和保守边界，不证明完整业务分析已经完成。
