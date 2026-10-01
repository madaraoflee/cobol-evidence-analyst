# 默认业务回答恢复与安全诊断

基线：已发布 `d222e240e83aeb26eb2eb376b5ea89b75fb78531`。本批使用独立目录和修复分支，未写原 Mac 使用目录或切换其分支。最后保护检查发现原 COBOL 目录已有另一组未提交改动，HEAD 仍为 `92c0796`，保留未触碰；原瑄瑄目录干净，HEAD 仍为 `a398488`。本报告只包含公开代码审计、合成中性夹具和离线 fake transport 结果。未读取现场源码、手册、`.env`、私有索引或历史恢复数据，未调用外部模型、修改凭据或部署。

## 已确认的代码原因

1. 首轮自动补读和综合提示受“详细”等少数词控制。现在默认充分回答相关业务结论、步骤、条件、例外和依据，保留 `answer_detail=brief` 及明确简短问句偏好，不要求用户写特定关键词，不要求输出无关固定栏目。
2. “计算”调查要求算术候选。已有 IF/MOVE 等完整过程仍会显示缺公式。现在无相关算术候选时，在现有路径、候选和读取预算内检查过程步骤；明确字段按当前路径内的实际读写索引匹配，同程序的无关字段算式不能满足目标字段问题。不把赋值或条件冒充算术公式，未知输入、外部实现和动态目标仍保留缺口。
3. `finish_reason=length` 保留了正文，但停止原因可能显示材料充分。现在明确部分回答；还有调用预算时最多续答一次，原文及引用保留，续答再截断、拒绝或失败不循环。发送巨大初稿时只向续答提供有界尾段，完整首答仍保留。
4. 已接受的正常正文存在 60,000 字符本地裁剪。已移除解析和最终正文的这层裁剪。上游请求/响应硬上限、历史和证据预算保持原值；超过硬上限仍按错误或投影限制明确处理。
5. 三个 CLI 未传输出 flag 时仍显式传默认值，遮蔽环境设置。已修正；API 辅助视图按实际解析 choice 核对，正常长正文及会话持久化保留尾部。页面投影超过显示上限时单独标明，不归因于模型输出截断。

这些是代码级可复现原因，不能据此证明公司现场每个失败具有相同原因，也不能用 fake 的业务措辞证明模型回答质量。

## 实际默认配置

| 入口 | 未配置时输出 token | 覆盖关系 |
| --- | ---: | --- |
| 工作台 / workbench profile | 2048 | 显式参数 > 进程环境 > 本地配置 > profile |
| 直接适配器、run_agent、API probe CLI | 1024 | 同上 |
| analyze_source CLI | 4096 | 同上 |

`COMPANY_MAX_OUTPUT_TOKENS` 支持 128–8192；显式 Python/CLI 值允许 1–8192。连接或能力探测自身的少量 token 不是业务答案预算。旧独立实验的 1024-token 限制也不是工作台默认值。本批不提高默认值、不改现场配置；真实供应商是否接受配置上限仍需现场核对。

默认互动预算：最多 5 次模型请求、源码上下文 36,000 字符、历史 18,000 字符、请求 230,000 UTF-8 字节；适配器请求硬上限 256,000 字节，响应硬上限 1,000,000 字节。历史发送会裁剪，不等于会话存盘正文被裁剪。大报告投影/单正文显示限制仍独立存在，并有明确提示和完整磁盘报告。

## 现场三步排查

1. 在现场采用合并代码并重启实际工作台进程。核对诊断摘要 `runtime.commit` 是否为该现场运行版本。只更新磁盘而未重启旧进程不能证明正在使用新代码。
2. 对现有的一次分析，在“API 返回”中使用“复制诊断 JSON”或“导出诊断 JSON”。这只导出固定字段的摘要，不导出问题、源码、回答、历史、路径、原始模型 ID、API 地址或密钥。不要分享整份 API 返回、quality trace 或完整分析报告；它们是本地调查材料，可能含业务信息。
3. 查看 `configured.max_output_tokens`、`runtime.profile/output_limit_source`；逐请求核对 `request_bytes/source_*`、实际 `choice_index/finish_reason`、`raw_content_characters/parsed_answer_characters`；对照 `output.final_answer_characters` 和页面补充的 `rendering.render_input_characters/persisted_conversation_characters`。JSON 包装解包、Markdown 规范化、配置值遮蔽和引用清理会产生合理字数差异；字数相同也不证明语义正确。

本机只读配置检查也可在现场源码目录运行以下命令，不发模型请求。它读取该现场进程可见的环境/本地配置并仅输出安全事实；不要粘贴原 `.env`。

```sh
python3 - <<'PY'
import json, sys
sys.path.insert(0, 'poc')
from model_profiles import model_config
for profile in ('workbench', 'adapter'):
    print(json.dumps(model_config(profile).safe_summary(), ensure_ascii=False))
PY
```

若看到 `length`，摘要和正文会明确部分回答；查看是否已在原预算内尝试一次续答。达到请求上限时不再续答。配置值来自 `explicit` 时先查命令行/Python 参数，来自 `environment` 时查启动进程环境，来自 `dotenv` 时在现场检查本地配置；不要盲目扩大所有预算。

## 定位缺口的区分

安全摘要采用固定代码：索引为空 `index_not_ready`、明确身份未在当前索引定位 `explicit_source_not_indexed`、身份多个候选 `source_identity_ambiguous`、检索无命中 `search_no_match`、业务词项未对应 `business_terms_unresolved`、已定位未读 `located_source_not_read`、实际源码预算遗漏 `source_budget_omitted`、读取失败 `source_read_failed`、外部实现缺失 `external_implementation_missing`、动态目标未定 `runtime_target_unresolved`。这些都不等于文件在磁盘上不存在。没有明确程序身份时不能猜某个程序。

模型身份仅以 SHA-256 指纹表示，便于两次现场摘要核对，避免输出可能含私有标识的配置字符串。诊断摘要覆盖默认 retrieval 业务路径；legacy focused/full_chain 仍使用原有阅读报告与页面限制提示，不将它们的聚合请求伪装成互动 5 请求预算。

## 验证与边界

定向验证涵盖真实索引的 IF/MOVE 过程、完整/未读源码、外部/动态/未知输入缺口、同路径候选上限和缓存；默认/brief 透传及三个 CLI 配置优先级；一次 length 续答、失败保留首稿、实际被选 choice；超 60,000 字符正文尾部、包装前后字数、会话重载和页面警告；带正文/路径/私有配置 canary 的安全摘要导出。

最终整合离线检查 212 项通过，显式禁止 socket 连接，外连尝试为 0。工作台/adapter 定向 Python 42 项、Node UI 51 项通过；独立审查复核所有发现项已闭环，`git diff --check` 通过。测试涵盖 raw response 选中第二 choice 后被拒绝时仍准确报告该 choice，空正文 length 的计数与截断状态，以及发送了匹配源码但解析失败时不误报检索无命中。

远端合并 SHA 记录在交付报告。现场服务未连接，实际生产模型回复、索引状态、供应商输出上限与客户业务验收仍待现场验证。本批没有触发额外真实模型调用，也没有接续旧未验收实验或合并旧 `89db3ed`。
