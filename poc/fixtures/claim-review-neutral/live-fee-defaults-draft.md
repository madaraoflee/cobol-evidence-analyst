**结论**

`FEE-RULE` 的手续费规则是：先按“本金 × 1.25 ÷ 100”计算，并四舍五入到两位小数；再执行最低收费 2、最高收费 15；最后如果状态精确为 `REVIEW`，则无条件覆盖为 0。因此，在非 `REVIEW` 状态下，本金 100、500、2000 分别收 **2.00、6.25、15.00**；本金 201.20 的原始结果为 2.515，舍入后为 **2.52**。若 `REQUEST-STATE = 'REVIEW'`，上述四笔最终都是 **0.00**，且 0 可以低于最低收费 2，因为 `REVIEW` 覆盖发生在最低、最高收费之后。[ev_page_eedf37efdaa217d174b754ef]

**执行顺序**

1. **调用前提**：`ENTRY-RULE` 只有在 `REQUEST-KIND = 'STANDARD'` 时才调用 `FEE-RULE`，并把 `BASE-AMOUNT`、`FINAL-FEE`、`REQUEST-STATE` 传入。若 `REQUEST-KIND` 不是 `STANDARD`，`FEE-RULE` 不会执行，`FINAL-FEE` 保持调用前的值。`ENTRY-RULE` 的默认值包括 `REQUEST-KIND = 'STANDARD'`、`REQUEST-STATE = 'NORMAL'`、`FINAL-FEE = 0`、`BASE-AMOUNT = 500`；下文中 100、2000、201.20 的本金，是按“调用前已将 `BASE-AMOUNT` 设为该值”的假设计算的。[ev_page_110efdf954c5348f26e847d6]

2. **基础公式**：`RATE` 是工作存储字段，初值为 1.25，程序内没有其他赋值，所以本次费率按 1.25% 计算。计算式是：

   `FINAL-FEE = ROUNDED(BASE-AMOUNT × RATE ÷ 100)`

   也就是本金乘以 1.25 再除以 100，等价于乘以 0.0125。`ROUNDED` 作用于把表达式结果赋给 `FINAL-FEE` 时，按 `FINAL-FEE` 的两位小数进行舍入。[ev_page_eedf37efdaa217d174b754ef]

3. **最低收费**：如果舍入后的 `FINAL-FEE < 2`，就将它改为 2。[ev_page_eedf37efdaa217d174b754ef]

4. **最高收费**：随后如果 `FINAL-FEE > 15`，就将它改为 15。最低和最高是两个独立的判断，按源码顺序执行。[ev_page_eedf37efdaa217d174b754ef]

5. **`REVIEW` 覆盖**：最后检查 `REQUEST-STATE` 是否精确等于 `'REVIEW'`；如果等于，直接将 `FINAL-FEE` 改为 0。该赋值位于最低、最高收费之后，之后没有再执行最低收费判断，所以最终 0 不会恢复为 2。[ev_page_eedf37efdaa217d174b754ef]

**逐笔结果**

下表“非 `REVIEW` 最终”列假设 `REQUEST-STATE` 不是 `REVIEW`，例如默认的 `NORMAL`；“`REVIEW` 最终”列则假设状态精确为 `REVIEW`。

| 本金 | 未舍入计算值 | `ROUNDED` 后 | 最低/最高调整 | 非 `REVIEW` 最终 | `REVIEW` 最终 |
|---:|---:|---:|---|---:|---:|
| 100.00 | 1.25 | 1.25 | 触发最低，改为 2.00 | **2.00** | **0.00** |
| 500.00 | 6.25 | 6.25 | 不触发 | **6.25** | **0.00** |
| 2000.