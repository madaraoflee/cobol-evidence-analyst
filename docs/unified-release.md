# 统一版本

源码以 GitHub `main` 为准，Mac 开发目录同步同一提交。Windows 是主要交付平台；Windows x64 与 Mac arm64 应用分别在对应系统构建，使用相同源码。GitHub 的[最新发布](https://github.com/madaraoflee/cobol-evidence-analyst/releases/latest)是唯一日常下载入口，发布说明记录冻结提交和验证结果。

## Windows 使用

下载 `COBOLWorkbench-windows-x64.zip`，先退出旧工作台，将新包完整解压到一个新目录，再打开其中的 `COBOLWorkbench.exe`。EXE 和依赖目录需保留在一起。也可使用包内 `OPEN-IN-BROWSER.bat` 启动浏览器界面。

应用自动沿用 `%LOCALAPPDATA%\COBOLWorkbench` 下已有 API 配置和工作台状态，分析结果仍在之前选定的结果目录。曾使用历史独立试用启动脚本的环境，继续保留其专用数据目录；该目录与默认配置目录不同，更新时按原入口的数据目录配置启动。

## Mac 使用

下载 `COBOLWorkbench-macos-arm64.zip`，退出旧工作台，解压并打开新包内的 `COBOLWorkbench.app`。使用新应用的具体路径启动，避免通过相同应用名称打开历史副本。最低 macOS 版本以包内 `START-HERE.txt` 和 `BUILD-INFO.json` 为准。

应用沿用 `~/Library/Application Support/COBOLWorkbench` 下已有 API 配置和工作台状态。源码更新与应用更新是两个动作；Git 拉取后，已经编译的旧应用仍保留旧代码，需使用对应的新发布包。

## 维护与核对

开发者在项目目录使用 `git switch main`、`git pull --ff-only` 更新。构建前保持工作区干净，记录完整提交；Windows 工作流与 Mac 构建都从该提交开始。发布时同时保存包的 SHA-256、源码提交、构建信息、自检结果及业务评估记录。平台依赖不同，两个包的文件指纹可以不同，源码对应关系按提交和文件清单核对。

本机当前交付统一放在 `release/current`，包含 Windows ZIP、Mac ZIP、新 Mac 应用入口及交付核验记录。历史目录和基线分支仅作回退留档，不作为日常使用入口。升级保留本机配置与结果，构建包中不包含 API 密钥、真实业务源码或历史数据。

默认回答保持详细，展开当前问题涉及的业务条件、处理顺序和结果。本轮业务质量升级已将共享语句事实、来源与切片、关键结论核验及调查恢复接入默认工作台；实现和离线验证见[升级交付报告](reports/2026-10-13-business-quality-upgrade.md)。每个平台的实际可下载版本、冻结提交、构建校验及评测适用范围以对应发布说明为准；保留历史评测记录，不将离线机制通过等同于真实模型业务准确率。

多轮追问继续沿用同一工作台入口，程序、限定字段与用户明确排列的对象由本地源码重新绑定，支持会话恢复及旧版本证据保留；本轮实现、供证前后对照和离线验证见[多轮追问报告](reports/2026-10-14-conversation-followup.md)。

问答延迟修复覆盖密集字段布局、请求裁剪、索引升级、整题预算与连接等待，并核对工作台调查恢复及未知调用影响；机制、实测数据和验收边界见[交互延迟报告](reports/2026-10-14-interactive-latency.md)。候选构建与正式发布分别记录，未完成现场验收的候选包不替换日常下载入口。

正式 Windows 发布现须通过[同包联合验收](joint-release-acceptance.md)：最终 EXE 离线回归、真实模型、逐条人工评审和目标办公设备验收缺一项即保持候选。自动构建只上传通过原生离线门禁的候选；正式发布统一使用 `tools/publish_desktop_release.py`，不得直接修改草稿状态绕过门禁。
