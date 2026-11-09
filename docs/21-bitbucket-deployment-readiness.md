# Bitbucket 独立索引任务：部署前核定与执行顺序

2026-11-08。下一步先把独立任务的依赖、源码范围和访问能力变成可核对的记录，再在公司环境试运行。本次交付源码与部署准备；没有仓库目标和公司执行环境时，不启用流水线、不上传资料，也不把合成演练记成上线。

## 已确定的部署边界

输入是 Bitbucket 仓库中的固定提交及选定目录，任务独立于同事的每周同步。输出是不可变分析资料；业务电脑按需读取资料和源码片段。候选执行位置是获准的托管 Pipelines，备选是公司已有的批处理环境，不新增常驻服务器。新功能继续与本地分析隔离。

```mermaid
flowchart LR
    A[固定仓库提交] --> B[独立任务]
    B --> C[核验后发布资料]
    C --> D[业务电脑按需分析]
    E[部署前检查] --> B
```

公司外网访问受限的既有条件不变；不能由“源码在 Bitbucket Cloud”推断托管 runner 可达。本文只核查现有条件，不要求修改网络策略。若托管位置不适用，记录该结论并选用已有获准环境，不回头要求用户调查同事的同步机器。

## 先解决的三个问题

1. **工具是否完整、运行环境是否合格。** 当前模板只有“复制工具及依赖”的提示，尚缺可复现文件清单。生成端需要静态依赖闭包、Git 与 SQLite FTS5；还需要分别核查临时索引和资料输出位置的空间与写入能力。源码文件清单和哈希由独立准备工具生成；复制后在隔离目录完成合成 prepare 验证。这里准备的是 Python 源码，不生成应用或可执行验收工具。
2. **源码选择是否会漏成员。** 明确目录、编码、固定/自由格式，以及是否包含无扩展名成员。核查仓库文件扩展名与排除数量，不能把“选中内容均已读入”解释为所有仓库文件均已纳入。具体业务成员是否完整仍需实际仓库样本确认，不依赖同步机器信息。
3. **访问检查证明到了哪一层。** 只读预检核对仓库身份、固定提交和 Downloads 列表读取，可选读取 Pipelines 配置；不读取源码文件正文、不触发任务或写入远端。读取通过只能证明相应读接口可用，上传、CDN 回读、执行环境可达性和配额仍需真实试运行。

## 公司环境的实施顺序

| 阶段 | 动作 | 通过依据 | 未通过时 |
| --- | --- | --- | --- |
| A | 准备工具源码与部署配置 | 文件清单、哈希及隔离合成生成通过 | 修复依赖；不进入企业源码任务 |
| B | 在候选环境执行运行条件检查和只读连接预检 | Git/FTS5/目录可用；固定身份与版本核对；各项有明确状态 | 记录错误或未知，不自动增加权限、不修改网络 |
| C | 在获准环境选定真实源码范围，手动生成并发布一次 | 固定 commit、范围与编码确认；资料完整、上传回读通过，记录 release 名称 | 保留旧发布，候选产物不自动采用 |
| D | 业务电脑按版本接入并核验引用 | 指定原文哈希和行号核验、旧版本可继续使用 | 停留在原发布；记录真实失败 |
| E | 在既有模型授权内核对业务问题 | 人工核对业务回答、引用、追问及缺口；实际规模耗时和资源可接受 | 调整在线链路，不能用合成模型关闭验收 |
| F | 最后设置周任务 | 已验证任务、凭据管理、额度、保留政策及失败处理 | 保持手动执行，不假装周更已自动化 |

每一轮任务只认启动时固定的 commit；定时点或分支暂时不变不能证明本周上游同步完成。新版本发布失败不影响已选旧版本；发布最终响应丢失则按确定 release 名称核对，不能直接重新生成另一个“最新”指针。

## 平台条件与待确认项

Pipelines 配置读取接口要求仓库管理类权限；403 应记为无权确认，不等于 Pipelines 关闭。检查工具不要求业务只读凭据升级为管理员权限。[官方 API](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pipelines/)

Downloads 受实际套餐与权限限制，Free workspace 不可据此规划长期资料分发。普通流水线 artifacts 也不能替代长期资料存储；现有批准的 HTTPS 静态资料读取可以作为另一条接入路径，上传端按实际存储另行接线。[官方套餐变更说明](https://www.atlassian.com/blog/bitbucket/important-changes-coming-to-bitbucket-clouds-free-plan)

托管任务的出网身份与办公室电脑不同，必须在候选位置验证所需 API、Git、镜像和下载位置。发布试运行还要验证实际下载重定向，不自动添加新域名或放宽 TLS。[官方 runner 网络说明](https://support.atlassian.com/bitbucket-cloud/docs/ip-addresses-for-runners-behind-corporate-firewalls/)

资源预算先根据实际源码、临时数据库、导出资料和任务计费分钟记录，不用合成 GB 测量保证生产耗时。保留多少周资料、何时删除旧 commit、费用额度与失败处理人由公司部署配置确认；本轮不自动清理产物，也不自动购买或开通服务。

## 可执行检查

以下命令由部署人员在相应环境执行，不需要用户了解源码同步细节。`stage` 只准备工具 Python 源码和摘要清单，不含企业源码、配置、令牌、测试夹具或应用包；目标目录必须尚不存在。先在本项目根目录生成待审查工具目录，再将其中源码按模板位置交付到批准的任务环境：

```sh
python3 poc/online_deployment.py stage --output /approved/analysis-tools
python3 /approved/analysis-tools/online_deployment.py verify --directory /approved/analysis-tools
```

清单由静态导入关系得到，包含函数内惰性导入；不保证未来动态导入或额外资源自动可用。哈希用于核对文件一致性，不代替工具来源审查。后续源码变化应重新准备并核对整份清单。合成隔离试跑验证这份源码可以完成当前生成路径。

在实际任务运行位置检查运行时和两个已有目录；`--minimum-free-bytes` 是部署人员按实际规模指定的下限，不是系统预测的空间需求。示例的 8 GiB 只用于说明参数，不代表足够处理任意几 GB 企业源码：

```sh
python3 poc/online_deployment.py runtime --temporary-directory /approved/temp --output-directory /approved/material-output --minimum-free-bytes 8589934592
python3 poc/online_deployment.py inventory --source /approved/checkout/src --commit 0123456789012345678901234567890123456789 --subdirectory src --include-extensionless
```

运行检查不构建索引；目录写入检查使用随后删除的小型临时文件。目录扫描只输出汇总，不输出企业文件路径或原文，也不解析源码；Git 检查干净状态时可能读取工作区内容。它会说明扩展名选择、无扩展名文件、Git 跟踪范围和未纳入边界；报告仍不能独立证明上游全部源码已经同步，后续生成端还会再核验文件内容。模板要求显式配置 `SOURCE_ENCODING`、`SOURCE_FORMAT`、`INCLUDE_EXTENSIONLESS`（0 或 1）和磁盘下限，不能让默认扩展名与自动编码悄悄代替部署选择。

在公司可访问目标仓库的环境，以已有只读凭据执行元数据检查。使用 `ONLINE_SOURCE_TOKEN/USERNAME` 和 `ONLINE_MATERIAL_TOKEN/USERNAME`，不需要发布令牌，报告必须是新文件：

```sh
python3 poc/online_preflight.py check --source-repository example/business-source --commit 0123456789012345678901234567890123456789 --material-repository example/analysis-materials --output /approved/preflight-001.json
```

只有该身份已有相应权限且希望核对 Pipelines 状态时才追加 `--check-pipelines`。该操作不读取原文件、不跟随分页或任意远端链接，也不会修改仓库或触发任务。凭据仅进入内存请求；报告只保留所选身份、检查状态、固定错误码和流量汇总，不保存原始 API 响应或文件名。

| 预检退出码 | 报告状态 | 含义 |
| --- | --- | --- |
| 0 | `read_checks_passed` | 请求的只读检查通过，后续执行与写入仍待核实 |
| 2 | `blocked` | 配置、授权、身份或响应核验等出现明确阻断 |
| 3 | `incomplete` | 连接、限流、不可见目标或无权读取配置等使检查无法完成 |

404 无法区分不存在与无权查看，记录未知；Pipelines 配置 403 同样记录未知。即使退出码为 0，报告仍列出尚未验证的执行位置、资源、套餐、上传回读和业务验收，不显示“部署完成”。本机模拟请求通过不代表公司网络检查通过。

完整在线使用与发布契约见[在线工作台](./20-bitbucket-online-workbench.md)，与上游解耦的决定见 [ADR-0019](./adr/0019-index-repository-snapshots-independently-of-sync.md)。真实公司结果单独保存，不覆盖研发演练记录。
