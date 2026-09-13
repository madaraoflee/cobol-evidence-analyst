# 办公室电脑使用手册

公司源码必须经过“指定真实目录 → 更新索引 → 确认实际程序 → 提问”这条链。API Key 配好只解决模型连接；替换演示夹具不会让固定样例调查自动变成公司业务分析，直接运行旧 `run_agent.py --database ...` 也不会读取新换的源码文件夹。

本手册以 `analyze_source.py` 为实际使用入口。第一次按第 1～3 节准备并检查真实源码，再按第 8～9 节配置接口、提问；第 4～7 节是需要深入排查时才使用的底层工具。合成样例自检移到文末附录。当前为命令行实现，演示网页未接通真实源码和 API。

2026-09-12 新增不依赖固定金额样例的[通用框架审查入口](./13-framework-alignment.md)：可一次选择获准源码目录、入口和版本配置，生成原始索引与控制 COPY 来源报告。它不会自动回答业务问题，也尚未将派生控制接入 Agent；第一次自检成功后可以用它替代分别建清单/索引的步骤。Windows 包装脚本已提供，实际公司 Windows 环境仍待验收。

## 1. 先明确哪些东西放在哪里

建议把三类内容分开：

- 项目代码：D:\cobol-work\cobol-evidence-analyst
- 公司批准的源码：D:\cobol-data\source
- 本地结果：D:\cobol-output\analysis

源码、结构索引数据库、包含相对路径或程序名的报告，都应留在公司批准的电脑或目录内。不要把 structural-index.sqlite、源码片段、带标识符的报告、API Key 或 Agent 输出提交到 GitHub；GitHub 只保存本项目的代码和公开文档。路径包含空格时必须用双引号包住，优先使用本机磁盘，不要直接在不稳定的网络共享盘上建立索引。

## 2. 第一次准备 Windows 电脑

### 2.1 安装两个基础软件

在办公室电脑上准备：

1. Windows 10 或 Windows 11。
2. Git for Windows。
3. Python 3.10 或更高版本，并在安装时勾选“Add Python to PATH”。

本项目只使用 Python 标准库，不需要执行 pip install。索引依赖 Python 自带的 SQLite FTS5；如果后面出现 “This Python SQLite build does not include FTS5 support.”，按第 12 节处理。

打开“命令提示符”（cmd.exe），不要先打开一个带有旧环境变量的长期终端，逐项检查：

    python --version
    git --version

两条命令都应该打印版本号。若 python 不识别，先试：

    py -3 --version

以后所有 python 命令都可以把 python 替换为 py -3。若 python 和 py -3 都不识别，先修复 Python 安装或 PATH，不要继续建立索引。

### 2.2 登录 GitHub

浏览器打开 https://github.com/login ，按公司允许的方式登录。仓库是公开仓库，因此下载代码不需要把源码上传到 GitHub，也不需要为本项目创建私有仓库。办公电脑只需具备读取公开仓库的能力即可。

### 2.3 下载项目

在命令提示符中执行：

    mkdir D:\cobol-work
    cd /d D:\cobol-work
    git clone https://github.com/madaraoflee/cobol-evidence-analyst.git
    cd cobol-evidence-analyst
    git switch main
    git pull --ff-only origin main
    git config --local core.hooksPath .githooks
    git config --local --get core.hooksPath

看到 “Your branch is up to date with 'origin/main'” 或同等意思，表示项目已经是最新版本。检查当前目录：

    git status

正常情况下会显示位于分支 main，且工作区没有待提交改动。

必须在每一台新电脑上设置一次本地钩子。仓库里的 .githooks 文件会随项目下载，但 Git 的本地 hooksPath 设置不会从 GitHub 继承；第二条命令应打印 .githooks。该设置会让每次成功的 git commit 后自动尝试推送当前分支。它不会在你保存文件时自动 commit，所以“每次改动都有版本”仍然要求每次逻辑改动执行 add 和 commit。

如果目录已经存在，不要再次 clone；使用：

    cd /d D:\cobol-work\cobol-evidence-analyst
    git switch main
    git pull --ff-only origin main

如果 git pull 提示本地有改动，先用 git status 和 git diff 确认内容，再决定是否提交或暂存经确认的公开改动；不要强行覆盖本地文件。

## 3. 第一次运行：接入公司的真实源码

不要覆盖 `poc\fixtures`。把取得的程序、COPYBOOK 和相关源码放在独立目录，`--source` 指向同时包含它们的共同上级目录。若只给入口程序而缺少被调用程序或 COPY，工具只能报告已取得部分及缺口。输出目录必须独立于源码目录。

### 3.1 先离线检查，暂时不用 API

从项目根目录执行，替换两个路径：

    python poc\analyze_source.py ^
      --source "D:\cobol-data\source" ^
      --output "D:\cobol-output\analysis"

或使用等价的 Windows 包装脚本：

    poc\run_analyze.bat ^
      --source "D:\cobol-data\source" ^
      --output "D:\cobol-output\analysis"

`--source` 和 `--output` 都是必填项；这个入口没有默认样例目录。每次调用先按当前源目录更新本地结构索引，再生成本次诊断与程序清单。未变化文件可以增量复用；换目录、删除文件或修改源码后，需要重新运行同一条命令。不要依靠修改文件夹后继续查看旧报告来判断本次效果。

新入口默认纳入无扩展名的成员；如果无扩展名文件与源码无关，加 `--exclude-extensionless`。自定义扩展名加 `--extensions ".cbl,.cpy,.member"`，它会替换默认允许列表，因此要包含这批源码实际需要的所有后缀。

### 3.2 先看“读到了什么”，再判断能否提问

输出目录中重点查看：

| 文件 | 用途 |
| --- | --- |
| `diagnosis.md` / `diagnosis.json` | 本次源目录、快照、文件和程序覆盖情况、编码及解析问题。先打开 Markdown 阅读。 |
| `programs.json` | 实际发现的程序及其来源，后续 `--entry` 从这里选择。 |
| `structural-index.sqlite` | 本次源目录的本地事实索引，供调查工具使用。 |
| `agent-result.json` / `agent-result.md` | 本次问答状态与结果；未提问时为 `NOT_REQUESTED`，没有开启联网时为 `NETWORK_DISABLED`，不会用上一次的回答充当新结果。 |

`diagnosis.runner_status` 为 `INDEX_READY` 表示已具备本地调查入口；`NEEDS_ATTENTION` 表示存在需要核对的覆盖或解析问题；`BLOCKED` 表示本次不能继续。零文件、零程序，或指定的入口不存在/不唯一时会阻断模型调用。不要用 API 探测成功覆盖这些问题。

先核对报告中确实出现自己的程序名与预期文件量。只有部分程序出现时，先补文件或修正读取参数；“解码成功”不代表 COBOL 结构已识别，更不代表业务含义已证明。报告及程序清单含本地路径和源码标识符，应保留在公司批准的位置。

### 3.3 编码与源码格式不对时

若知道导出编码，直接指定，避免自动检测把文件当成另一种可解码文本。例如简体中文 Windows 导出：

    python poc\analyze_source.py ^
      --source "D:\cobol-data\source" ^
      --output "D:\cobol-output\analysis" ^
      --encoding gb18030 ^
      --source-format fixed

`--encoding` 默认 `auto`；实际为 CP950 时用 `--encoding cp950`，其他编码须用导出工具确认的名称。不要因为文件能打开就假定编码正确。EBCDIC 成员应按实际字符集和记录布局导出或转换成文本后再核对，直接给二进制文件改扩展名无法解决。

`--source-format` 可为 `auto`、`fixed`、`free`。固定格式的序号列、指示列及有效代码区会影响解析；自由格式导出应使用 `free`。这里的选项适用于本次目录，混合格式可先尝试 `auto`，再根据诊断拆分处理。第二次提问必须保留已经确认的编码、格式和扩展名选项。

看到真实目标程序后直接进入第 8～9 节。接下来的第 4～7 节用于独立检查底层索引和证据，正常使用不必重复执行一遍。

## 4. 底层工具：独立生成源码清单（可选）

清单阶段只统计候选文件、编码、行数、程序定义、COPY、CALL、PERFORM 和 EXEC SQL 等摘要。默认报告不放源码文本、绝对路径、相对路径或程序名，适合先做范围确认。

### 4.1 推荐命令

    cd /d D:\cobol-work\cobol-evidence-analyst
    mkdir D:\cobol-output\analysis
    python poc\repo_inventory.py ^
      "D:\cobol-data\source" ^
      --output "D:\cobol-output\analysis\repo-inventory.json" ^
      --markdown-output "D:\cobol-output\analysis\repo-inventory.md"

也可以使用：

    poc\run_inventory.bat ^
      "D:\cobol-data\source" ^
      --output "D:\cobol-output\analysis\repo-inventory.json" ^
      --markdown-output "D:\cobol-output\analysis\repo-inventory.md"

### 4.2 处理特殊文件

AS400 导出成员若没有扩展名，增加：

    python poc\repo_inventory.py ^
      "D:\cobol-data\source" ^
      --include-extensionless ^
      --output "D:\cobol-output\analysis\repo-inventory.json"

公司使用自定义扩展名时，显式加入扩展名（逗号分隔）：

    python poc\repo_inventory.py ^
      "D:\cobol-data\source" ^
      --extensions ".cbl,.cpy,.smartcob" ^
      --output "D:\cobol-output\analysis\repo-inventory.json"

只有在公司批准目录内查看时，才使用 --include-identifiers。这个开关会把程序名、COPY 目标、CALL 目标和相对文件名写入报告；不要把这类报告复制到项目目录、聊天窗口或外部服务。

### 4.3 先看清单再建索引

打开 repo-inventory.md，先确认：

1. candidate_file_count 是否覆盖预期文件量；
2. decoded_file_count 是否明显少于候选数；
3. unreadable_or_binary_file_count 是否为 0；
4. encodings 和 format_hints 是否符合这批导出文件；
5. unresolved_copy_target_count、unresolved_literal_call_target_count 是否需要后续补充文件。

如果大量文件不可读，先处理编码或导出方式，再建结构索引。不要把“解析成功”当成“业务完整”。

## 5. 底层工具：独立建立结构索引（可选）

结构索引会保存源码相对路径、文件 Hash、程序/段落/字段/关系和必要 EvidenceSpan。它是后续检索的本地数据库，必须留在公司批准环境。

### 5.1 推荐命令

    python poc\structural_index.py ^
      "D:\cobol-data\source" ^
      --database "D:\cobol-output\analysis\structural-index.sqlite" ^
      --report-output "D:\cobol-output\analysis\structural-index-report.json"

也可以使用包装脚本：

    poc\run_index.bat ^
      "D:\cobol-data\source" ^
      "D:\cobol-output\analysis\structural-index.sqlite"

成员没有扩展名时增加 --include-extensionless。自定义扩展名时增加 --extensions ".cbl,.cpy,.smartcob"。

### 5.2 读取索引报告

报告成功后，重点看：

- snapshot_id：这次源码快照的身份；
- files.decoded：实际解码的文件数；
- database_counts：符号、关系、EvidenceSpan 数量；
- relation_statuses：confirmed、candidate、unresolved 的数量；
- encoding_counts 或 parse 状态；
- coverage_boundary：仅靠源码快照不能证明的内容。

重复运行同一批源码时，未变化文件会按 Hash 跳过；源码发生变化后再次运行会更新对应文件。为了避免回答引用旧源码，每次更换源码批次都应使用新的输出目录或明确覆盖同一数据库后重新查看新的 snapshot_id。

### 5.3 编码边界

底层清单与索引也可显式指定 `--encoding` 与 `--source-format`。自动检测或回退只能说明文本可解码，不能保证所选字符集就是原始导出编码；诊断显示回退、乱码或零程序时，应按第 3.3 节核对。清单与索引必须使用同一组读取参数。

## 6. 底层工具：直接查询证据（可选）

所有调查都必须从已建立的本地 SQLite 开始。工具不接受任意文件路径读取源码；在 Agent 调查中，read-evidence 只能使用本次调查前面步骤已经返回的 Evidence ID。手工调用时也应遵守同一顺序，不要从数据库中猜 ID。

### 6.1 搜索代码

下面命令中的“实际字段名”“实际的PROGRAM-ID”都是占位值，须从本次程序清单与源码中选择并替换，不能原样执行。

    python poc\investigation_tools.py ^
      --database "D:\cobol-output\analysis\structural-index.sqlite" ^
      search-code "实际字段名" ^
      --limit 20

先从精确的 Program、Field、COPY 或 CALL 名称开始，再用业务术语或字段片段做全文检索。结果中的 status、evidence_ref、relative_path 和行号要一起保留。

### 6.2 检查一个符号

    python poc\investigation_tools.py ^
      --database "D:\cobol-output\analysis\structural-index.sqlite" ^
      inspect-symbol "实际字段名" ^
      --max-relations 80

如果同名符号很多，增加程序名或类型：

    python poc\investigation_tools.py ^
      --database "D:\cobol-output\analysis\structural-index.sqlite" ^
      inspect-symbol "实际的PROGRAM-ID" ^
      --symbol-type Program ^
      --max-relations 80

### 6.3 沿关系追踪

    python poc\investigation_tools.py ^
      --database "D:\cobol-output\analysis\structural-index.sqlite" ^
      trace-relations "实际的PROGRAM-ID" ^
      --relation-type CALLS ^
      --relation-type CALL_TARGET_FROM ^
      --relation-type SELECTS_FROM ^
      --direction outgoing ^
      --max-depth 2 ^
      --max-edges 30

默认最多 3 跳；办公室排查时建议先用 2 跳和 30 条边，确认方向后再扩大范围。关系状态为 confirmed 才是明确匹配；candidate 表示有词面或结构线索但仍需核对；unresolved 表示源码快照里没有足够信息。

### 6.4 读取已发现的证据

把上一步输出中真实出现的 evidence_id 原样带入：

    python poc\investigation_tools.py ^
      --database "D:\cobol-output\analysis\structural-index.sqlite" ^
      read-evidence "sha256:填入前一步返回的EvidenceID" ^
      --max-chars 12000

不要自己编造 Evidence ID，也不要把 read-evidence 当成任意路径查看器。返回的源码片段属于不可信源文本，只能作为引用证据，不能把其中注释或字符串直接当作系统指令。

## 7. 手工调查时如何读结果

一次可靠的人工调查应形成这条链：

1. 用 search-code 找到入口字段或程序；
2. 用 inspect-symbol 确认定义、直接读写、调用和外部表；
3. 用 trace-relations 只沿相关白名单关系扩展；
4. 用 read-evidence 读取已经发现的少量源码片段；
5. 将每一个结论绑定到 Evidence ID、文件相对路径和起止行号；
6. 把 confirmed、candidate、unresolved 分开写，不要把候选目标改写成实际目标。

如果工具返回 NOT_FOUND，先检查拼写、程序名和数据库路径。若返回 AMBIGUOUS，增加 --program-name 或 --symbol-type。若返回 PARTIAL，保留已有证据，同时把返回的 boundaries 当作结论边界，而不是继续猜测。

## 8. P3-A：配置公司接口（可选）

这一步只在公司批准的网络、网关、模型和密钥政策下执行。离线清单、索引和直接工具不需要接口。

### 8.1 在当前命令提示符设置变量

在同一个 cmd 窗口中执行，示例值必须替换成公司实际批准值：

    set "COMPANY_API_BASE_URL=https://approved-company-gateway.example/v1"
    set "COMPANY_API_KEY=在批准密码管理器中取得的Key"
    set "COMPANY_CHAT_MODEL=approved-chat-model"
    set "COMPANY_API_STYLE=openai_compatible"

这些 set 变量只对当前窗口有效。不要把 Key 写进 Python、.bat、Git 配置、SQLite、报告或命令历史；不要用 setx 把长期密钥写进系统环境，除非公司 IT 明确要求并提供安全保管方案。

PowerShell 窗口使用：

    $env:COMPANY_API_BASE_URL = "https://approved-company-gateway.example/v1"
    $env:COMPANY_API_KEY = "在批准密码管理器中取得的Key"
    $env:COMPANY_CHAT_MODEL = "approved-chat-model"
    $env:COMPANY_API_STYLE = "openai_compatible"

API 根地址必须包含版本前缀（例如 /v1），生产地址必须使用 HTTPS。HTTP 只允许在明确加 --allow-insecure-localhost 时用于本机测试。

### 8.2 先做能力探测

    python poc\company_api.py --allow-network

探测会验证基本 Chat、原生工具调用及工具结果回传、严格 JSON。输出只记录安全摘要，不记录 Key、完整 URL、模型名、请求体或响应体。成功时 agent_readiness.ready 为 true，并且 mode 是 NATIVE_TOOL_CALLING 或 VALIDATED_JSON_FALLBACK。

退出码含义：

- 0：探测完成，并且 Agent 所需模式可用；
- 1：请求完成但某项能力不满足，不能启动 Agent；
- 2：没有联网、配置不完整或配置无效。

Embedding 不是 run_agent.py 的必需项。只有拿到批准的嵌入模型时，才额外设置 COMPANY_EMBEDDING_MODEL 或传 --embedding-model，并执行 --probe-embeddings。

## 9. 选择实际程序，用 Agent 提问

在第 3 节输出的 `programs.json` 中选择一个实际程序，把下面 `实际的PROGRAM-ID` 换成其名称；也可使用报告里的相对路径或文件名。仅文件名相同时可用相对路径明确范围；同一 `PROGRAM-ID` 存在多个版本时，须把版本分开索引，路径不能消除程序身份歧义。不要照抄演示中的 `SYNP040` 或保费字段；如果报告里根本没有目标程序，先处理接入问题。

确认第 8 节接口可用，并在同一个保留 API 环境变量的终端执行：

    python poc\analyze_source.py ^
      --source "D:\cobol-data\source" ^
      --output "D:\cobol-output\analysis" ^
      --entry "实际的PROGRAM-ID" ^
      --question "请解释该程序的主要处理步骤、输入输出和调用，并引用源码行号。" ^
      --allow-network

也可以把第一行换成 `poc\run_analyze.bat`，后续参数不变。首次离线读取时如果指定过编码、格式或扩展名，这里也必须带上相同参数。

这条命令会先更新当前源目录的索引、检查入口，再做 API 能力探测及问答。`--entry` 和 `--question` 可以按需要使用，但首次调查建议同时给出真实入口与具体问题；仅有抽象中文业务词，未必能命中没有业务注释的 COBOL 标识符。`--entry` 确定调查起点，不代表整个调用链已完整解析。

问题与 `--allow-network` 分开控制：写了问题但未开启联网时会保存 `NETWORK_DISABLED`，不会调用模型；只有接口能力满足受控调查要求后才进入模型。普通 Chat 能返回一句话不代表 Tool Calling 或严格 JSON 已满足。

直接打开输出目录的 `agent-result.md` 读回答，`agent-result.json` 查看完整状态、证据和诊断。此结果始终属于这一次运行；源码有更新时重跑本命令。`run_agent.py` 仍作为底层接口保留，它只读取指定数据库，不会自动重新扫描源目录，日常使用优先走本节入口。

### 9.1 结果怎么判断

在 `agent-result.json` 中，实际进入 Agent 后重点看以下字段（没有进入时先看 `NOT_REQUESTED` / `NETWORK_DISABLED` 等运行状态）：

- runner_status：COMPLETED 表示受控流程正常结束；SAFE_STOP 表示安全停止；NOT_READY 表示没有满足运行条件；
- selected_mode：实际使用 NATIVE_TOOL_CALLING 或 VALIDATED_JSON_FALLBACK；
- agent_result.status：已核验的结构化计算语句可为 SUPPORTED_WITH_BOUNDARIES；普通自然语言陈述为 CITATION_VERIFIED_ONLY；无法形成可接受回答时为 ABSTAINED；
- agent_result.answer：带边界的回答文本；
- agent_result.evidence_refs / evidence_ids：回答引用的本地证据；
- agent_result.boundaries：未覆盖的内容、候选关系或安全边界；
- agent_result.question_coverage：当前为 not_assessed，问题相关性与完整性未核验；stop_reason_scope 只描述调查循环；
- diagnostics：停止原因及可操作线索。

当前实现会校验快照、Evidence 范围、文件 Hash、行号和词面锚点。P3-B 还会独立核验一种结构化断言：从单段完整证据解析 COMPUTE，比较目标字段、算式 token 顺序和 ROUNDED，再由本地模板生成陈述。全部陈述都通过该核验时可返回 SUPPORTED_WITH_BOUNDARIES；它仅证明这条语句的写法，仍不能写成“生产规则、最终值或精度已证明”。

普通自然语言陈述仍为 CITATION_VERIFIED_ONLY，表示引用有效、语义未核验。结构化断言与源码不匹配，或源码语法超出当前核验范围时，会保留拒绝原因；没有足够可用证据时可返回 ABSTAINED。这些结果说明此次调查的实际边界，不应人工改成支持状态。

### 9.2 什么问题适合问

适合问“源码快照能直接回答”的问题，例如：

- 当前入口程序的主要处理步骤是什么？
- 当前入口程序调用了哪些已确认的程序？
- 指定真实字段在哪里被赋值，相关条件和算式是什么？
- 哪些 CALL 目标在当前快照中仍是动态或未解析？

不适合要求它凭源码猜测生产事实的问题，例如实际费率值、控制表当前记录、运行时动态 CALL 的真实目标、Job Schedule、DB2/DDS 定义或某次生产输入的结果。对于这类问题，看到 ABSTAINED、candidate、unresolved 或 boundaries 应当停止扩展，不要用常识补答案。

## 10. 每天开始和结束时的固定流程

### 10.1 开始工作

    cd /d D:\cobol-work\cobol-evidence-analyst
    git switch main
    git pull --ff-only origin main
    git status

然后按第 3 节重跑离线检查，或直接按第 9 节用同一组参数提问。新入口会在每次提问前更新索引；仍须核对报告里的源目录和实际入口，避免命令仍指向旧文件夹。

### 10.2 结束工作

把 JSON、Markdown、SQLite 和源码留在公司批准的 D:\cobol-output 或其他指定位置。确认没有把它们复制进项目目录：

    git status --short

如果只看到你打算提交的公开代码或文档改动，才按团队约定提交并推送。若看到源码、数据库、报告或 .env 文件，先移出项目目录或检查 .gitignore，再继续。

## 11. 代码改动如何同步到 GitHub

项目已提供提交后的自动同步钩子；但每次改动仍应先检查内容、测试通过，再提交。自动同步的触发点是 commit，不是保存文件。典型流程：

    cd /d D:\cobol-work\cobol-evidence-analyst
    git status
    git diff --check
    python -m unittest discover -s poc/tests -v
    git add README.md DESIGN.md docs poc
    git commit -m "docs: update office usage guide"

commit 成功后，.githooks/post-commit 会自动执行 git push origin 当前分支。若终端提示推送失败，先保留本地提交，检查网络或登录状态，再手动执行：

    git push origin main

只添加本次确定属于公开项目的文件，不要使用 git add . 把源码、SQLite、报告或密钥一起加入。若团队要求先建分支，使用 codex/ 开头的分支名并通过 Pull Request 合并；不要在未确认分支策略时覆盖他人的 main。

如果提交后看到钩子已同步，仍用 git status 和 git log -1 --oneline 确认本地状态。不要为了“重试”而删除提交。若不想自动推送，可在公司允许的情况下临时移除本地 hooksPath；这只影响当前电脑，不会改变 GitHub 上的历史。

## 12. 常见故障排查

| 现象 | 原因判断 | 处理 |
| --- | --- | --- |
| python 不是内部或外部命令 | Python 未安装或 PATH 未生效 | 重新打开 cmd，试 py -3；仍失败就修复 Python 安装。 |
| git 不是内部或外部命令 | Git for Windows 未安装或 PATH 未生效 | 安装 Git for Windows，重新打开终端。 |
| FTS5 support 错误 | 当前 Python 的 SQLite 没有 FTS5 | 改用公司批准的完整 Python 发行版，不要改成猜测式全文搜索。 |
| 新入口发现零文件或零程序 | 指错目录、后缀过滤、编码/格式错误，或只有 COPYBOOK | 打开 diagnosis.md；核对源目录、--extensions、--encoding 与 --source-format，确认 programs.json 中出现真实程序后再联网。 |
| 文件可解码但中文乱码或没有程序 | 自动检测选中了错误编码或源码列格式 | 指定实际导出编码与 fixed/free，重跑；可解码不等于可正确解析。 |
| 修改源文件后回答仍是旧内容 | 使用了旧数据库或旧结果文件 | 改用 analyze_source.py --source ... --output ...，核对本次快照与程序清单。 |
| 报告仍出现 SYNP040 或固定保费演示 | 运行了 demo 脚本/演示网页，或源路径仍指向 fixtures | 使用第 3 节新入口；不要替换样例夹具。 |
| 指定入口不存在或有歧义 | 名称不在本次清单，或存在多个同名定义 | 从 programs.json 选择入口；同文件名用相对路径，同 PROGRAM-ID 多版本需分开索引。 |
| NETWORK_DISABLED / NOT_REQUESTED | 本次没有启用网络或没有提出问题 | 需要问答时按第 9 节传问题和 --allow-network；离线诊断本身不需要 Key。 |
| candidate_file_count 很高但 decoded_file_count 很低 | 编码或二进制成员不匹配 | 检查导出编码与不可读文件，显式 --encoding 后重跑。 |
| COMPANY_API_NOT_READY | 能力探测未通过或数据库打不开 | 先单独运行 company_api.py；检查 API 配置、网络授权和 SQLite 路径。 |
| BASE_URL_MISSING / BASE_URL_INVALID | URL 未设置、缺 /v1、使用了不允许的 HTTP 或含账号密码 | 按第 8 节重新设置，生产网关使用 HTTPS。 |
| API_KEY_MISSING | 当前终端没有 Key | 从批准的密码管理器重新设置当前会话变量，不要把 Key 写进脚本。 |
| HTTP 401 或 403 | Key、模型或网关权限不对 | 联系接口管理员确认，不要把响应正文复制到聊天或提交到 Git。 |
| STRUCTURAL_INDEX_INVALID | 数据库不是有效索引或被截断 | 用原始源码新建输出目录和数据库，重新执行清单、索引。 |
| search-code 返回 NOT_FOUND | 名称拼写不对或文件未被索引 | 先看清单，再用更短的字段片段全文检索。 |
| inspect-symbol 返回 AMBIGUOUS | 同名符号多个 | 增加 --program-name 或 --symbol-type。 |
| trace-relations 返回 PARTIAL | 深度、边数或解析边界已触发 | 缩小问题、读取 boundaries；不要把未解析边当成 confirmed。 |
| read-evidence 被拒绝 | Evidence ID 不在本次调查范围，或快照 Hash 不一致 | 回到 search-code/inspect-symbol 重新发现 ID，并确认数据库与源码批次一致。 |
| Agent 返回 SAFE_STOP 或 ABSTAINED | 工具契约、证据、快照或语义支持不足 | 保留结果与 diagnostics，补充批准的源码/结构数据后重跑；不要人工填空。 |
| git pull 要求处理冲突 | 本地有未提交改动 | 先 git diff 和 git status，提交或 git stash 经确认的公开改动，再同步。 |

## 13. 两张操作前后检查表

### 运行前

- [ ] 已进入 D:\cobol-work\cobol-evidence-analyst。
- [ ] 已 git pull --ff-only origin main。
- [ ] 源码来自公司批准位置，且本次数据库对应同一批源码。
- [ ] 已查看 diagnosis.md 的源码范围、编码、解析问题和快照。
- [ ] 已在 programs.json 找到本次要分析的真实程序，未照抄演示符号。
- [ ] 若要联网，已确认公司批准的网关、模型、权限和当前会话 Key。
- [ ] 问题是源码证据问题，不是要求猜生产运行数据。

### 运行后

- [ ] 输出保存在公司批准目录，不在项目目录。
- [ ] 已保存 runner_status、agent_result.status、evidence_refs 和 boundaries。
- [ ] 已区分 confirmed、candidate、unresolved。
- [ ] 没有把 CITATION_VERIFIED_ONLY 写成已完成语义证明。
- [ ] 若返回 SUPPORTED_WITH_BOUNDARIES，已阅读范围说明，没有把语句写法核验扩展成最终值、实际执行或精度证明。
- [ ] 已查看 question_coverage，没有把循环 COMPLETED 或单条公式支持当作完整业务分析通过。
- [ ] 没有把源码、SQLite、报告或 Key git add。
- [ ] 代码/文档有改动时已通过测试、提交并推送；无公开改动时保持工作区干净。

## 14. 按当前实现可以依赖的行为

下面这些不是宣传口径，而是当前代码可观察到的边界：

- 未显式传 --allow-network 时，新入口、company_api.py 和 run_agent.py 不创建真实网络请求。
- analyze_source.py 要求显式源目录，每次先更新索引并核对真实程序；零文件、零程序或无效入口不会继续调用模型。
- 结构索引建立在本地 SQLite/FTS5 上，并保存快照 Hash 与 EvidenceSpan，便于后续检查引用是否仍对应原文件。
- 未变化文件只有在解析器版本也未变化时才跳过；升级后重跑索引命令即可重建，COPY 变化会重绑消费程序。
- 普通数据 COPY 字段按程序隔离；同名字段宜指定 program_name，定义与 COPY 引用链可回溯，但不代表 CALL 参数值流。
- 多行显式 IF 保留完整条件，受支持 SQL 返回宿主变量读写，复杂或未支持语法保留边界。
- Agent 只能使用 search_code、inspect_symbol、trace_relations、read_evidence 四个只读工具；一次调查最多 6 次工具调用，并在连续无新进展时停止。
- Evidence ID、快照、文件 Hash、行号或工具结果契约不通过时，流程会安全停止或拒答。
- 读取源码证据后，调查范围会关闭；后续只能完成回答或拒答，不能继续无界搜索。
- 结构化 COMPUTE 断言会独立核对单段源码中的目标、算式 token 顺序和 ROUNDED；通过后由本地模板生成陈述，并强制保留范围边界。
- 普通自然语言结论只做引用和词面锚定校验，完整语义支持仍是边界。

对应实现位置：

- poc/analyze_source.py：真实源码接入、更新索引、诊断、程序清单与可选问答；
- poc/run_agent.py：对已有数据库进行一次受控 Agent 运行和退出码；
- poc/company_api.py：接口配置、HTTPS 校验、能力探测和安全审计摘要；
- poc/repo_inventory.py：离线清单；
- poc/structural_index.py：SQLite/FTS5 结构索引；
- poc/investigation_tools.py：四个只读调查工具；
- poc/agent_loop.py：工具白名单、预算、Evidence 范围和回答校验；
- poc/claim_support.py：独立 COMPUTE 断言核验与本地陈述生成。

## 15. 仍需要人工确认的事项

本手册不能代替公司审批。第一次在真实环境使用前，仍需由负责人员确认：

1. 办公电脑是否允许读取目标源码目录；
2. 源码实际编码、AS400 成员导出方式和自定义扩展名；
3. 公司批准的 API 网关地址、模型名、Key 获取方式和联网范围；
4. 哪些输出可以留在本机、哪些可以在团队内部共享；
5. 生产控制表、DDL/DDS、Job Schedule、DB/File 定义和运行日志是否另有可信来源。

只要这些条件没有确认，先完成离线自检、清单和结构索引，不要把 Agent 的候选回答当作生产结论。

## 附录：可选的合成样例离线自检

这一步不会读取公司的源码，也不会访问网络。它用于确认 Python、SQLite、项目路径和四个受限工具都能正常工作。

先从项目根目录执行：

    cd /d D:\cobol-work\cobol-evidence-analyst
    python poc\run_demo.py ^
      --database "D:\cobol-output\demo\structural-index.sqlite" ^
      --json-output "D:\cobol-output\demo\calc-01.json" ^
      --markdown-output "D:\cobol-output\demo\calc-01.md"

也可以使用 Windows 包装脚本：

    poc\run_demo.bat "D:\cobol-output\demo"

命令成功时会打印 JSON 摘要，重点检查：

- network_calls 是 false；
- tool_calls 不超过 6；
- support_status 是 PARTIAL，source_fact_coverage 为 16 项覆盖、3 项缺失、4 项边界；
- 输出目录中出现 structural-index.sqlite、calc-01.json 和 calc-01.md。

这个演示只索引项目内的 synthetic-insurance-v1 合成夹具。PARTIAL 是保留的业务验收结果：全部附加保障遍历、每次调整查询失败处理和基础费率生效日期尚未证明。程序运行成功不代表业务通过，也不代表读到了公司源码或接口可用。

可单独执行 `python poc\business_acceptance.py "D:\cobol-output\demo\structural-index.sqlite"` 查看逐项证据。有缺项时返回退出码 1；此离线扫描不属于六步调查，也不验证模型回答。

### 复杂程序组回归演示

从项目根目录执行 `poc\run_complex_demo.bat "D:\cobol-output\complex-v2"`，输出 `result.md`、`result.json` 与本地索引。该演示有 14 个主程序、4 个 COPY、五层调用链、配置型动态调用和每个调用点的异常处理；独立反例目录不会混入主场景。

当前应看到 205 条参数/成员对应、92 条候选回写及未决动态目标。查看报告中的配置来源、查询错误处理和委托清零，不能把它当成实际交易已执行。35 个案例期望均标为未执行。CONTENT/VALUE 不回写，REFERENCE 也只是可能回写；复杂布局和调用上下文仍有边界。升级后重跑索引命令即可更新旧解析事实。

重复调用的独立验收可运行 `poc\run_context_demo.bat "D:\cobol-output\context-v3"`。当前应看到 `source_context_acceptance: PASS`、48 项源码检查通过、12 个静态上下文。报告中，同一 SHAREDWK 的两条状态链分别对应 STATUS-A/B；它不证明实际运行状态隔离或错误一定到达入口。该验收失败时退出码为 1，12 个业务案例仍未执行。

局部异常与溢出验收可运行 `poc\run_exception_demo.bat "D:\cobol-output\exception-v4"`。当前应看到 8 个静态上下文、4 项模型预期通过，以及 EXWRAP 状态 91/24、EXJOIN 状态 25 对应事件的模型退出输出为零。详细报告包含分支路径；它不是交易运行记录，子程序正常返回值、循环和隐式作用域等仍有限制。独立覆写反例的运行方法见 [P3-F 报告](./reports/2026-09-08-p3f-exception-path-feasibility.md)。

跨程序错误返回使用 `poc\run_error_return_demo.bat "D:\cobol-output\error-return-v5"`。本次应看到主样例 30 项预期通过，报告按六步实参记录把子程序状态 21 传到入口和汇总；另有复制隔断与调用方覆写 7 的反例。此工具分析受支持源码模型，不执行 COBOL；正常金额仍未知，也不连接问答界面。源码假设、反例命令与退出码说明见 [T01 报告](./reports/2026-09-08-t01-interprogram-error-returns.md)。Windows 入口脚本已提供，本轮未在 Windows 实机验收。


这些演示的源文件、程序名、调查顺序或业务验收条件是预设的。即使某个演示提供 `--source` 参数，也不意味着它支持任意源码；不要用公司的源码覆盖夹具。当前项目尚未用你的公司源码、真实公司 API 或 Windows 实机完成此次验收。
