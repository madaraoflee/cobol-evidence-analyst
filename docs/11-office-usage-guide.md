# 办公室电脑使用手册

公司源码按“快速接入真实目录 → 选择实际程序 → 局部详细索引 → 提问”使用。一万多个大程序不应在首次接入时全部逐语句解析。API Key 配好只解决模型连接；替换演示夹具不会让固定样例调查自动变成公司业务分析，直接运行旧 `run_agent.py --database ...` 也不会读取新换的源码文件夹。

本手册覆盖已接通真实源码和 API 的 `poc/web_app.py` 工作台，以及等价的 `analyze_source.py` 命令行入口。第一次按第 1～3 节准备，再按第 8～9 节配置接口和提问；第 4～7 节是小范围研发排查工具，会读取更多正文，不是大库首次导入步骤。简版入口见[系统使用手册](./15-system-user-manual.md)。

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

本项目只使用 Python 标准库，不需要执行 pip install。本次目录性能验证使用 Python 3.14.4；8–16 GB 内存电脑应把源码和结果放在本机 SSD/硬盘，并采用目录接入和按入口分析。索引依赖 Python 自带的 SQLite FTS5；如果后面出现 “This Python SQLite build does not include FTS5 support.”，按第 12 节处理。

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

不要覆盖 `poc\fixtures`。把取得的程序、COPYBOOK 和相关源码放在独立目录，`--source` 指向同时包含它们的共同上级目录。即使只有入口程序，也可以分析其中可见的条件、计算、参数与调用；缺少被调用程序、COPY 或只有闭源对象时保留未知边界，不要求先补齐整个框架。输出目录必须独立于源码目录。

### 3.1 推荐使用网页快速接入

从项目根目录启动：

    python poc\web_app.py --port 8765

也可以直接双击 `poc\run_web.bat`。打开 `http://127.0.0.1:8765`，切换至“本机源码”，填写独立的源码目录和输出目录后接入。页面支持简体、繁體和 English；示例预览与真实源码模式分开。网页默认 `catalog` 轻量目录，先显示程序与文件清单；选择程序并发起分析时才建立其局部详细索引。接口配置自动读取项目根目录的 `.env`，首次填写方法见第 8 节，以后启动无需重新输入。

进度区域显示当前阶段、当前文件、已完成/总量、已用时间和可估算时的剩余时间。发现文件阶段尚无总数；解析和模型阶段不能可靠估算时会明确显示正在执行，不用假百分比。页面可取消任务；取消在处理边界生效。重新接入时沿用同一输出目录，已完成的目录检查点可以复用。

### 3.2 等价命令行接入，暂时不用 API

从项目根目录执行，替换两个路径：

    python poc\analyze_source.py ^
      --index-mode catalog ^
      --source "D:\cobol-data\source" ^
      --output "D:\cobol-output\analysis"

或使用等价的 Windows 包装脚本：

    poc\run_analyze.bat ^
      --index-mode catalog ^
      --source "D:\cobol-data\source" ^
      --output "D:\cobol-output\analysis"

`--source` 和 `--output` 都是必填项；这个入口没有默认样例目录。这里必须明确保留 `--index-mode catalog`，因为 CLI 为兼容研发脚本仍默认 `full`。目录通常先读取每文件 16 KiB，未找到 PROGRAM-ID 时最多探测 256 KiB；不先为全库生成语句索引。未变化文件按大小、mtime/ctime 时间戳和文件身份复用头部缓存，正文读取为零；换目录、删除文件或修改源码后，需要重新运行同一条命令。不要依靠修改文件夹后继续查看旧报告来判断本次效果。

新入口默认纳入无扩展名的成员；如果无扩展名文件与源码无关，加 `--exclude-extensionless`。自定义扩展名加 `--extensions ".cbl,.cpy,.member"`，它会替换默认允许列表，因此要包含这批源码实际需要的所有后缀。

### 3.3 先看“读到了什么”，再判断能否提问

输出目录中重点查看：

| 文件 | 用途 |
| --- | --- |
| `diagnosis.md` / `diagnosis.json` | 本次源目录、快照、文件和程序覆盖情况、编码及解析问题。先打开 Markdown 阅读。 |
| `programs.json` | 实际发现的程序及其来源，后续 `--entry` 从这里选择。 |
| `source-catalog.sqlite` | 持久轻量目录与逐文件检查点。保留原输出目录才能增量复用。 |
| `structural-index.sqlite` | 选择入口后才生成的局部事实索引，供证据调查使用。 |
| `agent-result.json` / `agent-result.md` | 本次问答状态与结果；未提问时为 `NOT_REQUESTED`，没有开启联网时为 `NETWORK_DISABLED`，不会用上一次的回答充当新结果。 |

`INDEX_READY` / `SOURCE_CATALOG_READY` 表示目录可选择，尚未完成全库结构分析。清单中的 `name_origin=path` 表示暂按文件名选择，不表示 PROGRAM-ID 已确认；有界头部也可能漏掉尾部嵌套程序。`NEEDS_ATTENTION` 是需要阅读的覆盖诊断，缺 COPY/CALL 并不自动阻止局部问答。无文件、无可明确识别的入口、快照损坏或接口条件不满足时仍会停止。

目录 `catalog-sha256:` 指纹只表示当前目录和缓存状态，不代表全文件内容已 Hash 核验。如果导出工具可能保留原文件元数据，使用 `--verify-content` 强制流式读取每个候选文件全文并计算摘要；这可能很慢，但不会为全库建立逐语句索引。选定入口后，证据内容会另行核验。

先核对报告中确实出现自己的程序名与预期文件量。只有部分程序出现时，检查读取选项和头部覆盖边界；“解码成功”不代表 COBOL 结构已识别，更不代表业务含义已证明。报告及程序清单含本地路径和源码标识符，应保留在公司批准的位置。

### 3.4 编码与源码格式不对时

若知道导出编码，直接指定，避免自动检测把文件当成另一种可解码文本。例如简体中文 Windows 导出：

    python poc\analyze_source.py ^
      --index-mode catalog ^
      --source "D:\cobol-data\source" ^
      --output "D:\cobol-output\analysis" ^
      --encoding gb18030 ^
      --source-format fixed

`--encoding` 默认 `auto`；实际为 CP950 时用 `--encoding cp950`，其他编码须用导出工具确认的名称。不要因为文件能打开就假定编码正确。EBCDIC 成员应按实际字符集和记录布局导出或转换成文本后再核对，直接给二进制文件改扩展名无法解决。

`--source-format` 可为 `auto`、`fixed`、`free`。固定格式的序号列、指示列及有效代码区会影响解析；自由格式导出应使用 `free`。这里的选项适用于本次目录，混合格式可先尝试 `auto`，再根据诊断拆分处理。第二次提问必须保留已经确认的编码、格式和扩展名选项。

看到真实目标程序后直接进入第 8～9 节。接下来的第 4～7 节用于独立检查底层索引和证据，正常使用不必重复执行一遍。

## 4. 底层工具：独立生成源码清单（可选）

本节旧清单工具会读取完整正文以统计编码、行数、程序定义、COPY、CALL、PERFORM 和 EXEC SQL 等摘要；大库快速接入不需要运行它。默认报告不放源码文本、绝对路径、相对路径或程序名，适合先做范围确认。

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

本节直接调用的结构索引工具默认详细解析全部输入文件，仅用于已选定的小范围，不要把一万多个大程序目录直接交给它。结构索引会保存源码相对路径、文件 Hash、程序/段落/字段/关系和必要 EvidenceSpan。它是后续检索的本地数据库，必须留在公司批准环境。

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

重复运行同一批选定源码时可增量更新；详细解析与证据引用使用内容 Hash。轻量目录另按文件 stat 复用，二者的验证范围不同。为了避免回答引用旧源码，每次更换源码批次都应使用新的输出目录或明确覆盖同一数据库后重新查看新的 snapshot_id。

### 5.3 编码边界

底层清单与索引也可显式指定 `--encoding` 与 `--source-format`。自动检测或回退只能说明文本可解码，不能保证所选字符集就是原始导出编码；诊断显示回退、乱码或零程序时，应按第 3.4 节核对。清单与索引必须使用同一组读取参数。

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

### 8.1 配置一次本机 .env

从 GitHub 下载源码后，在项目根目录（与 `README.md` 同级）将 `.env.example` 复制为 `.env`，用文本编辑器填写以下三项；这些内容保存在文件中，不是在终端执行的命令：

```dotenv
COMPANY_API_BASE_URL=https://approved-gateway.example/v1
COMPANY_API_KEY=填入自己的接口密钥
COMPANY_CHAT_MODEL=填入公司的模型名或部署别名
```

三个示例值都需要换成公司实际提供的值。API 根地址应包含接口要求的版本前缀（例如 `/v1`），生产地址使用 HTTPS。`COMPANY_API_STYLE` 默认 `openai_compatible`，一般不必修改；`COMPANY_EMBEDDING_MODEL` 不是问答必需项。

文件保存为 UTF-8，支持 Windows 换行和 UTF-8 BOM。请在资源管理器显示扩展名，确认不是 `.env.txt`。值可使用单引号或双引号，`$` 等特殊字符按原样保留，不执行变量展开或命令。不要把密钥填进 Python、`.bat`、Git 配置、SQLite 或输出报告。

以后双击 `poc\run_web.bat` 即可，网页和命令行分析都自动读取项目根目录的 `.env`，不需要每次配置，也不依赖启动终端当前在哪个目录。修改 `.env` 后重启工作台；已有环境变量优先于 `.env`，显式命令行参数优先于二者。若修改未生效，检查启动进程是否保留旧的同名环境变量。

`.env` 已被 Git 忽略，只保留在公司电脑。使用 `git pull` 更新时保留该文件；如果下载到新的项目目录，把自己的 `.env` 复制到新项目根目录。不要用新的空白 `.env.example` 覆盖已经填好的 `.env`。网页只显示配置是否可用，不接收或保存 API Key。

### 8.2 先做能力探测

    python poc\company_api.py --allow-network

探测会验证基本 Chat、原生工具调用及工具结果回传、严格 JSON。输出只记录安全摘要，不记录 Key、完整 URL、模型名、请求体或响应体。成功时 agent_readiness.ready 为 true，并且 mode 是 NATIVE_TOOL_CALLING 或 VALIDATED_JSON_FALLBACK。

退出码含义：

- 0：探测完成，并且 Agent 所需模式可用；
- 1：请求完成但某项能力不满足，不能启动 Agent；
- 2：没有联网、配置不完整或配置无效。

Embedding 不是 run_agent.py 的必需项。只有拿到批准的嵌入模型时，才额外设置 COMPANY_EMBEDDING_MODEL 或传 --embedding-model，并执行 --probe-embeddings。

## 9. 选择实际程序，用 Agent 提问

在第 3 节输出的 `programs.json` 中选择一个实际程序，把下面 `实际的PROGRAM-ID` 换成其名称；也可使用报告里的唯一相对路径或 `entry_key`。目录中存在同名程序时从网页选择具体文件，或复制该条目的 `entry_key`；详细解析仍无法明确入口时应按版本拆分。不要照抄演示中的 `SYNP040` 或保费字段；如果报告里根本没有目标程序，先处理接入问题。

确认第 8 节接口可用后，从项目根目录执行；命令会自动读取 `.env`：

    python poc\analyze_source.py ^
      --index-mode catalog ^
      --source "D:\cobol-data\source" ^
      --output "D:\cobol-output\analysis" ^
      --entry "实际的PROGRAM-ID" ^
      --question "请解释该程序的主要处理步骤、输入输出和调用，并引用源码行号。" ^
      --allow-network

也可以把第一行换成 `poc\run_analyze.bat`，后续参数不变。首次离线读取时如果指定过编码、格式或扩展名，这里也必须带上相同参数。

这条命令会先增量刷新目录，再详细解析所选入口与可发现的 COPY/CALL 邻域，检查内容快照后才做 API 能力探测及问答。默认范围最多 24 个文件、两层依赖及 16 MiB 源文件总量；依赖发现每文件最多读前 2 MiB，所选文件随后按完整文件详细解析。未取得的依赖、尾部未扫描区域或达到预算的目标都会列为边界。单个入口超过 16 MiB 时目录继续可用，本次详细问答返回 `SCOPE_LIMIT`；选择较小入口或按业务范围重新导出后继续。`--entry` 和 `--question` 可以按需要使用，但首次调查建议同时给出真实入口与具体问题；仅有抽象中文业务词，未必能命中没有业务注释的 COBOL 标识符。`--entry` 确定调查起点，不代表整个调用链已完整解析。

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

普通自然语言陈述仍为 CITATION_VERIFIED_ONLY，表示引用有效、语义未核验。结构化断言与源码不匹配，或源码语法超出当前核验范围时，会保留拒绝原因；有局部证据且仍有缺口时可返回 PARTIAL；完全没有足够可用证据时可返回 ABSTAINED。这些结果说明此次调查的实际边界，不应人工改成支持状态。

缺少 AS400 Smart DXC 框架源码、COPY 或封装对象时，先解释可见源文件里的业务步骤、条件与传参，闭源实现和运行结果仍为未知。不能为使流程“通过”而假设某个封装对象必定成功。两次无新进展（`no_progress`）后，如果已发现证据且还剩工具预算，Agent 会读取已有证据尝试形成局部回答；无证据、完整性失败或预算耗尽时仍明确停止。

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

如果只看到你打算提交的公开代码或文档改动，才按团队约定提交并推送。若看到源码、数据库或报告，先移出项目目录或检查 `.gitignore`；本机 `.env` 已被忽略，应留在项目根目录供服务读取。若它仍出现在待提交清单中，先排除提交，不要上传密钥。`.env.example` 是可提交的空白模板。

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
| 新入口没有可用目录入口 | 指错目录、后缀过滤、编码/格式错误，或只有 COPYBOOK | 打开 diagnosis.md；核对源目录、--extensions、--encoding 与 --source-format，确认 programs.json 中出现真实程序后再联网。 |
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
| Agent 返回 PARTIAL | 已有可引用的局部结论，仍有缺依赖或未知运行行为 | 使用已有解释并阅读 boundaries；闭源对象无须伪造源码补齐。 |
| Agent 返回 SAFE_STOP 或 ABSTAINED | 没有可用证据、快照/契约失败或预算耗尽 | 保留 diagnostics；有证据时 no_progress 可尝试回收局部回答，不能人工填空。 |
| 导入一直慢 | 使用了 full 或强制内容校验，或大量文件需首次探测 | 网页默认目录模式；CLI 加 --index-mode catalog，保留原输出目录，看阶段、计数和缓存命中。 |
| SCOPE_LIMIT | 单入口超过 16 MiB 详细预算 | 目录可继续使用；换较小入口或按业务范围重新导出。 |
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
- analyze_source.py 要求显式源目录；公司大库使用 --index-mode catalog 增量目录与按入口解析，网页默认相同模式。目录复用不代表全文核验；不可识别入口和证据失败仍明确停止。
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
