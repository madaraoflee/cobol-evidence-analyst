**结论**

在应缴 **100**、账户余额 **20**、退回款 **10**、奖励 **5**，且 `BALANCE-ALLOWED`、`REFUND-READY`、`REWARD-ELIGIBLE` 都为 `"Y"` 的前提下：

- `READ-STATUS = "READY"`：会调用抵扣程序，三项抵扣都满足条件并依次执行，最终 `AMOUNT-DUE = 65`，`COLLECTION-RESULT = "CALCULATED"`。
- `READ-STATUS = "NOTFOUND"`：不调用抵扣程序，应缴保持 **100**，`COLLECTION-RESULT = "REVIEW"`。
- `READ-STATUS = "ERROR"`：不调用抵扣程序，应缴保持 **100**，`COLLECTION-RESULT = "RETRY"`。

`READY` 只表示“可以进行抵扣计算”，**不表示每一项抵扣都会执行**。每一项还要分别检查自己的资格标志、金额是否为正，以及轮到该项时剩余应缴是否仍大于零。[ev_page_910812aeae1af4d74b843f3b] [fw:173f3cfde39f36c3:13-16]

---

## 状态与业务分支

`READ-STATUS` 是请求准备阶段传入的输入，`COLLECTION-ENTRY` 自己不负责查询该状态。[fw:173f3cfde39f36c3:13-16]

| `READ-STATUS` | 是否调用 `OFFSET-RULES` | 本题应缴结果 | `COLLECTION-RESULT` | 业务含义 |
|---|---:|---:|---|---|
| `READY` | 是 | **65** | `CALCULATED` | 执行抵扣计算，得到剩余应缴 |
| `NOTFOUND` | 否 | **100** | `REVIEW` | 必要取数未找到，转业务复核 |
| `ERROR` 或其他未列值 | 否 | **100** | `RETRY` | 请求准备不可用，应重试或排查 |

`NOTFOUND` 和 `ERROR` 都不会进入抵扣程序，因此 `AMOUNT-DUE` 不会被抵扣修改。`ERROR` 并没有单独列出，而是落入 `WHEN OTHER`，与所有非 `READY`、非 `NOTFOUND` 的值走同一分支。[ev_page_910812aeae1af4d74b843f3b] [fw:173f3cfde39f36c3:10-10] [fw:173f3cfde39f36c3:11-11]

---

## `READY` 时的抵扣顺序和金额变化

`COLLECTION-ENTRY` 先把 `INVOICE-AMOUNT` 复制到 `AMOUNT-DUE`，所以本题进入抵扣程序前应缴为 **100**。`READY` 分支通过 `CALL "OFFSET-RULES" USING BY REFERENCE COLLECTION-REQUEST` 把整个请求记录交给抵扣程序；正常返回后，调用方再把结果标为 `CALCULATED`。[ev_page_910812aeae1af4d74b843f3b]

`OFFSET-RULES` 按固定顺序执行三项抵扣：**账户余额 → 退回款 → 奖励**。[ev_page_b760eb210649d164667926a9]

| 顺序 | 抵扣项 | 本次抵扣前应缴 | 条件检查 | 本次计入抵扣 | 本次抵扣后应缴 |
|---:|---|---:|---|---:|---:|
| 1 | 账户余额 `20` | 100 | `BALANCE-ALLOWED = "Y"`，余额和应缴均大于零 | 20 | 80 |
| 2 | 退回款 `10` | 80 | `REFUND-READY = "Y"`，退款额和应缴均大于零 | 10 | 70 |
| 3 | 奖励 `5` | 70 | `REWARD-ELIGIBLE = "Y"`，奖励额和应缴均大于零 | 5 | 65 |

因此在 `READY` 且三项条件都满足时，最终结果是：

- `AMOUNT-DUE = 65`
- `COLLECTION-RESULT = "CALCULATED"`
- 三项抵扣都执行了

三步的抵扣逻辑分别见 [ev_page_beb95fe5f9f3a384e596b729]、[ev_page_296db080a597e6d3de43a86b]、[ev_page_8f70c84c57b17da6f6de9599]。

---

## 为什么 `READY` 不等于“每项都会抵扣”

`READY` 只是进入抵扣程序的入口条件。进入 `OFFSET-RULES` 后，每一项都有自己的门槛：

- 对应资格标志必须等于 `"Y"`；
- 对应金额必须大于零；
- 轮到该项时，当前 `AMOUNT-DUE` 必须仍大于零。

任何一项不满足，该抵扣项就被跳过。比如：

- 资格标志不是 `"Y"`，该项不执行；
- 抵扣金额为零，该项不执行；
- 前一项已经把应缴抵扣到零，后续项不再执行。

此外，如果某一项金额大于当前剩余应缴，程序只会抵扣当前剩余额，使 `AMOUNT-DUE` 变为零，后续项同样因“应缴不大于零”而跳过。[ev_page_beb95fe5f9f3a384e596b729] [ev_page_296db080a597e6d3de43a86b] [ev_page_8f70c84c57b17da6f6de9599]

还要注意：`CALCULATED` 是由调用方在抵扣程序正常返回后设置的，**不表示每一项抵扣都实际发生**。即使三项资格条件都不满足、应缴仍是 100，只要 `READY` 正常完成计算调用，调用方仍会把它标为 `CALCULATED`。这个结果表示“已经完成剩余应缴计算”，并不表示已经收款、改变账户余额或保存了台账。[ev_page_910812aeae1af4d74b843f3b] [fw:173f3cfde39f36c3:13-16]

---

## 调用和业务结果的边界

`COLLECTION-REQUEST` 通过 `BY REFERENCE` 传给 `OFFSET-RULES`，抵扣程序对 `AMOUNT-DUE` 的减法会反映到调用方的同一请求记录中；`CALCULATED` 则是调用方在调用返回后设置的，不是抵扣程序返回的业务码。[ev_page_910812aeae1af4d74b843f3b] [ev_page_b760eb210649d164667926a9]

抵扣程序的注释也明确说明：符合条件的抵扣只减少 `AMOUNT-DUE`，这是计算，不是资金转账；调用方同样说明它不收取现金、不改变账户余额、不保存台账。[ev_page_b760eb210649d164667926a9] [ev_page_910812aeae1af4d74b843f3b]

源码在 `READY` 分支中没有写 `ON EXCEPTION`，也没有检查调用返回码。因此这里能确认的是：**调用正常返回后**会把结果置为 `CALCULATED`；如果调用本身异常或没有正常返回，源码没有给出替代业务结果，不能把 `CALCULATED` 当作异常兜底。[ev_page_910812aeae1af4d74b843f3b]
