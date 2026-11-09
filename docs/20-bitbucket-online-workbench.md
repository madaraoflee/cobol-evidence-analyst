# Bitbucket 在线分析：使用与部署

2026-11-07。独立在线工作区、业务调查、续查、引用、历史、版本选择与资料发布均已实现。当前企业仓库从外网不可达，这是既有访问条件；本次不修改网络策略。真实发布、账号权限、公司模型效果及目标设备验收须在能访问这些服务的公司环境执行。

分析从 Bitbucket 中选定的固定提交开始，与同事的 AS400 同步机器和脚本独立。用户只需知道源码每周更新，不需要了解或改造同步过程。独立索引任务仍需读取纳入范围的完整源码，业务人员的电脑则按问题读取分析资料和原文。不需要新增常驻查询服务器，不承诺现有套餐完全免费或没有计算、存储和 API 配额成本。

优先验证 Bitbucket 托管 Pipelines 能否作为批处理运行位置，前提是公司套餐、权限和现有网络条件允许。源码放在 Bitbucket 不等于已经启用索引任务，也不能据此推断托管 runner 能访问公司要求的全部服务。如果托管任务不适用，再选择公司已有的获准批处理环境；复用同步机器只是可选优化，不是前置条件。

## 日常使用

使用项目已有源码启动方式打开工作台，在侧栏选择 **Bitbucket 在线分析**，或打开同一本机服务的 `/online`。原有“从 Bitbucket 接入”仍是本机快照模式；新入口不迁移它的源码、索引和对话。

首次接入填写源码仓库链接，选择分析资料的位置。使用 Downloads 时，填写保存资料的仓库，读取已发布版本列表并明确选择版本；选择后可核对源码 commit。资料仓库可以与源码仓库不同。使用公司已批准的 HTTPS 静态位置时，填写资料目录地址及固定 commit，必要时填写资料修订以严格核对。

读取授权分为资料和源码两组。两者使用同一账号时可主动选择沿用；退出本机服务后，凭据不会保留。再次打开可阅读既有历史，继续调查前点击“更新授权”。修改资料位置不能自动把旧位置的令牌带过去。选择“结束授权”会清除当前内存授权并停止相关任务，历史不会因此删除。

提出业务问题时，页面使用原来已经配置的公司模型通道；勾选允许发送本次分析所需片段后开始。系统按问题检索资料、读取关系、回读原文并核对哈希，再形成带引用的说明。资料和源码内容被当作待分析数据，不当作操作指令。模型生成的业务解释仍需业务人员验证；存在引用不等于业务结论已经被独立证明。

每轮工作有明确的请求、原文和上下文预算。达到预算、手动停止或遇到连接问题时，问题、已核对资料和待查动作会保存。点击“继续调查”从检查点继续，不把部分调查说成完整。模型请求结果未知时，页面单独要求“确认重新调用模型”，避免不知情地重复调用。已经收到并保存的模型响应可直接恢复。

连续追问会带上先前问题、回答和版本绑定的引用定位线索；需要引用旧依据时会重新读取核验，不直接把旧引用编号当作本轮证据。历史或线索因预算省略时会向调查模型明确标记，不能承诺无限上下文。

引用按钮显示对应的源码片段及固定版本。对话可导出 Markdown 或 JSON，包含问题、回答、引用、范围与版本。新一周发布后，选择“选择新版本”；新版本进入独立工作区，旧对话仍指向旧 commit，不改写历史。发布失败、权限失效或资料校验失败时不自动覆盖旧版本。

## 一次性部署准备

正式接入前先执行[部署前核定与执行顺序](./21-bitbucket-deployment-readiness.md)：准备完整工具源码、检查运行资源和源码选择，再进行只读连接检查。首次手动发布及引用核验通过后才开启周任务；单纯读取接口成功不代表托管任务、上传权限或套餐已经验收。

部署人员需要为独立任务配置 Python 源码运行环境、Git、获准读取的干净源码 checkout，以及可长期保存派生资料的位置。这些是分析任务的执行条件，不要求业务用户调查同事的同步环境。分析工具项目应与被分析源码分开存放，避免把工具自带的合成案例混进企业资料范围；Pipelines 模板要求选择不含 tooling 的源码子目录。生成端复用现有静态索引器，新在线模块和网页资源随项目源码一起分发；本次未生成 app 或安装包。

Downloads 是否可用取决于公司 workspace 的实际套餐与权限；不能把 Pipeline 短期 artifacts 当作唯一长期资料来源。Bitbucket 原生文件上传接口支持多文件 multipart，下载可能跳转到对象存储；实现只向 Bitbucket API 发送仓库授权，跳转到允许的 HTTPS 下载域名时移除 Authorization。[官方 Downloads API](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-downloads/)

当前允许的下载域名为 `bbuseruploads.s3.amazonaws.com` 和 `cdn.bitbucket.org`，不接受任意通配域名或自动放宽证书校验。若真实账号返回其他官方端点，应先核对，再为发布 CLI 显式配置 `--download-host`；工作台适配也须按经确认的域名更新。不能用绕过 TLS/鉴权来解决网络或权限问题。

发布账号需要写入资料仓库 Downloads 的权限；业务人员使用只读账号。将凭据放在公司批准的进程环境或流水线安全变量中，勿写进源码、URL、同步凭据或提交记录：

| 用途 | 环境变量 |
| --- | --- |
| 自动发布 | `ONLINE_PUBLISH_TOKEN`、`ONLINE_PUBLISH_USERNAME` |
| 资料只读验证 / CLI | `ONLINE_MATERIAL_TOKEN`、`ONLINE_MATERIAL_USERNAME` |
| 源码只读验证 / CLI | `ONLINE_SOURCE_TOKEN`、`ONLINE_SOURCE_USERNAME` |
| 业务模型 | 沿用现有公司模型配置，不新增模型供应方 |

使用账号邮箱与 API token 时同时填写 username 和 token；适用 Bearer 的令牌不填写 username。网页中的这些凭据只进入当前运行内存，不保存到浏览器存储或状态数据库。

## 独立生成与发布

默认使用仓库快照模式：选定 Bitbucket 仓库、源码范围和完整 commit，在独立任务中取得该提交的干净 checkout，再生成并发布资料。无需同步程序提供完成凭据。在工具项目根目录运行：

```sh
python3 poc/online_publish.py prepare --repository-snapshot --source-repository example/business-source --commit 0123456789012345678901234567890123456789 --subdirectory src --source /approved/checkout/src --output /approved/materials/revision-001 --encoding auto --source-format auto
python3 poc/online_publish.py publish --repository-snapshot --material /approved/materials/revision-001 --workspace example --repository analysis-materials
```

任务会核对 checkout 的 HEAD、干净状态、源码范围和实际索引文件的 Git 跟踪情况。发布记录使用 `online-release/v2`，保存 `source_snapshot`，并明确标记 `sync_completeness: not_verified`：它表示分析绑定这个仓库提交，**不表示已经独立确认本周 AS400 同步全部完成**。

可以先手动选择提交运行，再按需要配置每周定时任务。[Pipelines 官方文档](https://support.atlassian.com/bitbucket-cloud/docs/pipeline-triggers/)支持手动和定时触发；定时任务运行所选分支的最新提交，流水线定义须存在于所选分支。参考[未启用的模板](./templates/online-material-pipeline.yml.example)，使用任务的 `BITBUCKET_COMMIT` 固定本次版本，不需要改造同步脚本。模板没有作为活动配置启用，也没有实际设置定时任务。

如果一次同步有多次 push，定时任务可能取得中间提交；固定 commit 能保证读取一致，不能证明同步周期结束。新资料只有通过构建与内容核验后才成为可选版本，用户明确选择后才用于新对话；旧版本和旧对话不自动切换。没有上游完成声明时，不显示“本周完整同步已确认”。

### 可选的同步完成声明

如果将来同步方已有可提供的完成声明，可以使用原有凭据模式，绑定最终 commit 和源码范围。这是可选能力，系统不要求用户取得同步程序、操作系统或运行机器的信息。例如：

```json
{
  "schema_version": "source-sync/v1",
  "repository": "example/business-source",
  "commit": "0123456789012345678901234567890123456789",
  "subdirectory": "src",
  "complete": true,
  "completed_at": "2026-11-07T08:00:00Z",
  "sync_id": "weekly-20261107"
}
```

这份文件是同步方的完成声明，不是系统独立证明 AS400 没有漏导出。必须来自真实成功判定，不能仅因为到了星期几就填 `complete=true`。没有该声明时使用上面的仓库快照模式，不伪造凭据。来源 checkout 的 HEAD、干净状态和实际索引文件是否受 Git 跟踪还会再次核对。生成目录必须与 `subdirectory` 对应。

在生成机器上，从工具项目根目录运行：

```sh
python3 poc/online_publish.py prepare --source /approved/checkout/src --output /approved/materials/week-001 --sync-receipt /approved/sync-complete.json --encoding auto --source-format auto
python3 poc/online_publish.py publish --material /approved/materials/week-001 --sync-receipt /approved/sync-complete.json --workspace example --repository analysis-materials
```

该模式继续发布兼容的 `online-release/v1`。每条命令明确选择 `--repository-snapshot` 或 `--sync-receipt`，两者不能同时使用；省略凭据不会隐式切换模式。

### 两种模式共用的处理规则

Windows 中可以用同样的 Python 脚本并将路径改为公司路径。对于已知 EBCDIC 或固定格式，应填写实际 `--encoding` / `--source-format`，不要把自动检测当作企业编码验收。当前原样使用默认静态解析能力，不自动导出机器上的私有框架文件。

首次生成是全量静态处理；以后也是在生成端建立当前提交的资料。相同内容对象及分包可复用发布，但**这不是跨提交增量解析**。同一输出目录不覆盖，应使用新目录；企业保留与清理政策需由管理员执行，代码不自动删除旧 commit 或旧发布资料。

发布会先遍历并核验所有可达对象，将小对象合并成内容地址包，分批上传并逐件回读核验，最后发布清单与不可变 release。客户端只发现完整 release；没有可被旧任务覆盖的全局 `latest` 指针，晚完成的旧任务不会静默切换用户正在分析的版本。

发布最后一步的响应丢失时，远端可能已经保存完整 release。CLI 返回 `publication_unknown`、确定的 `release_name` 和退出码 3；这时先核对该 release，或重跑相同内容的发布确认，不能按“肯定没发布”处理。失败留下的内容地址包不代表可用版本，也不自动删除共享包。

```sh
python3 poc/online_publish.py list --workspace example --repository analysis-materials
python3 poc/online_publish.py inspect --workspace example --repository analysis-materials analysis-release-REPLACE_WITH_RETURNED_HASH.json
```

独立任务可以手动或定时调用 prepare / publish，任何非零退出都不能记为已确认发布。公司当前的网络可达性必须先确认，不能假定托管 runner 等同于公司内网机器。本次既不修改同步流程，也不新增网络限制。

## 在公司网络内做真实验收

先发布一个已批准的小范围资料，保留 inspect 返回的完整 commit、revision 和 release 名称。以下命令只核对资料与一个指定源码片段，不调用模型，不把原文写进验证报告：

```sh
python3 poc/online_acceptance.py --release-name analysis-release-REPLACE_WITH_RETURNED_HASH.json --material-repository example/analysis-materials --repository example/business-source --commit REPLACE_WITH_SOURCE_COMMIT --revision REPLACE_WITH_MATERIAL_REVISION --path main.cbl --start-line 1 --end-line 20 --output /approved/check-001.json
```

使用 HTTPS 静态目录时以 `--material-url` 替代 `--release-name`。报告为新文件，拒绝覆盖。需要做真实模型机制检查时，另外提供 `--question` 与 `--allow-source-transfer`，它仍走既有公司模型配置。脚本会把模型调用状态和引用数量记为机制结果，始终保留 `business_answer_quality: not_assessed`；业务正确性需要人工核对真实问题、预期分支及引用。

随后在公司电脑启动工作台，验证新接入、真实问题、连续追问、停止/继续、重启授权、引用和导出；再发布下一周测试版本，确认新对话采用新版本、旧对话仍回读旧版本。最后在实际规模及变更比例下测生成耗时、上传请求、冷查询字节、限流、内存和模型前准备时间。合成 GB 样本不能替代这些检查。

## 当前边界

分析资料及对话也是源码派生资料，须保存在公司批准的位置。在线状态保存在独立 `online/online-state.sqlite` 中，只用于工作区、历史、检查点和已引用片段；不会下载完整源码 SQLite 索引。凭据不落盘，但已经保存的引用不会因远端撤权自动消失；保留和删除仍受公司资料政策约束。

目前单原文件上限 32 MiB，单资料对象上限 8 MiB；超限明确失败。默认分包目标约 1 MiB，硬上限 8 MiB。真实传输量可能高于逻辑对象量；Downloads 预算按实际收到的分包字节计算。Git LFS、私有框架完整语义、动态运行路径、同名 COPY 的企业库解析顺序和真正增量构建仍不在已验证能力范围内。

完整源码与测试记录见[验证报告](./reports/2026-11-07-online-materials/README.md)。真实企业服务连通性、生产 GB 性能、业务正确性和目标设备验收没有被本次合成验证替代。
