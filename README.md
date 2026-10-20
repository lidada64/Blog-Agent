# Blog-Agent

2026-10-15更新：提示2026-10-15.14分离检索分块与正文引用树，父章节含子节，标题样式规范化、重名用版本绑定ID消歧；引用失败时在同一输入预算中加入具体错误、待修草稿和当前可用来源。清单仅修复时注入，正常问答不增此负担；最多一次修复，失效/撤权/空间不足明确停止，恢复不重置额度。322项离线通过，真实生成另验。旧会话停旧进程后在轮次边界显式升级再启动，原会话未自动修改。

Python 博客助手。当前可在终端检索、阅读本地 Markdown，进行带来源的多轮问答；支持保存恢复、上下文压缩、工具权限与用量观测。

- `qa`：用户提问，当前只读。
- `manage`：管理员业务入口，目前也只读；整理、翻译、审批和发布尚未实现。
- 默认文章来源为仓库下 `Blog/`；网站接口尚未连接。
- 正式CLI默认本地多语言embedding+BM25混合检索，文章/章节来源与正常Markdown回答；见[安装配置与验证](docs/local-semantic-rag.md)。本机已下载模型，新机器运行 `python code/setup_local_embedding.py`；可用 `--retrieval keyword` 回退。
- Prompt Caching 完善、可选 Subagent、cache/compact 总成本与质量评测列为后续任务。
- 原2026-10-13.13能力继续保留：区分文章、运行能力、可验证四则计算、对话历史和旧回答改写；`1+1`本地验证，参考旧答总结时带入真实可见回复并重验文章来源，资料有效时不重新检索，一句话/简短格式最多一次预算内修复。任务记忆、task_local压缩及跨轮来源重验保留。旧会话需停止旧进程并显式 `--upgrade-prompt` 后重启，未完成轮先恢复；验证及真实质量限制见[台账011](docs/project-highlights/records-011.md)。

## 安装与启动

需要 Python 3.12+。在仓库根目录执行：

```powershell
python -m pip install -r requirements.txt
# 将 .env.example 复制为 .env，在本机填入模型服务凭据
python code/blog_agent.py --help
python code/blog_agent.py --mode qa
python code/blog_agent.py --mode manage
```

将自己的 Markdown 放入 `Blog/`，或用 `--posts-dir` 指定另一个目录。用户文章、凭据和运行会话均被 Git 忽略；首次检出包含空 `Blog/`，没有演示文章。检索该目录及普通子目录的 Markdown，不搜索选定根目录外的电脑文件。

```powershell
# 在选定目录中离线检查工具（不调用模型，但会读取本地文章片段）
python code/blog_agent.py --check-local
# 从保存的未完成轮次继续
python code/blog_agent.py --mode qa --resume
# 需要保留旧会话时使用单独状态文件
python code/blog_agent.py --mode qa --state-file .blog-agent-data/another-qa.json
```

每种模式默认独立保存会话；旧会话不允许静默切换目录或模式。`--new` 会替换指定会话。模式名称不等于登录认证；模型说自己是管理员不能改变执行层权限。正常问答会把检索到的片段发送至配置的模型服务。

## 阅读 Skill 试用

```powershell
python code/blog_agent.py --mode qa --skill efficient-web-research
```

这会使用独立的 `.blog-agent-data/qa-efficient-web-research.json`，启动时显示技能已开启。原始技能已安装在本机 Codex 技能目录；新机器需要先安装相同的已审核版本。该实验将原文和本地工具适配说明加入文章问答请求，不提供网页搜索。详见[试用说明](docs/memory-rag-skill-assessment.md)。

重复同一命令继续测试；中断轮追加 `--resume`。不加技能参数的默认入口仍使用原来的会话。对照实验请使用两个全新的独立 `--state-file`，一组加 `--skill efficient-web-research`，另一组加 `--skill none`，输入相同问题。已有会话不能中途切换技能。

## 目录

```text
code/blog_agent.py          当前 CLI 入口
code/blog_assistant/        博客工具、检索、权限、会话配置
code/agent_core/            共用循环、状态、压缩、指标和保留的兼容组件
code/test_*.py              博客与共用核心回归
code/evals/                固定任务、历史价格快照与评测说明
archive/                   旧 Skill Demo 和学习脚本
Blog/                      本地文章（内容不入库）
docs/                      仓库任务、协作边界与整理说明
```

旧入口已经归档到 `archive/agent_demo/agent_demo.py`；详见 [归档说明](archive/README.md)。现有 Skill 兼容组件保留在共享核心中，博客启动不要求 MCP 服务。

## 离线验证

Blog-Agent 默认记录本地元数据 Trace；[Trace grading](code/evals/trace-grading.md) 提供过程、权限、任务规则和预算评分：

```powershell
python code/grade_trace.py --state-file .blog-agent-data/qa.json
```

默认只校验过程，没有任务断言时返回 NEEDS_REVIEW；配置 rubric 后判定具体任务。历史缺失事件不伪造，完整评测需从新会话采集。事件与报告不复制正文，不调用付费模型或上传数据。

```powershell
python -m pip install -r requirements-dev.txt
python code/evaluate_agent.py --mode offline
```

统一离线入口包含博客和原核心回归，无付费模型或远程 MCP 调用。测试文章只在临时目录生成；评测结果保存在被忽略的 `code/eval_results/`。归档 Demo 的 MCP 运行另需 Node.js/npx。

## 开发资料

- [分层文章记忆运行说明](docs/article-memory-runtime.md)：独立后台、分层摘要/持久预算、总览选章已接入；缺少专用模型配置/额度/密钥时只索引，真实质量仍待验。旧会话显式升级提示后启用新工具。

本项目是实习展示的核心工程项目，关联网站定位为作品集；[项目亮点与简历素材](docs/project-highlights/README.md)持续记录已实现能力、作用、证据和可用简历表述。

- [任务](docs/TASKS.md)
- [Agent 调试题库：56组内容、记忆、边界与性能场景](docs/question-bank/README.md)
- [产品需求与优先级（主要维护入口）](docs/PRD.md)
- [当前业务包说明](code/blog_assistant/README.md)
- [完整实施计划](blog-agent-implementation-plan.md)
- [评估与迁移验收](blog-agent-acceptance-20261010.md)
- [与博客网站的协作边界](docs/integrations.md)
- [仓库整理记录](docs/repository-layout.md)

独立仓库：`https://github.com/lidada64/Blog-Agent`。本机 checkout 保留在 `D:/CST/code/Agent`，不与博客网站的 Git 仓库合并。
