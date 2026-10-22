# Agent 评估用法

按真实博客主题设计的[调试题库 v1](../../docs/question-bank/README.md)提供56组单轮、多轮、边界、性能及未来功能验收场景，附固定输入与评分规范。它是手动/未来驱动的设计文档，不能直接作为 `evaluate_blog_live.py` 的题目文件；需按其 id/prompt 格式准备独立题目。

Blog-Agent 的版本化本地事件与分维度评分见 [Trace grading v1](trace-grading.md)。入口 `python code/grade_trace.py --state-file .blog-agent-data/qa.json`；默认规则只测过程，任务目标需单独配置 rubric。evaluate_agent.py 仅保留 offline/checkpoint；旧 Demo live 入口已退役。

压缩/不压缩的实际 token 与费用比较、官方 Flash 价格表、输入用量生成对比图，见 [压缩费用测试](compression_costs.md)。

在项目根目录执行，依赖与主程序相同。检查点评估仅读取指定会话，报告写入独立输出目录。

## 离线回归

```powershell
python code/evaluate_agent.py --mode offline
```

不调用付费模型或远程 MCP。统一执行 test_*.py（核心与博客），覆盖多轮交互、工具选择与顺序、工具错误、模型错误、压缩、推理回传、恢复及缓存指标统计。模拟模型的测试验证程序逻辑，不能证明真实模型的回答质量。

## 真实博客模型评估

旧 Demo live 入口与 scenarios.json 已于 2026-10-20 退役。当前真实问答评测入口为：

```powershell
python code/evaluate_blog_live.py --help
python code/evaluate_blog_live.py --experiment-id <独立实验ID> --case-file <题目JSON> --posts-dir <授权文章目录> --max-spend-cny <本轮总额度>
```

题目文件格式为 `{"cases": [{"id": "case-1", "prompt": "问题"}]}`。该入口调用真实模型并维护费用保护账本；模型配置、外发文章范围、重复次数和本轮总额度须与授权一致。`--repetitions` 控制重复次数，`--efforts` 控制推理配置。默认配置不能视为用户费用授权。

## 检查已有会话

```powershell
python code/evaluate_agent.py --mode checkpoint --state-file conversation.json
python code/evaluate_agent.py --mode checkpoint --state-file conversation.json --expect-file code/evals/checkpoint_expect.example.json
```

仅读取，不调用 API 或执行工具。无 expect-file 时只验证结构和执行证据（工具 id、结果配对、文件检索顺序、轮数），PASS 不等于用户任务完成。加期待规则才会验证具体目标。旧会话没有 model_requests 时，缓存命中率显示“未知”，不会补造数据。

## 检查点期待规则

`--expect-file` 读取期待规则对象；示例见 checkpoint_expect.example.json。旧 scenarios.json 的场景驱动已退役，不再提供复制旧场景执行的命令。

期待规则：status 检查最终状态，min_compactions 检查成功替换次数；turns 按 number 定位轮次（0 是介绍）。tools 要求工具名称及顺序完全一致，forbidden_tools 禁止误用工具，tools_ok 默认 true，reply_contains 检查回复包含字符串（忽略大小写），result_contains 检查工具结果 JSON 包含原文。允许模型重试的场景可省略严格 tools，只使用禁用工具、结果和回复规则。未知规则会报错，避免拼写错误造成假通过。

这些规则不能全面判断语义、真实性或摘要遗漏。重要多轮任务应补充有证据的结果规则，并查看保存的 conversation.json；没有内置 LLM judge，避免让同一个模型自行证明答案正确。

## 报告和缓存指标

每次输出 code/eval_results/<UTC 时间>/report.json 和 report.md，失败退出码为 1，通过为 0；--output-dir 可指定空目录，已有报告不会被覆盖。真实博客评估在独立实验目录保留 manifest、账本及各次检查点。报告与检查点可能包含对话和工具资料，按任务数据管理。

每次主对话、旧 task、summary 和 standalone 请求自动写入检查点 model_requests：input_tokens、output_tokens、cached_tokens、cache_hit_ratio、elapsed_seconds、purpose、status、response_id、input_bytes、prefix_hash、compaction_count。失败及取消保留单独尝试；历史恢复后继续追加。正常 agent 的 SDK 自身可能重试，一个指标记录计量一次 SDK 调用的总耗时。工具账本新增 attempt_seconds 和累计 elapsed_seconds；成功压缩事件新增耗时、原有前后字节数。

prefix_hash 只摘要 model/instructions/tools/reasoning，用于发现固定配置变化，不包含输入内容，也不能证明服务器实际缓存。input_bytes 不是 token。报告命中率为“已观测 cached token 总量 / 对应请求 input token 总量”，不是各请求命中率的平均值；缺少缓存字段的请求排除出分母，展示可观测覆盖率。by_purpose 分组把摘要成本与主对话分开；未知字段保持 null，0 表示服务明确返回 0。

## Prompt caching 结论和使用方式

当前项目示例配置是 DeepSeek。其 [官方 Context Caching 文档](https://api-docs.deepseek.com/guides/kv_cache/) 说明服务端默认自动缓存共同前缀，无需手动开关，且不保证每次命中。代码每次重发固定系统指令、工具声明与不断追加的工作上下文，能够利用这种缓存；本地压缩重写 input 开头会改变历史前缀，所以可能降低历史部分的复用。store=False 控制响应保存，并不代表关闭 prompt cache。

新增指标优先读取 Responses 的 usage.input_tokens_details.cached_tokens，也兼容 Chat Completions 的 prompt_tokens_details.cached_tokens，以及 DeepSeek 的 prompt_cache_hit_tokens。具体 Responses 服务可能不暴露缓存字段；此时只能报告未知。实际 cached_tokens > 0 才是本次命中证据。

使用方法：保持系统指令与工具定义及顺序稳定，动态内容放在后面；使用真实博客入口的 --repetitions 做已授权的重复实验，查看实验报告和 model_requests。调整压缩阈值/模式后重新运行同一数据集，比较任务通过率、input/cache token、主对话及摘要耗时。不要只追求压缩后的字节数：压缩更小同时可能损失缓存和记忆。

如果改用 OpenAI，按 [OpenAI Prompt Caching 官方文档](https://developers.openai.com/api/docs/guides/prompt-caching) 核对所选模型的规则。当前没有强制添加 OpenAI 专属的缓存参数，避免向兼容服务发送不支持参数；是否命中依然由实际 usage 证明。
