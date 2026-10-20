# 分层文章记忆：运行与验收

2026-10-19。接续[骨架交接](article-memory-scaffold.md)，实现独立后台、层次构建与持久额度、前台总览/选章。真实模型质量和实际费用仍须E阶段单独验收；离线模拟不代表生产摘要质量。

## 使用入口

在Agent仓库根目录执行。普通`blog_agent.py`启动默认按`qa:local-v1`或`manage:local-v1`确保独立worker运行。关闭前台不关闭后台；未配置模型/额度或缺少专用密钥时，仅扫描索引，摘要显示blocked_config。

```powershell
# 建立所选文章库，执行首次扫描并自动启动后台；不会凭前台模型配置生成摘要
python code/blog_memory.py init --posts-dir <文章目录> --scope qa:local-v1
python code/blog_memory.py status --posts-dir <文章目录> --scope qa:local-v1
python code/blog_memory.py stop --posts-dir <文章目录> --scope qa:local-v1
python code/blog_memory.py resume --posts-dir <文章目录> --scope qa:local-v1
```

`stop`保存用户暂停并使旧worker令牌失效，普通启动不会撤销暂停；`resume`显式撤销暂停后恢复。默认扫描间隔30秒，可用`--interval`调整。后台在节点工作间隙对账，模型单请求最长45秒；间隔不是严格变更发现延迟保证。暂停不能取消供应商已经接收的请求，其用量仍结算或保守保留，迟到结果不能发布。进程意外退出后，下次启动接管过期租约并补扫；没有配置系统开机自启。

状态包含PID、心跳、令牌、日志路径、退出原因、扫描issues、任务计数和预算。父进程必须看到子进程心跳才显示running；starting/expired/paused不会冒充运行。日志位于数据库同目录，记生命周期和错误类型，不记录原文、密钥或供应商返回正文。

`--no-worker`仅供维护CLI离线诊断；`blog_agent.py --no-article-memory`不启动或连接记忆服务，但不会停止已经在运行的独立worker。`--check-local`和`--upgrade-prompt`仍不启动后台。

## 明确配置模型与维护总额度

复制[配置模板](article-memory-config.example.json)到忽略目录`.blog-agent-data/`，填写本次确认的模型、服务地址、币种和总额度，再配置。模板含空值/零值，故意不能直接启用付费生成。

- 专用密钥为环境变量`BLOG_MEMORY_API_KEY`，不使用前台`OPENAI_API_KEY`隐式授权后台。不把密钥放配置JSON或Git。维护CLI会读取仓库`.env`，普通CLI沿用其已有环境加载；worker继承启动环境。
- `max_requests/max_input_tokens/max_output_tokens/max_cost_microunits`是该库持久总上限，重启不补充。显式修改上限不会清空历史消费。未知消费继续占用额度。
- `currency`为统一币种标签；1微单位=该币种的百万分之一。`input_price_per_million/output_price_per_million`为每百万token的微单位价格，必须按实际服务填写足够保守的上界，包含适用的推理/输出计费。费用是配置价格估计，不是供应商账单；同scope禁止更换币种和价格口径。
- `chunk_chars`默认建议6000，`summary_chars`1200，`request_output_tokens`2048；可在校验范围内调整。字节数+固定开销用于保守预留输入token，不当成实际用量。实际usage缺失不按零结算；实际用量超过预留时计入真实返回值并暂停worker。
- `prompt_version`与模型/地址/解析器及输入输出形状共同决定生成版本。修改生成配置使旧摘要失效并排队；仅增加总额度不重做已完成节点。

```powershell
python code/blog_memory.py configure --posts-dir <文章目录> --scope qa:local-v1 --config .blog-agent-data/memory-config.json
```

configure自动对账并确保worker；如果库已由用户暂停，仍保持暂停。新增密钥后先stop再resume，使新子进程继承新环境。SDK自动重试关闭，每次应用重试独立预留并记录请求ID；单篇最多3次构建尝试、有限退避。失败不会阻断其他文章。已失败文章修改源版本或生成配置后重新排队。

## 总览和选章

新会话在工具表中获得`get_article_overview`及`read_article_section`。旧会话不静默改提示；停旧进程、完成未完成轮，再显式升级提示后重启：

```powershell
python code/blog_agent.py --mode qa --posts-dir <文章目录> --upgrade-prompt
python code/blog_agent.py --mode qa --posts-dir <文章目录>
```

用户可要求总结某篇文章并看目录，然后说“总结第2章”；也可直接指定唯一章节标题。总览工具每页最多8个真实标题和有界说明；编号绑定最近展示的文章、内容版本、scope和根目录并随会话保存，更新/删除/撤权后拒用旧编号。显式切换话题清理旧绑定，另一个文章不能借用旧编号；自然语言是否正确选中工具仍需真实模型测试。

总览/章节摘要直接交付数据库中经过当前文件版本重新核验的内容，不再额外调用模型改写；一次请求多个章节时分别交付全部已读取章节摘要。来源标记为派生摘要，不冒充全文已读。比较分析、数字、代码、精确引文仍走原文工具及既有正文引用校验。启用记忆后的原文单篇上限8MiB、单次读取仍80行/6000字符；其他前台检索枚举上限保留，不把后台支持大库等同于前台搜索已无限扩展。

没有摘要时返回当前真实目录和准确状态，不宣称后台正在生成，不返回旧摘要。也可不调用前台模型直接检查：

```powershell
python code/blog_memory.py overview --posts-dir <文章目录> --scope qa:local-v1 --post-id <相对路径去掉.md>
python code/blog_memory.py section --posts-dir <文章目录> --scope qa:local-v1 --post-id <文章ID> --selector <章节标题或ID>
```

独立维护CLI不保存对话编号绑定，因此section使用标题/ID；编号续问在正式会话中使用。无标题文章目前以文章范围作为单个目录项，不伪造作者标题。

## 一致性与费用观察

SQLite schema v2在事务内从v1迁移，保留原记录。worker按库独占租约，心跳独立于模型调用；扫描提交、预留、节点保存和发布都检查有效令牌。构建队列按文章持久化，内部块/章节/归并节点逐个保存；重启通过内容及语境缓存恢复，不重复生成已完成节点。摘要缓存键包含scope、文章、正文、祖先标题、子依赖及生成版本；定位与引用ID每个源版本重建。

正文按完整段落/围栏代码/连续表格分块，块→章节→文章按有界分组递归归并，不能在最终汇总重新超限。过长的单个段落/代码/表格、过深标题语境或未闭合代码围栏明确failed，不截断后标ready。父章节自身正文与子章节分开，前言也进入文章依赖。所有节点ready且源版本、配置、令牌仍有效才短事务切换active_version。发布前与读取时重验源文件；不是文件系统锁，读取中再次改动会拒绝交付。

```powershell
python code/blog_memory.py usage --posts-dir <文章目录> --scope qa:local-v1 --foreground-state .blog-agent-data/qa.json
```

usage合并该数据库所有库及显式指定的前台检查点；同路径去重。前后台分别显示已用与预留/未知，汇总请求和已观测token。缺前台usage或价格则总费用为null，不按免费计算。可用`--prices`提供现有`price_usage`格式、以模型名为profile的价格表；不同币种不强行相加。只覆盖选定检查点，不自动声称涵盖整个项目所有历史副本；不要同时传入同会话的多个备份以免重复统计。

## 验证与余项

专项入口：`python -m unittest discover -s code -p "test_article_memory*.py" -v`；统一入口：`python code/evaluate_agent.py --mode offline`。测试全用临时合成文章，模拟模型只在测试文件定义，真实进程测试不配置生成模型。覆盖迁移、单实例/暂停/租约恢复、前台父进程退出、增改删/失败不误删、单章复用、语境移动、有限重试/未知费用、构建中更新、读取失效、重启选章、多个章节与原文证据分离。

E阶段仍须确认真实供应商Responses兼容性、本次模型、外发文章范围、维护总额度，抽查中尾部事实、限定条件、代码表格与比较质量，并分别报告冷/热费用和延迟。此轮未调用付费模型、未扫描用户Blog、未改写文章；没有质量提升或成本节省的实测结论。网站界面/身份系统、系统开机自启、历史节点清理和更复杂全文解析不在当前交付内。
