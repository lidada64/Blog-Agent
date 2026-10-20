# Blog-Agent 组件现状与实施顺序

2026-10-17，检查对象是正式的 `code/blog_agent.py`，不把归档Demo或开发Codex的技能算进去。

| 组件 | 当前实际情况 | 代码证据 |
| --- | --- | --- |
| Hook | 部分有：推理、回复展示和会话结束回调；没有通用的轮前/轮后业务Hook注册机制 | [conversation.py](../code/agent_core/conversation.py)、[presentation.py](../code/agent_core/presentation.py) |
| 运行时组装system prompt | 有：基础instructions加上当前改写、预算收尾、截断和引用修复要求；尚未统一成独立的记忆/技能加载器 | [request_options](../code/blog_assistant/convergence.py) |
| Memory管线 | 部分有：任务目标、来源引用、未答问题持久化并重新装入输入；缺少四类记忆索引、按需加载、提取整理和跨任务约束范围 | [task_context.py](../code/blog_assistant/task_context.py) |
| Compact管线 | 有：正式文章问答用task_local重组输入；共享核心另有local/summary/standalone。并非设置summary就代表正式文章路径会调用摘要模型 | [task_context.py](../code/blog_assistant/task_context.py)、[context.py](../code/agent_core/context.py) |
| 计划工具 | 没有：有任务阶段和未答点，但没有模型可调用的计划工具 | [tools.py](../code/blog_assistant/tools.py) |
| 博客专用Skill | 10-18新增显式阅读实验：保存efficient-web-research原文与本地适配，注入问答请求；仍无通用按需技能发现机制 | [reading_skill.py](../code/blog_assistant/reading_skill.py)、[session.py](../code/blog_assistant/session.py) |
| 完整Error recovery | 没有完整闭环：已有保存恢复、一次截断/空答/引用/格式修复；本轮补已知故障，仍非所有异常自动恢复 | [convergence.py](../code/blog_assistant/convergence.py)、[errors.py](../code/agent_core/errors.py) |

## 本轮先修什么

1. **长文读不下，换读法继续。** 全文过长返回有大小限制的真实标题/章节范围，无标题用行段；模型选择范围后调用已有`read_post`。第一次得到分段提示算进展，重复相同提示不算，重启也不能重置次数。导航不是正文依据。
2. **失败说对原因。** 保存失败单独停止，告知最新状态未确认保存；文件访问失败和真正权限拒绝分开。从磁盘最后成功检查点恢复，不假装保存成功。读取停止原因不被后续引用失败覆盖。
3. **伪工具文本不交付。** 对已知协议形式的模型输出先拦截，不执行，不当引用错误反复修复。普通正文中解释协议不受此规则影响。

## 下一批按什么顺序

**任务契约 → 会话约束记忆与输出检查 → 完整按需Memory → 可选计划工具/Skill。** 当前最缺的是正确识别任务、保留要求并检查回答；计划工具和Skill不能代替这些逻辑。可在记忆管线接入时提取轮前/轮后Hook，避免先造一个没有使用者的框架。

这里的“完成检查”就是用具体场景运行程序：切题后默认三条仍在、本轮一条不影响下一轮、四条答案不能通过三条要求。复杂语义支持还需人工校准。详细设计见[可靠性指引](agent-reliability-guide.md)，真实付费模型质量另验。
