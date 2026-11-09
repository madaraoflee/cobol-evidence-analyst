# 在线分析资料：实现契约

2026-11-07。已实现独立资料模块、Downloads 分包发布、业务调查、历史与网页专属入口，使用和公司环境部署见[在线工作台](../docs/20-bitbucket-online-workbench.md)。本文描述底层资料契约；没有部署到企业仓库，没有运行 Pipelines 或真实模型。架构方向和冻结边界分别见[设计](../docs/19-bitbucket-online-analysis-design.md)与 [ADR-0017](../docs/adr/0017-bitbucket-first-with-local-analysis-frozen.md)。

实现把“生成端需要完整源码”与“查询端需要完整源码”分开：生成端原样调用已有静态索引器，导出分片；资料查询只读清单、命中的分片和需要引用的原文件，不要求本地源码目录或完整源码 SQLite。在线工作区另外使用独立 SQLite 保存元数据、历史和检查点。生成端仍需读取所有纳入范围的源码，并会二次扫描构建词项；跨提交增量计算未实现。周更分析通过独立 prepare/publish 任务选定 Bitbucket commit，不要求了解或修改同事的同步机器、脚本或成功分支；已提供尚未启用的手动／定时流水线模板。

## 模块与入口

| 新增模块 | 职责 |
| --- | --- |
| `poc/online_analysis.py` | 独立 CLI，不读取或写入工作台状态，不调用模型 |
| `poc/online_material_format.py` | 版本、内容地址、清单摘要、路径与 JSON 校验 |
| `poc/online_material_export.py` | 将已有业务稀疏索引只读导出，核对实际源码，保存覆盖边界 |
| `poc/online_material_reader.py` | 目录或同源 HTTPS 读取，按需加载，摘要校验，有界内存缓存 |
| `poc/online_material_query.py` | 词法检索、关系与反向引用、原文核验和引用 |
| `poc/online_source_client.py` | Bitbucket Cloud 固定 commit 的单文件 HTTPS GET |
| `poc/online_distribution.py`、`online_publish.py` | 短对象分包、受控重定向、固定快照或可选同步声明核验与不可变发布 |
| `poc/online_investigation.py` | 有界业务调查、连续追问定位、检查点与引用 |
| `poc/online_workspace.py`、`online_state.py`、`online_web_bridge.py` | 独立工作区、持久历史与鉴权后的 HTTP 分流 |
| `poc/online_acceptance.py` | 公司环境连通、源码及可选模型机制验收，默认不调用模型 |
| `poc/online_preflight.py` | 固定 API 路径的只读元数据检查，区分通过、阻断、未知，不把读权限当成部署完成 |
| `poc/online_deployment.py` | 生成端工具源码清单、复制核对、运行条件及源码选择汇总，不构建应用 |

没有修改本地核心、原回归测试、原索引格式、历史或构建工作流。共享 HTTP 外壳只新增经过原鉴权的 `/api/online/*` 分流和新静态资源；原页面只增加链接，未改本地 API 分支或原页面脚本。生成端使用公开的 `build_business_index`，指定临时数据库和 `framework_reference_path=""`，不隐式载入私有框架配置；生成完成后删除临时数据库。导出器读取已有内部表，但对解析器和 schema 版本严格匹配，版本变化时明确拒绝，需要单独升级导出契约。

## 资料身份与格式

目录包含 `material.json` 和 `objects/<sha256>.json`。所有 JSON 使用规范化序列化。清单的 `revision` 是除自身外整个清单的 SHA-256；每个对象引用记录路径、摘要和字节数。对象先写，清单最后写；只接受不存在的目标目录，失败删除本次新建目录，不覆盖已有修订。该行为仅保证本机生成完整，不代表已实现云端原子发布。

清单格式为 `online-analysis/v1.1`，读取端兼容首阶段的 `online-analysis/v1`，主要字段如下：

| 字段 | 语义 |
| --- | --- |
| `repository.identity / commit / subdirectory` | 仓库身份、完整固定提交、所选仓库内子目录 |
| `analysis` | 稀疏索引种类、解析器及 schema、读取配置、提交核验方式 |
| `coverage` | 实际索引文件/字节数、跳过文件、源范围和未解析边界 |
| `routes.files / terms / references` | 路径、词项与反向关系键的 256 个散列前缀路由 |
| `revision` | 当前分析资料修订，不依赖生成机绝对路径或时间戳 |

文件路由保存路径、源码 SHA-256、大小、编码、格式、物理行数和事实页引用。事实页包含符号、关系和规则的精简元数据与行号，不复制原始源码页或 `normalized_text`；它仍包含业务名称和条件等敏感派生信息，须按源码权限管理。最多 16 路径且 4 KiB 内的短 postings 内联，其他词项与反向关系路由指向最多 256 路径一页的 postings；事实页最多 256 行记录且约 1 MiB。

词项来自实际全文逐物理行扫描及路径/符号名称，因此不依赖 Bitbucket 原生搜索的文件大小门槛。检索做 NFKC、大小写和连字符处理，按命中词数排序，是词法候选发现；不等于语义搜索、唯一入口解析或业务问题已经回答。查询 `incoming` 的目标是源码中的 CALL/COPY 名称，例如 COPY 名称可能是 `RECORDS` 而非文件路径 `copy/RECORDS.cpy`。保留关系原有的 `confirmed / candidate / unresolved` 状态，不因名称匹配而消除歧义。

`coverage.scope` 始终为 `indexed_snapshot_only`。Git 核验只证明版本对应，不证明仓库里每个文件都已解析；跳过文件、动态目标、稀疏语义、未导出的规则 occurrence 细节会继续暴露为边界。没有命中只表示当前词法检索未命中，不建立“业务逻辑不存在”的结论。

## 发布依据与上游同步边界

发布 CLI 必须明确选择 `--repository-snapshot` 或 `--sync-receipt`，不能同时使用，也不会在缺少凭据时静默降级。两种模式共用 Git 核验、对象摘要、分包上传与回读、清单和 release 最后写入的规则，均不能发布 `caller_declared` 的未核验提交资料。

仓库快照模式发布 `online-release/v2`。它保留 `repository / revision / manifest / bundle_routes`，以 `source_snapshot` 代替旧版的 `sync_receipt`。快照字段为：

```json
{
  "schema_version": "repository-snapshot/v1",
  "repository": "example/source",
  "commit": "0123456789012345678901234567890123456789",
  "subdirectory": "src",
  "sync_completeness": "not_verified"
}
```

该描述从已核验清单生成，仓库、提交和范围必须严格一致；不加入时间戳，以保持同内容重复发布身份不变。`sync_completeness` 只接受 `not_verified`，不能借仓库快照声明同步已完成。定时任务选到的提交可能处于一次多 push 的同步中间，但资料仍可一致地描述该固定提交；版本由用户明确采用，不自动取代旧版本。

原有 `online-release/v1` 与 `source-sync/v1` 继续兼容。只有实际取得同步方的完成声明时才用该模式；声明也不能独立证明上游没有遗漏。用户仅知道每周同步一次时，直接使用仓库快照模式，无需伪造 `complete=true` 或提供同步环境详情。两种模式的 `published` 都仅指资料发布成功。

## 生成与查询

从项目根目录运行，Python 依赖沿用现有源码运行环境。这里的 `build` 只生成分析资料，不构建 app。

```sh
python3 poc/online_analysis.py build --source /approved/checkout/src --output /approved/materials/revision-001 --repository example/source --commit 0123456789012345678901234567890123456789 --subdirectory src --source-format free
python3 poc/online_analysis.py inspect --material /approved/materials/revision-001
python3 poc/online_analysis.py search --material /approved/materials/revision-001 PAYMENT-STATUS
python3 poc/online_analysis.py relations --material /approved/materials/revision-001 main.cbl --relation-type CALLS
python3 poc/online_analysis.py incoming --material /approved/materials/revision-001 WORKER-ENTRY
```

生成命令默认验证 Git HEAD 与指定 commit 相同，源码目录对应仓库子目录，范围内无已暂存/未暂存变更，实际索引文件已由 Git 跟踪。生成前后检查，导出时逐文件核对索引哈希。输出父目录须已存在。只有合成或离线验证可加 `--unverified-source`，清单会标为 `caller_declared`；它不能证明资料来自真实远程 commit。

查询也可将 `--material` 换为 `--material-url https://approved.example/materials/revision-001/`。可用 `--expected-repository`、`--expected-commit`、`--expected-revision` 锁定已选资料，发现不匹配立即拒绝。这些 expected 值应来自可信的版本选择；自包含摘要保障一致性，不是发布者签名。Downloads 已实现经过账户授权的不可变 release 发现与显式选择，不使用可变 `latest` 指针；不会自动选择上传时间最新的文件。

按固定提交读取原文：

```sh
python3 poc/online_analysis.py evidence --material /approved/materials/revision-001 main.cbl 10 30
```

原文从清单指定的 `workspace/repository`、完整 commit 和子目录读取；不会读取本机源目录，不解析移动分支，也不会下载 ZIP。当前为核对 SHA-256，取证会读取**被选中文件的完整字节**，然后只返回要求的行区间；不是 HTTP 按行读取。返回引用绑定资料修订、commit、路径、文件哈希及实际返回行号；字符限额导致行内截断时明确标记。

凭据仅从进程环境变量读取并在内存使用：`ONLINE_MATERIAL_TOKEN / ONLINE_MATERIAL_USERNAME` 与 `ONLINE_SOURCE_TOKEN / ONLINE_SOURCE_USERNAME` 分开。提供 username 时用 Basic，未提供时用 Bearer。CLI 不读取 `.env`、不保存凭据、不在异常中输出响应体或令牌。用户不应把令牌写入 URL 或命令行参数。

CLI 查询返回 `transfer`：`fetched_bytes` 和 `fetched_objects` 是逻辑资料读取量，前者含清单；`source_bytes / source_requests` 是原文读取量。工作区任务另记录 `network_material_bytes`，含实际 Downloads 分包开销。同一 reader 内保留有界内存 LRU，调查会话另外使用 32 MiB 原文内存缓存；跨进程不保存完整源码缓存，已引用片段会随独立历史和检查点持久化。

## 明确上限与失败行为

首阶段的单源码文件上限为 32 MiB；超过会阻止导出或取证，不能静默排除。单清单/路由对象上限 8 MiB，路由桶溢出须重新设计分层路由。单次 reader 操作默认最多 64 MiB 对象字节、2,048 对象、100 万条目；LRU 默认 32 MiB。这些限制有独立的错误码，不作为“无结果”返回。

查询还限制 64 个词项、10 万候选、50 万词项匹配/扫描事实，以及跨 reader 调用 128 MiB 传输预算；传输预算在每次 reader 调用后检查，最多超出一次 reader 操作额度。限制只界定当前实现的资源风险，不保证全局影响分析均可在额度内完成，也不等于进程总内存上限。

通用 HTTPS 客户端拒绝所有重定向，设 30 秒网络读取超时，不自动重试；401/403/404/429、长度不符、源码不符、取消、对象缺失及格式损坏分别失败。新增专用 Downloads 适配器处理允许域名的 302，并移除跨域 Authorization；它读取按内容地址发布的包，按实际包字节计预算。通用 HTTPS 适配器仍不能当作 Downloads 适配器使用。Git LFS 未实现；源码 404 表示此次该版本读取未取得，不代表业务实现不存在。

## 已验证与后续顺序

测试覆盖真实稀疏索引导出、跨生成目录一致性、同内容对象跨 commit 复用、输入只读、取消清理、并发数据库读取、超过 512 KiB 文件、失效证据、损坏分片、权限/限流、查询预算、混合编码和精确行号。整链测试删除生成端源码及临时数据库后再查询，用模拟原文传输核验引用，没有把它当作真实 Bitbucket 认证验证。

合成规模测量、分包传输、HTTP 业务调查及可复现脚本见[验证记录](../docs/reports/2026-11-07-online-materials/README.md)。它们不证明真实周更、云端延迟、企业 GB 性能或模型业务正确性。

独立调查、对话/续查/引用状态、网页入口及发布脚本已经实现；公司长期资料存储资格、真实认证、云端请求开销和业务效果仍须验收。原本地核心继续冻结。当前网页旧 Bitbucket 入口仍会生成本机快照，免整库下载请进入独立的“Bitbucket 在线分析”。
