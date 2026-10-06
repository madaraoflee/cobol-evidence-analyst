# 在办公室切换原版与改进版

**把原版和改进版放在两个独立文件夹，平时关闭当前版本，再双击另一个版本的入口即可。** 不需要每次切换 Git 分支，也不需要覆盖原程序。两个版本可以分析同一份只读 COBOL 源码，但各自使用独立配置、工作台状态和分析结果目录。

原版保存在 `codex/baseline-20261007`，对应提交 `7dd3e57`；改进版在 `codex/business-behavior-quality-speed`。原版分支是本次优化前的代码快照。Git 保存代码，不包含本机 `.env`、密钥、私有框架资料、历史对话或分析结果；已经在使用的程序目录与数据目录请继续保留。

## 第一次准备源码版

下面以 `D:\BusinessWorkbench` 为例，可替换成公司电脑允许使用的目录。需要已有 Python；准备代码时才需要 Git，之后双击启动不需要 Git。若办公室无法访问仓库，可在能够访问的电脑分别下载两个分支的代码，传入办公室后解压，并按以下名称放好。

在命令提示符中执行以下命令，分别取得两份代码。命令要求两个分支已推送到仓库；如果提示分支不存在，先完成分支发布，不要改为下载默认分支冒充原版。

```bat
mkdir D:\BusinessWorkbench
cd /d D:\BusinessWorkbench
git clone --single-branch -b codex/baseline-20261007 https://github.com/madaraoflee/cobol-evidence-analyst.git baseline-source
git clone --single-branch -b codex/business-behavior-quality-speed https://github.com/madaraoflee/cobol-evidence-analyst.git improved-source
copy improved-source\version-launchers\*.bat .
```

把原来使用的 `.env` **在本机**分别复制到 `baseline-source` 与 `improved-source` 根目录，保留相同接口、模型和回答参数。私有框架资料可继续引用同一份本机文件；若配置使用相对路径，应在两份目录下分别放好资料，或改为相同的绝对路径。不要把这些私有文件提交到仓库或放入交付包。

若原来修改过调查策略，可把原来的 `.poc-data\agent-settings.json` 分别复制到两份代码的 `.poc-data` 下；未配置过则无需创建。启动脚本清除本次进程的 `WORKBENCH_DATA_DIR` 和 `AGENT_SETTINGS_PATH` 覆盖，让每份源码使用自己目录下的配置；不改电脑的全局环境变量。接口相关的全局环境变量仍按现有规则优先于 `.env`，比较版本时应核对实际连接的模型一致。

目录结构如下。四个启动文件要复制到这里，不能直接在仓库内的 `version-launchers` 文件夹中双击：

```text
D:\BusinessWorkbench\
  START-BASELINE-SOURCE.bat
  START-IMPROVED-SOURCE.bat
  START-BASELINE-DESKTOP.bat
  START-IMPROVED-DESKTOP.bat
  baseline-source\
    .env
    .poc-data\
    poc\run_web.bat
  improved-source\
    .env
    .poc-data\
    poc\run_web.bat
```

第一次进入两个工作台时，都选取同一份 COBOL 源码，但分别选择 `D:\BusinessWorkbench\results\baseline-source` 和 `D:\BusinessWorkbench\results\improved-source` 作为结果目录，再各自导入一次。启动脚本只隔离本机配置和状态，**不会替你改界面里选定的结果目录**；不要把两个版本指向同一个结果目录。索引、对话和后续分析会写到结果目录，共用它就不再是独立的回退环境。

全新准备的两个目录不会自动带入旧工作台历史。原来的程序、`.poc-data` 和结果目录留在原处，原入口仍可查看原历史；新目录完成首次导入后，后续关闭再开会恢复各自的状态，无需每次重新导入。

## 日常使用源码版

| 想使用的版本 | 双击入口 | 浏览器地址 |
| --- | --- | --- |
| 本次优化前的原版 | `START-BASELINE-SOURCE.bat` | `http://127.0.0.1:8765` |
| 改进版 | `START-IMPROVED-SOURCE.bat` | `http://127.0.0.1:8766` |

这两个入口运行与原有 `poc\run_web.bat` 相同的源码工作台，并固定不同端口，便于辨认版本。切换时，在当前黑色命令窗口按 `Ctrl+C` 停止服务，若 Windows 询问终止批处理则确认；关闭旧浏览器页面，再双击另一个入口。只关浏览器标签页不会停止服务。

也可以继续在各自目录双击 `poc\run_web.bat`，但原有入口默认都用端口 8765，必须先停止正在运行的服务，并且不包含上述启动脚本的环境变量隔离设置。日常建议使用这两个清楚区分版本的入口；已有全局 `WORKBENCH_DATA_DIR` 时尤其如此。

之后只更新改进版即可，在命令提示符运行 `git -C D:\BusinessWorkbench\improved-source pull --ff-only`。原版目录与分支保留原样。如果改进版出现问题，先退出它，再打开原版，不需要撤销代码或迁移数据。

## 第一次准备桌面版

**两个 Windows 发布包需要在允许安装构建工具的 Windows 电脑上分别构建，再在目标 Windows 电脑验收。** 本次 macOS 开发环境不能生成或验证 Windows EXE。源码分支和启动脚本准备好，不等于两个 Windows 安装包已经生成。

维护者在构建电脑准备上面的两份代码，确认原版处于 `7dd3e57`，分别进入两个目录双击 `build_app.bat`。构建使用本地文件，因此两份目录中不要夹带不属于该版本的修改。完整构建准备见[桌面版打包与使用](./desktop-app.md)。将输出的两个 ZIP 在外部标记为“原版”和“改进版”，分别记录随包 `BUILD-INFO.json`，交付时说明对应分支与提交；不要混放或覆盖原有发布包。

办公室电脑只需接收并完整解压发布包。建立 `baseline-desktop` 和 `improved-desktop` 两个文件夹，分别把对应 ZIP 解压进去，并把四个启动文件放在它们的上级目录。标准 ZIP 解压后的结构如下：

```text
D:\BusinessWorkbench\
  START-BASELINE-DESKTOP.bat
  START-IMPROVED-DESKTOP.bat
  baseline-desktop\
    COBOLWorkbench\
      START-HERE.txt
      BUILD-INFO.json
      COBOLWorkbench\
        COBOLWorkbench.exe
        ...其余应用文件...
  improved-desktop\
    COBOLWorkbench\
      START-HERE.txt
      BUILD-INFO.json
      COBOLWorkbench\
        COBOLWorkbench.exe
        ...其余应用文件...
```

脚本兼容应用 EXE 直接位于版本文件夹、位于其中一层或两层 `COBOLWorkbench` 文件夹的解压方式；只接受找到一份应用的情况，避免误开多份残留构建。必须保留整个应用目录，不能只复制 EXE。桌面使用者无需获得源码目录、Python 或 Git；可只接收两份发布包和两个桌面启动文件。

双击桌面启动文件后，两个版本分别使用以下本机数据目录：

| 版本 | 配置与工作台状态目录 | 建议在界面选取的结果目录 |
| --- | --- | --- |
| 原版桌面 | `%LOCALAPPDATA%\COBOLWorkbench-baseline` | `D:\BusinessWorkbench\results\baseline-desktop` |
| 改进版桌面 | `%LOCALAPPDATA%\COBOLWorkbench-improved` | `D:\BusinessWorkbench\results\improved-desktop` |

每个版本第一次打开会创建自己的 `.env`。可通过“工作台 → 编辑接口配置”填写相同连接配置，或退出程序后，在本机把已经使用的 `.env` 分别复制到上述两个目录。已有调查策略文件时，同样把 `agent-settings.json` 放到两个目录各自的根部。私有框架资料可引用同一份本机只读文件；如果其路径是相对配置目录的，复制配置时也要调整。两份桌面数据目录与原来默认的 `%LOCALAPPDATA%\COBOLWorkbench` 相互独立，原有数据不会自动迁入，也不会被这两个入口覆盖。

随后分别选择源码和上表中独立的结果目录，完成一次导入。不要把桌面版结果与源码版结果设到同一个目录；四种入口各保留自己的结果与历史，才能独立比较和回退。

## 日常使用桌面版

双击 `START-BASELINE-DESKTOP.bat` 打开原版，双击 `START-IMPROVED-DESKTOP.bat` 打开改进版。可为它们在桌面建立快捷方式，改名为“业务分析－原版”和“业务分析－改进版”。想切换时，先正常退出当前应用，再打开另一个。

请使用这两个入口，不要直接双击内部 EXE 或发布包自带的 `OPEN-IN-BROWSER.bat`，因为后两者默认都使用同一个 `%LOCALAPPDATA%\COBOLWorkbench` 数据目录。若需要浏览器模式，可在命令提示符使用 `START-BASELINE-DESKTOP.bat --browser` 或 `START-IMPROVED-DESKTOP.bat --browser`，也可把 `--browser` 加到相应快捷方式的目标末尾。浏览器模式中，应在控制对话框选择“取消”停止应用，再打开另一个版本；仅关网页不会退出。

更新改进桌面版时，先退出应用，把旧 `improved-desktop` 改名留存，再创建新的 `improved-desktop` 并完整解压新包；不要把新包覆盖解压到旧应用文件中。改进版的数据保存在应用目录外，会继续使用；原版应用、原版配置与原版结果保持独立。

## 第一次使用前确认一次

先分别打开原版和改进版，核对源码版本、模型、框架资料和回答详细程度一致，结果目录不同。用同一道业务问题比较结论、关键条件与例外解释；速度应在两个版本都完成首次导入后比较。各自退出再打开，确认配置与历史恢复，然后再把改进版用于日常分析。

这些脚本只启动本机现有文件，不下载代码、不切分支、不复制凭据或迁移数据。脚本已按现有入口和数据目录规则检查；Windows 双击启动、路径兼容和发布包运行仍须在实际 Windows 环境验收。公司设备的应用运行限制也仍按现有流程处理。
