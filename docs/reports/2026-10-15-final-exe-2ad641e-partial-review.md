# 最终 2ad641e EXE 独立复核

核心判断：同包离线机制通过，月息修订的累加遗漏已在最终 EXE 正文补齐；当前仍有真实遗漏与未知，不能宣称正式质量验收完成。

原生执行 `4e090aedec6a95f1a55c923549093147`，67 个顶层样本及 14 个独立 probe 通过；耗时 612.174 秒。报告 SHA `dd58dfa6167ea6f2ed8e55386b6890ada45de84caf2ea5e5cfe0c2fc974ae882`。

实际主答案67条：27 ANALYZED、40 PARTIAL；另14个probe均PARTIAL，合计54个实际PARTIAL。上版48加本次6个CL观察，不是质量退化6题；原40身份全部配对，仍12 ANALYZED/28 PARTIAL。9条正文变化分别为超限101、正常月息条件/PIC、修订月息ADD/调用/条件/PIC各3条。月息修订的missing accumulation消失，但上游input未闭合，状态仍PARTIAL。

9个实际对话轮独立核对，inputs均SATISFIED、scope异常0。首轮3条仍缺条件与累加，第三轮3条仍缺累加；另前端3条缺舍入/覆盖、大库宽问5条缺后续调整，共14条answer_incomplete，全部保留，不借第二轮或旧评分遮掩。

| 实际类别 | PARTIAL数 | 新鲜复核结论 |
|---|---:|---|
| activation_trigger | 3 | 源码条件 READY-FLAG=1 与 LAUNCHER→JOB-STEP 调用可独立解释；entry/caller/condition 三义务均 SATISFIED。scheduler_binding 仍 OPEN，不推断生产调度或实际执行。 |
| conversation/1 | 3 | 首轮原正文仍只区分ACCOUNT-TOTALS清零与PAGE-TOTALS换页，已供应的条件与累加未展开；missing conditions/accumulation。inputs SATISFIED，作用域异常0；保留真实遗漏，不借其他轮评分。 |
| conversation/3 | 3 | 第三轮实际指向ACCOUNT-SUM累加与归零，但固定合成正文仍漏ADD，missing accumulation；accumulation_step SATISFIED表示已供证，不表示正文已解释。 |
| dense_record | 5 | 乘2已替换错误999，独立 MOVE0 保留。inputs 来源搜索未闭合，不能将 input_source_not_located 直接当作运行输入不存在。request_budget 有可用正文。 |
| frontend_semantics | 3 | ADD GIVING 的 A/B 读取、C 接收与限定 AMOUNT 乘2有结构支持；固定合成正文仍漏已供应的 ROUNDED/后续写入，missing result_adjustments/rounding/result_overrides。A 的 input_dependency_cycle 是来源frontier诊断，不是已证明运行循环。 |
| public_limit_rejection | 3 | 新增 READ ACCOUNT-FILE INVALID KEY 下常量101，真实正文、支持ledger及已供源码395-397行相符；102超限与103到期日后续覆盖原解释保留。上游inputs/dependencies仍未闭合，not_assessed不等于完整业务正确。 |
| public_account_update | 3 | 余额加累计、两个周期字段归零和REWRITE请求、状态0/12解释保留；上游输入与外部终止实现未闭合，不能宣称实际持久化成功。 |
| public_monthly_interest | 3 | 正文保留/1200与ADD累加，新增IF DIS-INT-RATE NOT=0条件和PIC S9(09)V99/无ROUNDED边界；赋值与条件ledger supported。PIC/调用/数值行为prose仍标unsupported，不冒充完整COBOL算术或持久化证明。 |
| public_monthly_interest_repair | 3 | 999仅保留在被拒绝审计claim中，最终/1200替代supported；新增实际ADD WS-TOTAL-INT累加、PERFORM写入调用、调用条件与PIC边界。missing accumulation已清，stop由answer_incomplete变question_evidence_incomplete；输入来源与写入实际效果仍有限。 |
| large_repository | 5 | 正文准确限定首条 IF 的乘1局部事实；已供后续覆盖仍未展开，missing result_adjustments/result_overrides。12k库入口/路径/候选预算及输入未闭合保持独立，不能升级为全库最终业务解释。 |
| cl_batch_context | 3 | 原生报告含SBMJOB、INLLIBL/JOBD、CHGLIBL/ADDLIBLE、OVRDBF、MONMSG、DLTOVR与两次独立BATCHPAY调用。正文保留变量成员&DAY未求值、实际调度/库列表/覆盖生效未知。CL正文ledger全为unsupported prose，不能宣称已逐结论核验；首句合并launcher与nightjob事实但句末只标launcher引用，跨页引用粒度仍待真实问答评审。 |
| cl_late_context | 3 | 实际第304行 CALL BATCHPAY PARM(&READY) 的精确供证为294-306行；索引保存第3行IF guard但未供应其原文，status=candidate/control_scope_supplied=false/missing_guard_source_lines=[3]，正文明确是否走到调用未知，未变成无条件执行。 |
| frontend_semantics/bare_group_field | 3 | 组内VALUE-00000=VALUE-00001*2和PIC定位有独立结构支持；初始输入来源/运行值仍未闭合。 |
| frontend_semantics/qualified_output_scope | 3 | INPUT-REC与OUTPUT-REC的AMOUNT身份精确区分，乘2支持；输入运行值尚未给出。 |
| frontend_semantics/ambiguous_group_field | 3 | 裸AMOUNT保留两个限定候选并给出独立乘2关系，没有任选一个或添加无关C舍入义务。 |
| large_repository/large_two_if_pic | 5 | 两条IF各自条件、乘1/乘2后续覆盖与PIC9(9)均实际出现在正文，4条结构claim supported；输入来源未知与后续全库影响仍限定，未夸大前两条IF范围。 |

原40身份中的conversation保留历史聚合清单turn_id=1键；该聚合行的run、正文和ledger实际来自第三轮，已逐一核对与实际turn3相同。首轮、第二轮、第三轮的9条实际观察在JSON的actual_conversation_turn_checks中分别记录，不能把聚合键当作首轮结论。

身份核对覆盖81个实际答案的272条ledger，284个可见引用关联、236个已归档供证版本关联、458个SATISFIED义务可见关联，均一致。23个观察未在原生报告归档row.evidence，相关48个版本关联保持“未归档”，没有当作不匹配，也没有拿source重放补证。原始provider wire与根级completion trace不在下载白名单内，不能声称重解码。变化组及CL共16个独立源码节录已另按固定原文件SHA/实际节录SHA核对，全部相同。

CL首句把launcher的NIGHTJOB提交与nightjob的BATCHPAY调用放在同一句，近邻引用只指launcher；nightjob虽已供应且后文引用，这个跨页结论的引用粒度仍待真实问答评审。CL prose ledger本来为unsupported，离线检查只证明源事实与供证，不证明每条自然语言结论已核验。迟到CALL仍保留第3行guard未供证的candidate状态，没有无条件执行推断。

候选ZIP/x64/CRC/commit/合同/校验和独立核对通过，candidate_allowed=true、publish_allowed=false。无真实/收费模型调用，无企业设备验收，无旧评分迁移；这轮复核不授权正式发布。
