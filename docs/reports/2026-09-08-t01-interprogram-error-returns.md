# T01：跨程序错误返回闭环验收

日期：2026-09-08 ｜ 结论：**受支持源码模型范围内通过；P2 仍进行中，T02 未开始。**

这次补上了原来最关键的连接：分析不再在子程序正常返回时，把引用输出一律变成未知，而是进入子程序的源码控制流、带入参数、沿普通返回继续调用方，并检查入口最终值。报告可以连续解释“错误在哪一层返回、经哪些参数传递、最终是否清零、返回后有没有再次覆盖”。这不是 COBOL 运行器，也没有接通模型问答或界面。

## 一个可以直接核对的例子

复用未改动的 v4 五程序样例：入口分别处理 A/B，包装程序调用共享叶子，结果映射程序输出分支结果，汇总程序给出最终状态。主样例仍为 5 个程序、8 个调用上下文，没有通过增加程序数量代替补齐语义。

在明确假设各次 CALL 可用的模型实验中，令 A 输入为 0、B 输入为 1：

| 实际模型步骤 | 状态如何传递 |
| --- | --- |
| 叶子正常返回包装程序 | `LEAF-CODE=21 → LEAF-STATUS=21`，REFERENCE 回写 |
| 包装程序正常返回入口 | `WRAP-STATUS=21 → STATUS-A=21`，REFERENCE 回写 |
| 入口调用结果映射程序 | `STATUS-A=21 → FINAL-STATUS=21`，CONTENT 传入 |
| 结果映射程序返回入口 | `FINAL-CODE=21 → CODE-A=21`，REFERENCE 回写；A 输出为 0 |
| 入口调用汇总程序 | `CODE-A=21 → LEFT-STATUS=21`，CONTENT 传入 |
| 汇总程序返回入口 | `JOIN-STATUS=21 → SUMMARY-STATUS=21`；总额为 0 |

这六步来自同一条实际生成的模型见证，并检查了调用上下文及参数值，不是用预写程序名串出的一段说明。CONTENT 传入的状态可以被读取、再赋给另一个引用输出；**这不等于 CONTENT 参数本身能够反向写回**。

B 不会被误拼成 A 的错误 21。当前模型保留其正常状态 0 与溢出状态 24 两个备选，正常金额仍未知。输入 1 并不意味着实际会溢出；T01 没有实现算术可达性与精确计算。镜像 B 错误案例也验证了汇总的左侧错误优先规则，而不是强行让每个错误都成为最终状态。

## 正例、反例与退出标准

| 验收项 | 本轮实际结果 |
| --- | --- |
| 双路业务错误、A 单侧错误、B 单侧错误 | 3 个案例、30 项检查通过；包括六步返回记录与全部记录的入口终态 |
| CONTENT/VALUE 中间层隔断 | 6 项检查通过：叶子局部为 21，入口仍分别为 7/8；核对正确父上下文，不能换到另一支路凑数 |
| 调用者在结果映射返回后写入 7 | 临时源码副本的原安全验收为 **FAIL：5 项失败**；定位到入口覆盖语句和非零最终值，这是应有报警 |
| 子程序状态从 21 改为 22 | 同一数据库重新索引后，快照与入口状态同步改变，不沿用旧返回结果 |
| 递归、未决目标、缺证据、预算截断 | 独立反例拒绝给出完整返回结论；缺失证据会明确报错，不保留过期成功 |
| 普通返回与特殊存储边界 | CALL 失败与正常业务错误分开；callee STOP RUN 不当作 GOBACK；别名、共享存储与未绑定 LINKAGE 使用不假装支持 |

全量回归 **438 项通过**，ResourceWarning 按错误处理；新增 61 项测试方法，包含 12 项核心测试、21 项独立对抗测试、9 项独立样例测试和 19 项演示验收测试。案例内的多项断言不另加到测试方法数中。这些都是源码分析程序的测试，不是 438 个真实业务案例。

默认接口还探索调用失败备选：该 v4 样例得到 2,582 个模型状态、201 个入口退出和 50 个事件，没有探索边界或探索截断。默认最多展示 64 条见证，因此该运行的见证展示会明确截断；全部 201 条入口退出及事件统计仍保留。主报告的三个受控案例只有 1/2/2 条入口退出，不存在这项展示截断。

## 独立审查实际改正了什么

第一处是未绑定 LINKAGE：原实现可能把没有建立地址的 LINKAGE 项当成普通本地字段，写零后声称确定返回。现已把未绑定项排除出数值模型，读写或用于 CALL 实参时停在边界；根入口只假设 PROCEDURE USING 列出的参数已由外部提供。未使用的声明不一概封杀。该边界符合 LINKAGE 只描述外部存储、非 USING 项需要另行建立地址的规则。[LINKAGE 说明](https://www.ibm.com/docs/en/cobol-zos/6.4.0?topic=overview-linkage-section)、[地址建立说明](https://www.ibm.com/docs/en/i/7.5.0?topic=program-setting-address-linkage-section-items)。

第二处是演示验收的错支路风险：只看“叶子没有回写”和“某根字段仍为 8”，不足以证明它们属于同一参数链。现已核对叶子→中间层→根的父上下文参数关系和有序返回记录；故意把 CONTENT 的根字段换成 VALUE 支路字段时，验收失败。报告也不再把 CONTENT 返回时的局部值画成反向赋值。

代码理解技能在本项中的作用是把“源码调用关系”“参数身份”“实际生成的返回路径”分开验证；没有把调用图或字段同名直接升级成返回值证明。

## 打开结果或自行复现

本地生成结果不纳入公共源码：

- [主样例报告](../../.poc-data/error-return-v5/result.md) 与 [机器结果](../../.poc-data/error-return-v5/result.json)。
- [复制隔断报告](../../.poc-data/error-return-v5-copy/result.md) 与 [机器结果](../../.poc-data/error-return-v5-copy/result.json)。
- [故意覆写的报警报告](../../.poc-data/error-return-v5-regression/result.md) 与 [机器结果](../../.poc-data/error-return-v5-regression/result.json)。

从项目根目录运行：

```text
python poc/error_return_demo.py --database .poc-data/error-return-v5/structural-index.sqlite --json-output .poc-data/error-return-v5/result.json --markdown-output .poc-data/error-return-v5/result.md
python poc/error_return_demo.py --profile poc/fixtures/error-return-v5/copy-boundary/profile.json --database .poc-data/error-return-v5-copy/structural-index.sqlite --json-output .poc-data/error-return-v5-copy/result.json --markdown-output .poc-data/error-return-v5-copy/result.md
python poc/error_return_demo.py --caller-overwrite --database .poc-data/error-return-v5-regression/structural-index.sqlite --json-output .poc-data/error-return-v5-regression/result.json --markdown-output .poc-data/error-return-v5-regression/result.md
python -W error::ResourceWarning -m unittest discover -s poc/tests
```

第三条命令预期退出码为 1，因为故意覆写应使原安全预期失败；只修改临时副本，原 v4 源码保持不变。JSON 保存变更锚点、快照、结果及见证，可用该命令复现临时源码变体。

Windows 启动入口为 `poc\run_error_return_demo.bat "D:\cobol-output\error-return-v5"`；脚本已提供，**本轮未在 Windows 实机验收**。默认源码位置相对所选 profile 目录解析，报告路径不能覆盖源码、profile 或索引文件。

## 本次在哪里停

T01 达到所选源码模型的退出门槛，下一项是 **T02：正常业务计算闭环**，本轮没有启动。当前仍不支持完整算术、循环/SQL/EVALUATE、动态配置目标、共享状态及任意程序的真实可达性证明；工作区每次保守视为未知，不声称运行时每次重新分配。事件后的各根字段终态是观察，不是自动推断的因果关系或清零义务。

没有执行 COBOL、连接公司 API 或接通交互界面，业务案例仍标记未执行。完整业务分析仍按[进度总表](../12-task-plan-and-progress.md)的 P2–P4 及真实业务验收继续推进。
