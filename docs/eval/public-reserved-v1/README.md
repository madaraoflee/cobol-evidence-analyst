# 固定公开留出题

本目录冻结 4 个业务题组、6 个回合，其中一个题组包含 3 轮同义表达及会话指代。4 个程序和相应业务路径未用于本轮公开开发题；源码仍来自公开 CardDemo 固定提交 `59cc6c2fd7ebd7ef7925cad552a01a4b8b6e4d5e`，保留在外部目录，没有复制到代码仓库。14 个程序/COPY 文件、下载 archive、许可文件及题目/gold 的实际 SHA-256 记录在 manifest 和 freeze 中。

[cases.json](cases.json) 使用现有 `live_business_eval.py` 的单题/多轮格式，每轮绑定原始文件哈希、行范围、必须解释的事实与禁止新增结论。它是在产品实现之后独立审阅源码形成的保留集。冻结时未运行检索、模型、默认机制，也未用这些 gold 调整产品代码；待实现负责人宣告代码冻结后才能执行离线供证验收。冻结后的题目与 gold 不随失败结果改写，必要勘误须保留原身份及理由。

这是一组公开来源的保留样例，不代表企业目标方言、生产数据或真实模型准确率。离线检索/供证若执行，应分别报告定位与实际供应，不能把看到gold或本地fake回答当正确率。真实模型运行依然需要已有授权范围内的明确配置与独立评审。

人工评审应通过现有 automatic、reviewed-context 或 paired-synthesis 报告，绑定实际 run、turn/branch、answer hash、模型/prompt/policy、源码/框架版本、gold及实际供证指纹；填全表单与答案正确分别统计。再次生成答案不会沿用原评分。

最后一次代码冻结后，已在实际 `cobol-facts-v5`、pipeline v11、prompt v16 上真实运行 [mechanism-results.json](mechanism-results.json)。运行前后产品 Python 文件的联合 SHA-256 保持一致；6 个回合均实际供应目标程序，三轮序列分别保留 0、1、2 个此前用户回合。fake transport 只收到默认请求中的原文与会话，不收到 gold、预期路径或评审范围，回答仅说明供证状态，因此业务答案正确性与真实模型准确率均为 null。

原始审阅范围完整供应为 3/6 回合。冻结标注有一处末行超出真实 EOF 两行，而该例实际完整源码已经供应；[annotation-erratum.json](annotation-erratum.json) 保留原题/gold哈希并单独说明此作者标注缺陷，按该明确勘误边界计算为 4/6。其余两个追问供应了有关语句片段，但未覆盖全部原始审阅范围。这里计算同文件同哈希实际页面的原始行并集，不能将片段供应或行覆盖率称为语义支持率；没有据留出结果调整产品代码。

第一轮冻结后来因独立审查反例撤销，结果保存在 `withdrawn-mechanism-results.json` 并明确撤回；它不计入最终验收。冻结题目与 manifest 的 SHA-256 均未变化。需要重复机制测时，先声明新的产品代码冻结身份，再执行：

```bash
python3 docs/eval/public-reserved-v1/run_mechanism.py --source-root /path/to/fixed-public-checkout --code-freeze-id your-frozen-code-id --output /path/to/reserved-mechanism-results.json
```

该命令只有本地 fake 请求，调用现有 automatic 评估器的 dry-run 和默认问答入口；来源或冻结题目指纹改变时立即拒绝。真实 reviewed-context 评估使用范围勘误时，应建立新的评估身份并保留原冻结集，不能静默修改旧结果。
