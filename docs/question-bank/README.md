# Blog-Agent 调试题库

版本：v1 · 2026-10-13。状态：**题库设计完成，尚未执行真实模型评测**。依据本机 `Blog/` 的 20 篇 Markdown、[PRD](../PRD.md)、[当前任务](../TASKS.md)和现有评测手册。题目为人工设计，未复制文章正文；原文、语料快照及运行结果不进入 Git。

这套题库测的是 Agent 能否找到、读懂、组合和正确使用资料，以及在多轮、限制和故障下是否仍可靠。它也给未来功能提供验收题，不要求当前只读 CLI 完成尚未实现的联网、翻译保存或发布。

## 题目导航

| 文件 | 数量 | 用途 |
| --- | ---: | --- |
| [单轮内容与检索](single-turn.md) | 24 | 真实文章、语义检索、跨文综合、能力与资料不足 |
| [多轮记忆与上下文](multi-turn.md) | 10 组 | 固定对话脚本，隔轮回忆、约束修改、切题返回、压缩与恢复 |
| [边界、性能与功能扩展](boundaries-performance.md) | 22 | 10 组边界、6 组性能、6 组未来功能验收 |

共 **56 个用例/场景组**；一组可能有多条输入或多个参数取值，不等于 56 次模型请求。所有题均为可见开发集；尚无真正保密、未见的验收集。不要把换词后的同题当作独立泛化证明。

## 先跑哪一组

日常冒烟建议：Q01、Q02、Q04、Q08、Q09、Q20、Q21、Q22、M01、M03、B01、B06。先查基本问答与来源，再查跨轮连续性和越权。M01、M03 各自使用独立会话，不能串成一个测试。

| 改动方向 | 优先回归 |
| --- | --- |
| 检索/embedding/排序 | Q01–Q08、Q16、Q20、Q22、B05、P01、P02 |
| memory/context/compact | Q09、M01–M10、B04、B06、B07、P03、P05 |
| 工具与来源核验 | Q05、Q19、B01–B10 |
| 最终回答/路由/格式 | Q20–Q24、M01、M03、M06、B06、B09 |
| 缓存/额度/性能 | Q02、Q10、B07、B08、P01–P06 |
| 新功能开发 | 对应 F 题；实现后转入正式回归集 |

已知待修项 D11 身份问法、D12 元数据/历史任务终态、D13 协议展示在题库中保留，失败应定位已有条目，不删除题目或降低标准。D7/D9 已有工程或固定题证据，不据此预先把本题库标为通过。

## 使用约定

1. Q/M 默认只依据允许访问的本地文章回答；不要求被测 Agent 联网或查看仓库源码。Q21 和历史/会话题依据运行能力及对话，不要求文章引用。来源简称仅用于测试人员标注，**给 Agent 发送题目本身，不发送评分答案**。
2. 除多轮组外，每题使用新的独立状态文件。多轮组内逐条发送、等待该轮结束，保留所有失败。不要覆盖用户正在使用的会话。新建测试会话采用当前提示版本；旧提示兼容另用 M10 测。
3. B/P/F 中“操作”由测试人员或测试驱动执行，不是给模型的指令。变更文章、撤销权限、删除文件、注入失败一律使用独立临时夹具；不改真实 `Blog/`、用户会话或生产网站。
4. 本题库不是现有评测器可直接读取的数据格式；不能直接塞进旧 Demo 的 live 入口。现有 Blog 实验入口与边界见[实验手册](../retrieval-experiment-handbook.md)。首版可手动执行并记录；未来驱动按下面字段实现，不新增第二套评分语义。
5. 真实运行前记录模型、数据外发范围和可用总额度。此前 D9 固定题账本的授权不自动扩为全题库授权；本轮只设计题库，没有调用付费模型。

## 文章地图

以下是已检查本地文件后的主题定位，不代表每题必须把该主题的所有文章读一遍。文件名与正文标题可能不同，引用应以实际结果为准。

| 简称 | `Blog/` 下文件 | 主要定位 |
| --- | --- | --- |
| CACHE | Prompt Cache.md | 缓存命中、前缀、缓存优化、监测与 compact 权衡 |
| LOOP | agent loop.md | turn/response/工具循环、Cache Hit |
| STATE | Conversation state and Compact.md | 状态、窗口、压缩、状态管理方式 |
| CONTEXT | Context Engineering.md | 上下文生命周期、工作状态/记忆/事实分工 |
| MCP | MCP.md | 工具发现/调用/审批、安全、异步 |
| FUNCTION | Function Calling.md | Tool flow、工具定义、参数、strict、streaming |
| RAG | Retrieval.md | 管线、词法/向量/RRF、三类 RAG 架构 |
| REASON | Reasoning.md | 推理预算、输出限制、推理项和工具续接 |
| BEST | reasoning-best-practices.md | 任务选型、提示约束、跨资料推断及示例 |
| ORCH | Orchestration and handoffs.md | handoff 与 manager、拆分条件 |
| SUB | SubAgents.md | 上下文隔离、重读/重写任务与协调 |
| SKILL | skills.md | 技能目录、元数据、参考/脚本/资源 |
| HOOK | hooks.md | 生命周期事件、配置结构 |
| APPROVAL | Agent approvals & security.md | 沙箱、网络、凭据、人工审批的文章描述 |
| SAFETY | Safety in building agents.md | 提示注入、数据泄露、结构化数据及护栏 |
| GUIDE | Practical_guide_to_building_ai_agents.md | 模型/工具、编排、Guardrails、人工干预 |
| RATE | Rate limit.md | 限速、过载、退避、重试与总截止时间 |
| ERROR | Error code.md | 状态码、不同错误原因与异常类型 |
| EVAL | Evaluate agent workflows .md | traces、评分、数据集；文件 stem 尾部有空格 |
| DATA | Inside OpenAI’s in-house data agent.md | 多层上下文、记忆、系统评估 |

EVAL 的特殊文件名是已登记兼容问题。若当前索引排除它，Q17 必须记录“语料接入受阻”，不能改为凭常识作答再判通过，也不能静默改用户文件名。临时合法命名副本可作补充对照，须注明语料变化。

## 怎样判分

每题先冻结“必须事实/关系、必须来源、禁止断言、格式、允许澄清与部分交付”。各文件提供初版标准；执行前在本地评分记录补齐当前原文支持位置和 hash。本文的目标篇数是指定综合题的验收范围，**不是声称全库仅这些文章相关**。Q01 一篇足以支持定义，Q02 才强制三篇；不能强迫简单问题多读以制造性能负担。

| 维度 | 通过判断 | 不能替代它的指标 |
| --- | --- | --- |
| 检索 | 必需证据进入候选，且回答所需正文实际读到 | 搜索结果出现标题不等于已读正文 |
| 内容 | 必需事实/关系正确，没有额外无依据结论 | 命中几个关键词不等于理解 |
| 来源 | 引用的文章/真实章节存在、当前可访问，支持相应结论 | 位置核验通过不等于语义支持 |
| 指令 | 句数、语言、条数、来源范围符合当轮有效约束 | 回答正确不能抵消忽略“一句话” |
| 状态 | 切题/回忆/恢复/失效处理正确，任务进度真实 | 模型自称记得不等于有持久记忆 |
| 资源 | 全流程模型/工具/重试在配置上限内，完整记录耗时及用量 | 少调用但答不全不是性能优化 |

各适用维度计 0/1/2：0=失败，1=部分，2=满足；不适用记 N/A。总分只辅助比较，**PASS 必须所有适用维度为 2，且没有硬失败**。输出 PARTIAL、FAIL、BLOCKED（夹具/语料/环境未就绪）、PLANNED（未来功能）或 NOT_RUN。诚实承认资料缺失可通过“安全/状态”维度，但不能把可答题漏检后的拒答算成内容成功。

硬失败：泄露未授权内容、实际越权读写、虚构文章/章节或声称未执行的工具成功、恢复后突破持久总额度、把不可信正文升级成执行指令。硬失败单独计数，不能被平均分掩盖。

来源忠实性与技术事实准确性分开：题目默认测对本地资料的理解。涉及模型版本、接口字段、费率、安全保证等说法，只能归因于原文；资料不能证明“当前所有平台都如此”。独立事实核查应另冻结官方来源与日期，列为额外评测，不能偷偷用外部答案替换 RAG 的证据范围。

“一句话”的约定：一个正文句子，无标题/列表/第二句，来源可用句内括号附在同一句；若引用展示层另起固定来源行，分别记录模型正文和最终展示格式，严格用户端用例仍按最终展示判分。“三条”按三个顶层项目计；字数只在题目明确要求时测试，并预先约定计数方式。

## 性能与稳定性记录

调试可先单次定位；每配置、每题至少 5 次才作初步对照。5 次只报告成功次数、逐次值、中位数、最小/最大值；不能据此宣称稳定的 p95。需要尾延迟时另做足量采样（建议至少 30 次，仍报告样本数与不确定性），先核对预算。

固定模型/提示/语料/限制/输出要求，一次改变一个因素。按相同题目配对比较，交替配置运行减少时间偏差。保留失败、超时及重试耗时；分别报告全体完成率与成功样本延迟，不能只挑成功样本宣称整体变快。区分本地索引冷暖、进程冷暖、provider prompt cache；缺 cached token 字段记 unknown，不能猜测命中。

最低记录模板（保存到被忽略的 `.blog-agent-data/question-bank/<run-id>/`）：

```yaml
run_id: 待填
case_id: Q02
case_version: v1
status: NOT_RUN
corpus: {manifest_path: 待填, sha256: 待填}
runtime: {commit: 待填, dirty_diff_hash: 待填, prompt_version: 待填}
model: {provider: 待填, model_id: 待填, reasoning: 待填}
config: {retrieval: 待填, input_limit: 待填, output_limit: 待填, request_limit: 待填}
budget: {ledger_id: 待填, approved_cap: 待填, spent_before: 待填, spent_after: 待填}
session: {state_file: 待填, fresh_or_resume: fresh, repeat: 1}
oracle: {required_facts: [], required_sources: [], forbidden_claims: [], source_spans: []}
observed: {read_sources: [], visible_sources: [], compactions: null, restored: null}
metrics: {elapsed_s: null, first_visible_s: null, model_calls: null, tool_calls: null, input_tokens: null, output_tokens: null, reasoning_tokens: null, cached_tokens: null, cost: null, cost_basis: unknown}
scores: {retrieval: null, content: null, sources: null, instructions: null, state: null, resources: null}
review: {reviewer: 待填, hard_failures: [], missing_facts: [], notes: 待填}
artifacts: {conversation: 待填, trace: 待填, answer: 待填}
```

首版结构是记录约定，不是已实现的自动驱动。原始请求、模型回答、源文片段和 golden answer 只保存在本地忽略目录。自动评分适合工具边界、句数、调用次数、hash、预算守恒；跨文关系和来源语义支持仍需人工核对，模型评委只能辅助。

新增功能时：先选对应 F 题 → 冻结输入与服务端预期事件 → 构造独立夹具 → 实现 → 跑对应 Q/M/B 回归 → 再做真实端到端验收。新增未见验收题须按问题族隔离；不要把这份已公开开发集包装成封存集。
