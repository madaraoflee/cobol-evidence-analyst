# POC 运行说明

当前 POC 已包含三个完全离线的事实构建阶段，以及一个显式授权才会联网的 P3-A Agent 阶段。离线工具只使用 Python 标准库；`company_api.py` 和 `run_agent.py` 默认也不发送任何网络请求，只有显式传入 `--allow-network` 才会连接公司 API。

办公室电脑从零开始的完整操作请看：[办公室电脑使用手册](../docs/11-office-usage-guide.md)。本手册按当前命令行实现编写，包含安装、同步仓库、离线自检、源码清单、结构索引、四个调查工具、接口探测、Agent 提问和故障排查。

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
- IF、ELSE、EVALUATE、WHEN 的基础控制依赖；
- READ、WRITE、REWRITE、START 的 I/O 边界；
- EXEC SQL 的表级 SELECT/UPDATE 边界；
- 原始相对路径、文件 Hash、起止物理行号和源码 EvidenceSpan；
- SQLite FTS5 全文检索，以及按文件 Hash 跳过未变化文件。

关系只会标记为 confirmed、candidate 或 unresolved。动态 Program 实际目标、DB/File 定义、运行值和 DXC 方言语义没有证据时不会被猜测。

## P1-B：受限代码调查工具

`investigation_tools.py` 在结构索引之上提供 Agent 唯一允许使用的四个只读工具：

- `search_code`：精确符号优先，再进行 FTS5 全文检索；
- `inspect_symbol`：查看精确定义以及直接读写、调用、PERFORM、条件和外部表；
- `trace_relations`：只沿白名单关系追踪，最多 3 跳并受边数预算限制；
- `read_evidence`：只能按已经发现的 Evidence ID 读取源码，不接受文件路径。

Windows 可执行演示：

    run_demo.bat "D:\poc-output\calc-01"

该命令会索引 `fixtures\synthetic-insurance-v1`，执行 CALC-01 的六步调查，并生成 JSON 与 Markdown 结果。输出应显示 `SUPPORTED_WITH_BOUNDARIES`、6 次工具调用、12 段 EvidenceSpan 和 `network_calls: false`。

单独调用工具示例：

    python investigation_tools.py ^
      --database "D:\poc-output\structural-index.sqlite" ^
      inspect-symbol OUT-INSTALMENT-PREMIUM

源码文本只会由 `read_evidence` 返回，并标记为 `UNTRUSTED_SOURCE_TEXT`。搜索、符号检查和关系追踪只返回结构事实及 Evidence 引用。

## P3-A：公司 API 探测与受控 Agent

先在公司批准的终端中设置环境变量，不要把 Key 写入代码、命令行参数、SQLite 或输出文件：

    set COMPANY_API_BASE_URL=https://approved-company-gateway.example/v1
    set COMPANY_API_KEY=...
    set COMPANY_CHAT_MODEL=approved-chat-model

第一步只做能力探测：

    python company_api.py --allow-network

探测会实际验证基本 Chat、指定函数工具调用及 `role=tool` 回传、严格 JSON。只有工具完整闭环或严格 JSON 行为探测通过，运行器才会选择对应模式；普通 Chat 成功不再足以宣布 JSON fallback 可用。`/models` 只是信息项，网关不暴露它不会阻断 Agent。`--timeout-seconds` 是整次 capability probe 的应用层总预算，单个网络响应也使用有界分块读取并把剩余时间下发到 socket；同步 DNS 或自定义 transport 若自身阻塞，仍需要未来用进程级 watchdog 才能提供绝对墙钟截止。Embedding 默认不探测；只在已批准嵌入模型时使用 `--embedding-model ... --probe-embeddings`。生产地址必须是 HTTPS；HTTP 只能在显式加上 `--allow-insecure-localhost` 时用于本机测试。

探测通过后，执行一次最多 6 步的调查：

    run_agent.bat "D:\poc-output\structural-index.sqlite" ^
      "分期保费最终是怎样计算出来的？" ^
      --allow-network

运行器会每次先重新探测，然后选择原生 Tool Calling 或严格 JSON fallback。模型只能选择 `search_code`、`inspect_symbol`、`trace_relations`、`read_evidence`；工具参数仍由应用校验，`read_evidence` 只能使用当次调查先前已发现的 ID。源码一旦返回，下一轮请求不再携带工具，模型只能完成回答或拒答，避免不可信源码诱导 Agent 扩张调查范围。连续两步没有新证据、达到 6 次工具预算或触发安全门禁时立即停止；安全硬停在运行器层标记为 `SAFE_STOP`，不会伪装成成功。

当前回答层会校验快照一致性、工具契约、Evidence 范围、源文件 Hash、行号、引用和代码锚点。模型写入 claim 与 boundary 的数量、长度、Markdown 结构和长源码复制也受本地预算限制。这些还不等于独立的语义 Claim Support Checker，所以自然语言 claim 暂时只返回 `CITATION_VERIFIED_ONLY`，并显示为“候选陈述；仅引用有效，语义未核验”。它不表示部分语义支持。

### 重要隐私差异

repo_inventory 的默认报告是去标识符聚合结果。

structural-index.sqlite 为了支持源码检索与证据引用，会保存相对路径、Program/Field 名称和必要源码片段。它必须留在公司批准的本地环境，不能上传到外部服务、公开仓库或本项目的公共开发环境。

## 本项目测试

    python -m unittest discover -s poc/tests -v

当前 84 项测试全部通过。除原有的聚合隐私、CP950、快照 Hash、结构抽取、FTS5、四工具和六步演示外，现在还覆盖 API 离线默认、HTTPS/重定向、Key 隔离、总超时、深层 JSON、Tool 类型和完整回传、严格 JSON fallback、六步上限、无进展停止、Evidence 越权、读取后范围关闭、快照异常脱敏、结果严格投影、调用 ID/诊断脱敏、拆分源码复制拦截、引用幻觉、CALC-01 四步真实工具闭环和两类拒答。新增的完整链路测试会让真实 API 客户端驱动真实调查工具，并验证模型在读到源码后无法再搜索新的敏感符号。所有 API 测试使用注入的本地假传输，本项目环境尚未调用真实公司端点。
