# 从 Bitbucket 接入源码

Bitbucket Cloud 与 Server / Data Center 都可从左侧栏“从 Bitbucket 接入”进入。首次获取把指定提交的源码保存为独立本机快照，之后重启或打开已保存资料库直接复用源码和索引，不会再次下载。需要获取一周后的更新时，再主动选择“检查 Bitbucket 更新”：先比较提交，提交没有变化且本地索引可用时，不下载 ZIP，也不重建索引。

本地目录入口继续使用。远程下载和索引全部成功后才切换到新快照；下载、解压、索引失败或用户取消时保留原资料库和对话。两种来源共用问答、结果展示、引用、对话记录及导出，回答详略、阅读范围、回答要求与可选编译／框架设置也沿用相同逻辑。

## 使用方法

1. 选择“从 Bitbucket 接入”（窄屏先展开左侧导航），粘贴仓库页、目录页或 HTTPS clone 链接。Cloud 示例：`https://bitbucket.org/sample-workspace/business-source/src/main/stage/`；Server / Data Center 示例：`https://code.example.invalid/projects/DEMO/repos/business-source/browse/stage?at=refs%2Fheads%2Fmain`。
2. 分支／标签／提交和仓库内目录可以留空，沿用链接中的设置；链接也没有版本时，使用仓库默认分支。显式填写的非空值优先于链接中的设置。目录填写 `stage` 这类仓库相对路径；需要多个源码子目录及共同 COPYBOOK 时选择共同父目录。
3. Cloud 的 API token 搭配 Atlassian 账号邮箱填写，并至少授予 `read:repository:bitbucket` 权限，工作台使用 HTTP Basic 认证。仓库／项目／工作区 access token 与 API token 是不同类型：使用 access token 时邮箱留空，按 Bearer 发送；Server / Data Center 的只读 HTTP access token 同样留空邮箱。邮箱和令牌只用于本次接入或检查更新，不写入配置、资料库或日志，表单提交或关闭后清空。私有仓库下次主动检查更新需重新填写；打开已有本机副本不需要凭据。公开仓库可留空，浏览器登录或 SSO 状态不会自动提供 API 授权。
4. 核对编码、固定／自由格式；需要时在“进阶读取选项”填写框架资料、编译／COPY 设置和扩展名，再提交。第一次会显示解析版本、下载、解压及索引进度；相同来源已有副本时直接打开，显式更改读取设置时使用该本机源码重新索引，不重新下载。接入和更新本身不调用模型。

Cloud 也接受仓库根地址、`https://bitbucket.org/sample-workspace/business-source.git` 和带完整提交哈希的 `/src/<commit>/...` 链接。Server / Data Center 支持部署前缀，例如 `https://code.example.invalid/code/projects/DEMO/repos/business-source/browse/stage`，以及 `https://code.example.invalid/scm/DEMO/business-source.git`。链接不能包含账号、密码或令牌。SSH clone、浏览器 SSO 登录、OAuth 登录流程和定时自动同步尚未实现。

带完整提交哈希的链接代表固定版本，检查更新不会擅自改成其他分支。希望跟随仓库日后的更新时，明确勾选“跟随仓库默认分支”，或填入需要跟随的分支名。勾选后本次接入及以后主动检查更新使用默认分支，忽略原链接和版本栏的固定值；它不代表启动时自动联网。Cloud 默认分支先从仓库信息读取，再检查该分支的提交，不会将其他分支的最新提交误作当前版本。

## 快照和原资料如何保存

首次接入或远端提交改变时创建新的源码目录及结果目录，不覆盖以前的本地目录或远程快照。默认保存在应用数据目录下的 `remote-sources/<随机编号>/source` 和同级 `analysis`；源码运行通常位于 `.poc-data/remote-sources/`，配置了 `WORKBENCH_DATA_DIR` 时使用该目录。索引、对话与不含凭据的来源信息随本机数据保管，不提交到公开仓库。

源码资料页的“已保存的源码资料库”可离线打开登记过的本机副本；状态接口的 `source_workspaces` 提供这些条目，打开操作使用 `/api/source/open`，不访问远端。重启恢复上次资料库。重新提交相同仓库、版本选择和目录的链接也会复用已有副本；仅当读取设置改变或缓存索引缺失、损坏、需要升级时，从现存源码重新建立索引。源码目录本身已丢失时无法凭索引还原源文件，需要恢复本机目录或重新获取。

“检查 Bitbucket 更新”使用 `/api/source/remote/update`。提交不变时零 ZIP 下载；本地索引可用就直接继续使用，索引需要修复则从缓存文件本地重建。提交改变时，先将分支解析为完整提交编号，再按该编号下载，防止检查与下载之间分支移动。新提交形成另一份资料库，旧对话、引用和源码快照仍属于旧版本；不会自动把旧回答或引用改挂到新源码，也不自动删除旧快照。

先前本地路径和读取设置仍保留在本地接入表单。右侧“最近对话”按“本地源码”和“Bitbucket”分组，并显示所属目录或仓库；点击历史对话会恢复它自己的源码资料库、索引和框架资料。不同资料库的对话不会混用当前源码，删除另一资料库的对话也不会切换当前分析对象。原源码暂时不可用时仍可查看已保存历史，但需要恢复源码与有效索引后才能继续分析。

对话标题从第一个有实质内容的问题提炼主题，保留程序名、字段名与关键条件，去掉请求套话和输出格式要求，例如“REQUEST01 的金额检查流程”。简单的“继续”“为什么”等追问不会覆盖原主题。旧的自动截断标题在读取时显示为摘要标题，原消息、时间和自定义标题保持不变；生成标题不增加模型调用。

最近对话只读取工作台已登记的资料库，不扫描其他磁盘目录；最多保留 100 个资料库登记，合并展示最近 200 段对话，超出显示范围不会删除磁盘数据。旧版本保留的当前资料库与可验证的本地路径会自动纳入；其他旧资料库可通过“切换源码目录”重新接入原源码和原结果目录。下载失败会清理本次未完成的缓存；异常退出或清理时磁盘故障可能留下未采用的目录，应先确认它不是正在使用的快照再处理。

## 连接和容量边界

运行工作台的电脑需要能够访问仓库，必要时先连接企业网络或 VPN。HTTP 401／403 表示令牌或读取权限问题，404 也可能由权限隐藏造成。服务器跳转到登录页时，使用最终 HTTPS 仓库地址和有效令牌；工作台不跟随重定向。企业证书需被 Python 的 HTTPS 信任配置接受，程序不会关闭证书校验。

当前限制为压缩下载最多 512 MiB、解压文件总量最多 2 GiB、归档条目最多 100,000，ZIP 中央目录元数据最多 32 MiB。网络读操作超时为 30 秒，获取和解压还受 15 分钟总预算约束；阻塞读取可能到单次超时才响应取消。

Server / Data Center 使用服务端目录归档；Cloud 官方 ZIP 接口返回整个仓库，工作台只将选定子目录提取到本机资料库。这不是 Git 增量拉取：Cloud 首次下载和检测到新提交后的下载仍是整个 ZIP，选择较小目录不会减少该 ZIP 的传输大小。未选目录中的 Linux 专用名字不会成为本机文件，也不会因 Windows 保留名而阻断选定目录；选定路径仍须可安全写入 Windows，所有归档路径仍拒绝绝对路径、上级跳转、反斜杠及异常根目录。超过整包容量时，可改用获准的本机工作副本接入；Server / Data Center 还可缩小服务端归档目录。范围以外的 COPYBOOK 不进入资料库，应选择共同父目录覆盖分析依赖。Git LFS 实体和子模块内容不会另外拉取。

实现依据官方 [Cloud 仓库信息 API](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-repositories/)、[Cloud 提交 API](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-commits/)、[Cloud ZIP 下载说明](https://support.atlassian.com/bitbucket-cloud/kb/how-to-download-repositories-using-the-api/)及 [Cloud 认证说明](https://developer.atlassian.com/cloud/bitbucket/rest/intro/)。Server / Data Center 沿用 [Server REST API](https://docs.atlassian.com/bitbucket-server/rest/7.17.0/bitbucket-rest.html) 的 commits 与 archive 接口和 [HTTP access tokens](https://confluence.atlassian.com/bitbucketserver086/http-access-tokens-1188782454.html) 认证方式。

## 验证范围

自动回归使用中性合成源码和模拟仓库响应，覆盖 Cloud／Server 链接解析、默认分支与固定提交、Basic／Bearer 请求、ZIP 范围与安全解压、成功切换、失败／取消回滚、重启恢复、离线打开、相同来源复用、未变提交不下载、读取设置变化及坏索引的本地重建。另以公开 Cloud 仓库只读验证实际版本解析、ZIP 响应与根目录格式；未进行企业私有仓库认证或完整企业源码下载。此前本机 HTTPS 模拟仓库测试的提交解析、下载、索引及 401 回滚同样不代表企业环境验收。

整合测试同时检查跨来源问答和自定义回答要求：同名程序在两个资料库使用不同规则，切换历史、重启和重试后仍取各自源码与对话，保存的回答要求进入实际模型请求。前端回归涵盖导入取消时恢复框架设置、历史恢复期间的操作冲突、迟到响应隔离，以及摘要标题不会截成错误程序名。

此前使用隔离浏览器检查了来源分组、历史对话、源码引用和回答要求入口；[2026-10-28 入口截图](./reports/2026-10-28-remote-source-ui/left-entry-grouped-history.jpg)保留为该轮历史记录，不作为新 Cloud 功能已在企业环境通过的证明。源码流程的真实模型测试与最终 Windows EXE 的离线回归分别记录，不能互相代替；企业服务器版本、代理、证书、真实权限及同包办公设备验收仍需在获准环境完成。
