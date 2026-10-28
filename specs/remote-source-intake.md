# 独立远程源码入口：实现契约

观察范围为 `poc/web_app.py` 的接入与恢复流程、`poc/remote_source.py` 和 `poc/web/` 的新增表单。当前是 Python 标准库本机 HTTP 服务配合静态浏览器界面；下载模块独立，COBOL 分析器和本地导入参数保持原有契约。

入口位于左侧栏原有本地源码卡片下方，保留原本地入口的位置。支持 Bitbucket Cloud 与 Server / Data Center 的 HTTPS 仓库、目录和 clone 链接。源码资料页列出已保存的资料库，右侧按本地源码与 Bitbucket 分组展示最近对话；选择资料库或历史对话时恢复对应源码范围。远程获取完成后的提问、回答、引用和导出使用相同组件及接口；远程接入的读取、回答详略、编译和框架设置通过同一选项校验后交给同一分析器。

| 触发条件 | 实现要求与证据 |
| --- | --- |
| 当用户提交本地源码目录时 | 系统继续通过原 `/api/analyze` 和 `WorkbenchState.start` 建立或更新索引。 |
| 当用户首次提交远程来源时 | 通过受现有会话与同源保护的 `/api/source/remote` 解析链接，解析完整提交后下载到独立工作区，再调用同一个分析器。见 `start_remote`、`resolve_remote_source`、`fetch_remote_source`、`_run`。 |
| 当再次提交相同仓库、版本选择和目录时 | 使用已登记的本机副本，不请求仓库、不下载 ZIP；显式读取设置改变或索引需要修复时，仅从该本机源码重新索引，不静默忽略本次读取设置。 |
| 当打开已保存的资料库时 | `/api/state` 和完成任务响应的 `source_workspaces` 提供登记条目；`/api/source/open` 先恢复选定资料库再采用它，不连接仓库或调用模型。见 `saved_sources`、`open_source_workspace`、`_adopt_workspace`。 |
| 当主动检查远端更新时 | `/api/source/remote/update` 只接受当前远程资料库，先用 `resolve_remote_source` 查询提交元数据。提交不变且索引可用则零 ZIP 下载、零索引重建；索引缺失、损坏或需升级时从现存源码本地重建。已删除的源文件不能由索引还原。 |
| 当远端提交变化时 | 将解析结果交给 `fetch_remote_source(resolved=...)`，避免重复查版本和分支移动；在新 source/output 工作区完成下载和索引后才切换。旧工作区、对话与引用保持原版本，不跨版本复用证据。 |
| 当链接带固定提交哈希时 | 保持固定提交，不因检查更新而擅自跟随其他分支；只有显式填写分支或设置 `follow_default_branch=true` 才改变版本选择。默认分支模式不触发启动时联网。Cloud 从仓库 `mainbranch` 取得默认分支，再读取该分支提交，禁止用跨分支 commits 列表的首项代替。 |
| 当下载、解压或索引失败，或用户取消时 | 系统恢复原资料库与对话，并清理本次独立缓存；恢复本身失败时保留恢复材料并报告。见 `_restore_after_failure` 和 `_run`。 |
| 当获取成功时 | 系统采用新资料库，保存不含凭据的 provider、URL、版本选择、目录、实际提交及已知默认分支；不改写原本地源码或结果。provider 区分 `bitbucket_cloud` 与 `bitbucket`。 |
| 当工作台重启时 | 系统恢复本机索引和来源信息，不自动连接仓库。见 `_save_workspace`、`_restore_workspace`。 |
| 当打开其他来源的历史对话时 | 通过 `/api/conversations/open` 校验工作区与对话归属，先恢复候选资料库再切换；失败保留原源码范围。缺失源码只能查看历史，不借用当前库的索引权限。 |
| 当删除其他来源的对话时 | 通过 `/api/recent-conversations/<workspace_id>/<conversation_id>` 定位所属库，不切换当前源码；只有匹配工作区和对话编号的当前会话会被清空。 |
| 当展示对话标题时 | 从首个实质问题提炼主题，保留程序标识和条件，不增加模型调用；历史自动标题只读转换，不改原消息、时间或自定义标题。 |
| 当请求携带访问凭据时 | Cloud 的 `username` 是 API token 对应的账号邮箱，二者组成 Basic Authorization；邮箱留空时 token 以 Bearer 发送，适用于 access token，Server / Data Center 保持原 Bearer 用法。凭据仅在 HTTPS 请求头和本次线程内存中使用，拒绝带凭据的 URL 和重定向，不送入分析器、状态快照或持久化配置；前端提交或关闭表单后清除。见 `parse_remote_source`、`fetch_remote_source` 和 `startRemoteSource`。 |

Cloud 通过官方 `get/<commit>.zip` 获取整仓 ZIP，仅提取选定目录；Server / Data Center 沿用服务端按目录归档。此方案不是 Git 增量拉取。Cloud 选择较小子目录不会减少 ZIP 传输量，只有未变提交检查可以完全避免下载。

远程响应及 ZIP 内容作为不可信输入，沿用原下载、条目数、中央目录和解压大小限制。所有路径仍拒绝路径穿越、绝对路径、反斜杠和异常根目录；Cloud 未选目录的合法 Linux 文件名不会写入本机，不套用 Windows 保留名检查。被提取路径及其祖先仍拒绝链接、跨平台危险文件名、大小写／Unicode 冲突和文件／目录冲突。测试位置为 `poc/tests/test_remote_source.py`、`poc/tests/test_web_remote_source.py` 和 `poc/tests/web_remote_source.test.cjs`；本地回归沿用 `test_web_app.py`、`test_source_update.py` 与 `web_source_updates.test.cjs`。

协议依据为官方 [Cloud 仓库 API](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-repositories/)、[Cloud 提交 API](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-commits/)、[ZIP 下载接口](https://support.atlassian.com/bitbucket-cloud/kb/how-to-download-repositories-using-the-api/)及 [API token／access token 认证](https://developer.atlassian.com/cloud/bitbucket/rest/intro/)。Cloud API token 至少需要 `read:repository:bitbucket` 权限。

已做公开 Cloud 仓库的只读版本解析与 ZIP 响应／根目录格式核对；尚未验证用户企业私有仓库的认证、权限、代理与证书链，不能据此声称企业接入已通过。SSH、SSO／OAuth 登录流程、定时自动同步、跨快照对话迁移和自动清理旧快照不在当前实现范围。正式发布门禁保持原状。使用说明见 `docs/remote-source-intake.md`。
