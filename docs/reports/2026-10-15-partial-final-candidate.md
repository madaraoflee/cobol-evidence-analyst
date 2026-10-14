新候选 EXE 修正了静态调用来源与空手续费段的调查误判，但原来 40 个 PARTIAL 没有全部结案：同题新运行有 9 个 ANALYZED、31 个 PARTIAL。剩余项既有真实未知，也有已知源码义务尚未闭合和合成短答遗漏；尤其三轮题的 VALUE 来源与文件对象绑定仍是本地缺陷，不能归为运行数据未知。

本报告冻结于 `9409c210152fbee2466dba8d2f979824c291a826`。后续修复须用新包重新验收；这里保留冻结版的缺口。逐项原始义务、stop、run、answer hash、源码版本、证据和独立事实见 [40 项 JSON](2026-10-15-partial-final-candidate.json)。

对照对象是旧 `44fcb5f` EXE 在 Windows 原生 HTTP 工作台上重新跑出的 55 项报告，和新 `9409c21` EXE 的 Windows 构建运行 `38632060929`，不是把旧原始 CI 样本改判。旧重复的 helper 为 `530448a`，两份报告各有独立执行 ID 与文件指纹：

| 身份 | 旧 EXE 原生重复 | 新候选 EXE |
|---|---|---|
| 应用源码 commit | `44fcb5f9e769ae559c9cd6b5b12d396959613998` | `9409c210152fbee2466dba8d2f979824c291a826` |
| 报告执行 ID | `b4772b6d2f6a9ad29878ba4e3aada667` | `ee40ad5dd9f15aaf0f8d987209833722` |
| EXE SHA256 | `38ccd5e9a4147e4107a18216722eaa59681e4e625e65bda714391d2ec9a412df` | `66b43673d4060c1e99e5f7c500a12f6c59056a3f2c8ad31c10afb967fd909e23` |
| 压缩包 SHA256 | `065f1ea39e1cdd6313b9eb2d826f987af1286ef56bf8de733450df86d2f947e0` | `eaac061ca8939672c1663c8b6d21c62744512f00e5aa912a20336a6d37e6f454` |
| 报告 SHA256 | `ad136e1382aae3e349a1057fc372be46726baf45befc0b0e6c2adca15c647a7b` | `5111e99e9c130746490cc3291908dccc4fcc226f3d5ad6ffb094325e79cdd11d` |
| manifest SHA256 | `fbdc9ffe0475da02330a06db57e8efc97ff492d2197f11b91760c17567040844` | `7eebcf7fd5d30ee6d8c8a71ad5311c0cef98ca59c32645b9c7f704efa23c76b8` |

40 项按 case / turn / sample 配对，全部核过嵌入诊断与 run、回答正文哈希、状态、final 指纹的绑定。新旧 40 个最终正文完全相同，因此状态改变证明的是本地调查义务或范围修正，不能解释成真实模型回答质量提高。旧原始 CI 报告及其评分保持原样；本报告没有结案任何旧 run。

公开源码固定 commit、manifest 与题目指纹一致；12024 文件大库生成器、59039908 源码字节、2712992 行和整库指纹也一致。运行夹具的生成函数 AST、题干和每题预算没有变化。前端主问仍使用相同 `frontend.cbl`，新包额外加入 `record.cbl` 及三项 probe，证据上下文不是完全相同。新断言增严了静态来源闭合、空段范围与四种默认入口，未移除旧断言或放宽既有时间门槛。

旧 40 项停止原因是答复遗漏 20、证据义务未闭合 12、请求预算 8；新同组是充分材料 9、答复遗漏 8、证据义务未闭合 15、请求预算 8。下表只说明可核对的变化，不给真实模型准确率评分。

| 原 PARTIAL 场景 | 项数 | 新状态 / stop | 具体变化与仍未完成部分 |
|---|---:|---|---|
| `conversation` | 3 | PARTIAL / `request_budget` | 已声明 VALUE 的初始定义尚未纳入有效到达来源；循环 frontier 与首轮短答条件遗漏仍在，不得归为纯运行输入未知。 |
| `dense_record` | 5 | PARTIAL / `request_budget` | IN-AMOUNT 仅有 PIC，无 VALUE 或其他输入赋值；真正未知的是初始输入数值/来源，不是公式。单请求策略阻止再发请求。 |
| `frontend_semantics` | 3 | PARTIAL / `answer_incomplete` | 三次仍 answer_incomplete；真实供应中的 ROUNDED、后续覆盖未被合成主答解释，A 来源 cycle 也仍在。新 probe 通过不能抵销旧主问缺口。 |
| `static_call_chain` | 3 | ANALYZED / `sufficient_material` | 从 input_source_not_located 转为 source_flow_origins_supplied，真实供应三程序、参数绑定、正常返回的到达定义共同闭合新运行来源义务。 |
| `public_limit_rejection` | 3 | PARTIAL / `question_evidence_incomplete` | input_source_not_located 与 dependency_candidate_budget 仍在；短答只说102和后续103，没覆盖 INVALID KEY 101。不能全部算作不可知外部值。 |
| `public_account_update` | 3 | PARTIAL / `question_evidence_incomplete` | 去掉无关全程序公式/字段预算后仍有输入来源义务；真实依赖 CEE3ABD 在错误路径保留，未冒充已知外部终止实现。 |
| `public_monthly_interest` | 3 | PARTIAL / `question_evidence_incomplete` | answer_incomplete 改为 question_evidence_incomplete 是范围与义务调整；TRAN-CAT-BAL、DIS-INT-RATE 仍未知来源，错误依赖仍保留，不能按新 stop 断言整题完整。 |
| `public_monthly_interest_repair` | 3 | PARTIAL / `question_evidence_incomplete` | 错误999保留 contradicted 原记录并提供 supported 的1200替代；新 stop 仍反映来源缺口。修订短答未完整展开累加和写交易效果。 |
| `public_file_record_mapping` | 3 | PARTIAL / `question_evidence_incomplete` | ACCOUNT-FILE 被当字段而 requested_field_not_bound，连带 business_steps_not_located；SELECT/FD/READ/REWRITE 已供应，是本地义务建模残余。实际运行资源路径未知另行记录。 |
| `public_unimplemented_procedure` | 3 | ANALYZED / `sufficient_material` | 只检查完整1400段后，不再把范围外月息公式/输入义务强加给空手续费段；新运行无 gap。 |
| `public_unimplemented_procedure_repair` | 3 | ANALYZED / `sufficient_material` | 虚构1%手续费被完整空段证据修订；旧 contradicted 记录保留，新运行范围外义务消除。 |
| `large_repository` | 5 | PARTIAL / `answer_incomplete` | 原宽问题五次仍缺后续调整/覆盖，located_path_budget 省略12016路径且公式/字段/条件 frontier 在；新增具体段两条 IF/PIC probe 通过只能证明局部默认问答。 |

8 项请求预算的根因必须拆开看。3 项 conversation 是每项三轮的汇总记录，实际每轮只允许一次模型请求，首轮仍为 `answer_incomplete`（漏条件），后两轮 `request_budget`；新来源 witness 对 TRAN-AMT、PAGE-SUM、ACCOUNT-SUM 报 `entry_state_or_external_input_not_defined`。然而同版本 `report.cbl` 明确写着 TRAN-AMT VALUE 25.00、PAGE-SUM 与 ACCOUNT-SUM VALUE 0，且 ADD / 两处清零语句已供应。这是初始化到达定义未闭合及 ACCOUNT-SUM 候选 cycle，不能用“输入来自运行时”结案。

另外 5 项 dense_record 的 IN-AMOUNT 只有 PIC 9(9)，没有 VALUE、读取或其他输入赋值。这里不能给实际数值或凭空补来源；但乘2公式和 STATUS-CODE 清零仍独立成立，错误乘999已本地修订，原错误 claim 仍保留。两类停止均由冻结 helper 的 `max_model_requests=1` 和每轮实际一次请求的断言解释；本地数秒完成，不是远程模型卡几十分钟，也不能据此断言公司模型的补查成本可接受。原始 `input_source_not_located` 只说明来源义务未满足，本身不能区分真实未知与已供定义未用上；报告里没有 `source_source_definition` 这一字面原因码。

文件映射的三个样本是第二个仍确定的本地缺口：ACCOUNT-FILE 的 SELECT / FD / READ INTO / REWRITE FROM 已实际供应，新的计划却给出 `requested_field_not_bound` 和 `business_steps_not_located`。输入义务改为 NOT_APPLICABLE 是正确方向，但文件实体被当字段依然没解决。实际数据集路径、文件内容和错误分支 CEE3ABD 的实现未知，是另外三件事，不能用它们解释本地文件对象没绑定。

跨程序三项现在明确以三文件、参数映射和确认返回到达定义闭合 OUT-AMOUNT 来源，未使用同名候选猜闭合。空手续费段六项现在用完整1400段证明无计算或写入，移除范围外月息义务；修订题的1%虚构陈述保留 contradicted 记录并提供 supported 的空段替代。这些是已通过的局部源码机制。ledger 的语句支持范围、`prose_semantics_verified=false` 与 `execution_verified=false` 均保留，不能推成真实执行或所有业务文字已验证。

前端三项仍漏 ROUNDED、后续覆盖及结果调整；原来的词法、字符串、限定字段、同一行 occurrence 与 Section 机制断言已通过，主答完整度没有因新 probe 通过而解决。公开月息两组虽然不再被范围外条件误判为 `answer_incomplete`，仍是 PARTIAL；修订短答也没有完整解释累加、写交易和调用点条件。超限题的短答漏 INVALID KEY 101，账户/月息题仍有真实错误依赖与记录来源；这些必须用公开逐 finding 金标准及真实答复审查，不能凭 stop 换名结案。

新增 14 个默认工作台 probe 全部通过：裸组字段、限定输入/输出字段、歧义字段各三次；12024 文件库里的指定段两条 IF、PIC 和覆盖五次。它们有各自的真实 EXE run、回答、请求与源码证据指纹，实际有些仍为 PARTIAL，因为未知输入被保守保留。通过的是明确供证、身份、语义与预算断言，不是强制所有题显示 ANALYZED。旧版本没有对应原生 probe，不能据此计算前后准确率或时延提升。

速度只在同一问题和计时范围内比较。新前端和大库主样本的 `question_seconds` 包含新增 probe；下表用 `primary_question_seconds` 对比旧主问，另把 probe 总时间保留在 JSON。conversation 是三轮合计，其余是单主问。

| 场景 | 旧主问中位秒 | 新主问中位秒 | 新主问最大秒 | 新首次请求最大秒 |
|---|---:|---:|---:|---:|
| `conversation` | 1.668 | 1.664 | 1.691 | 0.428 |
| `dense_record` | 0.722 | 0.755 | 1.495 | 1.185 |
| `frontend_semantics` | 0.423 | 0.479 | 0.680 | 0.379 |
| `static_call_chain` | 0.405 | 0.429 | 0.562 | 0.333 |
| `public_limit_rejection` | 0.953 | 0.934 | 1.023 | 0.671 |
| `public_account_update` | 0.843 | 0.855 | 0.928 | 0.605 |
| `public_monthly_interest` | 0.769 | 0.760 | 0.868 | 0.558 |
| `public_monthly_interest_repair` | 0.792 | 0.775 | 0.854 | 0.551 |
| `public_file_record_mapping` | 0.812 | 0.756 | 0.833 | 0.533 |
| `public_unimplemented_procedure` | 0.813 | 0.702 | 0.849 | 0.499 |
| `public_unimplemented_procedure_repair` | 0.840 | 0.717 | 0.850 | 0.536 |
| `large_repository` | 8.729 | 8.147 | 8.526 | 4.477 |

大库一次冷导入旧 413.730 秒、新 401.533 秒；这是一次导入观察，门槛保持 1800 秒，不能用作稳定吞吐结论。新具体范围 probe 五次 5.846–5.943 秒，含主问加 probe 的五次总时长 13.975–14.454 秒，满足原 60 秒问题门槛。原宽问题仍有 12016 路径未展开、公式/字段/条件 frontier 及 3 秒 initial_context 边界，主答也未解释后续覆盖；局部快不能冒充全库调查完整。宽记录和前端主问本次略慢，不能泛称全部性能提升。

这版 55 主样本和 14 probe 的 Windows 原生 EXE 门禁通过，发布身份仍为 `candidate_unaccepted`。本次使用本机合成 transport，真实模型调用和收费调用都是0，真实模型准确率及业务正确率均未测。Windows Server CI 不代替公司目标电脑 GUI、公司模型或企业方言验收；下一版先修上述两个本地确定缺陷，再用新包重跑机制与逐项真实问答。正式发布仍须按同包联合验收门禁，旧评分不得附着到新答案。

审计输入：

- 旧原生重复：`release/office-acceptance/530448a-native-baseline/Windows-office-acceptance-kit-530448a65bd61a3cf0fecfb05e7deaf52436ec2c/office-native-check/COMPILED-ACCEPTANCE.json`。
- 新候选：`release/joint-acceptance/9409c21/candidate/COMPILED-ACCEPTANCE.json` 与同目录 manifest / policy / BUILD-INFO。
- 旧原始 CI：`release/joint-acceptance/44fcb5f/candidate/COMPILED-ACCEPTANCE.json`（SHA256 `9277f8debaf6ae24a39f12822707147133b27936f985e8ba2dc2a1a51c9df536`，未修改）。
- 冻结运行源：`530448a` 与 `9409c21` 的 `tools/desktop_acceptance_scenarios.py`、`tools/desktop_acceptance_runtime.py`，函数正文与 AST 指纹保存在 JSON。
- 本报告仅写这两份文档，不改应用、门槛、旧诊断或人工评分。
