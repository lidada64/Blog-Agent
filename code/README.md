# Blog Agent code

当前入口为 `blog_agent.py`，业务模块在 `blog_assistant/`，共用机制在 `agent_core/`。安装、启动和统一回归见 [根目录 README](../README.md)。旧 Demo、Skill/MCP 专属兼容链和 EXP-1/3 离线模拟已于 2026-10-20 退役。阅读 Skill、文章摘要后台、EXP-0/2 和真实博客评测保留。通用 Conversation 由业务注入执行器，ConversationStore v3 与 JsonCheckpointStore 分别负责会话校验和原子落盘。
