# Blog-Agent agent rules

## Scope and references

This independent Python repository implements Blog Agent. Start each task with [docs/TASKS.md](docs/TASKS.md). Before changing product scope, priorities or business features, read [docs/PRD.md](docs/PRD.md), the authoritative product requirements, then [the implementation plan](blog-agent-implementation-plan.md) for technical phases. Runtime entry and module responsibilities are in README.md.

开始新开发会话时，读取 [长期问题与亮点台账](docs/project-highlights/README.md) 的“下次会话优先”部分；仍待处理时先提醒用户解决预算额度与跨轮次状态问题。遵循用户当前任务，不阻断其他明确请求；完成修复后更新提醒状态。

The website remains a separate Next.js repository. Before changing website integration, auth, import or publishing, read [docs/integrations.md](docs/integrations.md), then the target website's AGENTS.md and relevant requirements. Local study material is reference only, not a default publication source.

## 实习导向与亮点维护

Agent 是核心项目，博客是作品集载体。开发中发现问题、形成解决方式、完成修复或实验时，按 [亮点台账](docs/project-highlights/README.md) 的记录规则同步问题、方案、实施状态、作用、证据、限制及简历表述；规划与未验证指标保留为待办。

AI 判断遇到开发难点/bug，或用户明确指出“大难题”时，使用 [challenge-log skill](docs/skills/challenge-log/SKILL.md)；形成方案、修复和验证时再次更新。按修改规模、用户追问频率、推测难度、用户明确判定四维分级，把台账索引从小到大排序；未知依据不补造。该 skill 源码在此仓库，安装副本在 `C:/Users/12281/.codex/skills/challenge-log/`；修改 skill 时同步安装副本并校验。

台账统一存储于 docs/project-highlights/，入口README，详情按连续编号分卷；默认单卷6000非空白字符，超限用同ID续篇并更新定位。每次尝试记录执行者、假设、操作、观察、结果和取舍，失败尝试同样保留；按skill模板与容量检查执行。

## Implementation and verification

Keep blog business changes in code/blog_assistant/ and shared runtime changes in code/agent_core/. The product CLI is code/blog_agent.py; archive/ is historical reference, with only narrow fixes to keep an explicitly used legacy entry runnable. Reuse the core rather than copying it.

Enforce tool permissions and actual argument validation in application code. Preserve mode/directory isolation, durable tool results and recoverable state. New frameworks or large dependencies require user approval. Keep credentials in local environment files; Blog/ article contents, traces, checkpoints, reference copies and generated results stay outside Git.

Before committing, run the unified offline evaluation and inspect staged paths for credentials, article contents and runtime output. Website changes additionally follow that repository's required lint/build and page verification.

## Git reminder after major changes

每次大改完成后，在最终回复中报告 Git 提交状态，并提醒用户提交尚未提交的改动。大改包括新增业务能力、权限/持久化机制调整、模块或目录迁移、批量文件整理。若本轮已获用户授权并完成 commit/push，明确报告提交与推送结果；提醒规则本身不授权自动提交或推送。
