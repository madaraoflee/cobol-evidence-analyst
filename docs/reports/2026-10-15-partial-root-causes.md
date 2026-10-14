# 44fcb5f 候选 EXE 的 PARTIAL 调查

这 40 个 PARTIAL 不能统一归为“模型没答好”，也不能直接改成通过。现有证据显示至少有三种不同问题：合成答复只验证某一机制，输入或外部效果确有未知，以及调查义务越过了用户所问的段落或字段。尤其是 `1400-COMPUTE-FEES` 的正确“只有 EXIT，未实现”答复，仍被按同程序其他段落的月息公式和条件判作不完整；这属于本地范围选择问题，换真实模型也不保证消失。

本报告只调查原因，未修改冻结的应用代码或 EXE，未调用真实模型，未给任何原样本补打人工质量分。正式发布仍未通过。

## 原始产物与诊断边界

原报告为最终 Windows HTTP 验收的 `COMPILED-ACCEPTANCE.json`，应用提交 `44fcb5f9e769ae559c9cd6b5b12d396959613998`，ZIP SHA-256 为 `065f1ea39e1cdd6313b9eb2d826f987af1286ef56bf8de733450df86d2f947e0`，EXE SHA-256 为 `38ccd5e9a4147e4107a18216722eaa59681e4e625e65bda714391d2ec9a412df`，报告 SHA-256 为 `9277f8debaf6ae24a39f12822707147133b27936f985e8ba2dc2a1a51c9df536`。

55 个原样本中，40 个 PARTIAL 的原始停止信息为：20 个 `answer_incomplete`，12 个 `question_evidence_incomplete`，8 个未归档停止原因。32 个保存了正文、ClaimLedger 和证据指纹，但没有归档 `answer_completion`、`question_investigation` 的具体义务；另 8 个为对话和密集字段测试，连正文及停止原因也没有进入汇总。不能用另一次运行的同名案例或相同答复填补这些缺项。

为定位原因，本次在当前源码默认桌面 HTTP 路径复现了 13 个非大库案例各一次，对话案例包含三轮。产品 Python 文件相对冻结提交没有改动，使用仓库固定的公开源和本机合成 HTTPS，13 个机制案例通过，11 个仍 PARTIAL；完整诊断保存在新的独立 run 中。这是 `source_python_http`、Darwin、修改过重复次数的诊断运行，不是 Windows EXE 验收，也不是 55 样本重验。两次未能启动的预尝试分别受本机 loopback 沙箱及临时 Python 缺少 `certifi` 影响；在隔离环境使用仓库固定版本 `certifi==2026.7.22` 后完成复现，失败结果没有替代最终结果。

大库的具体义务来自已有的较早源码诊断提交 `057e54fce4f75ce5f2bddd4b0456568644f9a0b6`。其语义未知和候选预算仅作补查线索，没有冒充本次原生 Windows 的五次运行结果。

## 逐案例原因

下面覆盖全部 40 个原 PARTIAL 样本，每个原 run、答复哈希和已知停止信息另存于 [逐样本诊断 JSON](2026-10-15-partial-root-causes.json)。表中的具体义务来自独立源码复现；原生 run 缺失的诊断仍保留为缺失。

| 原案例及样本数 | 原始停止信息 | 独立复现中观察到的具体原因 | 必须在真实 EXE 问答中核对的结论 |
| --- | --- | --- | --- |
| conversation，3 | 未归档；每样本包含三轮 | 首轮漏 `conditions`；后两轮为 `request_budget`，来源候选图出现 `ACCOUNT-SUM` 循环。合成接口三轮都返回同一短句，只说明账户清零，没有完整解释累加、卡号变化和 `FIRST-ITEM` 条件。 | 源码有 `ADD TRAN-AMT TO PAGE-SUM ACCOUNT-SUM`、两个独立汇总段及控制条件，来源不是全部未知。区分换页清零 `PAGE-SUM` 与换账户清零 `ACCOUNT-SUM`，逐轮说明真正所问对象。候选字段图的环不等于真实执行环。 |
| dense_record，5 | 未归档 | 复现为 `request_budget`；公式本身已支持，`IN-AMOUNT` 的输入来源未定位。密集 fixture 给该字段 PIC，但没有 VALUE、输入语句或有效赋值。 | 可确认乘二公式和状态码写零；不能凭 PIC 断言输入值。确认未知只限制金额数值或来源，不让独立公式失去解释。 |
| frontend_semantics，3 | answer_incomplete | 漏 `result_adjustments`、`rounding`、`result_overrides`；候选来源图出现 `A` 环。合成短答只说明两个公式、GIVING 角色和字面量，没有解释 ROUNDED、后续 MOVE 和 Section 完整执行范围。 | 七个词法反例已在 IR/基础关系中通过，仍需真实答案按各 occurrence 区分局部赋值与最终值，解释两个 Section 的边界及覆盖，不把同名字段候选合并成执行链。 |
| static_call_chain，3 | question_evidence_incomplete | 三个文件和叶程序公式已供应；要求 `OUT-AMOUNT` 来源时，同文件候选搜索仍报 `input_source_not_located`，没有把已得的跨程序参数/返回摘要用于完成来源义务。 | 这里不是缺失源代码：入口 MOVE 4，经 MIDDLE 和 LEAF，叶程序乘二，返回读取到 FINAL-AMOUNT。将调用映射、正常返回和 reaching definitions 一同核对；不能把候选搜索失败说成输入不可知。 |
| public_limit_rejection，3 | question_evidence_incomplete | 整程序条件/赋值/调用候选达到预算，多个文件状态来源未定位。关键比较、102 和后续 103 的局部结构均支持。 | 合成答案缺少黄金案例要求的读取 INVALID KEY → 101 和余额来自外部文件；原机制“通过”不表示这条完整业务回归通过。检查预算缺口是否真影响所问的 1500-B 段，保留运行数据未知。 |
| public_account_update，3 | question_evidence_incomplete | 来源未定位，整程序赋值/公式候选预算；错误终止调用 `CEE3ABD` 无实现。已供更新余额、周期金额清零、REWRITE 和状态处理。 | 可独立解释发出改写请求及成功/失败分支，不能声称真实写入成功。外部终止机制未知要绑定到错误路径；不能让它自动遮蔽正常路径的直接事实。 |
| public_monthly_interest，3 | answer_incomplete | 本地完整性检查要求 `formula`、`conditions`，短答只给月息除 1200、累计 ADD、后续 PERFORM，报 `conditions` 缺失；输入字段来源另有缺口。候选包含整个程序的五个公式。 | 1300 段自身是 COMPUTE/ADD/PERFORM 顺序，没有局部 IF。判定该条件义务来自相关调用控制还是同程序无关公式，不能要求模型凭空增添条件。利率及余额的真实数据仍不可从源码猜出。 |
| public_monthly_interest_repair，3 | answer_incomplete | 999 已被本地核验修订为 1200，原错误保留在 ledger；修订后的短答未交代累计和写入路径，还报 `conditions` 缺失。 | 不能把原 ledger 的 contradicted 计数当成最终正文仍错，也不能因为公式已修订就宣称完整。真实答案须核对累加、调用与相关条件，并限定运行数据未知。 |
| public_file_record_mapping，3 | question_evidence_incomplete | 静态映射已有供证；调查仍引入整程序月息输入、候选预算、错误终止依赖。正文的映射解释属于当前有限 ledger 尚不支持的 prose。 | SELECT/ASSIGN、FD、COPY 记录、READ INTO/REWRITE FROM 可由源码确认；实际数据集路径需运行配置。未被通用 prose checker 核验不是源码映射错误，也不意味着整程序月息的输入都是本题义务。 |
| public_unimplemented_procedure，3 | answer_incomplete | 正确指出完整 1400 段只有注释与 EXIT，仍要求全程序月息 `formula`、`conditions`；planner 实体为空，意图为 calculation，出现 `conversation_field_not_bound`。 | 明确的范围误选：完整无操作段本身足以支持“本段未实现”。不能据名字编造手续费，也不能为补齐章节要求展开无关月息或拒绝这条正确短答。 |
| public_unimplemented_procedure_repair，3 | answer_incomplete | 编造 1% 手续费已改为完整段无操作事实，仍沿上项错误范围要求 conditions；“主流程调用”未被 prose checker 支持。 | 修订与原错误分别记录；核对调用点及完整段，无关条件不能作为放行/拒答依据。 |
| large_repository，5 | answer_incomplete | 原生正文只解释 INPUT-000 > 0 下第一条乘一，不解释随后多条计算与 ADD；旧源码复现确有 `result_adjustments`、`result_overrides` 缺失、输入未定位和多种预算 frontier。该计划列出 12,024 条路径，搜索范围很大。 | 局部公式支持不能替代最终 BILL-AMOUNT 解释。最终值受多段和后续 ADD 影响；无实参的静态调用与未初始化输入还需要受控未知分析。企业大库真实提问必须绑定对象、段落和读取点，不能靠关闭 frontier 掩盖召回或完整性问题。 |

## 根因与下一步验收

最值得优先修正的是调查范围和义务的连接：`question_investigation.py` 的输入候选遍历明确是同文件写入候选，不是 reaching definitions；却仍决定答案是否 PARTIAL，跨程序服务已产生的返回事实没有关闭对应义务。`business_synthesis.py` 根据所供的整程序候选和文本信号要求条件/覆盖，未可靠约束到所问段落，导致空手续费段的正确短答被其他算法的要点拖住。单纯提高预算、增加模型轮数或取消 PARTIAL 都会掩盖原因。

同时必须保留有限核验的边界：`PROSE_SEMANTICS_NOT_SUPPORTED` 表示当前 checker 没证明这段文字，并不自动触发 PARTIAL，也不证明文字错误。原误公式经本地支持的替代结构修订后，应按最终答复和替代结构评审，不能只读原 claim 的 contradicted 数量。真实模型需要逐题黄金结果和人工支持审阅，不得继承本机合成回复的评分。

公司电脑复跑时，应由外部验收工具保存 EXE 返回的每轮 `business_review.answer_completion`、`investigation_state.question_investigation`、`stop_reason`、`boundaries` 及 quality trace 的 final；这些信息当前 EXE 已能产生，不需要为了记录诊断重编译它。用同一 run_id 和 answer hash 合并具体义务，另一次或另版本的复现只放在 supplemental。每项应标明是已供源码未解释、义务范围错误、未完成本地补查、真实外部未知中的哪一种，并由与同包、同题、同答复绑定的实际评审决定是否满足本题，而不是要求所有题都变为 ANALYZED。

## 重现与工具验证

只读审计原报告：

```powershell
python tools/partial_acceptance_audit.py --report COMPILED-ACCEPTANCE.json --output PARTIAL-ROOT-CAUSES.json
```

新办公室报告可直接读取每行的 `acceptance_diagnostics`，核对 run、实际答案哈希、状态、停止原因及 final 哈希；真实问答行没有顶层 answer hash 时，从最终正文计算。若同次运行的 quality-traces 还在工作目录，也可增加 `--trace-root`；只有 run_id 和最终 answer hash 同时吻合，才接入具体义务。独立源码诊断使用成对的 `--supplemental-report` 和 `--supplemental-trace-root`，永远不能自动结案原 EXE 样本。工具本身不调用模型、不开端口、不修改验收报告。

本次原报告只读审计、13 案例本机诊断、旧大库资料交叉检查均已执行，机器可读数据为 [2026-10-15-partial-root-causes.json](2026-10-15-partial-root-causes.json)。工具的 14 项单元回归通过，覆盖跨 run 错配、同 run 不同答复、错误修订保留、缺诊断不结案、三轮明细、重复身份冲突、候选循环与真正外部未知的区分、大计划截取必须保留全量计数和哈希，以及原生/真实报告内嵌诊断和正文的身份核对。

本次没有真实模型准确率、公司设备结果、正式发布资格或新的性能提升数字。40 项原生问题均仍待同包真实答复和实际评审关闭。
