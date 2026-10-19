**结论**

`ENTRY-RULE` 只有在 `REQUEST-KIND` **精确等于 `'STANDARD'`** 时，才会执行到 `CALL 'FEE-RULE'`；否则整个 `IF` 体被跳过，`FEE-RULE` 不会被调用，`FINAL-FEE` 也不会被这次调用改写。[ev_page_110efdf954c5348f26e847d6]

调用时，三个参数按位置传入 `FEE-RULE`：`BASE-AMOUNT`、`FINAL-FEE`、`REQUEST-STATE`。`FEE-RULE` 没有返回值语句，手续费是通过第二个参数 `FINAL-FEE` 按引用回写的；按当前声明初值 `BASE-AMOUNT=500`、`REQUEST-STATE='NORMAL'`、`REQUEST-KIND='STANDARD'`，正常返回后 `FINAL-FEE` 为 **6.25**。[ev_page_110efdf954c5348f26e847d6] [ev_page_eedf37efdaa217d174b754ef]

## 1. 调用条件

`ENTRY-RULE` 的过程部首先判断：

- `REQUEST-KIND = 'STANDARD'` 成立：进入 `IF` 体，执行其中的 `CONTINUE`，最后到达 `CALL 'FEE-RULE' USING BASE-AMOUNT FINAL-FEE REQUEST-STATE`。
- 不成立：跳过整个 `IF` 体，直接执行 `GOBACK`，不会发起调用。

`REQUEST-KIND` 定义为 `PIC X(8) VALUE 'STANDARD'`，而 `'STANDARD'` 正好是 8 个字符，所以这是精确的 8 位字符比较。像 `'EXPRESS'`、空格或其他内容都不会命中该分支。中间的多个 `CONTINUE` 没有业务效果，不改变条件或数据。[ev_page_110efdf954c5348f26e847d6]

## 2. 三个参数怎样传入、手续费怎样返回

| 位置 | `ENTRY-RULE` 传入的字段 | `FEE-RULE` 入口声明 | 传入时的作用 | 返回时的变化 |
|---:|---|---|---|---|
| 1 | `BASE-AMOUNT` | `BASE-AMOUNT PIC 9(7)V99` | 作为计算本金 | `FEE-RULE` 只读，未修改 |
| 2 | `FINAL-FEE` | `FINAL-FEE PIC 9(7)V99` | 接收手续费结果 | `FEE-RULE` 计算并覆盖，按引用回写 |
| 3 | `REQUEST-STATE` | `REQUEST-STATE PIC X(6)` | 用于最后判断是否 `'REVIEW'` | `FEE-RULE` 只读，未修改 |

`FEE-RULE` 的入口是 `PROCEDURE DIVISION USING BASE-AMOUNT FINAL-FEE REQUEST-STATE`，没有声明 `BY CONTENT` 或 `BY VALUE`，因此按 COBOL 的默认方式按引用传递。`FEE-RULE` 对 `FINAL-FEE` 的 `COMPUTE` 和 `MOVE` 会直接作用到 `ENTRY-RULE` 的 `FINAL-FEE` 上。[ev_page_eedf37efdaa217d174b754ef]

调用方没有使用 `RETURNING` 接收返回码；手续费不是通过函数返回值返回，而是通过第二个参数 `FINAL-FEE` 传出。调用返回后，`ENTRY-RULE` 也没有再对 `FINAL-FEE` 做其他赋值，所以正常返回时看到的就是 `FEE-RULE` 最后写入的值。[ev_page_110efdf954c5348f26e847d6]

## 3. 按当前初值的执行结果

按首次激活或已按声明初始化处理，`ENTRY-RULE` 的初值为：

- `BASE-AMOUNT = 500`
- `FINAL-FEE = 0`
- `REQUEST-STATE = 'NORMAL'`
- `REQUEST-KIND = 'STANDARD'`

执行顺序如下：

1. `REQUEST-KIND = 'STANDARD'` 成立，进入 `IF` 体并执行到 `CALL`。
2. 调用 `FEE-RULE`，传入 `BASE-AMOUNT=500`、`FINAL-FEE=0`、`REQUEST-STATE='NORMAL'`。
3. `FEE-RULE` 用固定在工作存储中的 `RATE=1.25` 计算：  
   `500 × 1.25 ÷ 100 = 6.25`，经 `ROUNDED` 保留两位小数后仍为 6.25。
4. 6.25 不低于 2，所以最低收费分支不触发。
5. 6.25 不高于 15，所以最高收费分支不触发。
6. `REQUEST-STATE` 是 `'NORMAL'`，不是 `'REVIEW'`，所以最后清零分支不触发。
7. `FEE-RULE` 执行 `GOBACK` 返回，`ENTRY-RULE` 的 `FINAL-FEE` 变为 **6.25**。

上述计算和覆盖顺序均在 `FEE-RULE` 原文中按顺序出现。[ev_page_eedf37efdaa217d174b754ef] [ev_page_110efdf954c5348f26e847d6]

如果 `REQUEST-STATE` 在调用时是 `'REVIEW'`，虽然仍会先计算并经过最低/最高收费判断，但最后会被 `MOVE 0 TO FINAL-FEE` 覆盖，返回给调用方的手续费为 **0**。[ev_page_eedf37efdaa217d174b754ef]

## 4. 不是 `STANDARD` 时，`FINAL-FEE` 怎样变化

`REQUEST-KIND` 不是 `'STANDARD'` 时：

- `IF REQUEST-KIND = 'STANDARD'` 条件不成立；
- `IF` 体内的所有 `CONTINUE` 和 `CALL 'FEE-RULE'` **都不执行**；
- 程序直接执行 `GOBACK`；
- `FEE-RULE` 不会运行，因此不会对 `FINAL-FEE` 做计算、最低收费、最高收费或 REVIEW 清零；
- `FINAL-FEE` **保持调用前的值不变**。[ev_page_110efdf954c5348f26e847d6]

需要注意“保持不变”不是“每次都重置为 0”。`FINAL-FEE` 的 `VALUE 0` 只在首次初始化或程序被初始化时生效；如果同一个程序实例之前已经调用过 `FEE-RULE`，工作存储通常保留之前的结果，那么非 `STANDARD` 路径下它仍是上一次留下的值。`ENTRY-RULE` 原文中没有其他对 `FINAL-FEE` 的赋值，因此不能把这条路径概括成“手续费清零”。[ev_page_110efdf954c5348f26e847d6]

## 5. 异常与边界说明

`ENTRY-RULE` 的 `CALL` 没有 `ON EXCEPTION` 分支，也没有检查调用返回状态。正常返回时，按引用传递的 `FINAL-FEE` 会带回 `FEE-RULE` 的结果；如果调用发生异常，源程序没有显式处理路径，不能仅凭这段代码断定 `FINAL-FEE` 一定被更新为 6.25。`FEE-RULE` 自身也没有参数校验或拒绝分支，只要被调用进入，就按上述计算顺序处理。[ev_page_110efdf954c5348f26e847d6] [ev_page_eedf37efdaa217d174b754ef]