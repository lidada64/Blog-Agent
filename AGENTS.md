# Blog-Agent agent rules

## Scope and references

This independent Python repository implements Blog Agent. Start each task with [docs/TASKS.md](docs/TASKS.md); read [the implementation plan](blog-agent-implementation-plan.md) before business changes. Runtime entry and module responsibilities are in README.md.

The website remains a separate Next.js repository. Before changing website integration, auth, import or publishing, read [docs/integrations.md](docs/integrations.md), then the target website's AGENTS.md and relevant requirements. Local study material is reference only, not a default publication source.

## Implementation and verification

Keep blog business changes in code/blog_assistant/ and shared runtime changes in code/agent_core/. The product CLI is code/blog_agent.py; archive/ is historical reference, with only narrow fixes to keep an explicitly used legacy entry runnable. Reuse the core rather than copying it.

Enforce tool permissions and actual argument validation in application code. Preserve mode/directory isolation, durable tool results and recoverable state. New frameworks or large dependencies require user approval. Keep credentials in local environment files; Blog/ article contents, traces, checkpoints, reference copies and generated results stay outside Git.

Before committing, run the unified offline evaluation and inspect staged paths for credentials, article contents and runtime output. Website changes additionally follow that repository's required lint/build and page verification.

## Git reminder after major changes

每次大改完成后，在最终回复中报告 Git 提交状态，并提醒用户提交尚未提交的改动。大改包括新增业务能力、权限/持久化机制调整、模块或目录迁移、批量文件整理。若本轮已获用户授权并完成 commit/push，明确报告提交与推送结果；提醒规则本身不授权自动提交或推送。
