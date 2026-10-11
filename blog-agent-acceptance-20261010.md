# Blog Agent 评估验收与首批迁移

日期：2026-10-10。依据当前代码、实施计划、历史评测原始结果及本轮实测。Recall 已依用户指令通过博客 `.recall/.capture-paused` 暂停；未使用保存的历史上下文作验收依据。

## 验收结论

**评估基础通过；首批迁移通过。** 已有评估不是完整 P0/P7 的完成证明；本轮实现 P1 的核心解耦及 P2 的样例问答片段，不能标记全部阶段完成。没有接通真实博客、管理整理、审批或发布。

既有评估产物有可读报告、原始检查点、provider 用量和确定性规则；旧回归最终 78 项通过。原静态复用评估中“没有 usage/perf_counter 观测”已过时，当前 metrics.py、工具耗时和压缩计量均已实现。对失败尝试保留记录，报告没有仅保留最好样本：历史结果索引在本轮证据目录的 historical-report-index.json。

核查的既有证据：

- `code/eval_results/20261010T011221875806Z/report.json`：两个真实场景通过，7 次请求，输入/cache 14,482/11,392 token。规则得分不等同于全面语义质量。
- `code/eval_results/20261010T015807490761Z/report.json`：旧离线 78 项通过。
- `code/eval_results/compression-20261010T015809959901Z/`：none/local/summary 各两次，均通过固定记忆/重读任务，local/summary 各触发六次压缩；比较包含摘要请求成本，缓存字段来自服务。
- 上述实验中 local 与 summary 均比 none 更贵。输入减少并不保证省钱；每组两次不足计划建议的五次，只能支持该样本下的结论，不能称“缓存优化完成”。未重新验证公开价格，费用仅沿用当时报告的估算。

## 本轮实现

1. 抽出 `Conversation` 共用循环，应用传入工具集合和执行器；`SkillConversation` 保留旧接口和工具顺序。MCP 导入放回可选 Skill/搜索适配路径，博客入口不需要安装、导入或连接 Skill MCP。
2. 会话 kind、指令、介绍、轮次上限可配置。Skill 仍为 10 轮，博客默认 50 轮。检查点版本 3；旧 Skill v2 显式补充介绍/10 轮并迁移，历史和工具记录保留；跨业务加载拒绝。仅 load 不改原文件，start/继续保存时才落盘迁移。
3. 新增 `code/blog_agent.py` 与 `code/blog_assistant/`。qa/manage 使用独立会话，应用配置模式；每次执行先核对白名单和实际参数。两模式本轮均只读，管理写工具尚未注册。伪造 role/mode 或直接请求 publish_post 被拒绝，账本中保留 PermissionError 及对应 function_call_output。
4. 默认检索两篇人工 Markdown 样例，返回 source、SHA256 version、行范围与片段；限制目录、文件和返回长度，越界链接与目录联接拒绝；不读取原 Blog/ 或真实 SQLite。未知内容返回空结果，提示模型明确说明不足。
5. CLI、业务层、循环、模型和原子状态职责图以及逐文件调整说明见 `code/blog_assistant/README.md`。运行产物位于 `.blog-agent-data/`，忽略规则已添加；没有新依赖。

## 本轮证据与实测

证据目录：`D:/CST/code/Agent/.blog-agent-data/acceptance-20261010/`。

- 原离线回归：78 项通过，无跳过。报告 `code/eval_results/20261010T023331659628Z/report.json`。迁移中曾出现交互展示层对旧调用对象的四项兼容错误，已修复；失败报告保留在 `20261010T023120166543Z`。
- 博客离线：11 项通过，无跳过，见 `blog-tests.log`。覆盖检索/读取往返、状态恢复、模式/目录不可跨会话复用、参数绕过拒绝、越界路径/Windows junction、输入大小、空结果、v2 Skill 状态迁移、超过旧十轮上限及阻断 MCP/openai/dotenv 导入时仍可运行离线探针。
- 真实问答：用户明确授权发送人工样例至 api.deepseek.com 后，使用 deepseek-flash 完成两篇综合与无答案测试。实际读取两篇正文，引用原文行号、来源和版本，区分原文与推断；人工核对引用范围和事实关系，无访问量资料时未编造。见 `live-qa.json` 与 `live-report.json`。
- 真实调用：6 次，无错误；input/output/cache = 10,846 / 1,439 / 8,448 token；API 累计约 10.761 秒。缓存命中率约 77.89%，无压缩；这是一次 smoke 验证，不是质量/性能统计结论。不估算缺失 reasoning token，不声称完整内部推理可观测。
- 拒绝 trace：`permission-trace.json`。模型请求 publish_post，执行层拒绝，无文章写入。
- 环境与源码指纹：`environment-and-source.json`；Python 3.12.7、openai 2.29.0、mcp 2.3.0、python-dotenv 1.2.2。当前 Agent 根目录没有 Git 仓库，未创建提交；原三份核心代码备份在 `.blog-agent-data/migration-backup-20261010/`。
- 证据中对当前配置的密钥/secret/token/password 精确扫描无匹配；未打印或写入实际凭据。
- CLI `--help` 与 `--check-local` 实际运行通过。
- 博客 `npm run lint` 通过（7 条既有警告），`npm run build` 通过。独立 `npx tsc --noEmit` 仍有既有三处错误：next.config.ts 的 eslint 配置、ScrollSmoother.d.ts 非模块、post-meta.tsx 的 Element.offsetHeight。现有构建配置跳过类型验证，故不能声称博客完整类型检查通过；本轮未改该配置或业务 TypeScript。

## 后续阶段与目录

**今后 Agent 改动继续在 `D:/CST/code/Agent/code/`，业务优先 `blog_assistant/`。** 提示词/业务会话改 session.py，工具改 tools.py，权限改 permissions.py，检索改 retrieval.py，启动改 blog_agent.py；共用循环/压缩/恢复/指标改 agent_core/，每次共用层改动跑旧测试和博客测试。

博客 `D:/CST/code/Carrykit blog2` 继续承载页面、鉴权、正式数据、审批和未来接口。无需把 Python 搬入博客目录。当前有业务包可开发，不代表运行时接口已连接。

仍待完成：P0 的 5–10 篇多样样例、固定博客任务集及统一 run/session/step/call 事件；P1 更完整的业务工具差异和执行前后 hook 契约；P2 固定两步与自主补查对照、正文诱导测试及系统化引用评分；P3 经鉴权真实博客只读接口。后续才是整理、审批发布、网页、worker 和部署。用户个人理解按计划 §18 单独验收，本轮没有替用户标记学习掌握。


## 追加交付：文件名与正文关键词搜索

按用户要求新增博客 find_file/find_text。搜索仅 --posts-dir 当前层 Markdown；正文定位强制使用已完成文件发现结果，返回行号、来源、版本和受限片段。可处理含空格/点号文件名。跨轮往返、未发现路径拒绝、参数数组/输出预算、非 Markdown 排除及越界 junction 测试通过。当前博客 suite 共 18 项（包含另行已有的展示测试），旧回归报告为 code/eval_results/20261010T052047986188Z/report.json。无用户文章正文读取或新增外发；模式仍 qa/manage，均只读。


## 当前默认目录（取代此前样例默认值）

按用户要求，默认文章目录现为 D:/CST/code/Agent/Blog；两篇 code/test_fixtures/blog-posts 样例及空目录已删除。历史验收数据仅作证据保留，不用于问答。所有离线测试改用临时文章，未读取/发送用户正文；博客 19 项及原回归通过（code/eval_results/20261010T060549465789Z/report.json），博客 lint/build 通过。旧会话若绑定样例目录会拒绝恢复至新目录，使用独立状态文件或显式 --new。
