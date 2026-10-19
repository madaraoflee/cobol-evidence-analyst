**结论**

只有当 `ENTRY-RULE` 执行到该判断时，`REQUEST-KIND` 的值等于 `'STANDARD'`，程序才会调用 `FEE-RULE`。调用时，程序按位置把三个工作存储字段 `BASE-AMOUNT`、`FINAL-FEE`、`REQUEST-STATE` 作为实参传入；`FEE-RULE` 通过 `FINAL-FEE` 这个按引用传递的参数把手续费写回调用方，程序没有使用 `RETURNING`，也没有返回码。如果 `REQUEST-KIND` 不等于 `'STANDARD'`，整个 IF 分支（包括 `CALL`）都不会执行，`FINAL-FEE` 在本程序内不会被重新赋值，因此保持进入判断前的值；首次初始化时为 **0.00**，重复激活时则可能保留上一轮的结果。[ev_page_110efdf954c5348f26e847d6] [ev_page_eedf37efdaa217d174b754ef]

## 一、调用条件

`ENTRY-RULE` 的调用点位于 `IF REQUEST-KIND = 'STANDARD'` 的 THEN 分支内。该分支内先连续执行多条 `CONTINUE`，这些语句只是占位，不改变控制流；最后才执行 `CALL 'FEE-RULE' USING ...`。[ev_page_110efdf954c5348f26e847d6]

因此，是否发起调用取决于两个层次：

1. **程序必须先执行到该 IF 判断。** 提供的源码中，这个 IF 前面没有其他提前返回或分支。
2. **`REQUEST-KIND` 必须等于 `'STANDARD'`。** 条件成立时，才执行 THEN 分支并到达 `CALL`；条件不成立时，IF 没有 ELSE，直接跳到 `END-IF` 后执行 `GOBACK`。

需要特别说明：按所提供的 `ENTRY-RULE` 源码，`REQUEST-KIND` 声明为 `PIC X(8) VALUE 'STANDARD'`，程序内没有其他给它赋值的语句，也没有 LINKAGE 参数从外部接收它。因此，在首次初始化并正常运行的前提下，该字段就是 `'STANDARD'`，程序会走调用分支。“不是 STANDARD”是条件不成立时的行为分支；在现有源码中，程序自身无法把该字段改成其他值，必须依赖未提供的运行环境或后续修改才能出现。[ev_page_110efdf954c5348f26e847d6]

## 二、三个参数怎样传入

调用语句为：

`CALL 'FEE-RULE' USING BASE-AMOUNT FINAL-FEE REQUEST-STATE`

源码没有写 `BY CONTENT` 或 `BY VALUE`，因此按 COBOL 的默认方式按引用传递。`FEE-RULE` 的入口声明为 `PROCEDURE DIVISION USING BASE-AMOUNT, FINAL-FEE, REQUEST-STATE`，两边的顺序和字段一一对应。[ev_page_110efdf954c5348f26e847d6] [ev_page_eedf37efdaa217d174b754ef]

| 位置 | `ENTRY-RULE` 实参 | `FEE-RULE` 入口字段 | 声明 | 在被调方的作用 |
|---|---|---|---|---|
| 第 1 个 | `BASE-AMOUNT` | `BASE-AMOUNT` | `PIC 9(7)V99` | 只读，作为计算本金 |
| 第 2 个 | `FINAL-FEE` | `FINAL-FEE` | `PIC 9(7)V99` | 先被计算赋值，再被最低/最高收费和 `REVIEW` 规则修改；最终值回写调用方 |
| 第 3 个 | `REQUEST-STATE` | `REQUEST-STATE` | `PIC X(6)` | 只读，判断是否等于 `'REVIEW'` |

费率 `RATE` 不是参数。它固定在 `FEE-RULE` 的工作存储中，声明为 `PIC 9V99 VALUE 1.25`，因此被调方使用的费率恒为 1.25%。[ev_page_eedf37efdaa217d174b754ef]

按 `ENTRY-RULE` 的声明初值，首次初始化并进入调用时，传入的值分别是：`BASE-AMOUNT = 500`、`FINAL-FEE = 0`、`REQUEST-STATE = 'NORMAL'`。这些是调用发出时的实参状态；`FINAL-FEE` 进入被调方后首先会被 `COMPUTE` 覆盖，因此传入的 0 不参与手续费计算。[ev_page_110efdf954c5348f26e847d6]

## 三、手续费怎样返回

`FEE-RULE` 收到参数后按以下顺序处理：[ev_page_eedf37efdaa217d174b754ef]

1. `FINAL-FEE ROUNDED = BASE-AMOUNT * RATE / 100`，即本金乘以 1.25%，按分四舍五入后写入 `FINAL-FEE`。
2. 如果 `FINAL-FEE < 2`，将 `FINAL-FEE` 置为 2。
3. 如果 `FINAL-FEE > 15`，将 `FINAL-FEE` 置为 15。
4. 如果 `REQUEST-STATE = 'REVIEW'`，将 `FINAL-FEE` 置为 0；该覆盖发生在最低/最高收费之后。
5. 执行 `GOBACK` 返回调用方。

手续费就是通过第 2 个参数 `FINAL-FEE` 按引用回写到 `ENTRY-RULE` 的工作存储字段。被调程序没有 `RETURNING` 子句，也没有单独返回码；调用方在 `END-CALL` 之后也没有检查返回状态或对 `FINAL-FEE` 再做赋值，而是直接执行 `GOBACK`。[ev_page_110efdf954c5348f26e847d6] [ev_page_eedf37efdaa217d174b754ef]

按首次初始化的默认输入推导：
- 本金 500、费率 1.25%，计算得 6.25，舍入后仍为 6.25；
- 6.25 不低于 2，也不高于 15，因此不调整；
- `REQUEST-STATE` 为 `'NORMAL'`，不等于 `'REVIEW'`，因此不覆盖；
- 最终回写 `ENTRY-RULE` 的 `FINAL-FEE` 为 **6.25**。

需要区分“回写到调用方字段”和“对外返回”：`ENTRY-RULE` 的 `FINAL-FEE` 是它自己的工作存储字段，而 `ENTRY-RULE` 的 `PROCEDURE DIVISION` 没有 LINKAGE 对外参数。因此这个 6.25 留在程序内部，不会因为接收了 `FEE-RULE` 的引用参数就自动成为对外输出；提供的源码中也没有后续使用它的语句。[ev_page_110efdf954c5348f26e847d6]

## 四、不是 STANDARD 时 `FINAL-FEE` 怎样变化

如果执行到判断时 `REQUEST-KIND` 不等于 `'STANDARD'`，则：[ev_page_110efdf954c5348f26e847d6]

- `IF` 条件不成立，THEN 分支内的所有 `CONTINUE`、`CALL 'FEE-RULE'` 和 `END-CALL` 都不会执行；
- 控制流直接到 `END-IF` 之后，随后 `GOBACK`；
- `ENTRY-RULE` 内没有任何其他给 `FINAL-FEE` 赋值的语句，因此该路径上 `FINAL-FEE` **不发生变化**，保持进入判断前的值。

这里要区分两种前提：

| 前提 | 跳过 CALL 时 `FINAL-FEE` 的值 |
|---|---|
| 程序首次初始化，或此前被 CANCEL 后重新初始化 | 取声明初值 **0.00**，因为 `VALUE 0` 在初始化时生效 |
| 重复激活且工作存储保留 | 保持上一次的当前值；如果上一轮调用 `FEE-RULE` 回写为 6.25，则本次跳过调用后仍是 **6.25**，不会自动恢复为 0.00 |

声明中的 `VALUE 0` 只说明初始化时的值，不代表每次进入程序或每次跳过调用都会重置。[ev_page_110efdf954c5348f26e847d6]

另外，`FEE-RULE` 本身没有参数校验或拒绝分支，也不写文件，因此不存在“传参后被业务校验拒绝”或“写入失败”的路径。`ENTRY-RULE` 的 `CALL` 也没有 `ON EXCEPTION`；这部分只表明源码没有处理调用异常，不改变上述正常调用与跳过分支的结论。[ev_page_110efdf954c5348f26e847d6] [ev_page_eedf37efdaa217d174b754ef]