# 结论

`ENTRY-RULE` 只有在 `REQUEST-KIND = 'STANDARD'` 时才发出 `CALL 'FEE-RULE'`，并把三个工作存储字段 `BASE-AMOUNT`、`FINAL-FEE`、`REQUEST-STATE` 依次作为实参传入。`FEE-RULE` 计算后直接改写共享的 `FINAL-FEE`，该改写回写到调用方同名字段，这就是手续费的返回方式。**如果不是 STANDARD，整个 IF 体（包括 CALL）都不执行，没有 ELSE 分支，程序直接 `GOBACK`；`FINAL-FEE` 不会被赋值、不会被最低/最高收费调整、也不会被 REVIEW 置 0，保持调用前的值（首次运行即初值 0）。** [ev_page_110efdf954c5348f26e847d6][ev_page_eedf37efdaa217d174b754ef]

# 调用条件

`ENTRY-RULE` 的过程部只有一个业务判断：

- 当 `REQUEST-KIND = 'STANDARD'` 时，进入 IF 体，执行一串不产生业务效果的 `CONTINUE`，随后执行 `CALL 'FEE-RULE' USING BASE-AMOUNT FINAL-FEE REQUEST-STATE`，再结束 IF，最后 `GOBACK`。[ev_page_110efdf954c5348f26e847d6]
- 当 `REQUEST-KIND` 不等于 `'STANDARD'` 时，IF 条件为假，**IF 体内的所有 `CONTINUE` 和 `CALL` 全部跳过**，没有 `ELSE` 分支，直接到 `GOBACK`。[ev_page_110efdf954c5348f26e847d6]

`REQUEST-KIND` 定义为 `PIC X(8) VALUE 'STANDARD'`，源码中没有其他给它赋值的语句。因此按声明初值启动时，条件恒成立，默认一定会调用。只有运行环境在调用前另行把该字段改成其他值，才会走跳过路径；这条赋值不在已供源码中。[ev_page_110efdf954c5348f26e847d6]

需要特别区分：`ENTRY-RULE` **不检查 `REQUEST-STATE`**。是否调用只看 `REQUEST-KIND`；`REQUEST-STATE` 只作为第三个实参传入，由 `FEE-RULE` 内部决定是否把结果清零。

# 三个参数怎样传入

调用语句没有写 `BY CONTENT` 或 `BY VALUE`，按 COBOL 默认的按引用方式传递；`FEE-RULE` 的 `PROCEDURE DIVISION USING BASE-AMOUNT FINAL-FEE REQUEST-STATE` 按同样顺序接收，且两边字段描述一致，因此实参与被调方形参指向同一存储区域。[ev_page_110efdf954c5348f26e847d6][ev_page_eedf37efdaa217d174b754ef]

| 顺序 | 调用方字段（`ENTRY-RULE`） | 字段定义 | 调用时初值 | 被调方（`FEE-RULE`）用途 |
|---:|---|---|---|---|
| 1 | `BASE-AMOUNT` | `PIC 9(7)V99` | `500.00` | 只读，作为本金参与计算 |
| 2 | `FINAL-FEE` | `PIC 9(7)V99` | `0.00` | 输出，被调方直接写入计算结果 |
| 3 | `REQUEST-STATE` | `PIC X(6)` | `'NORMAL'` | 只读，判断是否等于 `'REVIEW'` |

`ENTRY-RULE` 在 CALL 之前没有对这三个字段做任何其他赋值，所以调用发出时的值就是各自声明的初值。[ev_page_110efdf954c5348f26e847d6]

# `FEE-RULE` 内部怎样产生手续费

进入 `FEE-RULE` 后按固定顺序执行：[ev_page_eedf37efdaa217d174b754ef]

1. **基础计算**：`RATE` 是 `FEE-RULE` 自己工作存储区的 `PIC 9V99 VALUE 1.25`，不从调用方传入。执行 `COMPUTE FINAL-FEE ROUNDED = BASE-AMOUNT * RATE / 100`，即本金 × 1.25 ÷ 100，并四舍五入到 `FINAL-FEE` 的两位小数。
2. **最低收费**：若结果小于 2，改为 2。
3. **最高收费**：随后若结果大于 15，改为 15。
4. **REVIEW 覆盖**：最后若 `REQUEST-STATE = 'REVIEW'`，把 `FINAL-FEE` 改为 0；此步在最低、最高收费之后，会覆盖前面所有结果。

按当前调用方初值 `BASE-AMOUNT = 500.00`、`REQUEST-STATE = 'NORMAL'` 推导：500 × 1.25 ÷ 100 = 6.25，舍入后仍为 6.25；不低于 2、也不高于 15，无需调整；状态不是 `'REVIEW'`，不清零。因此 `FINAL-FEE` 最终为 **6.25**。[ev_page_eedf37efdaa217d174b754ef]

# 手续费怎样返回

返回不是通过 `RETURNING` 数值完成的。`FEE-RULE` 没有 `RETURNING` 子句，它通过第二个形参 `FINAL-FEE` 直接写入调用方传入的同一存储区域；`ENTRY-RULE` 的 `FINAL-FEE` 因此被改写为 6.25。CALL 返回后，`ENTRY-RULE` 不再对 `FINAL-FEE` 做任何赋值，直接 `GOBACK`。[ev_page_110efdf954c5348f26e847d6][ev_page_eedf37efdaa217d174b754ef]

还要注意：`ENTRY-RULE` 的过程部没有 `USING` 参数区，也没有 `RETURNING`，所以 `FINAL-FEE` 只是本程序的工作存储字段，不是对上层调用者的对外输出参数；它的值留在 `ENTRY-RULE` 内，供其后续使用（当前源码中 `GOBACK` 前没有其他使用）。[ev_page_110efdf954c5348f26e847d6]

# 不是 STANDARD 时 `FINAL-FEE` 怎样变化

**什么也不发生。** 具体过程：

- IF 条件不成立，条件体整体不执行，因此既不传参，也不进入 `FEE-RULE`。
- `FEE-RULE` 中的基础公式、`ROUNDED` 舍入、最低 2、最高 15、REVIEW 置 0 **全部不会执行**——不是执行后得到别的金额，而是根本没有被调用。
- `BASE-AMOUNT` 和 `REQUEST-STATE` 也保持不变，不产生任何读写。
- `FINAL-FEE` 保持该次运行中此前的值。若这是程序首次运行、尚未发生任何改写，则为声明初值 **0.00**；若是同一运行单元内重复激活且工作存储未被重新初始化，则可能保留上一轮的值。源码没有在入口处重置 `FINAL-FEE`，因此不能把它说成“只要不调用就一定重新变成 0”。[ev_page_110efdf954c5348f26e847d6]

换句话说，非 STANDARD 路径下**不存在“未收取手续费所以为 0”的业务判断**，`FINAL-FEE` 的值只是没有被触碰；初值恰好是 0，但程序并没有为此做赋值。

# CALL 异常与未覆盖的分支

`CALL` 语句没有 `ON EXCEPTION` 子句，调用后也没有检查任何返回状态码。若 `FEE-RULE` 无法加载或调用异常，`ENTRY-RULE` 没有恢复或替代处理，异常由运行环境按 COBOL 规则处理；源码本身不能保证此时 `FINAL-FEE` 一定保持 0，也不能保证被调方未部分执行。[ev_page_110efdf954c5348f26e847d6]

另外，`ENTRY-RULE` 没有在调用前后把 `REQUEST-STATE` 设置为 `'REVIEW'`，其初值为 `'NORMAL'`。因此按现有源码初值，REVIEW 清零分支不会触发；只有在运行环境把 `REQUEST-STATE` 改成 `'REVIEW'` 且 `REQUEST-KIND` 仍为 `'STANDARD'` 时，`FEE-RULE` 才会被调用并把最终手续费覆盖为 0。[ev_page_110efdf954c5348f26e847d6][ev_page_eedf37efdaa217d174b754ef]