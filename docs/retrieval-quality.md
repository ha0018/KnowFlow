# S3：检索质量与可复现评测

S3 提供冻结语料、证据范围指标、B0/B1/B2 对照、逐题回答与人工复核、独立安全矩阵，以及真实 PostgreSQL/pgvector 的索引诊断。默认仍是哈希向量、原文摘录和本地词项重排，不能据此宣称真实语义模型质量或生产性能达标。

## 数据与冻结规则

[quality-v1.json](datasets/quality-v1.json) 是自编合成资料和人工编写标签，共 100 题：70 条有答案、15 条无依据或冲突、10 条权限、5 条注入/工具滥用。固定调参集 40 题（28/6/4/2），保留集 60 题（42/9/6/3）。它是项目内部保留集，不是第三方独立测试集。

每份资料通过正常上传、解析、切片、Embedding 与发布链路导入；管理员、编辑、读者及隔离租户使用独立身份。标准证据绑定文档版本 SHA-256、解析块编号和原文字符范围，不绑定某次切片的 Chunk ID。标签包含必要的多段证据、编号、限制条件、追问和冲突；目前的限额表采用扁平文本表述，不代表复杂表格解析验收。

运行快照保存源 JSON 校验值、完整发布语料/切片指纹、题目与标签指纹、模型/profile、切片 pipeline、检索参数、Prompt/指标版本、实际额度、代码修订、依赖锁、平台/CPU/内存（平台可用时）。排队后配置变化、非 gold 文档变化或题目变更都拒绝继续使用原冻结实验。密钥和服务地址不进入报告。

同一已导入数据库的 manifest 可直接复验。跨全新导入时 UUID 和并列排序可能不同，HNSW 构图也非逐位确定；因此独立 CI 导入的绑定 manifest 不混为一次实验。源 JSON 哈希用于核对原始内容，各组 B0/B1/B2 在同一次冻结导入内比较。

先使用调参集确定参数，提交代码后显式选择 `holdout` 做最终确认。CI push 只运行调参集；保留集通过 workflow_dispatch 的 split 输入选择。不能依据保留集结果反复调参；如果这样做，必须创建新的保留集和版本。脚本 `scripts/build-quality-fixture.py` 是本版标签的编写来源，不是自动评判器。

## 本地复现

先按 README 导入演示租户。以下命令在 `backend` 目录执行：

```powershell
uv run python manage.py seed_quality --password '你的本地演示密码'
uv run python manage.py evaluate_quality --split tuning --output ../.runtime/s3-tuning.json
uv run python manage.py evaluate_quality --split tuning --baselines B1 --rewrite --output ../.runtime/s3-rewrite.json
uv run python manage.py verify_retrieval_security --output ../.runtime/s3-security.json
```

要保存实际回答和引用，增加 `--answers`。正式确认时显式传 `--split holdout`；B0/B1/B2 始终使用同一个 split 和相同冻结语料。命令默认不生成回答，默认不接触保留集。

评测复用 S2 原子额度控制，文档向量、问题向量、改写、重排、生成分别计量。40/60 题多配置实验可能超过默认每日每人 100,000 的额度；先为隔离演示环境设置合适额度，结果记录实际额度，失败不会被静默剔除。CI 的隔离数据库使用每人 1,000,000、租户 10,000,000 的每日额度。已有租户的 `quota_config` 优先于环境默认值。

导入不会重设已有账号密码，不会重写已有冻结版本。重复导入会校验版本、原文哈希与发布状态。私有/跨租户题不把禁止来源当作可发送给模型的 gold 原文；撤权状态转换另由安全矩阵执行。

## 三组配置与可选增强

| 配置 | 向量候选 | 关键词候选 | 融合 | 重排 |
| --- | --- | --- | --- | --- |
| B0 | 精确 cosine，最多 40 | 无 | 单路 RRF | 关闭 |
| B1 | 精确 cosine，最多 40 | 最多 40 | RRF，常数 60 | 关闭 |
| B2 | 与 B1 相同 | 与 B1 相同 | 与 B1 相同 | 前 20 个融合候选 |

最终取 5 个片段，每文档最多 2 个。B0 的数据库排序使用距离加零的表达式，避免安装 ANN 索引后把精确对照变成近似检索。SQLite 使用有界精确 cosine，超过 5,000 Chunk 明确拒绝；不将点积假设为已归一化向量的 cosine。

`RETRIEVAL_RERANK_ENABLED` 和 `RETRIEVAL_REWRITE_ENABLED` 默认关闭。检索调试 API 和页面可逐次选择开关；问答链路冻结本次检索参数，并只传当前有权、当前发布版本的历史。评测 CLI 的 `--rewrite` 同时作用于所有比较组；要做单变量实验，分别保存开/关报告。

- `REWRITE_PROVIDER=rules`：NFKC/空白规范化，并为指代追问附加最近已授权用户问题。它不是语义改写模型。
- `REWRITE_PROVIDER=generation`：使用已配置的生成服务，单次最多 15 秒，JSON 对象输出只允许 query 字段，并检查原问题编号/数字。使用独立 `rewrite` 调用记录和额度，输出不允许发起工具调用。
- `RERANKER_PROVIDER=lexical`：查询词项覆盖率的透明本地对照，不是 cross-encoder。
- `RERANKER_PROVIDER=compatible`：独立服务地址/密钥/model，使用 Jina-compatible `/rerank` 的 query/documents/top_n 协议。响应必须是原候选的完整 index 排列，评分须有限。最多发送 20 个已授权片段，每片段前 2,400 字符；截断值与候选数进入快照。

独立重排配置见 `.env.example` 的 `RERANKER_*`。不会向重排服务回退发送生成/向量服务的密钥。不自动猜测供应商或价格；空价格表示未知。改写和重排的网络请求有总超时、响应体上限、有限重试和按次结算；错误会终止本次实验，不静默跳过增强步骤冒充该配置的结果。

所有模型调用前、候选生成后、重排后和返回前检查当前权限与活动版本。授权改变时不返回旧候选。远程协议由真实 HTTP socket 的确定性替身验证，不能写成真实供应商验收。

## 指标与人工复核

| 指标 | 口径 |
| --- | --- |
| DocRecall@5 | 前五个片段去重为文档版本，命中标准版本的比例 |
| EvidenceHit@5 | 所有必要 gold 原文范围均被返回片段完整覆盖；多段证据必须全部满足 |
| MRR@5 | 第一个完整覆盖任一标准证据的原始片段位次倒数；不先去重文档 |
| Precision@5 | 完整覆盖证据的片段数 / 5；返回不足五个不缩小分母 |
| 引用有效率 | 实际回答中的引用标记属于本轮已授权、已定位、实际发送的来源集合的比例 |
| Faithfulness、引用支撑率 | 人工记录有支撑断言数 / 复核断言数；分母零显示 N/A |
| 答案通过、正确拒答 | 人工复核；完整分集尚未复核完时总体通过率显示 N/A |

检索指标的主汇总只针对有答案题。逐题错误保留在对应分母并记零，耗时保留失败样本；另存成功率、错误码、改写/向量/召回/关键词/融合/重排耗时、生成 TTFT/总耗时与 usage。检索总耗时包含 Embedding/Rewrite/Rerank，不应直接用于 A10 的排除模型口径。

原有 20 条开发集没有精确证据范围，因此继续提供文档召回，证据指标为 N/A，不伪造新的 gold 标签。

在“效果评估”中选择数据集、分集和配置。勾选生成回答后，可查看标准要点、实际回答、原文和配置，逐题填写复核依据、事实数、引用断言数与支撑数。服务端校验支撑数不超过总数，并记录复核人和时间。未授权用户不能读取其他人的运行。结果再次读取或导出时同时检查当前请求者及原题身份权限；撤权后隐藏正文、gold、trace 和复核。导出的本地文件无法自动撤回。

## 独立安全矩阵

`verify_retrieval_security` 执行 50 项：三身份 × 三配置 × 四访问范围共 36 项；KB 与文档授权的实际撤销各三项；八条非法工具/schema 参数。验证返回来源和生成输入没有未授权原文。撤销演练只操作 fixture 身份，finally 恢复授权。

这份矩阵独立于 100 题分集，不据其数量宣称 A01 的完整 100 项隔离验收，也不据确定性替身宣称真实模型能抵御所有提示注入。工具 runner 的超时、循环与自主规划归 S4。

## HNSW 与规模诊断

在隔离 PostgreSQL 数据库执行：

```powershell
uv run python manage.py benchmark_retrieval_index --disposable --sizes 1000 10000 --queries 30 --output ../.runtime/s3-index.json
```

使用真实 `active_chunks` 授权查询、两租户、固定随机种子的合成 128 维向量、5%/50% 可见范围，比较精确与 HNSW 的邻居一致率、P95、错误、索引体积与 EXPLAIN ANALYZE BUFFERS。每组预热两次，再执行 30 次；保存逐请求结果。合成数据和临时索引在事务结束时回滚，仍要求隔离数据库，因为建索引会短暂持锁。

HNSW 诊断使用 m=16、ef_construction=64、ef_search=100、strict_order 迭代扫描。为确认实际执行索引，诊断臂关闭 seqscan/bitmapscan/sort，报告标明这一强制 planner 设置并检查实际 index 名称；业务查询不强制 planner。它是单连接索引诊断，不是生产语义召回，不是并发 10/1,000 请求/至少 10 分钟的 A10 压测。

默认继续精确检索。选择开启前，先在有代表性的真实语料与授权选择率上验证质量/负载，用迁移账号执行 `configure_retrieval_index --create` 创建并发 cosine HNSW 索引，再设置 `RETRIEVAL_VECTOR_INDEX=hnsw`。普通应用账号不需要建索引权限。vector HNSW 限制维度不超过 2,000；实际 PG planner 可能在小数据集选择精确扫描。候选不足时补做权限范围内精确检索，报告 `ann_fallback`；候选充足不代表 ANN 没有召回损失。

## 验收边界

本次自动化报告不能替代人工语义复核、真实供应商调用或规定时长的负载测试。A06 保留集的 demo 结果、A07 人工待评、A08 安全矩阵及拒答人工待评、A09 列表接口负载待测、A10 索引诊断及正式压测待测、A11 真实并发问答待测、A12 固定真实 Embedding/PDF 时效待测，均分别保留状态。

协议与索引实现核对来源：[OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、[Jina API](https://api.jina.ai/docs)、[pgvector 0.8.1](https://github.com/pgvector/pgvector/blob/v0.8.1/README.md#iterative-index-scans)。这些文档只支持协议和机制说明，不构成本项目质量或性能证据。
