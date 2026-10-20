# 分层文章记忆骨架与后续交接

2026-10-19更新：B–D实现已继续落地；当前命令、配置、行为与限制以[运行说明](article-memory-runtime.md)为准。下文保留2026-10-18骨架阶段的交接快照，不作为最新功能状态。

2026-10-18。按本轮用户要求先交付骨架。完整需求仍见[实施指引](article-memory-implementation.md)与[PRD §3.2](PRD.md#32-本地-rag)。这不是已经接入问答的记忆系统。

## 已可运行的范围

- `code/blog_assistant/article_memory_store.py`：独立SQLite库，`PRAGMA user_version=1`；libraries/documents/nodes/jobs/usage表，连接显式关闭，短事务写入。未知schema拒绝打开，不静默降级。后续schema修改必须增加版本与迁移测试。
- `article_memory_sync.py`：初始化/全量hash扫描、增改删对账、同版本幂等构建意图；扫描失败或超限明确partial且不推断删除。最新签发scan_epoch才可提交，过期/重复扫描拒绝。
- `article_memory_build.py`：复用真实引用章节树，保存文章/章节元数据与引用ID映射；定义SummaryGenerator、SummaryRequest/Result。没有安装模拟或真实生成器。
- `code/blog_memory.py`：开发维护CLI，仅显式执行一次扫描或查询状态。`code/blog_agent.py`及正常问答路径没有接入此骨架。
- `code/test_article_memory.py`：临时合成语料专项；统一离线入口自动发现这些测试。

默认库位于忽略目录 `.blog-agent-data/article-memory.sqlite3`。article_id沿用相对路径去掉`.md`，重命名按删旧增新；库身份由规范根目录+可信scope组成。scope仅是本地隔离命名空间，不是鉴权。未来调用者必须根据可信身份和可见性版本构造scope，不能接受模型自报权限。

数据语义：`initialized`仅表示曾完成过全量原文目录对账，不表示摘要完成；最新结果看`scan_state`与issues。当前文档只有stale/deleted，active_version始终为空；所有构建意图停在blocked_config，reason说明生成器和预算执行尚未实现。nodes是索引元数据，不是ready摘要或完整构建依赖图；usage只有schema，不提供消费额度保障，不能据此调用付费模型。

扫描默认上限100,000个目录项、8MiB/篇，内部ScanLimits可调；达到上限不假报初始化完成。每次扫描全文hash并进行双读稳定性校验，能发现同大小/mtime变化；并非文件系统快照，后续发布/读取仍必须重验原文。目录链接/junction及非规范`.MD`后缀标partial，既有非法路径规则不放宽。旧节点和任务保留，尚无历史清理策略。

## 开发验收命令

在Agent仓库根目录执行，先指定自己选择的文章目录；不会发起模型调用，但会读取该目录Markdown建立元数据索引。请勿把自定义数据库输出放进可提交目录。

```powershell
python code/blog_memory.py init --posts-dir <文章目录> --scope qa:local-v1
python code/blog_memory.py status --posts-dir <文章目录> --scope qa:local-v1
# 修改、新增或删除所选目录内的测试文章后
python code/blog_memory.py scan --posts-dir <文章目录> --scope qa:local-v1
python -m unittest discover -s code -p test_article_memory.py -v
python code/evaluate_agent.py --mode offline
```

重跑init不会重复添加同版本任务；退出并重启CLI也不清空库。scan/status要求库已初始化；partial退出码2。真正后台循环和ensure_worker尚未实现，因此不能显示“后台正在整理”。本轮只在临时合成文章上执行，不批量扫描用户Blog，也不使用历史测试额度。

## 后续agent按顺序接手

### B：独立进程生命周期

新增`article_memory_worker.py`与独立入口，复用synchronize；接入正式CLI初始化及启动的ensure_worker。增加进程租约/递增fencing token、心跳、用户暂停、启动确认、本地日志和status/stop。scan_epoch只防扫描乱序，不能代替worker令牌。不要用空循环加PID文件宣称并发安全。

验收：两个前台同时启动唯一有效worker；退出前台后修改文件仍被检测；暂停不被普通启动撤销；异常退出可接管，旧worker不可提交。未配置C能力时仍只扫描并保持blocked_config。

### C：分层构建、预算与发布

扩展现有文章级jobs为块→章节→文章依赖任务。新增内容+祖先标题语境+parser/model/prompt版本缓存键、正文来源映射、复用规则及有界归并。现有节点范围覆盖父章节子树，不能把父子范围当互不重叠正文；前言/无标题/代码表格必须有明确覆盖策略，无标题文章目前仅保存文章范围。

在调用SummaryGenerator前实现持久总额度预留、调用后结算、未知计费保守占额、重启守恒和有限重试；配置变更需显式重建API（目前register_library拒绝隐式改generation_version）。增加worker令牌检查、依赖验证和提交前源版本核验，事务切换active_version。模型调用在事务外；只有验证后的构建版本可发布。

验收：改一章复用无关章节但重建当前版本定位；构建中再次修改使旧任务失效；A→B→A源版本恢复；旧令牌发布拒绝；崩溃未知用量不退还额度；大章/多章最终合并仍有界。先用测试内模拟生成器验证，不能将其接成生产摘要。

### D：前台派生证据与选章

新增独立摘要读取服务，每次读重新校验当前源版本/权限/完成状态，再接tools、task_context、session与引用守卫。摘要不得塞入已读正文通道。保存article_id/source_version/章节编号→node_id映射，重启与切题不串章；目录有界展开、细节回原文，直接指定章节无需先走总览。

验收：当前摘要总览+真实目录、选第二章及重启续问；源修改/删除/权限撤回后拒用旧摘要；未生成时准确说明状态；代码/数字/精确引用回原文取证。未实现发布/read服务之前禁止直接查询nodes.summary用于问答。

### E：真实质量与运行验收

冻结语料/问题集，明确本次生成模型、外发范围和总额度，检查中尾部事实、限定条件、代码表格及跨文差异；分别报告首次构建和热读取的费用/延迟。离线测试数量不代表真实摘要质量。各阶段更新TASKS与D7/D9/D12台账，执行统一离线回归；不替用户自动提交或推送。

## 本轮验证

15项专项覆盖初始化/重启幂等、增改删及恢复、失败不误删、首次超限、源目录消失、扫描乱序、scope/根目录隔离、相同mtime修改、章节ID、101篇与超128KiB、未知schema、真实CLI、读取竞态、事务回滚、链接边界。首轮测试清理暴露SQLite上下文不关闭连接造成Windows WinError32，测试改为显式关闭后通过；不是用户数据库故障。统一离线353项0失败/错误/跳过，报告`code/eval_results/20261018T082951484029Z/report.md`。
