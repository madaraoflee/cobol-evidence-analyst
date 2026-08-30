# 项目 Skill 选型记录

> 核心判断：安装的 Skill 只服务于四件事——收敛产品楔子、维护领域语言和架构决策、建立代码理解方法、把评测与安全变成质量门。Skill 是工作方法，不是产品运行时依赖，也不能替代真实代码验证。

状态：`Accepted`  
日期：2026-08-27  
安装位置：`<project>/.agents/skills`

## 1. 选择方法

候选来自 GitHub 与公开 Skill 目录，按任务匹配度、维护活跃度、许可证、社区采用、指令质量和潜在风险筛选。优先选择范围清楚、内容可审阅、不会要求不必要外部操作的 Skill；安装版本固定到本次审阅的 Git commit。

## 2. 已安装 Skill

| Skill | 来源与审阅版本 | 在本项目中的职责 | 使用判断 |
| --- | --- | --- | --- |
| `ai-product-strategy` | [RefoundAI/lenny-skills](https://github.com/RefoundAI/lenny-skills) `13598cc54e09` | 选择首个高价值业务楔子、定义自主程度和长期护城河 | 用于产品判断，不把其中的经验观点当技术证据 |
| `writing-prds` | [RefoundAI/lenny-skills](https://github.com/RefoundAI/lenny-skills) `13598cc54e09` | 把问题、用户、成功标准和非目标写成可执行产品章程 | 当前只做轻量章程，不急于写完整 PRD |
| `domain-modeling` | [mattpocock/skills](https://github.com/mattpocock/skills) `6654f6b60cd9` | 维护 `CONTEXT.md` 和 ADR，防止概念在产品、图模型和回答中漂移 | 采用其上下文/决策方法，结合本项目事实—推断边界裁剪 |
| `architecture-designer` | [Jeffallan/claude-skills](https://github.com/Jeffallan/claude-skills) `882ef55e377d` | 架构驱动因素、组件边界、NFR、失败模式和 ADR | 通用模板只作检查表，技术选择仍由实测决定 |
| `spec-miner` | [Jeffallan/claude-skills](https://github.com/Jeffallan/claude-skills) `882ef55e377d` | 后续从 COBOL 行为逆向生成可追溯的 EARS 风格规格 | 当前项目尚无业务代码，留到 M3–M5 使用 |
| `codebase-comprehension-algorithms` | [pproenca/dot-skills](https://github.com/pproenca/dot-skills) `cf93c57cac89` | 提供多层图、SCC、共变更、词汇、Reflexion、稳定性和消融方法 | 这是有意保留的实验性 Skill；采用度较低，算法必须先在真实 COBOL 上验证 |
| `ai-evals` | [RefoundAI/lenny-skills](https://github.com/RefoundAI/lenny-skills) `13598cc54e09` | 建金标准、收集运行轨迹、做错误分类和分层评测 | 评测设计参考，不照搬与本项目无关的通用指标 |
| `security-threat-model` | [openai/skills](https://github.com/openai/skills) `49f948faa925` | 在源码边界、权限、外部模型和部署形态明确后做威胁建模 | 只在明确触发安全评审时使用；M0 不凭空编造部署威胁 |

本次审阅没有发现所选 Skill 需要执行未经确认的远程操作；主要内容是 Markdown 指令和参考资料。`spec-miner` 的本地文件还与固定 commit 的源文件做了哈希核对。后续升级仍应重新审阅差异，不能把“来自热门仓库”当作永久信任。

## 3. 这些 Skill 如何影响当前设计

- 产品 Skill 让第一版收缩为一个业务域中的“可信代码调查”，而不是通用代码助手。
- 领域与架构 Skill 促成了统一术语、三个 ADR、非功能要求和失败模式，而不是先决定技术栈。
- 代码理解 Skill 让事实层采用类型化多图：调用、控制、数据流、词汇和共变更不被混成一张无语义权重图；业务域发现还必须做 SCC、全局工具节点过滤、稳定性和消融。
- 评测 Skill 让解析、检索和回答分别设门，先观察错误分布，再决定优化解析器、检索还是模型。
- 安全 Skill 被安排在数据流和部署边界可以被具体描述之后，避免产出泛化威胁清单。

## 4. 暂不安装的候选

| 候选 | 暂缓原因 |
| --- | --- |
| `to-spec` | 假设已有 issue tracker 和交付流程，并倾向直接发布规格；当前要先证明产品与证据底座 |
| `improve-codebase-architecture` | 面向已有代码库重构；本项目还没有实现代码 |
| `technical-design-doc-creator` | 过重且规定性强，与当前轻量架构基线重复 |
| `domain-analysis` | 与已选领域建模能力重叠，且仓库许可证标识不够清楚 |
| `evaluate-rag` | 所在仓库已归档，且偏文本分块 RAG；本项目的核心是图引导证据路径 |

## 5. 使用规则

Skill 只对项目协作过程生效，不直接进入生产 Agent。升级 Skill 时记录来源 commit、审阅差异和用途；若 Skill 的方法与金标准实测冲突，以实测和 ADR 为准。项目级目录采用 `.agents/skills`，符合 Codex 对仓库内 Skill 的发现约定。
