# POC 运行说明

## 真实源码入口：先确认读到了哪些程序，再问问题

从**项目根目录**执行下面的命令，把两个路径换成办公室电脑上的真实目录；源码目录应包含分析所需的程序与 COPYBOOK。输出目录与源码目录分开，且保留在公司批准的本地位置。

    python poc\analyze_source.py ^
      --source "D:\cobol-data\source" ^
      --output "D:\cobol-output\analysis"

Windows 包装脚本接受相同参数：

    poc\run_analyze.bat --source "D:\cobol-data\source" --output "D:\cobol-output\analysis"

这个入口要求显式提供源目录，不默认读取 `fixtures`；每次先为当前目录更新索引，再输出实际发现的程序和诊断。不需要 API Key，也不会联网。先打开 `diagnosis.md` 和 `programs.json`，核对文件数量、程序名、编码和未解决的 COPY/CALL。零文件、零程序或指定入口找不到时会阻断模型调用；`INDEX_READY` 只说明可开始源码调查，`NEEDS_ATTENTION` 表示仍有诊断项需要核对，均不证明业务已经分析完整。

无扩展名成员默认纳入新入口，可用 `--exclude-extensionless` 排除；自定义扩展名加 `--extensions ".cbl,.cpy,.member"`，它是完整的允许扩展名列表。已知导出编码时显式加 `--encoding gb18030` 或 `--encoding cp950` 等；格式识别不对时加 `--source-format fixed` 或 `--source-format free`。编码与格式默认均为 `auto`，自动解码成功也应检查中文和 `PROGRAM-ID` 是否正确。

把 `实际的PROGRAM-ID` 换成这次报告中的程序名，在已配置公司接口的同一终端执行：

    python poc\analyze_source.py ^
      --source "D:\cobol-data\source" ^
      --output "D:\cobol-output\analysis" ^
      --entry "实际的PROGRAM-ID" ^
      --question "请解释该程序的主要处理步骤、输入输出和调用，并引用源码行号。" ^
      --allow-network

`--entry` 也接受报告中的相对路径或文件名。仅文件名相同时可用相对路径明确范围；同一 `PROGRAM-ID` 存在多个版本时，必须把版本分开索引，路径本身不能消除程序身份歧义。第二次及以后的问题也从这一入口执行，并保留第一次用到的编码和扩展名参数。这样换目录或改文件之后会先更新事实索引，不会误用上一次演示数据库。`--allow-network` 允许本次调查把必要源码证据发送给已配置的接口；只有 API 能力探测通过才进入模型问答。输出中的 `agent-result.json` 和 `agent-result.md` 保存本次问答状态与结果；未提问或未开启联网时会明确显示 `NOT_REQUESTED` 或 `NETWORK_DISABLED`。

**下文是底层工具及固定案例的研发说明。** `run_demo.py`、其他 `*_demo.py` 和 `business_acceptance.py` 依赖预设样例符号与验收条件；替换它们的夹具不等于分析公司源码。演示网页也尚未连接真实索引和 Agent。API 连通、样例 PASS、工具流程 COMPLETED 都不能单独证明公司业务已经被解释完整。

当前 POC 包含离线事实索引、受控 Agent、单条 Claim 核验、业务覆盖检查、P3-D 跨程序错误审查、P3-E 静态调用点链上下文、P3-F 局部异常控制流，以及 T01 跨程序错误返回组合。离线工具只使用 Python 标准库；`company_api.py` 和 `run_agent.py` 默认不发送网络请求，只有显式传入 `--allow-network` 才会连接公司 API。

办公室电脑从零开始的最新完整操作请看：[系统使用手册（2026-09-14）](../docs/15-system-user-manual.md)。旧版参考仍保留在[办公室电脑使用手册（历史版本）](../docs/11-office-usage-guide.md)。

## F01：通用项目入口与框架源码审查

不依赖金额样例的目录入口已新增：`run_project_poc.py --source SOURCE --entry PROGRAM --output OUTPUT [--profile PROFILE_JSON] [--question QUESTION]`。入口精确匹配 PROGRAM-ID；源码与输出不可相互包含，输出必须新建或为空。无扩展名成员始终纳入扫描，带后缀范围可用 `--extensions` 配置。

它同时交付原始索引与独立框架审查：受支持过程 COPY 中的 PERFORM 保留 COPY 原始行、哈希和宿主包含链；入口类型、I/O 功能及两类状态属于版本配置声明。展开不完整则停止过程观察，不猜测库优先级、替换或私有框架规则。F01 命令仍只交付结构审查；需要条件化路径时使用下方新入口，二者均不自动调用模型回答问题。

可运行的非金额 batch / online 例子、输出和限制见[框架对齐与使用说明](../docs/13-framework-alignment.md)。旧 `run_demo.py` 仍只是固定样例自检，不能通过更换源码路径把其中的固定调查当作真实业务分析。所有新索引和报告均按公司源码同级保管。

## F02/F03：控制路径与可执行运行契约

从项目根目录执行，输出目录须新建或为空：

```sh
python3 poc/run_framework_paths.py \
  --source poc/fixtures/framework-path-v2/source \
  --contract poc/fixtures/framework-path-v2/runtime-contract.json \
  --case-set poc/fixtures/framework-path-v2/cases.json \
  --output .poc-data/framework-paths-new-run
```

Windows 使用 `poc\run_framework_paths.bat`，参数相同，尚未实机验收。入口沿实际控制 COPY、section/循环、子程序和参数返回推进；外部接口按明确版本契约影响记录、游标、锁、事务和检查点。15 个案例覆盖筛选/跳过、正常/失败和前次检查点恢复，输出逐步 Markdown 证据与 JSON 模型结果。单场景可用 `--entry`、`--scenario`、`--initial-values`、`--output-fields` 替代案例目录。

这是一条给定输入下的有界路径，不遍历所有路径，不执行业务程序或数据库。未知条件、缺失控制源码和不支持的布局/语义明确停止；边界处仍显示已知的待提交写入。文件筛选只支持单数据集的等值合取和唯一升序键，源程序文本状态只支持 ASCII。新入口没有自动接入 Agent。详细规则见[框架路径与运行契约](../docs/14-framework-paths-and-runtime-contracts.md)。

## P0：聚合代码库画像

repo_inventory.py 用于在公司允许的 Windows 本地环境统计已下载的 COBOL/COPYBOOK。

默认报告仅包含聚合数字，不包含源码、绝对路径、相对路径、Program-ID、COPYBOOK 名称或 CALL 目标。

Windows 运行：

    run_inventory.bat "D:\path\to\downloaded-source" ^
      --output "D:\poc-output\repo-inventory.json" ^
      --markdown-output "D:\poc-output\repo-inventory.md"

如果 AS400 导出的成员没有扩展名：

    run_inventory.bat "D:\path\to\downloaded-source" --include-extensionless

如果扩展名是公司自定义格式：

    run_inventory.bat "D:\path\to\downloaded-source" --extensions ".cbl,.cpy,.smartcob"

只有在报告始终留在公司批准环境时，才使用 --include-identifiers。不要把包含标识符的报告复制到本项目或外部对话；默认聚合报告是否可以分享，也必须遵守公司政策。

## P1-A：SQLite/FTS5 结构索引

structural_index.py 读取同一个本地源码文件夹，建立用于后续 Agent 调查的确定性事实层。

Windows 运行：

    run_index.bat "D:\path\to\downloaded-source" ^
      "D:\poc-output\structural-index.sqlite"

也可以直接运行：

    python structural_index.py "D:\path\to\downloaded-source" ^
      --database "D:\poc-output\structural-index.sqlite" ^
      --report-output "D:\poc-output\structural-index-report.json"

如果成员没有扩展名，增加 --include-extensionless。

当前抽取内容：

- Program、Section、Paragraph、Field、88 级 Condition Name 和 COPYBOOK；
- 字面量 CALL、动态 CALL 的目标字段、PERFORM/PERFORM THRU 和 COPY；
- MOVE、COMPUTE、ADD、SUBTRACT、MULTIPLY、DIVIDE 的直接字段读写；
- 多行显式 IF 条件和基础控制依赖，保留续行证据；句末句号关闭全部开放范围；
- 无替换的 DATA COPY 字段按消费程序绑定，保留 COPY 位置与原始定义的引用链；
- READ、WRITE、REWRITE、START 的 I/O 边界；
- EXEC SQL 的表级边界，以及受支持 SELECT/FETCH INTO 输出和查询/DML 宿主输入；
- 原始相对路径、文件 Hash、起止物理行号和源码 EvidenceSpan；
- SQLite FTS5 全文检索，以及按文件 Hash 和解析器版本增量更新；COPY 变动会重绑未改动的消费程序。

关系只会标记为 confirmed、candidate 或 unresolved。动态 Program 实际目标、DB/File 定义、运行值和 DXC 方言语义没有证据时不会被猜测。

## P1-B：受限代码调查工具

`investigation_tools.py` 在结构索引之上提供 Agent 唯一允许使用的四个只读工具：

- `search_code`：精确符号优先，再进行 FTS5 全文检索；
- `inspect_symbol`：查看精确定义以及直接读写、调用、PERFORM、条件和外部表；
- `trace_relations`：只沿白名单关系追踪，最多 3 跳并受边数预算限制；
- `read_evidence`：只能按已经发现的 Evidence ID 读取源码，不接受文件路径。

Windows 固定合成案例演示（以下命令在 `poc` 目录执行，不能替代真实源码入口）：

    run_demo.bat "D:\poc-output\calc-01"

该命令会索引 `fixtures\synthetic-insurance-v1`，执行六步调查，并生成 JSON 与 Markdown。输出应显示 `PARTIAL`、6 次工具调用、12 段 EvidenceSpan 和 `network_calls: false`。独立的本地金标准扫描另行报告 16 项来源事实覆盖、3 项业务缺失和 4 项边界，不属于六步检索成果，也不证明模型已答完整。

单独调用工具时，从当前真实源码报告中复制实际字段与程序名，替换下面两个中文占位值：

    python investigation_tools.py ^
      --database "D:\poc-output\structural-index.sqlite" ^
      inspect-symbol "实际字段名" --program-name "实际的PROGRAM-ID"

源码文本只会由 `read_evidence` 返回，并标记为 `UNTRUSTED_SOURCE_TEXT`。搜索、符号检查和关系追踪只返回结构事实及 Evidence 引用。

同一 COPY 字段在不同程序有不同实体，不指定 `program_name` 时可能返回 `AMBIGUOUS`。`definition.copy_binding` 给出最多八层 COPY 引用链，定义位置仍指向原始 copybook。共享定义不表示共享运行时存储；COPY REPLACING、独立 REPLACE、库限定、循环、重复与缺失目标会保留边界。CALL USING/LINKAGE 的受支持位置与布局对应另由 P3-D 核对，不自动等于运行时值传递证明。

## P3-A：公司 API 探测与受控 Agent

先在公司批准的终端中设置环境变量，不要把 Key 写入代码、命令行参数、SQLite 或输出文件：

    set COMPANY_API_BASE_URL=https://approved-company-gateway.example/v1
    set COMPANY_API_KEY=...
    set COMPANY_CHAT_MODEL=approved-chat-model

第一步只做能力探测：

    python company_api.py --allow-network

探测会实际验证基本 Chat、指定函数工具调用及 `role=tool` 回传、严格 JSON。只有工具完整闭环或严格 JSON 行为探测通过，运行器才会选择对应模式；普通 Chat 成功不再足以宣布 JSON fallback 可用。`/models` 只是信息项，网关不暴露它不会阻断 Agent。`--timeout-seconds` 是整次 capability probe 的应用层总预算，单个网络响应也使用有界分块读取并把剩余时间下发到 socket；同步 DNS 或自定义 transport 若自身阻塞，仍需要未来用进程级 watchdog 才能提供绝对墙钟截止。Embedding 默认不探测；只在已批准嵌入模型时使用 `--embedding-model ... --probe-embeddings`。生产地址必须是 HTTPS；HTTP 只能在显式加上 `--allow-insecure-localhost` 时用于本机测试。

真实源码优先使用本文开头的 `analyze_source.py`，它会在提问前更新索引。仅在手动确认数据库对应当前源码后，才直接调用底层运行器；以下命令在 `poc` 目录执行，问题中的程序名须换成真实名称：

    run_agent.bat "D:\poc-output\structural-index.sqlite" ^
      "请解释实际程序名的主要处理步骤并引用源码。" ^
      --allow-network

运行器会每次先重新探测，然后选择原生 Tool Calling 或严格 JSON fallback。模型只能选择 `search_code`、`inspect_symbol`、`trace_relations`、`read_evidence`；工具参数仍由应用校验，`read_evidence` 只能使用当次调查先前已发现的 ID。源码一旦返回，下一轮请求不再携带工具，模型只能完成回答或拒答，避免不可信源码诱导 Agent 扩张调查范围。连续两步没有新证据、达到 6 次工具预算或触发安全门禁时立即停止；安全硬停在运行器层标记为 `SAFE_STOP`，不会伪装成成功。

当前回答层会校验快照一致性、工具契约、Evidence 范围、源文件 Hash、行号、引用和代码锚点。模型写入 claim 与 boundary 的数量、长度、Markdown 结构和长源码复制也受本地预算限制。普通自然语言 claim 仍只返回 `CITATION_VERIFIED_ONLY`，并显示为“候选陈述；仅引用有效，语义未核验”；它不表示部分语义支持。

## P3-B：独立 COMPUTE 断言核验

`claim_support.py` 提供第一个独立 Claim 核验切片。模型提交结构化断言，不在该模式中提供陈述正文或自评支持状态：

```json
{
  "kind": "code_fact",
  "assertion": {
    "predicate": "compute_statement",
    "target": "OUT-AMOUNT",
    "expression": ["IN-AMOUNT", "*", "WS-FACTOR"],
    "rounded": true
  },
  "evidence_ids": ["ev-example"]
}
```

示例 Evidence ID 只是占位值；实际断言必须引用当次调查已读取并通过完整性校验的 ID。核验器要求单段未截断证据，独立解析完整的受支持 `COMPUTE` 语法，并精确比对目标字段、算式 token 序列和 `ROUNDED` 布尔值。语法不支持、证据不足或断言不匹配时会给出原因并拒绝升级；通过时只由本地模板生成中文事实陈述。

首版语法只覆盖 ASCII 标识符、十进制常数、以空格分隔的 `+ - * /`、括号和单个一元符号，语句必须以句点或 `END-COMPUTE` 结束。固定格式要求空白指示列，72 列外不能有有效内容；含注释、特殊续行、限定字段、下标、函数、`ON SIZE ERROR` 或多条语句的证据会拒绝核验。这个保守子集不替代完整 COBOL 方言解析。

全部陈述都通过这类核验时，回答可返回 `SUPPORTED_WITH_BOUNDARIES`，且必带适用范围：只确认源码中这条计算语句的写法，不确认它是否执行、是否决定最终结果或具有什么数值精度。自由文本仍走仅引用核验路径，完整自然语言语义核验尚未实现。该切片不增加调查工具、依赖或网络调用；CALC-01 已接入四次工具调用的离线端到端测试。实现与验收范围见 [P3-B 进度报告](../docs/reports/2026-09-07-p3b-progress-report.md)。

## P3-C：业务问题完整性与来源事实覆盖

每份 Agent 结果现在都有本地生成的 `question_coverage.status=not_assessed`，正文也明确“尚未核验问题相关性与完整性”。`stop_reason_scope=investigation_loop` 表示停止原因只描述调查循环。即使全部 COMPUTE 断言核验通过，也不能升级为完整业务答案；业务推测与待确认问题分别显示为未核验、未形成结论。

独立离线验收命令：

    python business_acceptance.py "D:\poc-output\calc-01\structural-index.sqlite"

这是明确选择的 CALC-01 金标准，不是从任意问题自动推断验收项。公式、SQL 输入输出和控制条件必须落在对应语句上，并附快照、行号与 Hash；它核对已存索引，不重新读取当前工作文件。删除状态续行、缴费因子 INTO、错误清零或公式组成项，会使对应检查失败。三项已审查缺口——全部附加保障遍历、每次调整查询失败处理、基础费率生效日期——须人工复核后更新金标准，不会被普通语法匹配自动消除。

有缺失项时命令返回退出码 1，这是业务覆盖未达标，不是程序崩溃。这个检查不增加 Agent 工具，不替代模型检索、答案完整性或 COBOL 运行验收。实现范围见 [P3-C 报告](../docs/reports/2026-09-08-p3c-business-chain-progress.md)。

现有四步 Agent 集成测试仍由本地脚本提供模型响应，用于验证真实调查工具与核验器的接线；它不能证明模型理解了用户问题。六步演示也使用预设调查顺序，界面原型尚未接通运行器。

## P3-D：复杂跨程序与错误路径演示

新增 `fixtures/complex-business-v2/main`，含 14 个主程序与 4 个 COPY。五层静态链、不同名参数、三种传递方式、配置查询后的动态 CALL、子程序、数组循环和调用/算术错误处理同时存在。`boundaries/` 单独索引，用来验证不支持形式、数量不符、替换与递归不会被错误升级。v1 保留原有反例。

    python complex_demo.py --database .poc-data/complex-v2/structural-index.sqlite --json-output .poc-data/complex-v2/result.json --markdown-output .poc-data/complex-v2/result.md

Windows 从项目根目录也可执行 `poc\run_complex_demo.bat "D:\cobol-output\complex-v2"`。主场景当前得到 205 条参数/成员位置与布局对应、92 条候选回写；动态目标仍未解析。35 个案例账本均标为未执行，不是运行结果。

四工具新增可追踪关系 `PASSES_AS` 和 `MAY_WRITE_BACK`。参数位置、传递方式、调用点、组内位置与来源引用均受严格投影。CONTENT/VALUE 不产生对调用方的回写；REFERENCE 也只产生候选回写，不证明实际返回。静态调用缺少可核对参数时显示 `call_parameter_binding_incomplete`，细分原因保存在本地 `call_bindings` 表。

`error_paths.audit_error_paths` 根据显式字段约定检查查询错误分支、段落调用门禁、状态转交、异常清零、委托清零及后续覆盖。它不是第五个模型工具，也不是完整控制流引擎。报告始终保留未证明的运行行为、配置值、循环与不支持语法；复杂演示亦未调用模型。详见 [P3-D 报告](../docs/reports/2026-09-08-p3d-cross-program-error-flow.md)。

## P3-E：重复调用上下文可行性验收

`call_contexts.audit_call_contexts` 按完整静态调用点链区分同一子程序的多次使用，而不是按程序名或最后一个调用点合并。`context_paths.contextualize_errors` 绑定本地错误观察引用，并组合父子参数对应；只有全程 REFERENCE 且完整追到入口的链，才标为入口候选回写。CONTENT/VALUE、缺失映射和预算截断不能恢复或证明入口回写。

    python poc/context_demo.py --database .poc-data/context-v3/structural-index.sqlite --json-output .poc-data/context-v3/result.json --markdown-output .poc-data/context-v3/result.md

以上命令从项目根目录运行。Windows 也可执行 `poc\run_context_demo.bat "D:\cobol-output\context-v3"`。正例含 7 个程序、8 个源码调用点和 12 个静态上下文，48 项显式源码身份/映射验收通过。修改第二条状态连线、传参方式或删除调用，会使相应验收失败；退出码 0 只代表该源码验收通过。

静态上下文不是运行实例，也不证明工作区独立、别名不存在、条件可达或循环次数。12 个业务案例均未执行；程序完整错误路径、数值和模型回答仍须另行验收。v2 复杂演示同时输出上下文分析，但不会把动态配置目标展开为已确认程序。详见 [P3-E 报告](../docs/reports/2026-09-08-p3e-call-context-feasibility.md)。

## P3-F：局部异常与溢出路径验收

`exception_cfg.build_exception_cfg` 为显式 CALL/COMPUTE/IF 与受限 PERFORM/THRU 建立有界局部控制流，未知语法和预算耗尽停在边界。`exception_paths.audit_exception_paths` 区分正常/失败分支，跟踪可容纳的 MOVE 常量或未知值，检查错误发生后的模型退出输出，并提供非零覆写路径。该局部接口的 CALL 正常返回只使引用参数变未知；跨程序组合由下述 T01 独立接口提供，不改变旧局部报告的证明范围。

从项目根目录运行：

    python poc/exception_demo.py --database .poc-data/exception-v4/structural-index.sqlite --json-output .poc-data/exception-v4/result.json --markdown-output .poc-data/exception-v4/result.md

Windows 也可运行 `poc\run_exception_demo.bat "D:\cobol-output\exception-v4"`。主样例应得到 8 个静态上下文和 4 项预期验收通过：上下文数量，以及状态 91/24/25 对应事件的模型退出输出为零。独立 regressions 配置用于检查清零后覆写为 7；它的 PASS 表示检出风险，不表示程序正确。

不能把“全部已建模退出为零”当作全部真实路径通过。受支持的数值比较中未知输入保留两侧，正常算术结果保持未知；隐式句点作用域、不支持的条件、循环、SQL、EVALUATE 和别名等仍有限制。主 8 个业务案例与反例 1 个案例均未执行。来源规则、反例命令与精确范围见 [P3-F 报告](../docs/reports/2026-09-08-p3f-exception-path-feasibility.md)。

## T01：跨程序错误返回闭环

`interprogram_paths.audit_interprogram_paths` 在有限状态与调用深度内进入子程序源码路径，通过确认的位置/布局映射传参，普通返回时仅 REFERENCE 回写，继续调用方并记录入口终态。正常返回的非零业务状态不等于 CALL 调用异常。重复 CALL 与 PERFORM 中的调用实例分别保留上下文；正常 COMPUTE 数值仍未知，未知控制条件保留备选。

    python poc/error_return_demo.py --database .poc-data/error-return-v5/structural-index.sqlite --json-output .poc-data/error-return-v5/result.json --markdown-output .poc-data/error-return-v5/result.md

主样例复用 v4 原源码，30 项显式预期通过；包含 4 组各六步的参数路径检查。Windows 入口为 `poc\run_error_return_demo.bat "D:\cobol-output\error-return-v5"`，本轮未在 Windows 实机验收。另有 CONTENT/VALUE 隔断 6 项检查和 `--caller-overwrite` 临时变体；后者原安全验收预期 FAIL、退出码 1，不能把它误当工具故障。完整命令见 [T01 报告](../docs/reports/2026-09-08-t01-interprogram-error-returns.md)。

演示三个业务错误案例显式选择 `normal_return_only`，假设调用可用但仍保留算术异常备选；默认分析接口则探索调用失败。未绑定 LINKAGE 的使用、callee STOP RUN、别名、共享状态、递归、动态目标或缺失契约/证据不会略过后给出成功。`root_exits`、`events`、`witnesses` 分别保留终态、事件后逐字段观察和连续模型路径，展示截断与探索截断分开；不自动推断事件与所有输出的因果关系。所有真实运行和完整业务验收标记仍为 false。

### 重要隐私差异

repo_inventory 的默认报告是去标识符聚合结果。

structural-index.sqlite 为了支持源码检索与证据引用，会保存相对路径、Program/Field 名称和必要源码片段。它必须留在公司批准的本地环境，不能上传到外部服务、公开仓库或本项目的公共开发环境。

## 本项目测试

    python -m unittest discover -s poc/tests -v

T01 阶段记录为 438 项测试通过；当前测试数量以本次实际执行为准。其中保留 48 个 COMPUTE 源码金标子案例（10 个支持、38 个不支持）；子案例不与测试方法数相加。T01 新增 61 项，覆盖跨程序返回记录、正确的父上下文、复制隔断、业务错误与调用异常区分、未绑定 LINKAGE、覆写、重索引及预算边界；此前局部异常、调用上下文、索引一致性和原有工具回归均保留。业务案例账本仍未编译执行。

测试覆盖聚合隐私、CP950、快照 Hash、结构抽取、FTS5、四工具、六步演示、API 离线默认、HTTPS/重定向、Key 隔离、总超时、严格 JSON fallback、Evidence 越权、读取后范围关闭、结果严格投影、诊断脱敏、源码复制拦截、引用幻觉、CALC-01 四步真实工具闭环和拒答。P3-B 增加确定性断言的正向与对抗金标用例，覆盖算式、目标字段、舍入标记以及不支持语法的拒绝升级，并验证自由文本不会被升级为语义已支持。完整链路测试让真实 API 客户端驱动真实调查工具；API 响应均由本地假传输提供，本项目环境尚未调用真实公司端点。最新验收结果见 [P3-B 进度报告](../docs/reports/2026-09-07-p3b-progress-report.md)。
