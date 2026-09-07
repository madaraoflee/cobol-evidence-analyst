# P3-D：复杂调用链、参数映射与错误传播审查

日期：2026-09-08

本轮把主线推进到真正的跨程序关系：不仅增加复杂样例，还实现 CALL USING 到 LINKAGE 参数的位置与布局核对，并将错误状态、配置字段和结果字段接入这些关系。当前仍是有边界的源码分析，不是完整 COBOL 运行语义证明，也没有把离线样例结果当成真实模型回答。

## 复杂度如何进入验证

新增原创 `complex-business-v2`，主样例包含 14 个 COBOL 程序、4 个 COPY。独立反例目录另有 7 个程序与 1 个 COPY，避免参数数量不符、下标、替换和递归反例混入主场景。原 v1 保留，继续验证系统能发现此前的业务缺口。

最长主干示例：`TXNENTRY → TXNCORE → BASECALC → RATELOOK → DATECHK`。另有附加项目计算、调整因子、费用查询、配置路由和结果映射分支；代码混合固定格式与自由格式、内部段落、PERFORM THRU、局部 OCCURS/VARYING、EVALUATE 和 SIZE ERROR。每个主场景 CALL 都有 ON EXCEPTION，最终映射程序自身无法调用时，入口也有独立错误输出清零。

35 个案例期望包含 3 个手算正例与 32 个错误情形，全部标记为未执行。标准分支的手算示例为年化 299.00、分期 26.91；替代配置分支为年化 304.74、分期 27.43。这些数值来自显式输入账本，不进入源码事实核验，也不代表 COBOL 已运行。

## 跨程序参数现在能确认什么

`call_bindings.py` 核对字面量目标、调用方作用域、PROCEDURE USING 签名、LINKAGE 声明、参数数量、模式和受支持的声明布局。字段名字可以不同，例如 `BASECALC::INPUT-PRODUCT` 对应 `RATELOOK::RATE-PRODUCT`。兼容的固定组结构会继续映射组内成员，而非只连一个组名。

当前主样例产生 205 条参数或成员对应关系，92 条可能回写关系。`PASSES_AS` 表示受支持的位置/布局对应，不证明执行；`MAY_WRITE_BACK` 只对 BY REFERENCE 生成，始终为 candidate。BY CONTENT/BY VALUE 不产生调用方回写关系。VALUE 仅支持保守的匹配整数二进制声明；复杂 OCCURS、REDEFINES、预处理、布局不明和签名歧义不升级。

每条对应关系附调用位置、签名、双方声明与必要 COPY 来源引用。原有四个工具可检索和追踪这些关系，Agent 严格投影也已接通；引用仍须先发现再读取，读取后关闭调查的限制不变。未能建立参数关系的字面量调用会显示 `call_parameter_binding_incomplete`，不会静默缺边。升级后重跑原索引命令即可按解析器版本重建。

配置型调用保留为未决目标，但可沿参数关系找候选来源：`TXNCORE::CALC-ROUTE-PROGRAM` 连到 `ROUTESEL::ROUTE-OUTPUT` 的 SQL 写入，看到 `ROUTE_CONFIG` 以及产品和日期输入。候选写入点不是当前调用的唯一到达定义；没有版本化配置记录，就不能把 CALCSTD/CALCALT 中任何一个标成实际调用目标。

## 错误路径如何审查

`error_paths.py` 接受明确的状态/输出字段约定，进行只读、快照绑定、有预算的审查。它展开受支持的段落 PERFORM/THRU，记录 SQLCODE 错误分支、非零状态起点、状态转交、成功门禁、调用异常、输出清零，以及之后可能的状态复位或金额覆写。递归、GO TO、数组敏感条件、未支持的操作和预算截断会保留边界。

系统区分“本程序没有直接清零”和“通过被调用程序清零”：TXNENTRY 到 RESULTMP 的状态可按 CONTENT 传入，输出按 REFERENCE 传回，构成两项委托清零候选。若调用方或被调用方之后写入非零金额，候选会被阻断。映射程序调用失败后的入口直接清零，另行作为异常子句源码形态记录。

删除一个 SQL 错误分支、绕过成功检查、移除清零、清零后再覆盖、把引用输出改为内容复制，均有回归用例。检查识别源码形态，不等于全部条件组合都经过执行；`complete` 与 `full_control_flow_proven` 始终为 false。

## 运行与下一步

完整回归测试 237 项通过，资源警告按错误检查；其中新增 28 项参数映射专项、17 项错误路径专项、14 项复杂夹具、6 项工具契约和 8 项演示集成/预算测试。现有工作区的 SQLite 测试连接泄漏也已修复。测试通过不等于 35 个业务场景已经实际执行。

独立复核补住了依赖消费者上下文的 COPY 子片段误绑定：若片段被插在 OCCURS/REDEFINES 内，不能只看片段自身声明判断兼容。此类片段及单文件复合/嵌套程序暂保留边界。来源追踪和调用图另有节点、关系、结果数量及深度预算；大量循环或单字段大量写入也会明确截断。

```text
python -m unittest discover -s poc/tests -q
python poc/complex_demo.py --database .poc-data/complex-v2/structural-index.sqlite --json-output .poc-data/complex-v2/result.json --markdown-output .poc-data/complex-v2/result.md
```

Windows 可运行 `poc\run_complex_demo.bat "D:\cobol-output\complex-v2"`。Markdown 展示调用链、参数实例和配置来源，JSON 保留逐项错误观察与证据。返回码 0 只表示本地演示执行成功，不表示完整业务验收通过。真实源码与输出仍应留在公司批准目录。

下一段主线是把这些关系推进为上下文敏感的业务路径：异常与数值溢出子句的正式控制流、数组/循环的值变化、同一子程序多次调用的独立上下文，以及带版本的运行配置收窄。随后才验收模型能否针对真实问题收齐证据并回答完整。本轮未访问公司 API、未执行 COBOL/SQL、未接通界面。

代码理解技能在本轮要求把调用关系、参数对应和错误路径分别验证；因此引用回写、配置目标与实际执行都保留各自的证明边界，没有合并成一个“已理解全部业务”的状态。
