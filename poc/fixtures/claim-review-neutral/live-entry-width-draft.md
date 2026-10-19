**结论**

`ENTRY-RULE` 只在 `REQUEST-KIND` 精确等于 `'STANDARD'` 时调用 `FEE-RULE`；这正是第 10 行所写 `IF REQUEST-KIND = 'STANDARD'` 的条件。[ev_page_110efdf954c5348f26e847d6] 条件成立时，程序按 `BASE-AMOUNT`、`FINAL-FEE`、`REQUEST-STATE` 的顺序把这三个字段作为实参传入，被调用程序处理后将手续费写回调用方的 `FINAL-FEE`。这里没有返回值，也没有返回码；唯一的回传渠道就是这个引用参数。条件不成立时，整个 `IF` 体（包括 CALL）都会被跳过，`FINAL-FEE` 不发生任何写入，保持调用前已有的值；首次激活时该值为声明初值 0.00。[ev_page_110efdf954c5348f26e847d6] [ev_page_eedf37efdaa217d174b754ef]

**一、发起调用的条件**

`ENTRY-RULE` 是入口主程序，没有外部参数入口；四个工作存储字段的初值决定了默认走向：

| 字段 | 定义 | 初值 |
|---|---|---|
| `BASE-AMOUNT` | `PIC 9(7)V99` | 500 |
| `FINAL-FEE` | `PIC 9(7)V99` | 0 |
| `REQUEST-STATE` | `PIC X(6)` | `'NORMAL'` |
| `REQUEST-KIND` | `PIC X(8)` | `'STANDARD'` |

判断发生在过程部开头：`REQUEST-KIND` 为 8 位定长字符，与字面量 `'STANDARD'`（正好 8 字符）整串精确比较，成立才进入 `IF` 体。[ev_page_110efdf954c5348f26e847d6]

- **成立**：先顺序执行 300 余条 `CONTINUE`，这些语句不读写任何业务字段、不改变控制流，只是空操作；随后到达第 311 行的 `CALL 'FEE-RULE' USING BASE-AMOUNT FINAL-FEE REQUEST-STATE`，第 312 行 `END-CALL` 结束调用语句，之后 `GOBACK`。[ev_page_110efdf954c5348f26e847d6]
- **不成立**：直接跳到 `END-IF` 之后执行 `GOBACK`，`FEE-RULE` 完全不被进入。[ev_page_110efdf954c5348f26e847d6]

由于 `REQUEST-KIND` 在程序内没有任何重新赋值，且声明初值为 `'STANDARD'`，**正常首次运行必然走调用路径**；只有工作存储被外部改变或程序被重复激活而保留旧值时，才可能出现“不是 `STANDARD`”的情况。`VALUE` 只决定首次初始化，不保证每次激活都重置。[ev_page_110efdf954c5348f26e847d6]

**二、三个参数怎样传入**

`CALL ... USING` 未写明 `BY REFERENCE`/`BY CONTENT`，按 COBOL 默认的按引用传递；被调方 `FEE-RULE` 的 `PROCEDURE DIVISION USING BASE-AMOUNT FINAL-FEE REQUEST-STATE` 也按位置接收同名参数。绑定依据是**位置顺序**，而非字段名巧合，PIC 声明两侧完全一致，因此不存在长度或小数位不匹配的问题。[ev_page_110efdf954c5348f26e847d6] [ev_page_eedf37efdaa217d174b754ef]

| 位置 | 调用方实参 | 被调方形参 | 被调方如何使用 | 方向 |
|---|---|---|---|---|
| 1 | `BASE-AMOUNT` `9(7)V99` | `BASE-AMOUNT` | 只参与计算式，不被赋值 | 只读输入 |
| 2 | `FINAL-FEE` `9(7)V99` | `FINAL-FEE` | 先被计算式结果覆盖，再可能被改为 2、15 或 0 | 输入/输出，结果写回调用方 |
| 3 | `REQUEST-STATE` `X(6)` | `REQUEST-STATE` | 只与 `'REVIEW'` 比较，不被赋值 | 只读输入 |

需要特别说明的是：调用方把 `FINAL-FEE` 当前值（首次为 0.00）也一并传入，但被调方第一步就执行 `COMPUTE FINAL-FEE ROUNDED = BASE-AMOUNT * RATE / 100` 把它整体覆盖，所以**传入的旧值对本次手续费毫无影响**，`FINAL-FEE` 实际是纯输出。[ev_page_eedf37efdaa217d174b754ef]

**三、手续费怎样返回**

`FEE-RULE` 没有 `RETURNING` 项，也没有返回码或状态参数，调用方也没有 `ON EXCEPTION`。所谓“返回”就是通过第 2 个引用参数把最终金额写回 `ENTRY-RULE` 的 `FINAL-FEE`：先按“本金 × 1.25 ÷ 100”并舍入到两位小数，再依次执行最低收费 2、最高收费 15、`REQUEST-STATE = 'REVIEW'` 时置 0 这三个判断，然后 `GOBACK`。[ev_page_eedf37efdaa217d174b754ef]

在默认输入下（本金 500、状态 `'NORMAL'`）：500 × 1.25 ÷ 100 = 6.25，不低于 2 也不超过 15，状态不是 `REVIEW`，因此回写 6.25；`END-CALL` 之后 `ENTRY-RULE` 直接 `GOBACK`，没有再读取、判断或再次输出 `FINAL-FEE`，所以 6.25 就是程序结束时的最终手续费。[ev_page_110efdf954c5348f26e847d6] [ev_page_eedf37efdaa217d174b754ef]

`REQUEST-STATE` 传入 `'NORMAL'`，不满足被调方的 `'REVIEW'` 条件，该覆盖分支不执行；但这属于**在传入非 `REVIEW` 值之后才判定不成立**，与调用方层面“根本不调用”是两回事。[ev_page_eedf37efdaa217d174b754ef]

**四、不是 `STANDARD` 时 `FINAL-FEE` 的变化**

答案是**不变化**：`ENTRY-RULE` 中对 `FINAL-FEE` 的唯一写入途径就是 `FEE-RULE` 通过引用回写，而这条路径在 `REQUEST-KIND ≠ 'STANDARD'` 时根本不会走到；调用方自身没有任何 `MOVE`、`COMPUTE` 或初始化语句去重置它。[ev_page_110efdf954c5348f26e847d6]

因此：

- **首次激活**：`FINAL-FEE` 保持声明初值 **0.00**。
- **重复激活且工作存储被保留**：保持上一次运行结束时的值（例如上次为 6.25 就仍是 6.25），不会因为本次跳过调用而回到 0。声明中的 `VALUE 0` 只证明首次初始化时的取值。
- `BASE-AMOUNT`、`REQUEST-STATE`、`REQUEST-KIND` 同样不会被改写，程序不产生任何手续费计算结果，直接 `GOBACK`。

**一个限定**

`CALL` 语句没有 `ON EXCEPTION`，程序也没有检查任何返回状态。[ev_page_110efdf954c5348f26e847d6] 如果 `FEE-RULE` 在实际运行中无法加载或调用异常，源码未声明异常处理分支，此时 `FINAL-FEE` 是否保持原值取决于运行环境，不能仅凭本程序正文断言其结果；上述“回写一定生效”的结论以调用正常完成为前提。[ev_page_eedf37efdaa217d174b754ef]