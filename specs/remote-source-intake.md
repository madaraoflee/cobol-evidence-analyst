# 独立远程源码入口：实现契约

观察范围为 `poc/web_app.py` 的接入与恢复流程、`poc/remote_source.py` 和 `poc/web/` 的新增表单。当前是 Python 标准库本机 HTTP 服务配合静态浏览器界面；下载模块独立，COBOL 分析器和本地导入参数保持原有契约。

入口位于左侧栏原有本地源码卡片下方，保留原本地入口的位置。右侧按本地源码与 Bitbucket 分组展示已接入资料库的最近对话，选择时恢复对应源码范围。远程获取完成后的提问、回答、引用和导出使用相同组件及接口；远程接入的读取、回答详略、编译和框架设置通过同一选项校验后交给同一分析器。

| 触发条件 | 实现要求与证据 |
| --- | --- |
| 当用户提交本地源码目录时 | 系统继续通过原 `/api/analyze` 和 `WorkbenchState.start` 建立或更新索引。 |
| 当用户提交远程链接时 | 系统通过受现有会话与同源保护的 `/api/source/remote` 创建独立工作区，解析链接并下载固定提交，再调用同一个分析器。见 `start_remote`、`_run`。 |
| 当下载、解压或索引失败，或用户取消时 | 系统恢复原资料库与对话，并清理本次独立缓存；恢复本身失败时保留恢复材料并报告。见 `_restore_after_failure` 和 `_run`。 |
| 当获取成功时 | 系统发布新资料库，保存不含凭据的 URL、版本选择、目录和实际提交；不改写原本地源码或结果。 |
| 当工作台重启时 | 系统恢复本机索引和来源信息，不自动连接仓库。见 `_save_workspace`、`_restore_workspace`。 |
| 当打开其他来源的历史对话时 | 通过 `/api/conversations/open` 校验工作区与对话归属，先恢复候选资料库再切换；失败保留原源码范围。缺失源码只能查看历史，不借用当前库的索引权限。 |
| 当删除其他来源的对话时 | 通过 `/api/recent-conversations/<workspace_id>/<conversation_id>` 定位所属库，不切换当前源码；只有匹配工作区和对话编号的当前会话会被清空。 |
| 当展示对话标题时 | 从首个实质问题提炼主题，保留程序标识和条件，不增加模型调用；历史自动标题只读转换，不改原消息、时间或自定义标题。 |
| 当请求携带访问令牌时 | 系统只通过 HTTPS Authorization 头使用令牌，拒绝带凭据的 URL 和重定向，不把令牌送入分析器、状态快照或持久化配置。见 `parse_remote_source`、`fetch_remote_source` 和前端 `startRemoteSource`。 |

远程响应及 ZIP 内容作为不可信输入，下载、条目数和解压大小有界；路径穿越、绝对路径、链接、跨平台危险文件名与重复路径均被拒绝。测试位置为 `poc/tests/test_remote_source.py`、`poc/tests/test_web_remote_source.py` 和 `poc/tests/web_remote_source.test.cjs`；本地回归沿用 `test_web_app.py`、`test_source_update.py` 与 `web_source_updates.test.cjs`。

尚未验证：用户实际服务器的版本、认证配置、代理与证书链；图片不足以确认完整 URL 和部署类型。第一版明确限制为 Bitbucket Server / Data Center；Cloud、SSH、SSO、自动同步、跨快照对话迁移和缓存管理不在本次实现范围。使用说明见 `docs/remote-source-intake.md`。
