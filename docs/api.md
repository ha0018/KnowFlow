# API v1

根路径 `/api/v1`，统一不带尾随 `/`。除登录、刷新、健康检查外使用 `Authorization: Bearer <access>`。跨租户或不可读对象返回 404；操作权限不足返回 403。错误结构为 `{ "error": ..., "trace_id": "..." }`（SSE 前置错误返回 error，流内错误携带 trace_id）。UUID 由后端生成。

| 方法 / 路径 | 功能 |
| --- | --- |
| POST `/auth/login` | `{email,password}` → access、refresh、user |
| POST `/auth/refresh` | `{refresh}` → 轮换后的凭据 |
| POST `/auth/logout` | `{refresh}` → 撤销该刷新凭据，access 最长继续有效 15 分钟 |
| GET `/auth/me` | 当前用户与租户 |
| GET/POST `/members` | 管理员列出/创建本租户成员；创建输入 email、password、display_name |
| GET/POST `/roles` | 管理员列出/创建角色；输入 code、name、permission_codes |
| PUT `/members/{id}/roles` | `{role_ids:[...]}` 替换角色归属，递增 ACL epoch |
| GET/POST `/knowledge-bases` | 分页列表/创建；name、description、visibility（private/tenant） |
| GET/PATCH/DELETE `/knowledge-bases/{id}` | 读取/修改/软删除 |
| GET/POST `/knowledge-bases/{id}/acl` | 查询/整体替换授权；数组元素 `{user_id 或 role_id,access_level}` |
| GET/POST `/knowledge-bases/{id}/documents` | 分页文档/上传 multipart：file、title、可选 document_id（新版本） |
| GET/DELETE `/documents/{id}` | 查看/软删除文档 |
| GET `/documents/{id}/versions` | 文件版本清单 |
| GET `/documents/{id}/download` | 授权下载，支持 `?version_id=UUID` |
| POST `/documents/{id}/reindex` | 同版本、同 pipeline 复用构建；失败任务可重试，成功任务幂等返回 |
| POST `/documents/{id}/access` | `{acl_mode:"inherit"或"restricted",entries:[...]}` |
| GET `/tasks/{id}` | 处理阶段、状态、尝试次数、Chunk 数、trace_id |
| POST `/retrieval/search` | `{query,kb_ids?,top_k?,mode?,rewrite?,rerank?}` → 本次授权片段及分段 diagnostics |
| GET/POST `/conversations` | 分页会话/创建；`{title?,kb_ids?}` |
| GET `/conversations/{id}/messages` | 当前授权下的历史正文与引用 |
| POST `/conversations/{id}/messages:stream` | `{question,parent_id?}` + `Idempotency-Key`；parent_id 保留再生成关系 |
| POST `/conversations/{id}/agent:stream` | 受控 Agent；`{question,parent_id?}` + `Idempotency-Key`，需要 agent.execute |
| GET `/runs/{id}` | kind、运行状态、部分正文、usage、来源；可见 Agent 运行另含 tool_calls |
| POST `/runs/{id}/cancel` | 停止当前用户的运行 |
| GET `/references/{id}` | 当前权限下的原文摘录、不可变版本与位置 |
| POST `/messages/{id}/feedback` | `{rating:1或-1,reason?}` |
| GET/POST `/agents/tools` | 工具清单/单个只读工具调用 `{tool,args}` |
| GET `/agents/calls` | 本人最近 100 条工具审计元数据，需要 agent.execute |
| GET `/agents/references/{id}` | 本人 Agent 统计结果快照，重新检查当前权限与 ACL epoch |
| GET `/evaluations/datasets` | 数据集、分集计数与冻结 manifest hash |
| GET/POST `/evaluations/runs` | 运行记录/创建 `{dataset_id,mode:"dense"或"hybrid"}` |
| GET `/monitoring/dashboard` | 授权资源计数 |
| GET `/monitoring/calls` | 本租户最近 100 条调用元数据 |
| GET/POST `/monitoring/models` | 配置/维度状态；显式探测 `{kind:"generation"或"embedding"}`，需要 monitoring.read |
| GET/PUT `/monitoring/quota` | 查看本人额度；管理员更新 `{daily_tokens,user_daily_tokens}`，UTC 每日生效 |
| GET `/health/live`、`/health/ready` | 不在 `/api/v1` 下；活性/依赖就绪检查 |

角色权限代码：`knowledge.create`、`agent.execute`、`evaluation.run`、`monitoring.read`。租户管理员在本租户内具有上述能力。ACL 的 read/edit/manage 为递进权限，文档授权只能收紧知识库范围。

上传返回 HTTP 202 和 `document_id/version_id/build_id/task_id`。本地 inline 模式下处理会在请求返回前执行，因此仅用于开发；部署使用 Celery 异步队列。

SSE 依次发送 `meta`、`references`、零到多条 `delta`、`done`；错误发送 `error` 后结束。空闲等待时发送注释心跳。幂等键重复返回 JSON `{run_id,status,replayed:true}`，不会再开模型请求；同键不同请求返回 409。客户端应查询 run，本版本不续播。

```text
event: meta
data: {"run_id":"...","message_id":"...","trace_id":"..."}

event: delta
data: {"seq":1,"text":"根据资料……"}

event: done
data: {"status":"completed","usage":{"source":"unknown"}}
```

工具输入：

```json
{"tool":"KnowledgeSearchTool","args":{"query":"退款政策","top_k":5}}
{"tool":"DocumentSummaryTool","args":{"document_id":"文档 UUID","focus":""}}
{"tool":"DataAnalysisTool","args":{"date_from":"2026-01-01","date_to":"2026-12-31","group_by":"category"}}
```

工具参数拒绝未知字段及类型转换（例如 top_k 的字符串或布尔值）。检索 top_k 为 1–8，kb_ids 最多 20 个不重复 UUID，query 最多 2,000 字。统计 group_by 只允许 month/product/category，日期为 YYYY-MM-DD、时间跨度最多 366 天，product 可选且最多 80 字；使用固定 ORM 聚合，不接受 SQL。统计 total 为范围内完整计数，groups 最多 50 组，超出时 groups_truncated 为 true。摘要为有来源的原文摘录，最多检查 80 个 Chunk、返回八个片段（每片段最多 500 字）；focus 最多 500 字，用词项匹配排序，truncated 提示未覆盖完整文档。

## S4 Agent 流与审计

Agent 绑定已有会话及 kb_ids 范围，不接受客户端 tenant_id/user_id/工具白名单等覆盖。最多执行四次工具，第五轮模型仅能收尾。默认单工具 10 秒、总执行 60 秒；超时或取消停止子进程/HTTP 后完成审计和额度清理。Run.kind 参与幂等指纹，同一键跨 RAG/Agent 使用返回 409。

Agent SSE 为 meta、零到多组 tool、references、delta、done；失败可发送 error，终态仍以 done 或 GET run 为准。meta 另含 kind、provider、limits；tool 内容仅为 step/name/status，以及完成时的 call_id/duration_ms。资料只通过 references/受授权的引用接口提供，不输出模型推理内容。

```text
event: tool
data: {"step":1,"name":"KnowledgeSearchTool","status":"running"}

event: tool
data: {"step":1,"name":"KnowledgeSearchTool","status":"completed","call_id":"...","duration_ms":640}

event: references
data: {"sources":[{"id":"...","source_id":"S1","kind":"analysis","title":"演示业务统计","version":null,"locator":{"captured_at":"..."}}]}
```

上述事件示意覆盖不同工具的字段，不表示同一调用同时产生文档和统计结果。文档引用沿用 GET `/references/{id}`；kind=analysis 使用 GET `/agents/references/{id}`，响应同样提供 excerpt/locator，excerpt 是带查询范围、分组及采集时间的 JSON 快照。引用编号统一为 S1、S2 等。租户 ACL epoch 变化会保守隐藏旧统计快照及包含该快照的历史答案。

规划和最终回答均使用有界非流式 Chat Completions JSON 请求；通过最终引用与权限校验后，服务端将正文拆分为 delta 展示。这里的 SSE 不表示上游逐 Token 生成。usage.source 仅在所有调用均有供应商 usage 时为 provider，否则为 unknown；quota_tokens 是账本实际扣记额，包含未知用量策略，不等于测得的供应商 Token。

GET run 的 tool_calls 和 GET `/agents/calls` 提供工具名、状态、步骤、错误码、耗时、脱敏参数与 trace_id；不返回原始问题、focus 或模型推理。显式单工具接口也使用相同的严格校验与进程级期限。配置、停止原因和验收范围见 [受控 Agent](controlled-agent.md)。


## S3 评测与复核

POST `/evaluations/runs` 接受 dataset_id、baseline（B0/B1/B2）、split（development/tuning/holdout）、answers、rewrite。分层数据集默认 tuning，生成回答默认关闭；原 mode 字段保留向后兼容。

GET `/evaluations/runs/{id}` 返回配置、逐题标准证据、实际回答、来源、原始片段位次、指标、分段耗时与复核记录；仅允许创建者，且当前请求者和原题身份都必须仍能读取相关证据，否则隐藏。该响应可直接保存为 JSON 报告。

PUT `/evaluations/runs/{id}/results/{result_id}/review` 接受 answer_pass、refusal_correct（可为 null）、facts_total、facts_supported、cited_claims_total、cited_claims_supported、notes。仅允许已完成且生成过回答的评测；支撑数量不超过总数，notes 必填。服务端记录 reviewer_id/reviewed_at 并刷新汇总。未完成复核时总体通过率为 N/A。

检索 mode 为 dense/hybrid；rewrite/rerank 为布尔开关，服务端决定供应商和地址。sources 中包含版本 ID、原文 SHA、定位、RRF 和可选重排分数；diagnostics 包含本次参数、授权候选排名及阶段耗时，分数不是答案置信概率。
