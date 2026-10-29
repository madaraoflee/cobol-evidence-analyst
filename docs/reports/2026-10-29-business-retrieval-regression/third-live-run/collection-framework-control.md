**结论：只有 `READ-STATUS` 精确为 `READY` 时，`COLLECTION-ENTRY` 才会调用 `OFFSET-RULES` 做抵扣。** 在题设“应缴 100、账户余额 20、退回款 10、奖励 5，且三项使用条件都满足”的前提下：

- `READY`：三项抵扣按“账户余额 → 退回款 → 奖励”的顺序全部执行，最终 `AMOUNT-DUE` 为 **65.00**，`COLLECTION-RESULT` 为 **CALCULATED**。
- `NOTFOUND`：不调用抵扣程序，`AMOUNT-DUE` 保持 **100.00**，业务结果设为 **REVIEW**。
- `ERROR`：不调用抵扣程序，`AMOUNT-DUE` 保持 **100.00**，业务结果设为 **RETRY**。代码里没有单独的 `WHEN "ERROR"`，`ERROR` 会落入 `WHEN OTHER`。

`READ-STATUS` 是外部已经准备好的输入，`COLLECTION-ENTRY` 自己不做查询；`READY` 只是允许进入抵扣判断，不保证每一项抵扣都会实际执行 [fw:173f3cfde39f36c3:13-16]。

## 状态与分支对照

| `READ-STATUS` | 是否调用 `OFFSET-RULES` | 题设下最终应缴 | `COLLECTION-RESULT` | 业务含义 |
|---|---:|---:|---|---|
| `READY` | 是 | 65.00 | `CALCULATED` | 已计算抵扣后的剩余应缴 |
| `NOTFOUND` | 否 | 100.00 | `REVIEW` | 必要数据未找到，安排业务复核 |
| `ERROR` 或其他非前两值 | 否 | 100.00 | `RETRY` | 请求准备不可用，重试或排查 |

`COLLECTION-ENTRY` 在进入分支前先把 `INVOICE-AMOUNT` 复制到 `AMOUNT-DUE`，所以不调用抵扣程序时，`AMOUNT-DUE` 就是原始应缴金额；`NOTFOUND` 和 `RETRY` 都不会修改它 [ev_page_910812aeae1af4d74b843f3b]。框架资料也说明，`REVIEW` 和 `RETRY` 因未调用抵扣过程而保留原始应缴金额 [fw:173f3cfde39f36c3:13-16]。

## `READY` 时的抵扣过程

`READY` 分支通过 `CALL "OFFSET-RULES" USING BY REFERENCE COLLECTION-REQUEST` 把整个请求记录按引用交给抵扣程序，因此抵扣程序对 `AMOUNT-DUE` 的修改会直接反映在调用方的同一请求记录中 [ev_page_910812aeae1af4d74b843f3b]。`OFFSET-RULES` 随后固定按以下顺序执行：

1. `APPLY-BALANCE`
2. `APPLY-REFUND`
3. `APPLY-REWARD`

这个顺序来自主过程连续的三个 `PERFORM` [ev_page_b760eb210649d164667926a9]。

按题设金额逐步计算：

1. **入口**：`AMOUNT-DUE = 100.00`。
2. **账户余额抵扣**：`BALANCE-ALLOWED = "Y"`，余额 20.00 > 0，且当前应缴 100.00 > 0；余额小于应缴，因此抵扣 20.00，`AMOUNT-DUE` 变为 **80.00** [ev_page_beb95fe5f9f3a384e596b729]。
3. **退回款抵扣**：`REFUND-READY = "Y"`，退回款 10.00 > 0，且当前应缴 80.00 > 0；退回款小于应缴，因此抵扣 10.00，`AMOUNT-DUE` 变为 **70.00** [ev_page_296db080a597e6d3de43a86b]。
4. **奖励抵扣**：`REWARD-ELIGIBLE = "Y"`，奖励 5.00 > 0，且当前应缴 70.00 > 0；奖励小于应缴，因此抵扣 5.00，`AMOUNT-DUE` 变为 **65.00** [ev_page_8f70c84c57b17da6f6de9599]。
5. **调用返回后**：`COLLECTION-ENTRY` 把 `COLLECTION-RESULT` 设为 **CALCULATED** [ev_page_910812aeae1af4d74b843f3b]。

因此题设场景下总抵扣额为 **35.00**，剩余应缴为 **65.00**。

## `READY` 不代表每项抵扣都会执行

`READY` 只表示可以进入抵扣资格判断，并不表示三项都自动满足 [fw:173f3cfde39f36c3:13-16]。每一项抵扣都有自己的门槛：

- 对应标志必须为 `"Y"`：`BALANCE-ALLOWED`、`REFUND-READY`、`REWARD-ELIGIBLE`；
- 对应金额必须大于零；
- 执行到该项时，当前 `AMOUNT-DUE` 仍必须大于零。

因此，如果某一项标志不是 `"Y"`，该项就被跳过；如果前一项抵扣已经把 `AMOUNT-DUE` 降到零，后面的抵扣即使标志为 `"Y"` 也不会再执行。某项金额大于当前应缴时，只会抵扣到当前应缴额，使剩余应缴变为零，后续项目同样会被跳过。题设中三项条件都满足，且每次抵扣后应缴仍大于零，所以三项才都会执行。

还需要注意：`OFFSET-RULES` 本身不再按 `READ-STATUS` 分支，它只按各项标志和金额条件处理；状态过滤发生在 `COLLECTION-ENTRY` 的 `EVALUATE` 中 [ev_page_910812aeae1af4d74b843f3b]。另外，`READY` 下即使三项抵扣条件都不成立，调用正常返回后调用方仍会设置 `CALCULATED`，所以 `CALCULATED` 表示“走了剩余应缴计算路径”，不表示一定发生了抵扣，也不表示已经实际收款或保存账本 [fw:173f3cfde39f36c3:13-16]。

## 业务结果的含义

- `CALCULATED`：已经计算出抵扣后的剩余金额；源码注释明确说明该程序不收取现金、不改变账户余额、不保存账本 [ev_page_910812aeae1af4d74b843f3b]。
- `REVIEW`：对应 `NOTFOUND`，表示必要收款数据未找到，应安排业务复核 [fw:173f3cfde39f36c3:10-10]。
- `RETRY`：对应 `ERROR` 或其他未识别状态，表示请求准备尚未形成可用请求，应重试或进一步排查 [fw:173f3cfde39f36c3:11-11]。

以上 `READY` 下的 65.00 是在 `CALL` 正常进入并返回 `OFFSET-RULES` 的前提下得出的；`COLLECTION-ENTRY` 在调用后直接设置 `CALCULATED`，源码中没有显式的调用异常处理或返回码检查。
