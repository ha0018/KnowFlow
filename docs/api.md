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
| POST `/retrieval/search` | `{query,kb_ids?,top_k?}` → 本次授权的召回片段 |
| GET/POST `/conversations` | 分页会话/创建；`{title?,kb_ids?}` |
| GET `/conversations/{id}/messages` | 当前授权下的历史正文与引用 |
| POST `/conversations/{id}/messages:stream` | `{question,parent_id?}` + `Idempotency-Key`；parent_id 保留再生成关系 |
| GET `/runs/{id}` | 运行状态、部分正文、usage、来源 |
| POST `/runs/{id}/cancel` | 停止当前用户的运行 |
| GET `/references/{id}` | 当前权限下的原文摘录、不可变版本与位置 |
| POST `/messages/{id}/feedback` | `{rating:1或-1,reason?}` |
| GET/POST `/agents/tools` | 工具清单/单个只读工具调用 `{tool,args}` |
| GET `/evaluations/datasets` | 开发集清单 |
| GET/POST `/evaluations/runs` | 运行记录/创建 `{dataset_id,mode:"dense"或"hybrid"}` |
| GET `/monitoring/dashboard` | 授权资源计数 |
| GET `/monitoring/calls` | 本租户最近 100 条调用元数据 |
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

统计 group_by 只允许 month/product/category，时间跨度最多 366 天；使用固定 ORM 聚合，不接受 SQL。摘要为有来源的原文摘录，最多八个片段；focus 预留，当前不改变摘录排序。
