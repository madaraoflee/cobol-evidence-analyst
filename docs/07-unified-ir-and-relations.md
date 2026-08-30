# 统一 IR 与关系 Schema 设计

> 核心判断：统一 IR 必须表达“源码明确存在什么、静态分析可以确定什么、仍然无法解析什么”，并为每项事实保留可回到原始源码的证据。它不是解析器 AST 的复制品，也不是 Neo4j 的图模型，更不是给 LLM 使用的代码摘要。

状态：`Target architecture proposal — deferred behind POC`
当前轻量实现见：[可演示 POC](./09-demonstrable-poc.md)  
版本：`v0.1`  
日期：2026-08-28  
上游输入：[问题分类与调查策略矩阵](./06-question-investigation-matrix.md)  
下游消费者：解析器适配器、Neo4j 投影、Qdrant 投影、调查工具、Evidence Package Builder、评测系统

## 1. 这份设计解决什么

不同 COBOL 解析器会产生不同 AST，Neo4j 和 Qdrant 又需要不同的数据形态。如果让 Agent 直接依赖其中任何一种格式，解析器或存储一更换，上层调查逻辑就必须重写。

统一 IR 位于这些工具之上，规定产品认可的代码事实语言：

```text
原始源码与构建上下文
        ↓
解析器 / 静态分析 / 企业方言规则
        ↓
统一 IR：实体、关系、表达式、源码映射、边界和生成依据
        ↓
Neo4j / Qdrant / 调查工具 / Evidence Package
```

本设计只纳入第一版五类问题真正需要的语义，不试图重建完整 COBOL 编译器。后续新增影响分析、领域发现或 SDLC 能力时，通过 Schema 版本演进扩展。

## 2. 设计原则

### 2.1 原始源码是最终权威

IR 中每个可用于回答的实体和关系都必须绑定源码快照与证据位置。解析器输出、静态推导、图投影和向量索引与源码冲突时，源码胜出，当前派生数据失效。

### 2.2 事实模型独立于工具

IR 不使用 CodeGraph、ProLeap、Tree-sitter、Neo4j 或 LangGraph 的内部对象作为公共契约。解析器通过 Adapter 输出 IR；Neo4j 与 Qdrant 从 IR 构建可删除、可重建的查询投影。

### 2.3 不确定性必须成为数据

动态 `CALL`、多义字段、缺失 COPYBOOK、未支持 SQL 或外部程序不能被静默丢弃。系统必须保留候选目标、解析尝试、失败原因和调查边界。

### 2.4 关系状态不等于运行时必然性

`confirmed` 表示源码引用及关系端点已经由可重放规则确定，不表示该语句在每次业务运行中都会执行。是否进入某条路径由控制条件、入口和运行时数据共同决定，必须由单独的关系限定字段表达。

### 2.5 只保存直接事实，不保存无界传递闭包

IR 保存单次调用、单步赋值、直接控制依赖和直接包含关系。跨十个程序的调用路径或字段 lineage 在调查时计算，并作为 Evidence Path 放入证据包。这样避免派生边爆炸，也避免源码变化后大量传递关系失效。

### 2.6 COPY 展开必须上下文化

同一个 COPYBOOK 可以被多个程序、甚至同一程序多次包含，并使用不同 `REPLACING`。因此 `Field`、`Statement` 等实体属于具体程序和 COPY occurrence 的分析上下文；它们保留 COPYBOOK 原始定义位置，但不能把同名 COPY 字段合并成一个全局节点。

### 2.7 LLM 不产生代码事实

LLM 可以生成调查计划、查询词和业务解释，但不能创建 `CALLS`、`FLOWS_TO`、`CONTROL_DEPENDS_ON` 等代码关系。模型推断只能进入独立业务语义层，并经过人工确认或证据核验。

## 3. IR 逻辑组成

一次完整 IR Bundle 包含九类记录：

| 集合 | 作用 |
| --- | --- |
| `manifest` | 描述仓库快照、Schema、解析器、方言配置和整体完整性 |
| `source_files` | 保存源文件身份、编码、内容 Hash 和可引用范围 |
| `source_maps` | 连接标准化文本、COPY 展开文本和原始源码位置 |
| `source_evidence` | 保存可进行 Hash 校验的原始源码锚点 |
| `entities` | Program、Paragraph、Statement、Field、Table 等可寻址对象 |
| `expressions` | 保存计算和条件的运算结构、顺序与字段引用 |
| `relations` | 保存直接结构、调用、控制、数据、I/O 和语义关系 |
| `provenance` | 保存每项事实的生产者、规则版本和上游依据 |
| `diagnostics` | 保存未解析引用、缺失依赖、解析覆盖和冲突 |

M2 解析器 Bake-off 推荐使用带 JSON Schema 校验的 JSONL 作为交换格式：便于不同解析器输出同一契约、逐条比较和版本管理。它只是验证期的序列化方式，不是长期数据库选型。

## 4. 快照与 Bundle Manifest

所有记录必须属于一个不可变源码快照。`snapshot_id` 代表“这些文件及其构建上下文的确定组合”，不等同于 Git commit；IBM i 成员导出、非 Git 源码和外部 COPY 依赖也必须纳入快照 Hash。

```json
{
  "schema_version": "1.0.0",
  "repository_id": "hk-insurance-core",
  "snapshot_id": "snap-2026-08-28-a81f...",
  "snapshot_content_hash": "sha256:...",
  "source_language": "COBOL",
  "source_format": "IBM_FIXED",
  "encoding_profile": "company-approved-profile-v1",
  "dialect_profiles": [
    {"profile_id": "ibm-cobol-base", "version": "1.0"},
    {"profile_id": "dxc-smart-cobol-private", "version": "pending"}
  ],
  "producer_runs": [
    {
      "producer_type": "parser_adapter",
      "producer_name": "parser-candidate-a",
      "producer_version": "x.y.z",
      "configuration_hash": "sha256:..."
    }
  ],
  "capabilities": {
    "program_structure": "COMPLETE",
    "copy_expansion": "PARTIAL",
    "call_resolution": "PARTIAL",
    "control_flow": "PARTIAL",
    "field_access": "COMPLETE",
    "data_flow": "PARTIAL",
    "sql_mapping": "UNSUPPORTED",
    "file_mapping": "PARTIAL"
  },
  "bundle_status": "PARTIAL",
  "diagnostic_ids": ["diag-001"]
}
```

`capabilities` 使用 `COMPLETE`、`PARTIAL`、`UNSUPPORTED`，描述本次构建实际覆盖，不使用模糊置信分。只要某项关键能力是 `PARTIAL` 或 `UNSUPPORTED`，相关问题的 Evidence Builder 就必须携带边界信息。

`bundle_status` 只有三种：

| 状态 | 语义 | 是否允许回答 |
| --- | --- | --- |
| `COMPLETE` | 纳入范围内的文件、依赖和必需投影均完成且一致 | 可以 |
| `PARTIAL` | 快照有效，但存在已记录的语义缺口 | 只允许在缺口不影响当前证据义务时回答 |
| `INVALID` | Hash、Schema、索引版本或引用完整性冲突 | 禁止回答，必须重建或修复 |

## 5. 标识与版本规则

### 5.1 三种标识不能混用

| 字段 | 作用 | 稳定范围 |
| --- | --- | --- |
| `entity_id` | 系统内部引用实体的不可变 ID | 当前快照 |
| `canonical_key` | 可检查的语义身份，例如程序 + 字段层级路径 | 在名称和作用域不变时跨快照稳定 |
| `content_hash` | 验证当前源码内容没有漂移 | 当前内容 |

`entity_id` 对消费者必须视为 opaque string，不能靠拆分字符串获得程序名或路径。实现可以通过 `snapshot_id + entity_type + canonical_key` 的规范化 Hash 生成，但具体算法由后续 Schema 实现规范固定。

示意：

```text
canonical_key(program)   = program:POLPRM01
canonical_key(paragraph) = program:POLPRM01/paragraph:CALC-PREMIUM
canonical_key(field)     = program:POLPRM01/data:WORKING-STORAGE/
                           WS-PREMIUM-AREA/WS-NET-PREMIUM/copy-occurrence:03
```

Statement 没有稳定业务名称，其 `canonical_key` 使用所属 Program、Paragraph、语句类型、规范化 AST 指纹和同指纹 occurrence 序号。跨快照语句对应关系只能作为独立 `lineage_status` 的候选映射，不能冒充同一个事实。

### 5.2 关系标识

`relation_id` 在当前快照内唯一，由关系类型、源实体、目标实体、关键限定字段及推导规则共同决定。同一对实体可以拥有不同类型或不同调用位置的多条边。

### 5.3 Schema 演进

- 新增可选字段或新关系类型：minor version；
- 修改关系方向、状态含义或必填字段：major version；
- 修正文档且不改变数据语义：patch version；
- Projection Builder 必须声明能够读取的 Schema 版本范围；
- 旧 IR 不在读取时偷偷升级，必须通过可审计 migration 生成新 Bundle。

## 6. 通用记录结构

### 6.1 EntityRecord

```json
{
  "entity_id": "ent-...",
  "snapshot_id": "snap-...",
  "entity_type": "Field",
  "canonical_key": "program:POLPRM01/data:WORKING-STORAGE/WS-NET-PREMIUM",
  "display_name": "WS-NET-PREMIUM",
  "qualified_name": "POLPRM01.WORKING-STORAGE.WS-NET-PREMIUM",
  "language": "COBOL",
  "owning_program_id": "ent-program-polprm01",
  "owning_scope_id": "ent-data-ws",
  "attributes": {},
  "source_refs": ["ev-src-001"],
  "provenance": ["prov-parser-001"],
  "parse_status": "COMPLETE"
}
```

代码实体的通用必填字段为 `entity_id`、`snapshot_id`、`entity_type`、`canonical_key`、`source_refs`、`provenance` 和 `parse_status`。`parse_status` 使用 `COMPLETE`、`PARTIAL`、`UNSUPPORTED`；后两种必须引用 Diagnostic。Boundary 是唯一允许没有自身源码位置的核心实体：未解析符号和外部调用必须引用触发它的源码位置；`RESTRICTED_DOCUMENT` 或 `BUSINESS_RATIONALE` 可以没有直接源码位置，但必须保存 `trigger_entity_id` 或 `question_id`。业务语义 overlay 使用独立记录契约。

### 6.2 RelationRecord

```json
{
  "relation_id": "rel-...",
  "snapshot_id": "snap-...",
  "relation_type": "FLOWS_TO",
  "source_entity_id": "ent-field-base-premium",
  "target_entity_id": "ent-field-net-premium",
  "relation_status": "confirmed",
  "qualifiers": {
    "via_statement_id": "ent-stmt-compute-001",
    "transfer_kind": "computed_dependency",
    "condition_ids": []
  },
  "evidence_refs": ["ev-src-compute-001"],
  "provenance": ["prov-dataflow-rule-004"]
}
```

通用必填字段：`relation_id`、`snapshot_id`、`relation_type`、两个端点、`relation_status`、`evidence_refs` 和 `provenance`。

所有关系都有明确方向。对逻辑上对称的存储别名关系，也采用 canonical endpoint order 保存一次，并由查询层提供双向遍历；不得写入两条看似独立的重复事实。

### 6.3 ProvenanceRecord

```json
{
  "provenance_id": "prov-dataflow-rule-004",
  "producer_type": "static_rule",
  "producer_name": "single-step-value-flow",
  "producer_version": "1.0.0",
  "rule_id": "DF-MOVE-COMPUTE-001",
  "configuration_hash": "sha256:...",
  "input_entity_ids": ["ent-stmt-compute-001"],
  "input_relation_ids": ["rel-read-001", "rel-write-001"],
  "input_evidence_refs": ["ev-src-compute-001"]
}
```

允许的 `producer_type`：

- `parser`：语法结构直接输出；
- `static_rule`：确定性控制流或数据流规则；
- `dialect_rule`：公司内版本化方言规则；
- `manual_confirmation`：专家确认的业务语义映射；
- `migration`：Schema 迁移产生的等价记录。

LLM 推断不得使用这些生产者类型生成代码事实。若后续保存模型提出的业务解释，必须进入独立 `BusinessInference` 存储，并保持 `unconfirmed` 状态。

## 7. 源码证据与 Source Map

### 7.1 SourceFile

`SourceFile` 至少保存：

| 字段 | 说明 |
| --- | --- |
| `repository_relative_path` | 仓库内规范路径，不保存开发机绝对路径作为业务事实 |
| `member_name` / `library_name` | IBM i 导出时可选的原成员身份 |
| `source_kind` | COBOL、COPYBOOK、CL、JCL、DDS、SQL include、configuration |
| `encoding` | 实际读取编码 |
| `source_format` | fixed、free 或 mixed |
| `content_hash` | 原始字节内容 Hash |
| `line_count` | 完整性检查辅助字段 |

### 7.2 SourceEvidence

```json
{
  "evidence_id": "ev-src-compute-001",
  "snapshot_id": "snap-...",
  "source_file_id": "ent-file-polprm01",
  "span": {
    "start_line": 420,
    "start_column": 12,
    "end_line": 422,
    "end_column": 58,
    "start_byte": 33821,
    "end_byte": 34014
  },
  "raw_content_hash": "sha256:...",
  "normalized_content_hash": "sha256:...",
  "origin_kind": "AUTHORED"
}
```

行号用于人类阅读，字节范围和内容 Hash 用于完整性验证。固定格式 COBOL 必须保存原始列号；不能只保存预处理后的逻辑行号。

### 7.3 COPY 与预处理映射

每次 `COPY` 都建立独立 `CopyOccurrence`，至少保存 importing program、COPY 位置、目标 Copybook、`REPLACING` 参数和展开结果 Hash。

一个展开实体的 `source_refs` 可以同时包含：

1. 原程序中的 COPY 指令位置；
2. COPYBOOK 中的原始定义位置；
3. 标准化或替换后的分析位置。

`SourceMapSegment` 记录这些位置之间的映射。若某段生成文本无法映射回原始来源，该实体不得作为 `confirmed` 代码事实，并产生 `SOURCE_MAP_GAP` diagnostic。

## 8. 实体模型

### 8.1 源码与结构实体

| Entity Type | 关键属性 | 说明 |
| --- | --- | --- |
| `SourceFile` | path、kind、encoding、format、hash | 原始物理源码单位 |
| `Copybook` | name、source_file_id、library/qualifier | 可被 COPY 的语义源码单位 |
| `CopyOccurrence` | importer、copy_name、replacing、resolution | 一次具体 COPY 使用，不能按名称合并 |
| `Program` | program_id、nesting、source_file_id | COBOL Program 单元；嵌套程序有父 Program |
| `EntryPoint` | name、entry_kind、owning_program_id、parameter_order | 主 PROGRAM-ID 或 ENTRY 调用入口 |
| `Section` | name、ordinal、owning_program_id | Procedure/Data Division 中有语义的 Section |
| `Paragraph` | name、ordinal、owning_program_id | 所有精确查询必须带 Program 作用域 |
| `Statement` | statement_kind、ordinal、expression_ids | 可执行语句或重要声明语句 |
| `Condition` | condition_kind、expression_id、owning_statement_id | IF、EVALUATE 或异常路径产生的运行时分支谓词 |

`Statement.statement_kind` 是可扩展枚举。MVP 至少覆盖 `MOVE`、`COMPUTE`、`ADD`、`SUBTRACT`、`MULTIPLY`、`DIVIDE`、`SET`、`INITIALIZE`、`IF`、`EVALUATE`、`PERFORM`、`CALL`、`GO_TO`、`READ`、`WRITE`、`REWRITE`、`START`、`EXEC_SQL_SELECT`、`EXEC_SQL_UPDATE`、`STRING`、`UNSTRING`、`ACCEPT`、`ENTRY`、`GOBACK`、`EXIT_PROGRAM`、`STOP_RUN`、`CONTINUE` 和 `NEXT_SENTENCE`。不支持的语句仍生成 `Statement`，其 `parse_status=PARTIAL` 并附 Diagnostic。

### 8.2 数据实体

| Entity Type | 关键属性 | 说明 |
| --- | --- | --- |
| `Field` | name、level、qualified path、storage area、PIC、USAGE、scale、signed、occurs | 程序与 COPY occurrence 上下文化的数据项实例 |
| `ConditionName` | name、values/ranges、subject_field_id | COBOL 88 级条件名 |
| `RecordLayout` | name、storage area、record role | FD/SD、LINKAGE 或业务记录结构 |
| `DatabaseTable` | qualified name、access technology | SQL 表或经方言规则识别的数据对象 |
| `DatabaseColumn` | name、table_id、data type | 用于 SQL host variable 数据来源追踪 |
| `File` | logical name、assignment、organization、access mode | COBOL 文件及其外部绑定 |
| `ControlTable` | name、resolution_basis | 经版本化企业规则确认的表驱动配置来源 |

Field 必须保存 COBOL 数值语义所需的信息，包括 `PIC`、小数位、符号、`USAGE`、存储长度和可能的截断边界；Expression 同时保存中间运算顺序、接收字段转换、`ROUNDED` 与 `ON SIZE ERROR`。缺少其中任何关键语义时，系统可以证明表达式结构，但不能承诺精确的运行时数值结果。

### 8.3 运行与边界实体

| Entity Type | 关键属性 | 说明 |
| --- | --- | --- |
| `Job` | name、scheduler kind、entry program | 已纳入快照的批处理入口 |
| `Boundary` | boundary_kind、reference_text、reason_code、required_material | 当前静态分析不能跨越的明确边界 |

`Boundary.boundary_kind` 首版包含：

- `UNRESOLVED_SYMBOL`：有源码引用，但无法唯一绑定；
- `MISSING_SOURCE`：已知程序或 COPYBOOK 不在当前快照；
- `EXTERNAL_PROGRAM`：调用目标在分析范围外；
- `RUNTIME_CONFIGURATION`：结果依赖运行参数、环境值或实际控制表内容；
- `GENERATED_CODE`：执行逻辑来自未提供的生成产物；
- `RESTRICTED_DOCUMENT`：业务含义需要当前系统无权访问的文档；
- `BUSINESS_RATIONALE`：源码能证明做法，不能证明为什么采用该规则。

Boundary 是有效调查结果，不是异常垃圾。它允许 Agent 准确回答“代码到这里为止，继续需要什么”。

### 8.4 业务语义覆盖层

`BusinessConcept` 和 `BusinessRule` 可以作为独立 overlay 实体，但不得与代码实体混为一层：

- 代码实体与业务概念通过 `MAPS_TO_CONCEPT` 连接；
- 代码证据与业务规则通过 `EVIDENCES` 连接；
- 映射来源必须是公司内词典、专家确认或明确的规则；
- 模型提出但未经确认的概念映射只能是 `candidate`，不能证明代码行为；
- 删除整个业务语义层后，代码事实与调查能力仍应成立。

## 9. 表达式模型

只保存 `READS/WRITES` 无法回答“具体怎样计算”，因为它会丢失运算顺序、常量、函数和舍入方式。因此每个计算或条件语句必须保存规范化 Expression Tree。

```json
{
  "expression_id": "expr-net-premium-001",
  "snapshot_id": "snap-...",
  "owning_statement_id": "ent-stmt-compute-001",
  "expression_kind": "BINARY_OPERATION",
  "operator": "SUBTRACT",
  "operands": [
    {
      "position": 1,
      "kind": "FIELD_REFERENCE",
      "entity_id": "ent-field-base-premium",
      "source_ref": "ev-op-base"
    },
    {
      "position": 2,
      "kind": "FIELD_REFERENCE",
      "entity_id": "ent-field-discount",
      "source_ref": "ev-op-discount"
    }
  ],
  "result_target_ids": ["ent-field-net-premium"],
  "numeric_semantics": {
    "rounded": true,
    "rounding_location": "RESULT",
    "on_size_error_condition_id": null
  },
  "source_refs": ["ev-src-compute-001"],
  "parse_status": "COMPLETE"
}
```

`Expression` 采用有序树而不是自然语言公式。首版节点类型包括字段引用、字面量、一元运算、二元运算、比较、逻辑组合、范围、函数调用和未知表达式。每个字段引用必须解析到 `Field`，否则引用 `Boundary` 并将表达式标为 `PARTIAL`。

Expression 是规范 IR 对象，但默认不把每个运算节点都投影成 Neo4j 节点。Neo4j 保存 Statement、Field、`READS/WRITES/FLOWS_TO` 和必要的表达式摘要；需要回答公式时，从 IR 读取完整 Expression Tree。这样兼顾精确表达和图规模。

## 10. 关系模型

### 10.1 关系状态

| `relation_status` | 含义 | 能否单独满足事实义务 |
| --- | --- | --- |
| `confirmed` | 当前快照中，关系及目标由确定性、可重放规则唯一解析 | 可以，但仍受路径条件限制 |
| `candidate` | 源码证明存在引用，但目标只能缩小到有限候选 | 不可以；只能支持候选路径或边界说明 |
| `unresolved` | 关系意图存在，但目标或语义无法恢复 | 不可以；必须连接 Boundary 或 Diagnostic |

禁止把相关性分数、模型概率或解析器自报置信度转换成 `confirmed`。

### 10.2 结构与定义关系

| Relation | 方向 | 精确定义 | 必要限定 |
| --- | --- | --- | --- |
| `CONTAINS` | container → child | 直接结构包含；不保存传递闭包 | ordinal、scope role |
| `DEFINES` | Program/Copybook/RecordLayout → Field/ConditionName | 声明来源，不表示运行时值来源 | declaration role |
| `DECLARES_ENTRY` | Program → EntryPoint | 程序声明一个主入口或 ENTRY 入口 | entry kind、ordinal |
| `ACCEPTS_PARAMETER` | EntryPoint → Field | 入口按位次接收 PROCEDURE DIVISION/ENTRY USING 参数 | ordinal、passing mode |
| `INCLUDES_COPY` | Program/SourceFile → CopyOccurrence | 当前上下文出现一次 COPY 指令 | inclusion site |
| `RESOLVES_TO` | CopyOccurrence → Copybook | COPY 名称解析到具体源码单位 | library/path basis |
| `EXPANDS_COPY` | CopyOccurrence → contextual entity | 展开产生当前程序上下文中的实体 | replacing hash、source map id |
| `REDEFINES` | redefining Field → original Field | 声明级的存储重解释关系 | offset/length when known |
| `ALIASES_STORAGE_WITH` | canonical Field → canonical Field | 由布局计算出的实际存储重叠；逻辑上对称，只保存一条边 | overlap offset/length、derivation rule |
| `RENAMES` | level-66 Field → renamed Field/range | COBOL RENAMES 声明的别名范围 | range end when present |
| `OCCURS_DEPENDING_ON` | table Field → controlling Field | OCCURS 数量依赖控制字段 | min/max when known |
| `CONDITION_FOR` | ConditionName → Field | 88 级名称定义目标字段的值集合 | values/ranges |
| `RECORD_OF` | RecordLayout → File | FD/SD 记录布局属于哪个 COBOL File | record role |

### 10.3 执行与调用关系

| Relation | 方向 | 精确定义 | 必要限定 |
| --- | --- | --- | --- |
| `CALLS` | CALL Statement → Program/EntryPoint | 调用目标及入口在当前快照中唯一解析 | call form、entry point、condition ids |
| `POSSIBLY_CALLS` | CALL Statement → Program/EntryPoint | 动态调用的一个有限候选 | resolution basis、candidate set id |
| `PERFORMS` | PERFORM Statement → Paragraph/Section | 直接 PERFORM 目标 | inline/out-of-line、condition ids |
| `PERFORMS_THRU` | PERFORM Statement → start Paragraph | 范围 PERFORM | `range_end_entity_id` |
| `GOES_TO` | GO TO Statement → Paragraph | 静态或候选转移目标 | depending-on selector when present |
| `BRANCHES_TO` | Condition → Statement/Paragraph | 条件的直接分支入口 | true/false/when/other/exception |
| `CONTROL_FLOWS_TO` | Statement → Statement | 过程内直接 CFG 后继 | normal/branch/exception/return |
| `EXECUTED_BY` | Program → Job | 已知作业或调度入口执行该程序 | step/order/condition |

这些关系证明“源码中存在静态可达或条件可达路径”。回答“生产上一定执行”仍需要入口、控制条件和运行时数据。`CALLS` 的 `confirmed` 只表示目标绑定确定，不表示调用语句无条件执行。

### 10.4 数据访问与值流关系

| Relation | 方向 | 精确定义 | 必要限定 |
| --- | --- | --- | --- |
| `READS` | Statement/Condition → value entity | 语句或条件直接读取目标值 | access role、expression id |
| `WRITES` | Statement → value entity | 语句直接定义或修改目标值 | write kind、expression id |
| `FLOWS_TO` | source value → target value | 一次语句或参数边界上的直接值依赖 | via statement、transfer kind、conditions |
| `PASSES_AS` | caller Field → callee Field | CALL 参数位置上的跨程序值/引用映射 | call statement、entry point、ordinal、passing mode、direction |
| `SELECTS_FROM` | SQL Statement → DatabaseTable | SELECT 的表级读取 | alias、query block |
| `UPDATES` | SQL Statement → DatabaseTable | INSERT/UPDATE/DELETE 的表级写入 | operation |
| `READS_DB_COLUMN` | SQL Statement → DatabaseColumn | SQL 直接读取列 | expression/alias |
| `WRITES_DB_COLUMN` | SQL Statement → DatabaseColumn | SQL 直接修改列 | source expression |
| `READS_FILE` | I/O Statement → File | COBOL 文件读取 | record id、status field |
| `WRITES_FILE` | I/O Statement → File | COBOL 文件写入或改写 | record id、status field |
| `LOOKS_UP` | Statement → DatabaseTable/ControlTable/File | 经 SQL 或确认方言规则执行查表 | lookup keys、effective-date semantics |

`FLOWS_TO` 只保存单步值传递。例如 `MOVE A TO B` 生成 `A → B`；`COMPUTE C = A + B` 生成 `A → C` 和 `B → C`，完整公式仍由 Expression 保存。多跳 `A → B → C` 在调查时计算，不作为第三条 canonical edge 写回 IR。`PASSES_AS` 必须与目标入口的 `ACCEPTS_PARAMETER` 位次一致；BY REFERENCE 参数的 `direction` 使用 `IN`、`OUT`、`INOUT` 或 `UNKNOWN`，不能在缺少读写分析时猜测。数组下标和 reference modification 必须记录在 `READS/WRITES` qualifiers 中，否则对应访问只能标记为部分解析。

SQL `SELECT COL-X INTO :WS-X` 至少生成：

```text
SQL Statement --SELECTS_FROM--> Table
SQL Statement --READS_DB_COLUMN--> COL-X
COL-X --FLOWS_TO via SQL Statement--> WS-X
SQL Statement --WRITES--> WS-X
```

### 10.5 控制依赖关系

| Relation | 方向 | 精确定义 | 必要限定 |
| --- | --- | --- | --- |
| `CONTROL_DEPENDS_ON` | affected Statement → Condition | 目标语句能否执行直接受该条件控制 | branch outcome、derivation rule |

`CONTROL_DEPENDS_ON` 由 CFG 和支配/后支配分析产生，必须保留推导依据。文本上位于某个 `IF` 下方但无法证明控制依赖时，不得生成 confirmed relation。

### 10.6 边界与业务语义关系

| Relation | 方向 | 精确定义 |
| --- | --- | --- |
| `UNRESOLVED_REFERENCE` | source entity → Boundary | 源码出现了应解析的符号或目标，但当前能力无法绑定 |
| `EXTERNAL_DEPENDENCY` | source entity → Boundary | 调查路径离开当前快照、进入运行配置或外部系统 |
| `MAPS_TO_CONCEPT` | code entity → BusinessConcept | 代码对象与业务术语的受控映射 |
| `EVIDENCES` | code entity → BusinessRule | 代码事实为已记录业务规则提供证据 |

`MAPS_TO_CONCEPT` 和 `EVIDENCES` 不允许替代调用、数据流或控制关系。业务词映射可以帮助检索入口，但不能证明程序行为。

## 11. 关系限定与路径语义

每条可能受路径影响的关系至少支持以下 qualifiers：

| 字段 | 语义 |
| --- | --- |
| `condition_ids` | 该关系成立或语句执行所依赖的直接条件 |
| `path_semantics` | `unconditional`、`conditional`、`exceptional` 或 `unknown` |
| `branch_outcome` | true、false、WHEN 值、OTHER、AT END、INVALID KEY 等 |
| `via_statement_id` | 产生数据流或调用映射的直接语句 |
| `derivation_depth` | canonical direct relation 固定为 1；大于 1 只允许出现在调查结果，不写回 IR |
| `resolution_basis` | literal、symbol table、parameter position、dialect rule、finite candidate analysis 等 |

运行时条件与关系解析状态是两个维度。例如动态调用目标有三个候选时，每条 `POSSIBLY_CALLS` 是 `candidate`；即使调用语句位于无条件主路径，也不能把候选目标升级为 confirmed。

## 12. Diagnostic 与冲突处理

### 12.1 DiagnosticRecord

```json
{
  "diagnostic_id": "diag-dynamic-call-001",
  "snapshot_id": "snap-...",
  "severity": "WARNING",
  "diagnostic_code": "DYNAMIC_CALL_NOT_UNIQUE",
  "source_entity_id": "ent-call-stmt-009",
  "reference_text": "WS-TARGET-PGM",
  "candidate_entity_ids": ["ent-pgm-a", "ent-pgm-b"],
  "affected_capabilities": ["call_resolution", "execution_path", "data_flow"],
  "boundary_entity_id": "ent-boundary-009",
  "required_material": "运行时路由表或 DXC 动态调用规则",
  "source_refs": ["ev-call-009"]
}
```

首版至少定义以下 diagnostic codes：

- `PARSE_RECOVERY_USED`
- `UNSUPPORTED_STATEMENT`
- `COPYBOOK_NOT_FOUND`
- `COPY_REPLACING_PARTIAL`
- `SOURCE_MAP_GAP`
- `AMBIGUOUS_FIELD_REFERENCE`
- `DYNAMIC_CALL_NOT_UNIQUE`
- `EXTERNAL_PROGRAM_MISSING`
- `SQL_BODY_UNPARSED`
- `FILE_BINDING_UNKNOWN`
- `DATA_FLOW_INCOMPLETE`
- `PRODUCER_CONFLICT`
- `PROJECTION_VERSION_MISMATCH`

### 12.2 多解析器结果合并

解析器 Bake-off 期间不得采用“最后写入覆盖”：

- 多个 producer 产生完全相同的事实时，保留一条事实和多个 provenance；
- 一个 producer 解析成功、另一个明确不支持时，事实可以 confirmed，但 Manifest 必须反映各自能力；
- 多个 producer 对目标或结构产生冲突时，生成 `PRODUCER_CONFLICT`，保留候选，不自动选择“多数票”；
- 企业方言规则可以解析通用解析器无法处理的关系，但必须有版本、测试用例和源码依据；
- 人工确认只允许升级业务语义映射，不能凭口头判断创造不存在的代码边。

## 13. 示例：保费计算如何进入 IR

假设存在以下原创合成代码：

```cobol
       CALC-PREMIUM.
           COMPUTE WS-NET-PREMIUM ROUNDED =
               WS-BASE-PREMIUM - WS-DISCOUNT
           IF WS-NET-PREMIUM < ZERO
               MOVE ZERO TO WS-NET-PREMIUM
           END-IF.
```

关键实体：

```text
Paragraph: CALC-PREMIUM
Statement: COMPUTE
Statement: IF
Condition: WS-NET-PREMIUM < ZERO
Statement: MOVE
Field: WS-BASE-PREMIUM
Field: WS-DISCOUNT
Field: WS-NET-PREMIUM
```

关键直接关系：

```text
CALC-PREMIUM --CONTAINS--> COMPUTE
COMPUTE --READS--> WS-BASE-PREMIUM
COMPUTE --READS--> WS-DISCOUNT
COMPUTE --WRITES--> WS-NET-PREMIUM
WS-BASE-PREMIUM --FLOWS_TO via COMPUTE--> WS-NET-PREMIUM
WS-DISCOUNT --FLOWS_TO via COMPUTE--> WS-NET-PREMIUM
IF-CONDITION --READS--> WS-NET-PREMIUM
MOVE --WRITES--> WS-NET-PREMIUM
MOVE --CONTROL_DEPENDS_ON--> IF-CONDITION [branch=true]
```

Expression Tree 保存 `BASE - DISCOUNT` 的运算顺序以及 `ROUNDED`。第二次 `MOVE ZERO` 是条件覆盖写入，所以 Agent 回答时不能只给主公式，还必须说明“结果小于零时被覆盖为零”。这正是 Statement、Expression、`WRITES` 和 `CONTROL_DEPENDS_ON` 必须同时存在的原因。

## 14. 五类问题对 Schema 的可追踪性

| 问题类型 | 必需 IR 对象 | 满足义务的关键条件 |
| --- | --- | --- |
| `CALCULATION_EXPLANATION` | Field、Statement、Expression、READS、WRITES、FLOWS_TO、CONTROL_DEPENDS_ON、LOOKS_UP | 找到所有相关写入，恢复每条分支的表达式和输入来源 |
| `DATA_PROVENANCE` | Field、CopyOccurrence、DEFINES、EXPANDS_COPY、REDEFINES、ALIASES_STORAGE_WITH、ACCEPTS_PARAMETER、FLOWS_TO、PASSES_AS、SQL/File 关系 | 定义 lineage 与值 lineage 分开，并追到边界 |
| `CONDITION_IMPACT` | Condition、Expression、BRANCHES_TO、CONTROL_DEPENDS_ON、READS、WRITES | 能证明条件控制目标动作，覆盖默认和异常分支 |
| `EXECUTION_PATH` | Program、EntryPoint、Paragraph、Statement、CALLS、POSSIBLY_CALLS、PERFORMS、GOES_TO、CFG、SCC 信息 | 路径有序，候选边和循环明确，不把可达写成必然 |
| `ANSWERABILITY_BOUNDARY` | Boundary、Diagnostic、Manifest capability、UNRESOLVED_REFERENCE、EXTERNAL_DEPENDENCY | 明确停止点、受影响义务和继续所需资料 |

若任何一种问题无法仅凭本表中的对象表达其金标准证据，说明 IR 仍不完整；应先修改 Schema，再进入工具开发。

## 15. Neo4j 投影规则

Neo4j 是 IR 的查询投影，不定义事实语义。第一版投影遵守以下规则：

- 每个可寻址 Entity 投影为节点，唯一约束为 `(snapshot_id, entity_id)`；
- 每条 Relation 投影为同名有向边，并保留 `relation_id`、status、qualifiers、evidence refs 和 provenance refs；
- Statement、Field、Program、EntryPoint、Paragraph、Condition 和 Boundary 是在线调查的主节点；
- 完整 Expression Tree 默认保留在 IR，只在图节点上保存 `expression_id`、operator 摘要和可检索文本；
- 不投影任意传递闭包，不把多跳 lineage 写成 canonical edge；
- `candidate` 与 `unresolved` 边必须可查询且默认不能被 “confirmed-only” 路径混入；
- 图查询必须带 `snapshot_id`，禁止跨快照遍历；
- Projection Manifest 记录输入 Bundle Hash、Schema 版本、投影版本和完成状态。

## 16. Qdrant 投影规则

Qdrant 的最小检索单位不是任意字符切块，而是有实体锚点的 `RetrievalUnit`：

```json
{
  "retrieval_unit_id": "ru-...",
  "snapshot_id": "snap-...",
  "entity_id": "ent-paragraph-calc-premium",
  "representation_kind": "paragraph_context",
  "deterministic_text": "...",
  "payload": {
    "entity_type": "Paragraph",
    "program_id": "ent-program-polprm01",
    "source_file_id": "ent-file-polprm01",
    "canonical_key": "program:POLPRM01/paragraph:CALC-PREMIUM",
    "content_hash": "sha256:...",
    "ir_schema_version": "1.0.0"
  }
}
```

确定性检索文本可以组合完整标识符、COBOL 连字符拆词、受控缩写展开、注释、语句、SQL/File 引用和实体完整路径。必须保留原始 token 与展开依据。

禁止：

- 没有 `entity_id` 的游离代码 Chunk；
- 把 LLM 自动摘要作为第一版基础索引语料；
- 把向量相似度写入 IR 的 `relation_status`；
- 从 Qdrant 候选直接生成代码事实；
- 在同一个 collection 中静默混合不同快照或 Embedding 版本。

## 17. 完整性约束

IR Bundle 在发布为可查询快照前必须通过以下硬校验：

1. 所有 Entity、Relation、Expression、Evidence 和 Diagnostic 使用同一 `snapshot_id`；
2. 每个 relation endpoint 必须存在，或显式指向 Boundary；
3. 每个 `confirmed` relation 至少拥有一个可验证 Evidence 和一个可重放 Provenance；
4. 每个 Field 引用在其 Program、数据层级和 COPY occurrence 作用域内唯一，或被标成多义；
5. 每个 `FLOWS_TO` 都有 `via_statement_id`，且对应语句包含相容的 READS/WRITES 或参数映射依据；
6. 每个 `PASSES_AS` 的 entry point、ordinal 和 callee Field 与 `ACCEPTS_PARAMETER` 一致，或显式标成 candidate/unresolved；
7. 每个 `CONTROL_DEPENDS_ON` 都有 CFG 推导依据和 branch outcome；
8. 每个展开实体都能映射到 COPY occurrence 和原始 Copybook 位置；
9. 每个 Expression 字段引用都解析到 Field 或 Boundary；
10. `candidate` 关系保存有限候选集合与解析依据；`unresolved` 保存 reason code；
11. 不存在跨快照关系，不存在把传递闭包伪装成 direct relation 的记录；
12. SourceEvidence 的字节范围与内容 Hash 可以从只读源码快照重新验证；
13. Neo4j 和 Qdrant Projection Manifest 的 Bundle Hash 与当前 IR 完全一致；
14. Bundle 为 `INVALID` 时，在线调查接口必须 fail closed；
15. 任何由 LLM 产生且未经人工确认的内容都不在代码事实集合中。

## 18. Parser Adapter 最小输出契约

每个解析器候选必须为同一源码快照输出：

- Bundle Manifest 与能力覆盖；
- Program、EntryPoint、Section、Paragraph、Statement、Condition、Field 和 Boundary；
- 源码证据与 COPY Source Map；
- 能够确认的直接关系；
- Expression Tree 或明确的表达式解析缺口；
- 所有恢复解析、跳过语句、歧义和缺失依赖的 Diagnostic；
- producer 名称、版本、配置 Hash 和规则版本。

Adapter 不得为了通过 Schema 校验而制造空目标、猜测字段或吞掉不支持的语句。无法输出某项能力时，正确行为是产生 `PARTIAL/UNSUPPORTED + Diagnostic + Boundary`。

## 19. M2/M3 验证方式

### 19.1 解析器 Bake-off

在同一批挑战程序上比较：

- 结构实体精确率与召回率；
- COPY 展开和原始位置映射完整率；
- 字段引用唯一绑定率；
- `PERFORM/CALL` 关系精确率与召回率；
- Expression 运算顺序、舍入和异常语义覆盖；
- 单步数据流与跨程序参数映射覆盖；
- SQL/File/动态调用的边界识别质量；
- 失败是否被完整报告，而不是静默遗漏。

### 19.2 Projection 一致性

从同一 Bundle 构建 Neo4j 和 Qdrant 后，随机与定向抽查：

- `snapshot_id + entity_id` 一致；
- 实体数、关系数和按类型统计可解释；
- 删除两个索引后可以从 IR 完全重建；
- 变更一个源文件后，只使该文件及受依赖影响的派生事实失效；
- 旧 Evidence 引用不能穿透到新快照。

## 20. 已决定与待验证事项

### 已决定

- 源码 → IR → 可重建投影 → Evidence Package → 回答的权威顺序；
- IR 独立于解析器、数据库和 Agent 框架；
- COPY 字段按程序和 occurrence 上下文化；
- Expression Tree 是计算与条件解释的必要对象；
- `relation_status` 与运行时路径语义分开；
- Boundary 与 Diagnostic 是一等对象；
- canonical IR 只保存直接关系，不保存任意传递闭包；
- LLM 不生成代码事实。

### 待私有样本验证

- DXC Smart COBOL 动态调用和表驱动路由需要哪些专有实体或 qualifiers；
- ProLeap、CodeGraph、Tree-sitter 各自能够直接提供哪些 IR 字段；
- 跨程序参数、REDEFINES、别名和 SQL host variable 的可恢复程度；
- IBM i 源成员、COPY 库顺序和构建配置如何形成完整 snapshot；
- 完整 Expression Tree 是否需要为特定高频计算投影到 Neo4j；
- 大型代码库中 Statement 与关系投影的容量和查询成本。

这些问题不会改变统一契约的职责，但可能扩展实体、relation qualifiers 和私有 Dialect Profile。

## 21. M0 退出检查

本 IR 设计进入 M0 评审时，必须能够回答五个问题：

1. 解析器如何表达成功、部分成功和失败？
2. 任意代码事实如何回到当前源码快照和原始位置？
3. 计算、字段来源、条件和执行路径分别依赖哪些直接关系？
4. 动态调用、缺失源码和运行时配置怎样阻止过度回答？
5. 更换解析器、Neo4j、Qdrant 或 LLM 时，哪些核心契约保持不变？

本版本已经给出上述问题的目标设计答案，并进一步形成 [Agent 调查工具契约](./08-agent-tool-contracts.md)。Evidence Package 留到 POC 证明需要完整事实层后继续；当前只实现 CodeUnit、Symbol、Relation 和 EvidenceSpan 四个对象。
