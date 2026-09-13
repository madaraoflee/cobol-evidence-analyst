# Smart Developer 框架对齐：先恢复控制来源，再解释业务

更新：2026-09-12。本页保留 **F01 框架接入与证据基线的历史交付**，下文“下一步”指当时计划。后续 F02 已完成支持范围内的路径接线、F03 已交付可执行契约子集，最新效果见[框架路径与运行契约](./14-framework-paths-and-runtime-contracts.md)。均不代表 Smart Developer 全兼容或完整业务分析验收。

## 这次改正了什么

根据 GPT 中《寻找 Smart Developer指南》和《POC实施步骤》，分析对象必须从“程序中的计算”扩展到“框架、程序、生成数据接口和配置共同形成的业务流程”。用户在聊天中确认了批处理控制 COPY、编号 section、Data Set 和封装 I/O 的实际存在；公开研究不能替代公司内目标版本的实现契约。聊天里的附件没有可读取的文件，本次使用的是可见聊天内容，并重新核查下列公开来源。

`COPY MAINB` 不能被当成运行时调用一个独立模块：COBOL 的普通 COPY 是编译前的文本包含，可插入过程代码。已按这一原则增加独立过程展开视图；本实现只支持无替换、独立一行的 COPY 子集，不是完整预处理器。[IBM COPY 说明](https://www.ibm.com/docs/en/i/7.5.0?topic=statements-copy-statement)

公开开发笔记提供了批处理控制 section、联机控制、Data Set 与生成接口的调查线索，但并未证明用户生产版本的行为。MAINB / MAINF / MAING / MAINP 必须按实际代码和版本分别接入，不能按名称或相同编号推导含义；公开资料未给出完整重启实现。本项目不复制其中带专有或保密标识的源码。[开发标准笔记](https://www.cnblogs.com/starcrm/p/5867793.html)、[Data Set 实践笔记](https://www.cnblogs.com/starcrm/p/5833276.html)

## 已实现的第一层

| 组件 | 实际输入与输出 | 不能据此得出的结论 |
| --- | --- | --- |
| 通用离线入口 | 任意获准源码目录、精确入口程序、可选问题及版本 profile → 原始索引与审查报告 | 问题已被模型回答、旧计算演示已变成通用分析 |
| COPY 展开器 | 主程序和 COPY → 带原文件哈希、原行号、逐层包含位置的派生行 | 与目标编译器等价、替换/续行/所有方言已经支持 |
| 框架审查 | 派生行 → 实际 PERFORM 引用、定义候选、CALL 目标及证据 | 分支必然到达、循环次数或调用顺序已证明 |
| 版本 profile | 入口模式、控制 COPY、section 角色、I/O 功能、两类状态及所需资料 | 配置声明就是运行事实、已解析实际功能值或文件内容 |

原始索引和派生过程观察目前分开交付。派生证据保留 COPY 与宿主的双重来源，不伪造为原始 SQLite Evidence ID，也没有自动送入原有错误路径引擎或模型工具。这个取舍保护了已有证据身份，代价是 F02 仍需完成接线。[ADR-0008](./adr/0008-framework-source-and-contract-boundaries.md)

展开缺失、同名歧义、循环、REPLACING / REPLACE、续行、条件编译或预算截断时，保留包含证据与原因，**停止输出该入口的过程观察**；不跳过未知预处理后声称恢复了宿主控制逻辑。文件的存在只说明“已采集”，不表示 DDS、LF 或调度含义已解析。

## 能看的非金额案例

原创样例 [framework-flow-v1](../poc/fixtures/framework-flow-v1/README.md) 包含两种入口。批处理 `BATCHJOB` 的控制 COPY 引用了初始化、读取、校验、更新和关闭 5 个 section；联机 `SCREENJOB` 的另一份控制 COPY 引用了初始化、读取、校验、保存和下一画面 5 个 section。批处理的 `4000-CLOSE` 与联机的 `4000-NEXT-SCREEN` 不会因为编号相同而共享语义。

例如批处理初始化调用的证据在 `copybooks/BATCHCTL.cpy:2`，同时保留 `programs/BATCHJOB.cbl:9` 的包含位置；目标定义位于主程序第 11 行。报告中的表格是源码引用表，**不是运行轨迹**。该样例只有一轮记录处理，不代表完整批次循环。

两个入口调用的外部数据接口均未实现，LF 定义也故意缺失。报告将 I/O 状态与记录处理决定分开声明；即使源码先写了某个功能值，当前仍把调用到达时的功能值标为 `not_resolved`。不能由“配置里列了 NEXT”推导程序实际执行了 NEXT，更不能据此判断漏首条、共享游标、锁或提交。

## 在批准环境中使用

Python 3.10 或以上；仅标准库，无需模型、网络或 COBOL 编译器。以下从项目根目录运行，源码与输出必须是不相互包含的目录，输出须新建或为空：

```sh
python3 poc/run_project_poc.py \
  --source poc/fixtures/framework-flow-v1/source \
  --entry BATCHJOB \
  --profile poc/fixtures/framework-flow-v1/profile.json \
  --question "哪些请求会被更新？失败后如何处理？" \
  --output .poc-data/framework-batch-v1
```

Windows 可用 `poc\run_project_poc.bat` 传相同参数。改变 `--source` 和 `--entry` 即可选择自己的获准语料；不再依赖保费字段、预置程序名或固定证据行。`--extensions` 指定带后缀文件的选择范围，无扩展名成员始终包含，清单、原始索引和展开使用相同范围。

输出含 `structural-index.sqlite`、`framework-report.md`、`framework-report.json` 和 `inventory.json`。问题只被记录，尚未自动回答。运行器拒绝覆盖既有输出，并核对索引与观察的源文件清单/哈希；不是原子快照服务，仍应使用稳定的源码导出目录。失败时保留局部输出供排查，不自动删除，修正后使用新目录。

**SQLite 含完整已索引源码，报告包含标识和摘录定位；所有输出按公司源码同级保管。** 私有 profile 也只留在批准环境。公开代码、配置、注释和夹具使用中性名称，不能把本页讨论的专有标识复制进公开适配器。

## 后续以什么为验收标准

F02 将控制 COPY 的派生结构接入路径与证据工具：先证明目标业务中受支持的控制条件、section 路径和错误返回，保留双重来源，缺控制源码时不做闭合结论。

F03 再接版本化的数据与运行契约：Data Set → PF/LF → key / select-omit / join / record format → 生成接口；区分 I/O 状态和框架记录控制状态，证明到达 CALL 的功能值，再审查游标、锁、事务及重启。BGEN 与 BEGN 的拼写和语义必须单独核实，不能自动纠正；同一 I/O 程序也不能自动视为共享同一 ODP。Job / CL、调度及配置路由应与 COBOL 控制图共同组成业务入口。

这些是下一步的证据义务，并非本版能力。真实问题的输入、资格、状态转换、数据筛选、跨程序参数、错误最终结果和必要计算仍须逐项验收，最后才进入模型问答和界面交付。当前进度统一看[进度总表](./12-task-plan-and-progress.md)。
