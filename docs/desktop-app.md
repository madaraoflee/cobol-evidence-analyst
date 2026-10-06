# 桌面版打包与使用

需要同时保留原版与改进版、在办公室快速切换时，按[双版本使用说明](./version-switching.md)准备独立目录和启动入口；该方式也覆盖原有 `run_web.bat` 源码版。

**以后每次修改代码，只需在对应系统重新运行根目录的打包入口，把生成的 ZIP 交付给使用者。** Windows 生成包含 `.exe` 的完整应用文件夹，macOS 生成 `.app`；接收者不需要 Python、Git 或项目源码。当前采用 Nuitka 编译 Python 业务模块，以 pywebview 提供桌面窗口，原有源码分析与问答界面继续使用。

**办公室 Windows 电脑不能安装软件，已有 Python 3.13.4。** 因此 Windows 应用应在另一台允许安装构建工具的 Windows 电脑生成，办公室只解压运行。编译后的应用包含所需 Python 运行环境，与办公室已有 Python 版本无关，不需要更换它，也不要求办公室下载依赖、安装组件或取得管理员权限。

这解决的是应用交付和提高逆向成本，不能保证“无法反编译”。界面用到的 HTML、CSS、JavaScript 和随包数据仍可被查看，编译后的程序也可能被逆向分析。已经公开的 GitHub 版本与历史不会因打包而收回；后续版本的交付策略应以这一事实为前提。

## 在允许安装工具的电脑上准备一次

| 构建电脑 | 准备内容 | 以后双击的入口 |
| --- | --- | --- |
| Windows | 官方 CPython 3.10–3.13，可用已有 3.13.4，无需降级；Visual Studio 2022 Build Tools，勾选“使用 C++ 的桌面开发” | 项目根目录 `build_app.bat` |
| macOS | 官方 CPython 3.10–3.14，推荐 3.12；Xcode Command Line Tools | 项目根目录 `build_app.command` |

Windows 安装 Python 时启用 Python Launcher 与 PATH 选项。macOS 可在终端执行 `xcode-select --install` 安装命令行工具；已经安装则无需重复。优先使用 python.org 的 Python，避免构建工具与解释器架构不一致。Nuitka 的独立应用模式能包含 Python 运行依赖，构建时仍需要 Python 与 C 编译器。具体要求见 [Nuitka 官方用户手册](https://nuitka.net/user-documentation/user-manual.html)。

第一次打包需要联网下载构建依赖，脚本会准备独立的构建环境，安装固定版本的 Nuitka 4.2.2、pywebview 6.2.1、certifi 2026.7.22 等依赖，再执行编译。依赖下载和首次编译可能耗时较长；以后直接运行同一个入口即可。pywebview 官方说明支持使用 Nuitka 打包，见 [pywebview 部署说明](https://pywebview.flowrl.com/guide/freezing.html)。

应用图标使用你确认的透明底版本 `assets/app.png`，保留内层圆角方框和原有图案。对应的 `app.ico` 和 `app.icns` 已包含各系统所需尺寸并保留透明区域，双击 `build_app.bat` 或 `build_app.command` 时自动纳入应用，无需手动设置图标。

**Windows 版须在 Windows 构建，macOS 版须在 macOS 构建。** 当前脚本不提供跨系统编译，也不生成 macOS 通用架构包：Apple 芯片与 Intel Mac 分别使用对应架构的 Python 构建、验收和交付。构建成功只说明该环境完成打包，不代表已经覆盖其他系统版本与架构。

macOS 包的最低系统版本取决于构建时的 Python 和原生依赖。脚本从编译产物读取最低版本，写入 `START-HERE.txt` 和 `BUILD-INFO.json`。本次在本机实际生成并打开验证的是 **Apple 芯片、macOS 26.0 或更新版本**。需要支持更旧的 Mac 时，应使用兼容目标系统的 Python 与依赖重新构建，并在目标系统验收，不能仅修改版本标签。

## 每次出新版本

1. 将要交付的代码更新、保存到本地项目目录。脚本使用当前工作目录里的文件，包括尚未提交的修改；不会自动从 GitHub 拉取代码。
2. Windows 双击 `build_app.bat`；macOS 双击 `build_app.command`。两者共用根目录的 `build_app.py`。
3. 等待打包成功，找到本次输出中的 ZIP：`release/<platform>-<arch>/<timestamp>-<随机后缀>/COBOLWorkbench-<platform>-<arch>.zip`。系统名称为 `windows` 或 `macos`，架构为 `x64` 或 `arm64`；目录按系统、架构和构建时间分开，避免把旧版本误当作新版。
4. 在目标系统解压并完成下面的使用验收，再交付这一个 ZIP。Windows 必须保留解压出的整个应用文件夹，不能只拿出 `.exe`。

也可在项目根目录运行 `python build_app.py`，macOS 通常使用 `python3 build_app.py`。macOS 如果文件权限导致 `.command` 无法双击，先在项目根目录执行 `chmod +x build_app.command`，再双击。

需要检查当前构建目标时运行 `python build_app.py --check`；它显示目标、Python 版本和准备要求，不会安装或编译。已有完整构建环境时可用 `--skip-install` 跳过依赖安装步骤；编译工具仍可能需要下载缓存，因此它不保证完全离线构建。`--jobs 2` 可降低并行编译数量，`--output-dir "目录路径"` 可修改交付输出位置。

旧脚本 `poc/build_delivery.py` 仍然生成**源码 ZIP**，其中包含 `.py` 文件，不适合“只交付应用”的需求。桌面版请使用根目录的 `build_app` 入口及其 `release` 输出，不要把项目目录、构建中间目录或源码 ZIP 发给接收者。

## 在 GitHub 上构建 Windows x64 包

仓库提供 [Windows desktop x64 工作流](../.github/workflows/windows-desktop.yml)，使用 `windows-2022`、Python 3.13 x64 和 MSVC，调用现有 `build_app.py`。工作流只在手动运行，或面向 `main` 的 PR（含草稿） 修改应用代码、图标、构建入口或工作流时执行；没有 `push` 构建和自动发布。首次使用手动按钮前，工作流需要先存在于默认分支；在此之前可由包含该文件的 PR 验证。触发方式见 [GitHub 官方说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch)。

构建会先运行离线 Python 回归和浏览器入口自检，再编译并运行打包程序自检。自检检查资源、本机 HTTP 服务、桌面依赖导入和 TLS 初始化，模型调用数必须为零。构建依赖下载需要联网，但不需要配置模型接口或仓库 API 密钥。任何检查失败都不会上传交付包。

成功后，从该次运行的 **Artifacts** 下载 `COBOLWorkbench-windows-x64-<commit>-<attempt>`，保留期为 14 天。解开 GitHub 下载的外层封装，可得到三个文件：真正的应用交付包 `COBOLWorkbench-windows-x64.zip`、其 `.zip.sha256` 校验文件、以及从包内原样复制的 `BUILD-INFO.json`。校验可在 PowerShell 执行 `Get-FileHash .\COBOLWorkbench-windows-x64.zip -Algorithm SHA256`，将结果与 `.sha256` 第一列比较；随后按下节解压内层交付包并打开 EXE。PR 产物对应待合并测试版本，以运行页的提交信息为准。

工作流不会上传仓库源码 ZIP、构建中间目录或使用者配置，不生成签名安装器。2026-10-07 的 [Windows 原生构建](https://github.com/madaraoflee/cobol-evidence-analyst/actions/runs/37511714204) 已成功，包含 2,008 项回归（2 项既有 POSIX 条件跳过）、源码浏览器自检和编译后二进制自检；交付 ZIP 已下载并复核。完整工程与回答质量记录见[本轮报告](./reports/2026-10-07-quality-speed-windows.md)。办公室电脑实机验收尚未完成。即使云端自检通过，仍需按本文末尾检查窗口或浏览器启动、文件选择和真实业务问答。运行环境与构建依赖参考 [Windows runner 清单](https://github.com/actions/runner-images/blob/main/images/windows/Windows2022-Readme.md)；工作流使用官方 [checkout](https://github.com/actions/checkout)、[setup-python](https://github.com/actions/setup-python) 和 [upload-artifact](https://github.com/actions/upload-artifact) 的固定 major 版本。

## 接收者第一次打开

Windows 解压整个 ZIP，打开外层 `COBOLWorkbench`，再进入同名应用文件夹，双击 `COBOLWorkbench.exe`；macOS 解压后在外层 `COBOLWorkbench` 中打开 `COBOLWorkbench.app`，也可先将它移到“应用程序”。外层的 `START-HERE.txt` 是使用说明，`BUILD-INFO.json` 记录构建信息。

Windows 原生窗口使用电脑已有的 WebView2 Runtime 与 .NET 组件；依赖说明见 [pywebview 官方文档](https://pywebview.flowrl.com/guide/installation.html#windows)。如果原生窗口无法启动，应用会改用已有的系统浏览器显示本机工作台，不要求补装这些组件。也可直接双击发布包外层的 `OPEN-IN-BROWSER.bat`，或运行 `COBOLWorkbench.exe --browser`。浏览器模式仍由编译后的应用处理业务，不需要交付 `.py` 源码。

首次运行后，从菜单选择“工作台 → 编辑接口配置”，在本机 `.env` 中填写接口地址、密钥与模型名称，保存为 UTF-8，然后选择“工作台 → 重新载入页面”。在页面的“模型连接设置”中测试连接，再选取本机 COBOL 源码目录和结果目录。业务操作沿用[系统使用手册](./15-system-user-manual.md)。

Windows 浏览器模式没有上述桌面菜单，会保留一个控制对话框：“是”重新打开浏览器页面，“否”编辑本机 `.env`，“取消”停止应用。修改配置并保存后刷新浏览器页面。关掉浏览器标签不会停止后台应用，结束使用时请在控制框选择“取消”。

密钥、私有框架资料和分析结果由使用者在自己的电脑配置。打包并不代替模型接口服务；要进行模型问答，使用者仍需要有效的接口配置与对应网络连接。桌面版不自动沿用开发目录的 `.env`，可以把其中需要的配置复制到桌面版的本机配置文件。

从源码版迁移时，应核对相同的模型、接口、输出上限、框架资料、源码版本与回答详细程度，再比较答案。如果原来在 `.poc-data/agent-settings.json` 调整过调查策略，应将该文件复制到桌面版数据目录的 `agent-settings.json`，或用 `AGENT_SETTINGS_PATH` 指定它；未设置时两种入口使用相同默认策略。框架资料需要在目标电脑可读，并重新设置当地路径。只填写模型密钥而遗漏私有资料，可能让框架相关回答缺少依据，这与编译本身是否改变问答逻辑是两件事。

当前构建没有 Windows 发布者证书签名，也没有完成 Apple Developer ID 签名与公证；macOS 的临时签名不等于可验证的发布者身份。企业设备策略仍可能限制运行 EXE，浏览器模式也不能绕过这种限制；遇到拦截应交由 IT 按公司流程批准。正式交付前应在目标电脑验证打开流程；后续有证书时再补正式签名与公证。

已有源码的维护者也可继续使用现有 Python 3.13.4 运行 `python poc/web_app.py`，该浏览器入口不依赖第三方 Python 包。这是保留的源码开发方式；对领导的交付仍使用编译包。

## 配置和历史保存在应用外

桌面版把 `.env`、工作台状态与日志保存在当前用户的数据目录。菜单“工作台 → 打开数据目录”可直接打开它：

| 系统 | 默认位置 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\COBOLWorkbench` |
| macOS | `~/Library/Application Support/COBOLWorkbench` |

设置环境变量 `WORKBENCH_DATA_DIR` 可改用指定目录。源码与分析结果还会使用你在界面选定的路径，更新应用时应保留这些目录。

出新版时先退出旧应用，再解压并打开新版；同一用户继续使用同一数据目录，就能保留已有配置与工作台历史。不需要重新填写密钥或仅为替换应用而重新导入源码。不要为更新应用删除用户数据目录；如果从浏览器源码版迁移，首次需在桌面版重新配置接口并接入原来的源码、结果路径。

## 发给领导前验收一次

本轮已生成并在 Windows 云端完成自动自检的发布包，目标为 Windows x64。办公室电脑的系统版本、处理器架构与实际启动流程尚未确认，不能据云端结果宣称该电脑已通过验收。

本轮以 Windows 为主，先在目标 Windows 电脑上，用解压出的发布包完成：打开窗口、配置接口并测试连接、选择本机源码和结果目录、提出一个业务问题、关闭后重新打开查看历史，再替换为新包确认配置保留。如果另行交付 macOS 版本，应在对应目标架构的 Mac 上独立验收；Intel 和 Apple 芯片版本不能互相替代。

构建阶段的自动检查不能替代桌面窗口、WebView2、系统拦截与真实接口的实机检查。若窗口未能启动，可先看数据目录中的 `desktop.log`；若编译失败，保留构建窗口中的错误输出，再检查 Python 版本、编译器和依赖下载是否正常。
