# 办公室电脑使用手册

这份手册是办公室电脑上的实际操作入口。当前版本是命令行 POC：先在本地把 COBOL/COPYBOOK 源码做成事实索引，再用受限工具检索证据；只有在公司批准的接口已经通过能力探测后，才允许 Agent 组织回答。它不是图形界面，也不会自动连接生产系统。

阅读顺序不要跳过：第一次使用按“准备电脑 → 下载项目 → 离线自检 → 清单 → 索引 → 查询 →（可选）接口与 Agent → 日常同步”执行。以后每天从“日常开始”执行即可。

## 1. 先明确哪些东西放在哪里

建议把三类内容分开：

- 项目代码：D:\cobol-work\cobol-evidence-analyst
- 公司批准的源码：D:\cobol-data\premium-source
- 本地结果：D:\cobol-output\premium-2026-08-31

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

如果 git pull 提示本地有改动，先不要强行覆盖。把第 13 节的“保留本地改动”步骤做完，再决定是否提交或暂存。

## 3. 第一次运行：只使用合成样例做离线自检

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
- support_status 通常是 SUPPORTED_WITH_BOUNDARIES；
- 输出目录中出现 structural-index.sqlite、calc-01.json 和 calc-01.md。

这个演示只索引项目内的 synthetic-insurance-v1 合成夹具。它成功不代表已经读到了公司源码，也不代表公司接口可用；它只说明离线事实层可以运行。

## 4. P0：生成源码清单

清单阶段只统计候选文件、编码、行数、程序定义、COPY、CALL、PERFORM 和 EXEC SQL 等摘要。默认报告不放源码文本、绝对路径、相对路径或程序名，适合先做范围确认。

### 4.1 推荐命令

    cd /d D:\cobol-work\cobol-evidence-analyst
    mkdir D:\cobol-output\premium-2026-08-31
    python poc\repo_inventory.py ^
      "D:\cobol-data\premium-source" ^
      --output "D:\cobol-output\premium-2026-08-31\repo-inventory.json" ^
      --markdown-output "D:\cobol-output\premium-2026-08-31\repo-inventory.md"

也可以使用：

    poc\run_inventory.bat ^
      "D:\cobol-data\premium-source" ^
      --output "D:\cobol-output\premium-2026-08-31\repo-inventory.json" ^
      --markdown-output "D:\cobol-output\premium-2026-08-31\repo-inventory.md"

### 4.2 处理特殊文件

AS400 导出成员若没有扩展名，增加：

    python poc\repo_inventory.py ^
      "D:\cobol-data\premium-source" ^
      --include-extensionless ^
      --output "D:\cobol-output\premium-2026-08-31\repo-inventory.json"

公司使用自定义扩展名时，显式加入扩展名（逗号分隔）：

    python poc\repo_inventory.py ^
      "D:\cobol-data\premium-source" ^
      --extensions ".cbl,.cpy,.smartcob" ^
      --output "D:\cobol-output\premium-2026-08-31\repo-inventory.json"

只有在公司批准目录内查看时，才使用 --include-identifiers。这个开关会把程序名、COPY 目标、CALL 目标和相对文件名写入报告；不要把这类报告复制到项目目录、聊天窗口或外部服务。

### 4.3 先看清单再建索引

打开 repo-inventory.md，先确认：

1. candidate_file_count 是否覆盖预期文件量；
2. decoded_file_count 是否明显少于候选数；
3. unreadable_or_binary_file_count 是否为 0；
4. encodings 和 format_hints 是否符合这批导出文件；
5. unresolved_copy_target_count、unresolved_literal_call_target_count 是否需要后续补充文件。

如果大量文件不可读，先处理编码或导出方式，再建结构索引。不要把“解析成功”当成“业务完整”。

## 5. P1-A：建立本地结构索引

结构索引会保存源码相对路径、文件 Hash、程序/段落/字段/关系和必要 EvidenceSpan。它是后续检索的本地数据库，必须留在公司批准环境。

### 5.1 推荐命令

    python poc\structural_index.py ^
      "D:\cobol-data\premium-source" ^
      --database "D:\cobol-output\premium-2026-08-31\structural-index.sqlite" ^
      --report-output "D:\cobol-output\premium-2026-08-31\structural-index-report.json"

也可以使用包装脚本：

    poc\run_index.bat ^
      "D:\cobol-data\premium-source" ^
      "D:\cobol-output\premium-2026-08-31\structural-index.sqlite"

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

当前读取器支持常见 UTF-8、UTF-16、CP950/Big5、cp1252 及 Latin-1 回退。EBCDIC 二进制成员不能直接当作文本分析；先按公司批准流程转换成可读文本，再重新执行清单和索引。不要为了让计数变好看而把二进制文件强行改名为 .cbl。

## 6. P1-B：直接查询四个只读工具

所有调查都必须从已建立的本地 SQLite 开始。工具不接受任意文件路径读取源码；在 Agent 调查中，read-evidence 只能使用本次调查前面步骤已经返回的 Evidence ID。手工调用时也应遵守同一顺序，不要从数据库中猜 ID。

### 6.1 搜索代码

    python poc\investigation_tools.py ^
      --database "D:\cobol-output\premium-2026-08-31\structural-index.sqlite" ^
      search-code OUT-INSTALMENT-PREMIUM ^
      --limit 20

先从精确的 Program、Field、COPY 或 CALL 名称开始，再用业务术语或字段片段做全文检索。结果中的 status、evidence_ref、relative_path 和行号要一起保留。

### 6.2 检查一个符号

    python poc\investigation_tools.py ^
      --database "D:\cobol-output\premium-2026-08-31\structural-index.sqlite" ^
      inspect-symbol OUT-INSTALMENT-PREMIUM ^
      --max-relations 80

如果同名符号很多，增加程序名或类型：

    python poc\investigation_tools.py ^
      --database "D:\cobol-output\premium-2026-08-31\structural-index.sqlite" ^
      inspect-symbol SYNP040 ^
      --symbol-type Program ^
      --max-relations 80

### 6.3 沿关系追踪

    python poc\investigation_tools.py ^
      --database "D:\cobol-output\premium-2026-08-31\structural-index.sqlite" ^
      trace-relations SYNP000 ^
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
      --database "D:\cobol-output\premium-2026-08-31\structural-index.sqlite" ^
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

## 9. 用 Agent 提一个源代码问题

先确保：

1. structural-index.sqlite 对应本次源码批次；
2. 第 8 节能力探测 ready 为 true；
3. 当前终端仍保留三个接口环境变量；
4. 你明确知道本次会把哪些最小必要证据发给公司批准的接口。

然后执行：

    python poc\run_agent.py ^
      --database "D:\cobol-output\premium-2026-08-31\structural-index.sqlite" ^
      --question "分期保费最终是怎样计算出来的？" ^
      --allow-network

批处理包装脚本：

    poc\run_agent.bat ^
      "D:\cobol-output\premium-2026-08-31\structural-index.sqlite" ^
      "分期保费最终是怎样计算出来的？" ^
      --allow-network

包装脚本只转发一个可选参数；若要同时使用 --timeout-seconds、--max-output-tokens 或 --allow-insecure-localhost，请直接运行 python poc\run_agent.py。

建议把完整结果保存到公司批准的输出目录，而不是项目目录：

    python poc\run_agent.py ^
      --database "D:\cobol-output\premium-2026-08-31\structural-index.sqlite" ^
      --question "请列出最终分期公式及其源码证据。" ^
      --allow-network ^
      > "D:\cobol-output\premium-2026-08-31\agent-answer.json"

### 9.1 结果怎么判断

顶层字段重点看：

- runner_status：COMPLETED 表示受控流程正常结束；SAFE_STOP 表示安全停止；NOT_READY 表示没有满足运行条件；
- selected_mode：实际使用 NATIVE_TOOL_CALLING 或 VALIDATED_JSON_FALLBACK；
- agent_result.status：常见为 CITATION_VERIFIED_ONLY 或 ABSTAINED；
- agent_result.answer：带边界的回答文本；
- agent_result.evidence_refs / evidence_ids：回答引用的本地证据；
- agent_result.boundaries：未覆盖的内容、候选关系或安全边界；
- diagnostics：停止原因及可操作线索。

当前实现会校验快照、Evidence 范围、文件 Hash、行号和词面锚点，但没有独立的语义 Claim 支持核验。因此即使引用完整，正常回答也会标为 CITATION_VERIFIED_ONLY，不能把它写成“生产规则已证明”。模型没有足够证据时应返回 ABSTAINED；这是预期的安全结果。

### 9.2 什么问题适合问

适合问“源码快照能直接回答”的问题，例如：

- 哪个程序写入 OUT-INSTALMENT-PREMIUM？
- SYNP040 调用了哪些已确认的程序？
- 分期公式中的字段读写顺序是什么？
- 哪些 CALL 目标在当前快照中仍是动态或未解析？

不适合要求它凭源码猜测生产事实的问题，例如实际费率值、控制表当前记录、运行时动态 CALL 的真实目标、Job Schedule、DB2/DDS 定义或某次生产输入的结果。对于这类问题，看到 ABSTAINED、candidate、unresolved 或 boundaries 应当停止扩展，不要用常识补答案。

## 10. 每天开始和结束时的固定流程

### 10.1 开始工作

    cd /d D:\cobol-work\cobol-evidence-analyst
    git switch main
    git pull --ff-only origin main
    git status

然后确认源码目录、输出目录和数据库路径没有写错。源码有新增或变更时，重新执行第 4 节和第 5 节；不要直接拿旧数据库回答新源码问题。

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
| candidate_file_count 很高但 decoded_file_count 很低 | 扩展名、编码或二进制成员不匹配 | 检查 --extensions、--include-extensionless 和导出编码，先处理不可读文件。 |
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
- [ ] 已查看 inventory 报告的不可读文件和编码统计。
- [ ] 已查看 structural-index-report.json 的 snapshot_id 和 coverage_boundary。
- [ ] 若要联网，已确认公司批准的网关、模型、权限和当前会话 Key。
- [ ] 问题是源码证据问题，不是要求猜生产运行数据。

### 运行后

- [ ] 输出保存在公司批准目录，不在项目目录。
- [ ] 已保存 runner_status、agent_result.status、evidence_refs 和 boundaries。
- [ ] 已区分 confirmed、candidate、unresolved。
- [ ] 没有把 CITATION_VERIFIED_ONLY 写成已完成语义证明。
- [ ] 没有把源码、SQLite、报告或 Key git add。
- [ ] 代码/文档有改动时已通过测试、提交并推送；无公开改动时保持工作区干净。

## 14. 按当前实现可以依赖的行为

下面这些不是宣传口径，而是当前代码可观察到的边界：

- 未显式传 --allow-network 时，company_api.py 和 run_agent.py 不创建真实网络请求。
- 结构索引建立在本地 SQLite/FTS5 上，并保存快照 Hash 与 EvidenceSpan，便于后续检查引用是否仍对应原文件。
- 对未变化文件使用 Hash 跳过重复解析；源码变化后会更新对应索引记录。
- Agent 只能使用 search_code、inspect_symbol、trace_relations、read_evidence 四个只读工具；一次调查最多 6 次工具调用，并在连续无新进展时停止。
- Evidence ID、快照、文件 Hash、行号或工具结果契约不通过时，流程会安全停止或拒答。
- 读取源码证据后，调查范围会关闭；后续只能完成回答或拒答，不能继续无界搜索。
- 代码事实和引用完整性可以由当前实现检查；自然语言结论的独立语义支持仍是边界。

对应实现位置：

- poc/run_agent.py：一次受控 Agent 运行和退出码；
- poc/company_api.py：接口配置、HTTPS 校验、能力探测和安全审计摘要；
- poc/repo_inventory.py：离线清单；
- poc/structural_index.py：SQLite/FTS5 结构索引；
- poc/investigation_tools.py：四个只读调查工具；
- poc/agent_loop.py：工具白名单、预算、Evidence 范围和回答校验。

## 15. 仍需要人工确认的事项

本手册不能代替公司审批。第一次在真实环境使用前，仍需由负责人员确认：

1. 办公电脑是否允许读取目标源码目录；
2. 源码实际编码、AS400 成员导出方式和自定义扩展名；
3. 公司批准的 API 网关地址、模型名、Key 获取方式和联网范围；
4. 哪些输出可以留在本机、哪些可以在团队内部共享；
5. 生产控制表、DDL/DDS、Job Schedule、DB/File 定义和运行日志是否另有可信来源。

只要这些条件没有确认，先完成离线自检、清单和结构索引，不要把 Agent 的候选回答当作生产结论。
