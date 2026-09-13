# framework-path-v2：控制 COPY 驱动的请求处理路径

这是原创、中性、非金额业务夹具。它表达供路径引擎核验的控制流程和人工预期，不是生产框架实现，也不是编译运行结果。外部 `DATAACCESS` 仅由明确版本的模拟契约提供行为；文件、游标、锁、提交和检查点事件均属于该契约下的模型结果。

## 两种入口不能混为一个模板

批处理入口 `BATCHRUN` 从 `BATCHPATH` 展开控制过程。入口先执行初始化，再根据输入 `RESTART-FLAG` 选择首次读取或重启读取；随后执行 `PERFORM UNTIL` 循环，每条记录先经过 `REQUESTCHECK`，允许处理才保存、提交并生成检查点。正常结束、首读失败、后续读取失败、保存失败、提交失败以及回滚失败都有源代码分支。

联机入口 `SCREENRUN` 从 `SCREENPATH` 展开控制过程。它按输入 `REQUEST-ID` 读取一条请求，不进行批量循环，也不调用重启或检查点操作。保存或跳过后先关闭访问，再执行 `4000-NEXT-SCREEN` 选择 `SUMMARY`、`REVIEW` 或 `ERROR`。批处理的 `4000-CLOSE` 与联机的 `4000-NEXT-SCREEN` 不能因编号相同被解释为同一种行为；实际调用次序来自控制 COPY。

`REQUESTCHECK` 使用两个独立的 LINKAGE 标量：`INPUT-STATE PIC X(8)` 和 `OUTPUT-DECISION PIC X(4)`。两个入口以默认引用传递调用它，调用方的 `RECORD-DECISION` 必须接收被调程序写回的 `WORK` 或 `SKIP`；不能以任意成功返回替代这个调用链。

## 输入及契约边界

- `BATCHRUN` 必须显式提供 `RESTART-FLAG=0` 或 `1`。这个字段刻意不被初始化覆盖。`0` 调用 `FIRST`；`1` 直接调用 `RESUME`，不能在恢复游标后再无条件调用 `FIRST`。
- `SCREENRUN` 必须提供 `REQUEST-ID`。其他工作字段都有显式初始化，不依赖未读取的 `VALUE` 子句。
- `ACCESS-AREA` 包含 `ACCESS-FUNCTION X(10)`、`ACCESS-STATUS X(4)`，以及 `REQUEST-ID`、`REQUEST-TYPE`、`REQUEST-STATE`、`TARGET-STATE` 四个 `X(8)` 字段。函数长度能够容纳完整的 `CHECKPOINT`，不能静默截断。
- 外部接口按 `CALL 'DATAACCESS' USING ACCESS-AREA` 调用。`ON EXCEPTION` 仅处理调用异常；正常调用仍逐一检查 I/O 状态。受控场景的普通返回、锁冲突和提交失败不应被任意的调用异常分支替代。
- 访问路径、游标身份和锁所有者由契约及场景明确指定；源代码没有伪造能够证明共享访问路径的字段。
- `RESULT-FLAG=1` 只表示本次路径至少提交了一条记录，不是累计计数。发生后续错误时已经提交的记录不能被后续 `ROLLBACK` 撤销。

## 人工业务预期

下表是独立验收预期；只有运行对应检查后才能标为通过。基础数据按键排序为 `A001/SERVICE/PENDING`、`A002/OTHER/PENDING`、`A003/SERVICE/REVIEW`、`A004/SERVICE/PENDING`。访问路径只选 `TYPE=SERVICE`；业务子程序只允许 `STATE=PENDING`。这两个筛选层必须分别解释。

| 场景 | 源代码应到达的结果 | 不允许出现的解释 |
| --- | --- | --- |
| 正常批处理，`RESTART-FLAG=0` | 首次读取返回 A001；A003 被业务跳过；A004 被处理；A001、A004 提交为 DONE；最终 `JOB-STATUS=OK`、`RESULT-FLAG=1` | 不能把 A002 的文件筛选与 A003 的业务跳过混成一条 IF；不能漏处理首条记录 |
| 访问路径没有符合记录 | `FIRST` 返回 END，关闭访问，`JOB-STATUS=OK`、`RESULT-FLAG=0` | 不能把空输入解释为访问失败或尝试 SAVE |
| 只有 REVIEW 记录 | 读取成功且 `RECORD-DECISION=SKIP`，不保存、不提交、不生成新检查点 | I/O 成功不等于业务允许处理 |
| A001 被其他所有者锁定 | `SAVE` 返回 HELD，`JOB-STATUS=LOCK`、`ERROR-CAUSE=LOCK`，执行回滚并停止；没有提交 | 不能在锁冲突后继续更新或推进检查点 |
| 第一笔提交返回声明的非成功状态 INVL | 保存仅暂存；提交失败后回滚；`JOB-STATUS=CMER`、`RESULT-FLAG=0`；持久数据不变 | 不能把 SAVE 成功当作已经提交 |
| 提交失败且回滚也失败 | `JOB-STATUS=RBER`，`ERROR-CAUSE=CMER` 保留原始原因；待处理事务仍须如实报告 | 不能宣称事务已经清理成功 |
| 提交成功但检查点写入失败 | `JOB-STATUS=CKPE`、`RESULT-FLAG=1`；该次提交仍有效；没有新的持久检查点 | 不能把检查点失败等同于先前提交被撤销 |
| 使用在 A001 提交后生成的有效检查点重启 | `RESUME` 直接读 A001 之后的记录；A001 不再次保存；A003 业务跳过，A004 提交 | 不能只凭一个键字符串声称有效检查点，也不能恢复后回到 FIRST |
| 检查点属于其他契约、数据快照或访问路径 | 明确停止在不可验证的边界；不继续保存 | 不能猜测检查点有效或把证据不足伪装成普通业务返回码 |
| 联机读取 PENDING 请求 | 读取、验证、保存、提交、关闭后进入 SUMMARY，`RESULT-FLAG=1` | 不能执行批处理循环或自动重启 |
| 联机读取 REVIEW 请求 | 不保存、不提交；关闭后进入 REVIEW，`RESULT-FLAG=0` | 不能把读取成功直接路由为更新成功 |
| 联机保存锁冲突或提交失败 | 保留错误原因，执行回滚，关闭后进入 ERROR | 不能在失败后进入 SUMMARY |

重启验收需要由前一次成功提交及检查点事件生成有效的检查点对象，并使用与其一致的已提交数据；不得手写一个看似相同的键来代替有效性证据。首读是否返回记录、RESUME 是否直接返回下一条、锁在何时释放、失败提交是否保留暂存以及检查点的持久化边界，均必须来自当前加载的版本化契约。

`runtime-contract.json` 声明以上合成语义；`scenarios/` 只提供记录、外部锁和指定调用次数的故障，不混入验收结果。`cases.json` 独立保存 15 个批处理及联机场景的入口、输入和人工预期。状态故障使用契约已声明的 `INVL`；调用异常则由源代码的 `ON EXCEPTION` 写入 `FAIL`，两者必须分别测试。`interrupt-after-checkpoint.json` 在第一份检查点生成后让下一次读取失败，用于取得一致的已提交记录及真实模型产出的检查点，再启动第二次重启分析。最后一个 `batch-resume` 用例通过 `resume_from` 引用此前的失败用例，只继承该次实际模型结果中的已提交记录和检查点；不继承故障注入，也不手写检查点对象。

这些场景只验收声明契约下的有界路径，不证明真实文件、真实锁、生产事务管理或实际重启机制与合成契约一致。
