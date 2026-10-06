# 外部 COBOL 解析器小规模实测：可用作语句前端，尚不能替换业务语义层

日期：2026-10-07。结论：本次没有证据支持立即把外部 Tree-sitter 解析器设为默认。原版解析器能快速、准确地分开同一行的多个 `MOVE`，但对本次 SQL 样例的覆盖不足，而且 `IF` / `EVALUATE` 输出是平铺头部，并不提供现成的嵌套控制作用域。企业分支未在限定构建时间内产出 C 解析器，未进入语料测试；不能据此推论它的解析质量。

本轮更适合继续完善有限的 token / 语句结构分析，把同一行的语句边界、条件层次和覆盖顺序做正确，并为未来外部解析器保留中立适配接口。这是增量改进，不等同于完成 COBOL AST、控制流或数据流分析。

## 版本、环境和隔离范围

| 项目 | 固定版本 / 实际状态 |
| --- | --- |
| 原版 grammar | [yutaro-sakamoto/tree-sitter-cobol，550020ddf42ef9b718ad897868a9308d2afcda35](https://github.com/yutaro-sakamoto/tree-sitter-cobol/tree/550020ddf42ef9b718ad897868a9308d2afcda35) |
| 企业分支 grammar | [Spantree/tree-sitter-cobol-enterprise，86a2c479cb7e299e8dc3bb9db7a346502335d21e](https://github.com/Spantree/tree-sitter-cobol-enterprise/tree/86a2c479cb7e299e8dc3bb9db7a346502335d21e) |
| Python runtime | `tree-sitter==0.25.2`；Python 3.14.4；grammar ABI 14 |
| 生成器 | `tree-sitter-cli@0.20.8`，仅安装到试验目录，与企业分支声明的开发依赖版本一致 |
| 机器 | macOS 27.0.1，arm64；系统 clang |
| 隔离目录 | 项目 `.build/parser-trial/`；没有修改产品依赖或全局环境 |
| 数据范围 | 仅原创合成 fixtures 和新写的小型探针；没有读取 API 密钥、用户项目或公司源码 |

原版使用仓库已提交的 `src/parser.c` 和 `src/scanner.c` 编译，实测编译约 3.06 秒，生成动态库 13,872,888 字节。Python 通过 `ctypes` 取得语言指针，用官方 Python runtime 解析；这一加载方式只是研究原型，不是已经验证过的桌面分发方案。

两个 grammar 都是 MIT 许可。下载目录中的原始 LICENSE 完整保留，企业分支包含上游版权声明；runtime 的 LICENSE 位于隔离 venv 的 `tree_sitter-0.25.2.dist-info/licenses/`。如果产品将来分发 grammar 或生成二进制，必须一起携带这些许可和版权声明。本次未将第三方源码或二进制纳入产品。

## 语料和正确性结果

使用 `poc/fixtures/synthetic-insurance-v1/programs/` 的全部 8 个固定格式文件，以及 `framework-workbench` 中的 `service-entry.cbl`、`screen-control.cbl`。总共 10 个文件、455 行。直接解析原始字节，保留固定格式列和序号，不预先展开 COPY。

| 文件 | ERROR / missing 节点数 | warm 全量重解析中位数（毫秒） |
| --- | ---: | ---: |
| SYNP000.cbl | 0 | 0.140 |
| SYNP010.cbl | 1 | 0.066 |
| SYNP020.cbl | 1 | 0.037 |
| SYNP030.cbl | 2 | 0.086 |
| SYNP040.cbl | 1 | 0.096 |
| SYNP090.cbl | 0 | 0.024 |
| SYNP100.cbl | 1 | 0.042 |
| SYNP200.cbl | 1 | 0.040 |
| service-entry.cbl | 0 | 0.215 |
| screen-control.cbl | 0 | 0.189 |

原版 4/10 文件无语法错误，总计 7 个 ERROR，没有 missing 节点。主要失败集中于 SQL；有些 ERROR 覆盖从段落名直到文件末尾的大段源码。比如 `SYNP100.cbl` 的 ERROR 覆盖第 8–26 行，这意味着不仅 SQL 没有结构化，后面的 `COMPUTE`、`IF` 和错误返回也落入了错误区域。`SYNP030.cbl` 另在第 10 行的字符常量 `'A'` 出现 ERROR。不能把错误树里零散恢复出的语句当作完整结构。

所有被测试树节点的 `start_point` / `end_point` 与原始字节偏移一致。这个检查证明原始固定格式文本的坐标内部一致，不证明 COPY 展开映射、编码转换后的映射或业务证据语义正确。

额外原创探针 `inline-nested.cbl` 共 29 行，包含一行两条 `MOVE`、两层 `IF` 及两层 `EVALUATE`，没有 ERROR，9 条 `MOVE` 都被分开识别。第 11 行两条 MOVE 的起始列分别是 12、31；第 14 行分别是 20、39，能够区分同一行不同写入。

但是原版树中的 `if_header`、`else_header`、`END_IF`，以及 `evaluate_header`、`when`、`when_other`、`END_EVALUATE` 都是 `procedure_division` 下的平铺节点。它没有把内部 MOVE 挂在对应 IF 分支或 WHEN 下。因此仍需状态栈 / 作用域构建，不能因为使用 Tree-sitter 就宣称已经得到完整的嵌套控制语义。本次也没有验证最终赋值、路径可达性、别名、跨程序参数传播或业务回答正确率。

## 速度测量的含义

[可复现脚本](parser-trial-2026-10-07/benchmark.py)和[原始结果](parser-trial-2026-10-07/upstream-results.json)保留了文件 SHA-256、错误范围、语句范围、首轮和重复解析时间。

每个文件先解析一次并检查树，然后在同一进程和同一 Parser 实例中全量重解析 100 次，每次不传旧树，报告中位数与第 95 个排序值。各文件 warm 中位数相加为 **0.937 毫秒 / 10 文件**；这是小语料纯解析成本，排除了磁盘读取、树遍历、关系抽取和模型调用，不能代表完整索引或问答耗时。首轮受加载与页缓存影响明显，例如 SYNP000 首次解析 13.212 毫秒、service-entry 首次解析 5.530 毫秒，不应只宣传 warm 指标。

另启 7 个全新 Python 进程，包含进程启动、import、加载动态库、读取并解析 `screen-control.cbl`，墙钟中位数 **40.678 毫秒**。文件系统缓存未清空，因此这里的 cold 指新进程，不是冷磁盘。内部 import/加载/读取/解析计时中位数 2.988 毫秒不包含 Python 启动和 benchmark 模块之前的标准库 import，不能与前一个指标混用。

额外探针 warm 中位数为 0.083 毫秒。这些数据说明本地语法前端具备毫秒级处理小文件的潜力，并没有证明使用外部解析器能缩短当前问答总耗时。

## 企业分支的有限构建尝试

企业分支没有提交 `src/parser.c`，先在其目录执行 `tree-sitter generate`。生成进程运行 **5 分 24 秒**仍未产出该文件，最后观察到约 99% CPU、RSS 1,246,128 KiB；为控制试验范围，主动终止了这个进程。Node 包装器随后让预先串联的编译命令继续执行，clang 因缺少 `src/parser.c` 退出。没有后续残留生成进程。

该结果仅记为“本次工具版本和环境下，限时生成未完成”。它既不是解析质量失败，也不是永远无法构建；本次没有它的 ERROR 数、源行映射或运行时速度数据。不引用它 README 的百分比代替本地实测，不继续无界等待，也没有临时改 grammar 来强行完成试验。

## 接入和打包建议

本轮不默认替换解析前端。先把正在改进的 token / 语句分析用于消除一行多语句漏识别、嵌套作用域误归属，并对尚不支持的语法保留明确状态；不要把有限 token 结构描述成完整 AST。独立比较最终业务回答，才能确认质量收益。

后续外部适配器应输出统一的语句类型、读写对象、原始起止行列、解析器版本、原始文件哈希及未解析范围。只有完全位于无错误区域的候选结构才进入进一步校验；错误恢复节点不得覆盖已有事实。平铺 IF/EVALUATE 还需要独立的作用域建模，COPY 需要保留原文与展开文的双向映射。可按源码哈希缓存语法结果，并在后台索引时生成，避免在每次提问时重复解析或启动子进程。

已实际安装 macOS arm64 的 Python 3.14 runtime wheel，并仅下载确认 Windows x64 的 Python 3.13/3.14 runtime wheel 存在。项目当前 Windows 构建限制为 Python ≤3.13，因此有意义的目标是 cp313 wheel。**这不代表 Windows grammar DLL 或完整应用已经验证通过**。若要分发，需在目标平台编译并携带 grammar、让 Nuitka 正确收集 native runtime 和语言库、验证许可收集和签名后加载。不能让用户首次启动时联网下载或编译 grammar。本轮未验证这些产品打包步骤，故不能宣布已具备 Mac/Windows 默认接入条件。

## 重现原版试验

以下操作只写 `.build/parser-trial/`；依赖下载需网络。先在仓库根目录创建环境和下载固定版本：

```sh
mkdir -p .build/parser-trial
python3 -m venv .build/parser-trial/venv
.build/parser-trial/venv/bin/python -m pip install 'tree-sitter==0.25.2'
git clone https://github.com/yutaro-sakamoto/tree-sitter-cobol.git .build/parser-trial/upstream
git -C .build/parser-trial/upstream checkout 550020ddf42ef9b718ad897868a9308d2afcda35
clang -O2 -dynamiclib -fPIC -I .build/parser-trial/upstream/src .build/parser-trial/upstream/src/parser.c .build/parser-trial/upstream/src/scanner.c -o .build/parser-trial/upstream-parser.dylib
.build/parser-trial/venv/bin/python docs/research/parser-trial-2026-10-07/benchmark.py
```

脚本默认使用原版；输出 `.build/parser-trial/upstream-results.json`。源文件 SHA-256 若与保存报告不一致，应先检查 fixtures 是否变化，不能直接把两次结果当作同一语料。脚本为 macOS 研究入口，其他平台需要相应的共享库构建 / 加载方式。企业分支如果未来完成构建，可把导出的 `tree_sitter_cobol` 语言库保存为 `.build/parser-trial/cobol-parser.dylib` 并设置 `PARSER_TRIAL_CANDIDATE=enterprise` 运行同一脚本；这不是本次已经完成的测量。
