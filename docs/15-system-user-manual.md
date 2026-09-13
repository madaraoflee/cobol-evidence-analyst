# 系统使用手册（当前版本）

更新时间：2026-09-14

适用入口：`poc/analyze_source.py`、`poc/web_app.py`

本手册是办公室电脑的最新操作入口。系统的正确使用顺序只有一条：先对指定源码目录建立本地事实索引，确认程序清单和诊断，再针对清单中的程序提问。系统不会自动读取项目里的示例源码，也不会把“索引完成”当成“业务已经分析完成”。

## 一、第一次准备

在 Windows 电脑准备 Python 3.10 或更高版本、Git，以及公司批准的源码目录和输出目录。项目只使用 Python 标准库，不需要安装额外 Python 包；API Key 不写入代码、命令历史、输出目录或 GitHub。

在项目目录打开命令提示符，首次下载或换电脑时执行：

```bat
git clone https://github.com/madaraoflee/cobol-evidence-analyst.git
cd cobol-evidence-analyst
git config core.hooksPath .githooks
```

以后每天开始工作先同步：

```bat
git pull --ff-only
```

源码和输出目录应放在项目目录之外，例如 `D:\cobol-data\source` 与 `D:\cobol-output\analysis`。不要把真实源码、API Key、SQLite 数据库或问答结果上传到公开仓库。

## 二、先建立本地索引（不联网）

从项目根目录执行：

```bat
python poc\analyze_source.py ^
  --source "D:\cobol-data\source" ^
  --output "D:\cobol-output\analysis"
```

也可以使用包装脚本：

```bat
poc\run_analyze.bat --source "D:\cobol-data\source" --output "D:\cobol-output\analysis"
```

首次运行只扫描本地文件，不需要 API Key，也不会联网。每次运行都会刷新当前目录的索引，并验证扫描期间源码是否发生变化。输出目录至少会生成：

- `diagnosis.md` / `diagnosis.json`：文件数量、编码、格式、COPY/CALL 等诊断。
- `programs.json`：本次实际识别出的 PROGRAM-ID、相对路径和行号。
- `structural-index.sqlite`：供后续证据检索使用的本地索引。

打开 `diagnosis.md` 和 `programs.json`，确认程序名、文件数量、编码和未解决项。`INDEX_READY` 只表示可以开始调查；`NEEDS_ATTENTION` 表示要先处理诊断；零程序、路径不存在或索引期间文件变化会阻断后续调查。

常用输入选项：

```bat
--encoding gb18030          例如已知源码编码时指定
--source-format fixed       固定列 COBOL
--source-format free        自由格式 COBOL
--exclude-extensionless     排除无扩展名成员
--extensions ".cbl,.cpy,.member"  自定义完整扩展名集合
```

编码或格式不确定时先用默认的 `auto`，再检查诊断和 `PROGRAM-ID` 是否显示正常中文。

## 三、配置接口并进行问答

只有明确加上 `--allow-network`，系统才会把必要的源码证据发送给已配置的公司接口。按公司提供的网关信息设置环境变量（名称固定为 `COMPANY_API_BASE_URL`、`COMPANY_API_KEY`、`COMPANY_CHAT_MODEL`，接口风格可用 `COMPANY_API_STYLE` 指定）。优先使用 HTTPS；不要把 Key 直接写在命令行参数里。

先从 `programs.json` 复制真实的 PROGRAM-ID，再执行：

```bat
python poc\analyze_source.py ^
  --source "D:\cobol-data\source" ^
  --output "D:\cobol-output\analysis" ^
  --entry "实际的PROGRAM-ID" ^
  --question "请解释该程序的主要处理步骤、输入输出和调用，并引用源码行号。" ^
  --allow-network
```

`--entry` 也接受清单中的相对路径或唯一文件名。相同 PROGRAM-ID 出现多个版本时，必须先分开建立索引；系统会返回 `ENTRY_AMBIGUOUS`，不会自行猜版本。问答结果写入 `agent-result.json` 和 `agent-result.md`。没有提问时是 `NOT_REQUESTED`，未开启联网时是 `NETWORK_DISABLED`。

## 四、使用本机网页工作台

网页工作台适合办公室日常操作，但仍调用同一套本地入口。启动：

```bat
python poc\web_app.py
```

不自动打开浏览器时：

```bat
python poc\web_app.py --no-browser
```

浏览器访问终端打印的 `http://127.0.0.1:8765`。需要换端口时使用 `--port 8787`。在页面中依次填写源码目录、输出目录、编码/格式、入口程序和问题；先连接源码并确认程序清单，再开始分析。页面支持中文简体、中文繁體和 English 三种界面语言；切换语言只翻译系统界面，不翻译源码、模型回答或证据文本。

工作台只监听本机回环地址，并校验访问来源和当前会话；它不是对外发布的网站。一次只运行一个分析任务。重新开始分析会清除旧回答和旧证据，新的证据只对当前源码快照有效。

## 五、如何判读结果

优先查看回答中的证据编号、源码路径和行号，再回到 `programs.json` 与源码核对。若源码在索引后被修改、索引数据库损坏、证据不属于当前快照，系统会要求重新运行，而不是继续使用旧答案。

以下状态必须区别对待：

- `INDEX_READY`：索引可用，不代表业务解释完整。
- `NEEDS_ATTENTION` / `BLOCKED` / `NOT_READY`：先处理输入、路径、解析或配置问题。
- `SAFE_STOP` / `ABSTAINED`：证据不足或超出支持范围，系统主动拒答。
- `CITATION_VERIFIED_ONLY`：引用与源码位置可核对，但不等于证明运行时最终值。
- `SUPPORTED_WITH_BOUNDARIES`：只在明确支持的语法和边界内成立。

`run_demo.py`、其他 `*_demo.py` 和固定框架验收命令只用于合成案例回归，不是公司源码入口；不要用演示通过来替代真实源码验收。

## 六、常见问题

| 现象 | 处理 |
| --- | --- |
| `python` 或 `git` 找不到 | 安装并重新打开命令提示符，确认加入 PATH。 |
| 输出目录和源码目录相互包含 | 改用两个完全独立的目录。 |
| 识别不到程序 | 检查编码、固定/自由格式、文件扩展名，以及源码是否真的包含 `PROGRAM-ID`。 |
| 入口歧义 | 用唯一相对路径，或按版本拆分源码目录重新索引。 |
| 索引期间提示源码变化 | 停止同步/复制任务，确保源码稳定后重新运行。 |
| 问答被阻断 | 先看 `diagnosis.md`，确认入口来自 `programs.json`，再检查接口环境变量和 API 能力。 |
| 网页端口被占用 | 使用 `python poc\\web_app.py --port 8787`，访问新端口。 |
| 页面显示旧内容或会话失效 | 关闭工作台后重新启动并刷新页面；不要复用旧浏览器标签页。 |

## 七、日常保存和提交

修改代码或文档后，在项目根目录执行测试：

```bat
python -m unittest discover -s poc\tests -v
node --test poc\tests\web_i18n.test.cjs
```

确认源码和输出目录没有被放进仓库，再查看变更并提交：

```bat
git status
git add README.md docs poc
git commit -m "说明本次改动"
git status
```

本仓库已配置提交后自动推送钩子；提交成功后会自动推送当前分支。若办公室电脑是新克隆的仓库，必须先执行一次 `git config core.hooksPath .githooks`。推送失败时先执行 `git pull --ff-only`，解决网络或远端更新后再重新提交；不要强制覆盖远端历史。

## 八、当前边界和验收要求

当前系统是“以源码证据为底座的受控分析工具”，不是 COBOL 运行时，也不保证任意方言、隐式框架和完整业务流程都能自动解释。真实源码、实际接口、Windows 编码和办公室网络必须在目标环境做一次验收；本地合成案例、模拟接口和测试通过不能替代这一步。

实现层已确认的行为包括：扫描前后校验源码快照；网页只绑定 `127.0.0.1` 并校验会话；新任务清除旧证据并绑定当前快照；界面本地化不改动源码或模型文本；输入、证据或配置不满足条件时返回阻断/安全停止状态。上述行为分别由 `poc/analyze_source.py`、`poc/web_app.py`、`poc/web/i18n.js` 及其测试覆盖。
