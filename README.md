# Blog-Agent

Python 博客助手。当前可在终端检索、阅读本地 Markdown，进行带来源的多轮问答；支持保存恢复、上下文压缩、工具权限与用量观测。

- `qa`：用户提问，当前只读。
- `manage`：管理员业务入口，目前也只读；整理、翻译、审批和发布尚未实现。
- 默认文章来源为仓库下 `Blog/`；网站接口尚未连接。
- Prompt Caching 完善、可选 Subagent、cache/compact 总成本与质量评测列为后续任务。

## 安装与启动

需要 Python 3.12+。在仓库根目录执行：

```powershell
python -m pip install -r requirements.txt
# 将 .env.example 复制为 .env，在本机填入模型服务凭据
python code/blog_agent.py --help
python code/blog_agent.py --mode qa
python code/blog_agent.py --mode manage
```

将自己的 Markdown 放入 `Blog/`，或用 `--posts-dir` 指定另一个目录。用户文章、凭据和运行会话均被 Git 忽略；首次检出包含空 `Blog/`，没有演示文章。只检索该目录当前层的 Markdown，不递归扫描电脑。

```powershell
# 在选定目录中离线检查工具（不调用模型，但会读取本地文章片段）
python code/blog_agent.py --check-local
# 从保存的未完成轮次继续
python code/blog_agent.py --mode qa --resume
# 需要保留旧会话时使用单独状态文件
python code/blog_agent.py --mode qa --state-file .blog-agent-data/another-qa.json
```

每种模式默认独立保存会话；旧会话不允许静默切换目录或模式。`--new` 会替换指定会话。模式名称不等于登录认证；模型说自己是管理员不能改变执行层权限。正常问答会把检索到的片段发送至配置的模型服务。

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

```powershell
python -m pip install -r requirements-dev.txt
python code/evaluate_agent.py --mode offline
```

统一离线入口包含博客和原核心回归，无付费模型或远程 MCP 调用。测试文章只在临时目录生成；评测结果保存在被忽略的 `code/eval_results/`。归档 Demo 的 MCP 运行另需 Node.js/npx。

## 开发资料

- [任务](docs/TASKS.md)
- [当前业务包说明](code/blog_assistant/README.md)
- [完整实施计划](blog-agent-implementation-plan.md)
- [评估与迁移验收](blog-agent-acceptance-20261010.md)
- [与博客网站的协作边界](docs/integrations.md)
- [仓库整理记录](docs/repository-layout.md)

独立仓库：`https://github.com/lidada64/Blog-Agent`。本机 checkout 保留在 `D:/CST/code/Agent`，不与博客网站的 Git 仓库合并。
