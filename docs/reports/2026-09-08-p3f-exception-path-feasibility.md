# P3-F：调用异常与计算溢出的局部路径验证

日期：2026-09-08

本轮已把错误处理从“源码中出现了清零”推进到“在受支持的异常分支及其后续模型路径上，退出时输出是什么”。显式语法子集内的有界分析可行：正确样例的调用异常和溢出分别保持错误状态与零输出，清零后再覆写的反例会给出非零退出路径。这不是 COBOL 执行记录，也不是完整跨程序业务证明。

## 实际结果

主样例 `exception-flow-v4/main` 有 5 个程序、6 个源码调用点，按完整调用链展开为 8 个静态上下文。包含重复包装程序调用、共享子例程、PERFORM THRU、正常返回后的业务状态判断、调用失败处理、嵌套计算溢出/非溢出处理，以及结果映射和汇总。

| 所选事件 | 模型退出状态 | 模型退出输出 |
| --- | --- | --- |
| EXWRAP 调用 EXLEAF 异常 | 91 | 全部已建模退出为零 |
| EXWRAP 计算溢出 | 24 | 全部已建模退出为零 |
| EXJOIN 汇总溢出 | 25 | 全部已建模退出为零 |
| 独立反例 EXBAD 溢出后又 MOVE 7 | 24 | 存在输出 7 的模型路径 |

主验收 4 项通过（3 个事件预期加上下文数量）；独立反例验收 1 项通过，表示**成功检出了风险**，不是反例程序业务正确。主样例保留 10 个带上下文的局部事件引用，同一源码模型可在不同调用链下引用，不能计作多次真实错误。

EXBAD 的可复核路径是：初始化 → COMPUTE 选择溢出分支 → 状态设为 24 → 输出清零 → 输出被覆写为 7 → 程序退出。EXWRAP 的正常返回分支不会执行调用失败处理，溢出分支也不会落入非溢出处理。

## 本轮实现

`exception_cfg.py` 从已有只读索引恢复局部结构化控制流，拆分正常/异常、正常/溢出、IF 真/假分支，支持有界段落 PERFORM/THRU 与准确返回点。重复 PERFORM 保留不同局部实例链；GOBACK 不返回 PERFORM 续点，普通 EXIT 仍继续执行。来源引用绑定已有快照，预算耗尽不会留下一个丢失异常边却看似可用的图。

`exception_paths.py` 在该图上做有限状态探索。只保留接收字段数值布局能够容纳的 MOVE 常量，其他数值为未知；正常算术结果不假装已算出。正常 CALL 返回把已核对的引用参数及组成员变为未知，不执行子程序；参数效果不完整则停止于边界。错误事件独立于当前状态保存，因此后续把状态改为零不能把错误路径从报告中抹掉。

输出分为“全部已建模退出为零”“存在非零模型退出”“未证明”“模型未到达”，并保留路径节点、分支选择、退出状态与来源引用。受支持的数值比较中，未知输入保留两侧；不支持的条件停于边界，模型反例不保证实际输入下可达。探索预算与路径展示预算分别报告；被截断的探索不能升级为全部退出已核验。

`exception_demo.py` 用独立 profile 的来源锚点、状态值和输出预期验收结果，并把局部事件挂到 P3-E 调用上下文。锚点缺失或不唯一、输出仍非零、状态被改错都会失败。完整业务、运行、模型和网络标记仍为 false；原四个模型调查工具没有扩权。

## 语言规则与刻意保留的边界

CALL 的 ON EXCEPTION 不能解释成“捕获子程序任意业务错误”：正常返回后的非零业务状态必须另行判断。正常返回与异常处理各走自己的分支，处理块中的转移不能强行汇合。[CALL 语句说明](https://www.ibm.com/docs/en/cobol-zos/6.3.0?topic=statements-call-statement)、[调用错误处理范围](https://www.ibm.com/docs/en/cobol-aix/5.1?topic=errors-handling-when-calling-programs)

本轮只对有 ON SIZE ERROR 的单接收字段 COMPUTE 使用“溢出时保留原接收值”的规则；无处理分支会停在未处理边界。正常计算的精度与数值结果仍未证明。[SIZE ERROR 规则](https://www.ibm.com/docs/en/cobol-zos/6.4.0?topic=operations-size-error-phrases)

普通 EXIT 是无操作，PERFORM 范围结束才返回；GOBACK 离开整个当前程序。句点可隐式结束开放作用域，但本轮对这种复杂隐式闭合采取明确拒绝，而不按缩进猜测范围。[普通 EXIT](https://www.ibm.com/docs/en/cobol-zos/6.3?topic=statement-format-1-simple)、[GOBACK](https://www.ibm.com/docs/en/cobol-zos/6.4.0?topic=statements-goback-statement)、[作用域终止符](https://www.ibm.com/docs/en/cobol-zos/6.4?topic=division-scope-terminators)

数值 MOVE 可能截断，因此超出接收 PIC 容量或精度的常量不会继续被当成精确非零值。未知写入、不支持的语句、循环、SQL、EVALUATE、存储别名、共享工作区与外部副作用也没有被完整建模。[MOVE 数值赋值](https://www.ibm.com/docs/en/cobol-linux-x86/1.1.0?topic=items-assigning-arithmetic-results-move-compute)

## 验证与复现

全量回归 377 项通过，资源警告按错误处理。本轮异常控制流专用 23 项、独立控制流反例 9 项、路径状态专用 8 项、独立状态反例 12 项、夹具 11 项、端到端 10 项，共 73 项新增检查。之前上下文切片另补的两项预算检查也包含在全量测试中。

四种真实源码变异均使原安全预期失败：调用异常清零后覆写 7、错误状态 91 改为零、接收字段初值为 7 且删掉溢出清零，以及 PERFORM 返回后覆写输出。另覆盖 ON/NOT 分支互斥、嵌套归属、GOBACK、裸 EXIT、未知调用效果、窄接收字段、重复/缺失异常边及预算截断。

主样例 8 个业务案例及独立反例 1 个案例仍全部 `NOT_EXECUTED`。并未安装编译器或执行 COBOL、SQL；公式账本是手算期望，不能表述为业务运行结果。

```text
python -W error::ResourceWarning -m unittest discover -s poc/tests -q
python poc/exception_demo.py --database .poc-data/exception-v4/structural-index.sqlite --json-output .poc-data/exception-v4/result.json --markdown-output .poc-data/exception-v4/result.md
python poc/exception_demo.py --source poc/fixtures/exception-flow-v4/regressions/programs --profile poc/fixtures/exception-flow-v4/regressions/profile.json --database .poc-data/exception-v4-regression/structural-index.sqlite --json-output .poc-data/exception-v4-regression/result.json --markdown-output .poc-data/exception-v4-regression/result.md
```

Windows 主样例可运行 `poc\run_exception_demo.bat "D:\cobol-output\exception-v4"`。输出中的 PASS 及退出码 0 只表示所选模型预期一致，不能升级为业务验收通过。结果仍应留在获准本地目录。

## 下一段主线

接下来要把“局部异常路径”与“跨程序返回状态”组合，而不是继续把正常 CALL 的所有引用输出都视为未知：建立有边界的被调程序摘要，并在不同调用上下文中代入，验证错误能否传至最终结果映射。之后再扩循环/数组、配置调用和真实模型业务回答验收。

代码理解技能要求区分调用关系与控制流，因此本轮不会把图连通、局部模型通过或上下文引用复用当成完整业务执行证明。
