# 本地语义 RAG 与回答策略

2026-10-15更新：提示2026-10-15.14分离检索分块与正文引用树，父章节含子节，标题样式规范化、重名用版本绑定ID消歧；引用失败时在同一输入预算中加入具体错误、待修草稿和当前可用来源。清单仅修复时注入，正常问答不增此负担；最多一次修复，失效/撤权/空间不足明确停止，恢复不重置额度。322项离线通过，真实生成另验。旧会话停旧进程后在轮次边界显式升级再启动，原会话未自动修改。

2026-10-13，按 PRD §3.2 实现。正式入口为 `code/blog_agent.py`，默认 hybrid；原会话保留保存的提示词，须在轮次边界显式升级，未完成轮预算不增额。

此前2026-10-13.13的契约继续保留：保留D7任务记忆/本地compact/来源重验，新增身份能力、受限四则计算、上一轮用户消息和旧回答改写的证据契约。文章来源型改写仍核验来源，资料有效时关闭重新探索，并按一句话/简短要求有界修复；不全局放行文章外常识。旧会话需停旧进程，在轮次边界显式升级后重启。工程范围及真实生成复审见[TASKS](TASKS.md)和[011](project-highlights/records-011.md)。

## 本机配置

用户已授权下载配置本地 embedding。默认模型 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`，384维，FastEmbed 0.9.0 / ONNX CPU 两线程；实际权重 revision 保存在忽略目录的 manifest，并进入向量缓存命名空间。本机已安装和下载，不需要 GPU、PyTorch 或向量数据库。依赖在独立环境，原 Python 环境不被升级。

在新机器安装与下载：

```powershell
python code/setup_local_embedding.py
python code/index_blog.py
python code/blog_agent.py --mode qa
```

模型/依赖只在显式 setup 时联网下载。正常检索 worker 强制 `HF_HUB_OFFLINE=1`、禁用遥测、只读本地权重；文章和查询不会发到 embedding 服务。生成答案仍使用既有配置的模型 API，会发送实际选中的证据，这与本地向量化分开。

`--retrieval keyword` 关闭 embedding；`--embedding-python` 可指定已安装相同版本依赖的 Python。缺失 runtime、manifest、模型、异常向量、CPU超时均明确返回 `keyword_fallback`，保留 BM25。`--check-local` 继续为只读关键词探针，不创建索引或下载模型。

已有会话在结束当前轮后显式执行（使用原模式、目录及状态文件）：

```powershell
python code/blog_agent.py --mode qa --upgrade-prompt
```

首次语义检索提示版本为 `2026-10-13.11`，当前版本见本文开头。旧 `.10` 等版本的逐行来源协议仍按保存版本恢复，不把旧回复重新排版。

## 检索与来源

搜索路径：扫描选定根目录普通子目录 → Markdown标题/段落分块 → 本地向量与BM25 → 按文章RRF融合 → 最多五篇候选 → 读取正文 → 生成回答。目录/符号链接/大小和扫描上限继续由 SamplePosts 执行，qa/manage只是隔离模式，不构成网站身份认证。

350字符窗口保留原始行范围；超长单行带字符坐标分片，不静默丢掉尾部。代码中的 `#` 不作为章节标题。embedding输入的标题/章节各限制40字符，正文源标题仍完整保存；较长代码片段可能跨窗口，候选不代表正文已完整读取。单行超过现有6000字符读取上限时明确报告 `LineExceedsReadBudget`，不自动修改用户文章。

SQLite只保存向量和键，不复制用户正文；按根目录/模式/模型与实际权重revision/维度/切分版本隔离。新增增量编码，修改使旧片段失效，删除清理该作用域旧向量；扫描、向量化和交付前复核内容版本。中断后已提交批次复用，查询缓存最多128条。持久缓存只优化向量计算，不代表Provider Prompt Caching。

每轮保存CPU向量调用额度：最多40批/2112文本；每批最多64文本，最多30秒且受该轮绝对截止时间及15秒回答余量约束。先保存预约再执行，中断恢复不退款、不增额度；失败切关键词回退。外部embedding调用费用为0，电力/设备成本未计；本地token没有Provider账单，记为未知。生成请求预算、输出恢复和工具限制沿用D9机制。

来源协议在相关小节末或回答末使用 `[[post_id|真实章节]]` 或 `[[post_id]]`。程序核验本轮实际读到、当前版本一致的正文及真实章节；对外生成“引用自：*文章标题* **章节**”，去重，不展示行号、hash或摘录。允许标题/段落/有序列表/表格/代码，无八条限制；原文与推断明确区分。无来源/虚构章节/陈旧版本在剩余总预算内最多一次无工具修复，再失败返回明确核验失败，原始错误草稿不进对话历史。核验来源真实与读取范围不等于语义支持认证，回答正确性仍需人工评测。

能力/帮助请求由程序据当前实际只读功能直接回答，不消耗检索和用户轮模型请求；文章事实仍需正文证据。两次空发现后，仅当还保留一次内容搜索额度且未发生工具错误时，允许一次不同概念的改写；持续空结果返回资料不足并要求澄清。所有硬额度仍优先，不能凭空断言库中不存在。

## 验证与局限

```powershell
python code/evaluate_agent.py --mode offline
python code/evaluate_local_rag.py --dataset code/evals/semantic-holdout-v2.json --output .blog-agent-data/experiments/local-rag/report.json
```

本地模型比较失败样本保留在 `.blog-agent-data/experiments/semantic-local-20261013/`：E5-small初版召回8/11，排除标题空片段/文章去重后9/11，跨语言仍不足；改MiniLM后同一校准集11/11，不将校准成绩称为封存泛化。新冻结v2集11个可答题Recall@3=1、MRR=1，1个无答案题空结果；只代表小型合成检索集，不代表真实生成质量、独立人工引用支持或生产开放题已全面通过。来源/排版/权限/预算/取消恢复由正式对话循环的模拟模型回归另验，D9旧协议继续回归。

本机默认Blog目录实际搜索“系统构造设计”首个候选为“系统架构设计”。现有目录另有1个stem在`.md`前以空格结尾的合法文件，原ID校验将其排除并报告ValueError；本轮未更名或改写文章，兼容性问题已单列V2，不把索引称为全目录无遗漏。

模型依据：[FastEmbed官方模型清单](https://qdrant.github.io/fastembed/examples/Supported_Models/)；[官方运行方式](https://github.com/qdrant/fastembed)。选型最终依据本机实际测试，无远程embedding API费用。网站只读API、元数据管理、审批发布、付费生成扩题及Subagent不属于本轮实现。
