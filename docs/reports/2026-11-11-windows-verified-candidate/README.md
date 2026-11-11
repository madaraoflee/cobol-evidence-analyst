# Windows v2026.11.11.1-rc.1 验证记录

候选 86c8b28501eedbc90f8dbfdf991bc7ebe26cce0e，Windows run 42978782500，attempt 1。发布在 GitHub，九个附件的字节数、SHA-256 与原始上传附件逐一吻合。ZIP SHA-256 为 `4106cc59f0e249de7606c93a54afe1502a3826bb239009ea49338364489a988c`。

产品修复后的全量源码回归 5,427 项 Python 与 338 项浏览器检查通过。Windows 分组修复后的全量源码回归 5,430 项 Python 与 338 项浏览器检查通过，零失败、零跳过。Windows 四组共 5,419 个结果，5,417 通过、两项既有 POSIX 权限测试按原规则跳过。四组实际 24:39、19:04、19:22、25:40。10,001 文件在线完整清单用时 19:54.264。

同包 79 项联合样本、55 项续查与 13 项全库连续性全部通过。此前失败的五次精确问答均供给两条条件、三处声明及原输入范围；最长 51.183 秒，原 60 秒门槛通过。全库报告验证 257 处公式及分页导出和无进展停止后保留待办。真实公司设备、真实模型和逐条人工验收仍待完成。

补充工具原先误把编译 Python 诊断字段当作操作系统启动路径。冻结的 Nuitka 4.2.2 在 standalone 模式保存编译解释器的名称，并在运行时加上产品目录。实测包只有一个 EXE。原 Windows 工具直接启动了按 ZIP 摘要校验后的 EXE，并从已加载模块核对同目录的实际 SQLite DLL。补充工具现核对限定版本下的同目录兼容字段，继续拒绝异目录、错误编译器及 ZIP 中另带解释器的情况。[Nuitka 4.2.2 原代码](https://github.com/Nuitka/Nuitka/blob/4.2.2/nuitka/code_generation/ConstantCodes.py#L110-L118)

同一 ZIP 的 Windows 办公助手已在 GitHub runner 编译和启动，使用同一 SQLite 3.54.1，组装后用原 EXE 通过计划核对和 79 项原生离线样本。验收助手提供现场复测入口。公司现场六条仍保留为未验证。[办公助手运行记录](https://github.com/madaraoflee/cobol-evidence-analyst/actions/runs/42986439252)
