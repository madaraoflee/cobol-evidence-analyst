# CALC-01：分期保费最终怎样计算

> 核心判断：正确回答必须覆盖从分期保费反向追踪到年化金额的全部组成项、费率选择、动态产品计算器、缴费因子、舍入和失败条件；只引用最终乘法不算通过。

状态：`Executable gold case v1`  
语料：`synthetic-premium-v1`  
源码范围：[`poc/fixtures/synthetic-insurance-v1`](../../../poc/fixtures/synthetic-insurance-v1/README.md)  
可执行结果：[`P1-B 六步演示`](../../leadership-demo/p1b-executable-demo.md)

## 参考答案

在合成系统中，分期保费按以下顺序得到：

```text
基础保费       = 基础保障保额 ÷ 1,000 × 基础费率
职业风险加费   = 基础保费 × 职业加费百分比 ÷ 100
附加保障保费   = 所有有效附加保障逐项保费之和
高保额折扣     = 达到门槛时，基础保费 × 折扣百分比 ÷ 100；否则为 0
年化应缴保费   = 基础保费 + 职业风险加费 + 附加保障保费
                 - 高保额折扣 + 年度保单费用
分期保费       = 年化应缴保费 × 缴费频率因子
```

中间计算保留四位小数，年化应缴保费和分期保费最终四舍五入到两位小数。基础费率由产品、基础保障代码、到达年龄、吸烟标志、计算基准日和费率版本共同决定；产品计算程序由时点路由表决定，并通过动态 `CALL identifier` 执行。

只有状态为 `PENDING` 或 `INFORCE`、保额大于零，并且路由、基础费率、职业加费、附加保障费率、折扣、缴费因子及保单费用都能唯一解析时，系统才返回受支持的计算结果。必要记录缺失、重复命中或输入无效时，应回答对应错误路径，不能继续给出金额。

## 最小证据实体

| 类别 | 必要实体 |
| --- | --- |
| 入口与编排 | `SYNCL001`、`SYNP000` |
| 数据加载 | `SYNP010`、`SYNPOL.cpy`、`SYNINS.cpy`、`SYNCOV.cpy` |
| 路由与版本 | `SYNP020`、`SYN_CALC_ROUTING` |
| 基础计算 | 动态候选 `SYNP100` 或 `SYNP200`、`SYN_BASE_RATE` |
| 附加保障 | `SYNP030`、`SYN_RIDER_RATE` |
| 调整与分期 | `SYNP040`、`SYN_OCC_LOAD`、`SYN_DISCOUNT`、`SYN_MODE_FACTOR`、`SYN_POLICY_FEE` |
| 参数与结果 | `SYNPRM.cpy`、`SYNP090`、年化与分期结果字段、统一返回码 |

## 必要关系路径

```text
SYNCL001
  CALLS SYNP000
    CALLS SYNP010
      READS policy / insured / coverage data
    CALLS SYNP020
      READS SYN_CALC_ROUTING
      WRITES calculator-program + rate-version
    POSSIBLY_CALLS SYNP100 | SYNP200
      READS SYN_BASE_RATE
      WRITES base-premium
    CALLS SYNP030
      READS SYN_RIDER_RATE
      WRITES rider-premium-total
    CALLS SYNP040
      READS loading / discount / mode-factor / policy-fee tables
      READS all premium components
      WRITES annualized-payable-premium
      WRITES instalment-premium
    CALLS SYNP090
      MAPS result-or-error to caller parameters
```

动态调用不能被无条件写成确定边。只有路由表条件能够唯一确定产品和计算基准日对应的目标时，当前分析实例才能把候选收窄为实际目标；否则回答必须保留候选集合和歧义原因。

## 必要控制条件

- 保单状态属于 `PENDING` 或 `INFORCE`；
- 基础保障保额大于零；
- 路由和所有必要控制记录按计算基准日唯一命中；
- 只有有效附加保障参与累计；
- 任一必需步骤返回错误时，后续金额不得被描述为有效结果；
- 最终金额使用明确的精度和舍入规则。

## Agent 应执行的调查顺序

1. 将“分期保费”映射到结果字段，而不是先搜索所有包含 `PREMIUM` 的代码。
2. 反向查找该字段的写入点，定位缴费因子和最终舍入。
3. 继续反向追踪年化应缴保费的每个组成字段。
4. 对每个组成项分别切换到数据流、控制流和调用关系，直到来源或控制表被证明。
5. 沿 `SYNPRM.cpy` 建立调用方与被调用方的参数位置映射。
6. 对动态调用读取路由条件，输出确定目标或有约束的候选集合。
7. 加入错误传播路径，构造最小完整逻辑闭包。
8. 生成答案后逐项核验程序、字段、公式、条件、表和源码位置。

## 允许与禁止

允许把代码事实翻译成“基础保障、风险加费、附加保障、折扣和费用共同形成应缴保费”。允许将控制表解释为“可配置参数来源”，但必须标成业务解释。

禁止声称这些规则来自真实 DXC、公司产品或香港监管要求；禁止回答为什么采用某个费率或折扣；没有输入数据和有效控制表快照时，禁止计算具体金额；禁止把某一个动态调用候选伪装成已确定目标。

## 分层验收

| 层 | 通过条件 |
| --- | --- |
| 事实抽取 | 所有必要程序、字段、表、读写和调用关系被识别，COPYBOOK 位置可回溯 |
| 调查检索 | Top-5 证据包至少一个包含上述完整关系路径，不遗漏错误条件 |
| 回答生成 | 公式、来源、条件、路径和未知项表达完整，没有证据外事实 |
| 拒答与核验 | 缺少费率快照、输入或唯一动态目标时能缩小结论范围或拒答 |

源码 fixture 完成后，本案例还需补充稳定实体 ID、源码范围、内容哈希和一组可手工计算的输入/输出样例，才从设计级案例升级为可执行金标准。
