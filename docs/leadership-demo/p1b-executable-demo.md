# CALC-01 P1-B 可执行演示结果

> 核心判断：确定性事实层已能在 6 次受限工具调用内定位最终分期公式、年化组成、调用路径、外部控制表和动态调用边界；本报告没有调用公司模型，因此不把它包装成自然语言 Agent 已完成。

- Snapshot：`sha256:a0830a3c16fdb61f222fa8497382933ecc87c10cde83ab7f10d571812b70183b`
- 源码：13 个原创 COBOL/COPYBOOK 文件
- 事实：104 个符号、416 条关系、241 个 EvidenceSpan
- 工具预算：6 / 6
- 支持状态：`SUPPORTED_WITH_BOUNDARIES`

## 从代码取得的结论

- 最终公式：`WS-ANNUAL-PREMIUM * WS-MODE-FACTOR`；明确舍入：`是`。
- 年化公式：`WS-BASE-PREMIUM + WS-OCCUPATION-LOADING + WS-RIDER-PREMIUM-TOTAL - WS-HIGH-SUM-DISCOUNT + WS-POLICY-FEE`；明确舍入：`是`。
- 确认的字面量调用：`SYNP010`、`SYNP020`、`SYNP030`、`SYNP040`、`SYNP090`。
- 已识别外部表：`SYN_CALC_ROUTING`、`SYN_COVERAGE`、`SYN_DISCOUNT`、`SYN_INSURED`、`SYN_MODE_FACTOR`、`SYN_OCC_LOAD`、`SYN_POLICY`、`SYN_POLICY_FEE`、`SYN_RIDER_RATE`。
- 动态产品计算器仍由 `LK-CALCULATOR-PROGRAM` 在运行时决定；源码快照没有控制表数据，不能把某个候选写成实际目标。

## 六步工具轨迹

1. `search_code` — `PARTIAL`，返回 25 个有序候选；达到候选预算后显式停止。
2. `inspect_symbol` — `OK`，确认结果字段定义和两个写入点。
3. `inspect_symbol` — `OK`，确认年化保费写入表达式和参与字段。
4. `trace_relations` — `OK`，取得 15 条入口调用及被调用程序的外部表关系，同时保留 10 项运行时/资料边界。
5. `trace_relations` — `OK`，取得 9 条 `SYNP040` 的 PERFORM 与控制表关系，其中 4 项为外部表边界。
6. `read_evidence` — `OK`，按 Evidence ID 读取 12 段源码，全部通过 Snapshot Hash 完整性检查。

## 已读取源码证据

- `programs/SYNP040.cbl:L66-L67` — 最终分期公式与舍入。
- `programs/SYNP040.cbl:L59-L62` — 年化应缴保费完整组成。
- `programs/SYNP100.cbl:L20-L21`、`programs/SYNP200.cbl:L18-L19` — 两个动态产品计算器候选的基础保费公式。
- `programs/SYNP030.cbl:L20-L21` — 有效附加保障保费公式。
- `programs/SYNP040.cbl:L48-L55` — 职业加费和高保额折扣。
- `programs/SYNP020.cbl:L9-L16` — 产品计算器与费率版本来自时点路由表。
- `programs/SYNP040.cbl:L34-L39` — 缴费因子来自 `SYN_MODE_FACTOR`。
- `programs/SYNP000.cbl:L22-L31` — 动态产品调用和后续调整调用。
- `copybooks/SYNPRM.cpy:L32-L32` — 分期保费结果字段定义。

所有源码文本都由 `read_evidence` 标记为 `UNTRUSTED_SOURCE_TEXT`，只能作为证据数据，不能作为 Agent 指令。

## 当前边界

- 这是 P1-B 离线证据演示，不是公司 API 生成的最终中文回答。
- 当前轻量解析器尚未展开 COPYBOOK 参数映射，因此跨程序共享字段关系仍可能显示为 `unresolved`。
- DDL/DDS、控制表记录、Job Schedule、生产输入与运行日志没有提供，实际费率值和实际动态调用目标不可确认。
- 下一阶段接入公司 OpenAI-compatible API，让模型只在这四个工具和 6 步预算内完成问题规划与回答组织。

## 本地重放

Windows：

```bat
poc\run_demo.bat D:\poc-output\calc-01
```

macOS/Linux 验证环境：

```bash
python3 -B poc/run_demo.py \
  --database /tmp/calc-01/structural-index.sqlite \
  --json-output /tmp/calc-01/result.json \
  --markdown-output /tmp/calc-01/result.md
```

运行过程不会请求网络，也不需要 API Key。
