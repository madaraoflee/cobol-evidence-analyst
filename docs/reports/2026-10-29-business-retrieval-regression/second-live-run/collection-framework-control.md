**结论：**在题设应缴 100.00、账户余额 20.00、退款 10.00、奖励 5.00，且三项使用标志都为 `"Y"` 的情况下，只有 `READ-STATUS="READY"` 会进入抵扣计算。已供应源码可直接确认：`COLLECTION-RESULT = SPACES`（collection-entry.cbl:21）[ev_page_910812aeae1af4d74b843f3b]；在源码控制条件 `WHEN "READY"` 下，`COLLECTION-RESULT = "CALCULATED"`（collection-entry.cbl:25）[ev_page_910812aeae1af4d74b843f3b]；在源码控制条件 `WHEN "NOTFOUND"` 下，`COLLECTION-RESULT = "REVIEW"`（collection-entry.cbl:27）[ev_page_910812aeae1af4d74b843f3b]；在源码控制条件 `WHEN OTHER` 下，`COLLECTION-RESULT = "RETRY"`（collection-entry.cbl:29）[ev_page_910812aeae1af4d74b843f3b]。`NOTFOUND` 返回 **100.00 / `REVIEW`**，`ERROR` 落入其他状态分支，返回 **100.00 / `RETRY`**。`READY` 只表示允许进行抵扣资格评估，**不表示每一项抵扣都会执行**。

这笔处理的对象是保单的剩余应缴金额，不是实际收款。`COLLECTION-ENTRY` 根据数据准备状态决定是否调用抵扣程序；`OFFSET-RULES` 只计算剩余应缴，不收取现金、不改变账户余额，也不保存台账。[ev_page_910812aeae1af4d74b843f3b] [ev_page_b760eb210649d164667926a9]

### 状态与业务分支

主程序先执行 `MOVE INVOICE-AMOUNT TO AMOUNT-DUE`，所以进入状态判断时，应缴基数已经是票面应缴 100.00。随后按 `READ`READ-STATUS` 会进入三个互斥分支。调用前，主程序已把应缴基数置为 100.00，并把结果字段清空；`OFFSET-RULES` 没有单独的返回码，所以调用方只能在命中 READY 且 `CALL` 正常返回后，把结果设置为 `CALCULATED`。[ev_page_910812aeae1af4d74b843f3b]

| `READ-STATUS` | 是否调用抵扣程序 | 调用方看到的 `AMOUNT-DUE` | `COLLECTION-RESULT` | 业务含义 |
|---|---|---:|---|---|
| `READY` | 是 | **65.00** | `CALCULATED` | 已计算剩余应缴，不代表已收现或已记账 |
| `NOTFOUND` | 否 | **100.00** | `REVIEW` | 所需数据未找到，转人工复核 |
| `ERROR` 或其他值 | 否 | **100.00** | `RETRY` | 请求准备不可用，应重试或排查 |

`NOTFOUND` 和 `ERROR` 没有进入 `CALL`，因此不会执行任何抵扣，`AMOUNT-DUE` 保持调用前由票面应缴复制来的 100.00；这正是框架所说 `REVIEW`、`RETRY` 保留原始应缴金额的含义。[ev_page_910812aeae1af4d74b843f3b] [fw:173f3cfde39f36c3:13-16] `ERROR` 没有被单独列为 `WHEN`，而是落入 `WHEN OTHER`，所以与所有其他未识别状态一样得到 `RETRY`。[fw:173f3cfde39f36c3:11-11]

READY 下的抵扣按固定顺序执行：先账户余额，再退回款，最后奖励。[ev_page_b760eb210649d164667926a9] 在题设 100.00、20.00、10.00、5.00，且三项使用标志都为 `"Y"` 的情况下：

1. 账户余额 20.00 不超过剩余应缴 100.00，抵扣 20.00，剩余应缴变为 80.00。[ev_page_beb95fe5f9f3a384e596b729]
2. 退回款 10.00 不超过剩余应缴 80.00，抵扣 10.00，剩余应缴变为 70.00。[ev_page_296db080a597e6d3de43a86b]
3. 奖励 5.00 不超过剩余应缴 70.00，抵扣 5.00，剩余应缴变为 **65.00**。[ev_page_8f70c84c57b17da6f6de9599]

每一步都取“可用抵扣金额”和“当前剩余应缴”中的较小者再相减，所以即使某项抵扣大于剩余应缴，也只会把应缴减到零，不会减成负数。[ev_page_beb95fe5f9f3a384e596b729] [ev_page_296db080a597e6d3de43a86b] [ev_page_8f70c84c57b17da6f6de9599]

**READY 不意味着每项抵扣都会执行。** 它只允许程序进入抵扣资格评估；[fw:173f3cfde39f36c3:9-9] 每一项还要分别满足三个条件：对应使用标志为 `"Y"`、对应金额大于零、当时的 `AMOUNT-DUE` 仍大于零。任一项不满足，该步就跳过。因此若账户余额已把应缴抵到零，后面的退回款和奖励即使标志为 `"Y"` 也不会再执行；某个标志不是 `"Y"` 或金额为零时，也同样跳过相应步骤。

这笔处理的业务结果只是共用的 `AMOUNT-DUE` 被改写为剩余应缴；账户余额、退回款、奖励金额本身没有被扣减或转账，程序也不保存台账。调用方在 `CALL` 正常返回后设置的 `CALCULATED`，只说明“计算出了剩余金额”，不证明发生了现金收取或账务落账。[ev_page_910812aeae1af4d74b843f3b] [ev_page_b760eb210649d164667926a9] [fw:173f3cfde39f36c3:13-16]

还需限定一个边界：上述 `CALCULATED` 以 `CALL` 正常返回为前提。调用方源码没有 `ON EXCEPTION`，`OFFSET-RULES` 也没有返回码；若 `CALL` 本身发生异常，源码没有给出后续赋值或异常处理结果，不能把正常返回路径的 65.00 和 `CALCULATED` 直接扩展到该异常情形。[ev_page_910812aeae1af4d74b843f3b] [ev_page_b760eb210649d164667926a9]
