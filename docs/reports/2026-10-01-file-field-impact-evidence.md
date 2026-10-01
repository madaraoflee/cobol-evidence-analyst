# 扣减流程的文件与字段影响漏答

本次从远端 `main = ba9e06baedc31776df3da870756abfbcdd849819` 开始，未假定 PR #6
的默认详答、证据定位和续答已经解决文件影响分析。仓库最初没有未提交改动。
只使用原创中性合成源码与离线模拟接口；没有访问或上传公司源码、内部资料，
没有真实模型请求、付费调用或部署。

## 实际复现与修复

1. **检索方向遗漏写入子程序。** 原影响导航只沿 incoming CALL/COPY 查调用者。
   “扣减流程影响哪些 LF 和 field”能够命中扣减程序，却没有供应它调用的写入子程序。
   文件/字段影响问题现在保留调用者，并从问题根程序补入已解析的 CALL/COPY 依赖；
   限制为 32 个路径、4 层，预算截断明确报告。普通程序引用清单保持原有方向。
2. **计算型补读没有文件证据义务。** 原索引明明存有 READ/REWRITE/WRITE，
   必读项却只要求算术、赋值、条件和程序依赖。新增文件操作、SELECT/FD/record/COPY
   与 DDS 候选的有界补读；使用既有索引原文定位，实际供应仍通过源码版本和行号核验。
   一轮自动读取次数耗尽而另有内部证据待读时，在既有模型请求预算内继续，保留已知初稿。
3. **记录名被当成文件名。** 原结构图将 `REWRITE ACCTREC` 的操作数标为文件，
   没有 SELECT/FD/record 的对应关系。本次没有重写整个解析图或迁移索引格式；
   在实际请求保留下来的原文上增加保守语法观察，区分 READ 的文件操作数与
   WRITE/REWRITE 的记录操作数，经连续供应的 FD/COPY 原文映射文件和成员。
   不完整语句、缺页、歧义和不支持的 COPY/赋值形式不会变成已确认映射。
4. **原话式拒答未被识别。** “目前不足以可靠列出 LF 和字段名”在原 main 的
   完成性检查中没有任何缺答标记。新增中英文枚举拒答识别；有已供应证据时进行
   一次预算内复核，持续拒答标为 `PARTIAL / answer_incomplete`。仅说“会扣减并更新记录”
   而没提实际可见的文件/字段标识也会触发缺项提示；这是有界文字检查，不是语义验收。

WRITE/REWRITE 使用 FD 下的记录名，不能直接把操作数当文件名。
[IBM 的文件 I/O 说明](https://www.ibm.com/docs/en/cobol-linux-x86/1.1.0?topic=data-using-in-input-output-operations)
支持这个区别。DDS 的 PFILE 子句指明记录格式访问的物理文件；
匹配 ASSIGN 对象与 DDS 文件名只产生待核验的定义候选，不能证明部署后的系统 LF/PF 身份。
见 [IBM PFILE 定义](https://www.ibm.com/docs/en/i/7.4.0?topic=p-pfile)。

## 合成证据与回答边界

[file-impact-v1](../../poc/fixtures/file-impact-v1/README.md) 包含直接扣减、只读文件、
纯 COPY 记录布局、静态调用的间接写入、DDS、缺失外部实现及动态调用。

| 原文可证明的观察 | 证据位置 | 可以回答的范围 |
| --- | --- | --- |
| ACCTBAL 扣减；ACCTSTAT 设为 D；REWRITE ACCTREC | programs/debit.cbl:31–33 | 显式赋值与记录写入语句，附具体定位 |
| ACCTREC 属于 ACCOUNT-FILE | programs/debit.cbl:10–11；copybooks/ACCOUNTREC.cpy:1–4 | FD 与纯 COPY 的源码关系；字段成员不等于全部被修改 |
| READ RATE-FILE；ASSIGN RATELF | programs/debit.cbl:7、21 | 已知读取依赖；不能声称此文件有写入 |
| AUDITPOST 中赋值 AUDITKEY/AUDITAMT 并 WRITE AUDITREC | programs/audit.cbl:20–22；debit.cbl:25 | 被调程序的可见写入；属于静态间接影响候选，未证明运行 |
| PFILE(ACCOUNTPF) | dds/ACCOUNTLF.dds:1 | DDS 原文中的关系；LF 对象绑定和字段别名/派生关系仍需核验 |
| EXTERNALPOST 与 POST-TARGET | programs/debit.cbl:26–27 | 缺实现与动态目标的效果未知；不抹去其余已知证据 |

删除 DDS 后，仍能回答 ACCOUNT-FILE/ACCTREC、ACCTBAL/ACCTSTAT 与只读依赖；
删除 COPY 或只供应部分行时仍保留操作数和显式赋值，把受影响的文件/成员映射列为缺口。
没有推断未提供的 LF-PF 关系、完整执行路径、字段血缘或实际提交成功。

在独立检出的原 main 和修复工作树上，使用同一份合成源码及离线接口捕获实际请求，结果为：

| 提问方式 | 原 main 的最终原文 | 修复后的最终原文 |
| --- | --- | --- |
| 明确 DEBITFLOW 程序 | 直接赋值、读取和子程序写入已供应；DDS 未供应 | 上述证据与 DDS 均供应，1 次请求 |
| 仅中文“扣减流程影响哪些 LF 和 field” | 直接赋值和读取已供应；子程序写入与 DDS 未供应 | 直接/间接写入与 DDS 均供应，2 次请求 |

短程序中原 main 已供应部分证据，这也说明笼统拒答不合理；不能把所有漏答都归为
“完全没有取到源码”。修复检查的是补读缺口与对已有证据的回答义务，没有测真实模型质量。

## 验证与独立审查

新增回归检查实际序列化请求中的文件、原文、行号、源码摘要和引用，而非只断言模拟答案。
覆盖中文有/无程序名、英文、已有 sparse/structural 索引、缺 DDS、静态/动态调用、
读取与输出预算、来源裁剪、重复语句、COPY 歧义、字面量与注释、动态 ASSIGN、
未完成算术语句以及同名字段元数据预算。模拟回复只验证供应与恢复协议，
不能证明公司模型能正确解释真实业务。

独立审查额外复现了记录映射来源不完整、同名字段造成元数据膨胀、ASSIGN 修饰词被误认对象，
以及截断 SUBTRACT/GIVING 和 DDS 字符串被过度确认的问题；修复与对抗回归均纳入本次变更。
最终检查：完整 Python 回归 **1,410 项通过**；本次新增 **41 项相关回归通过**；
网页回归 **93 项通过**；`git diff --cached --check` 通过。
独立复核未发现剩余阻断合并的问题。轨迹版本更新为 `question-evidence-v5 / business-chat-v6`。

## 其他电脑验收

更新远端 main 并重启服务即可使用本次修复；索引格式未变，不要求仅因本次更新重新导入。
在公司批准的环境，用原问题和同一索引查看最终请求是否真的包含写入点、SELECT/FD、
记录 COPY、实际被修改字段和相关 CALL/PERFORM/DDS。
观察 `investigation_state.question_investigation.file_impact` 和本机 `quality-traces/` 的供应范围；
默认轨迹仍只保存范围与摘要，原文捕获需要显式启用。

本次没有公司失败现场或真实模型响应。因此只能确认合成复现中的漏证据与过度拒答已修复，
尚不能保证原公司案例已通过。通用观察没有新增私有框架语义、运行时文件覆盖解析、
全 COBOL 方言解析或完整 PF/LF 字段血缘。
