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

默认回答保持详细，完整展开当前问题涉及的业务条件、处理顺序和结果。此次统一版沿用 `9cb884f` 的业务生成流程和提示合同，仅修复两类特殊 Markdown 格式下的续写误裁。额外提示规则候选在实答复测中未呈现稳定收益，未进入最终业务逻辑。版本选择保留所有历史评测记录；最终提交、构建校验及评测适用范围随对应发布说明交付。
