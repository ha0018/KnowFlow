# S4 受控 Agent 验证报告

日期：2026-11-13（Asia/Shanghai）。代码修订：`94aa3dfc77e7699dcbbdabcff9a23e74c5fd37d4`，分支 `feat/controlled-agent`。机器可读汇总见 [JSON](controlled-agent-2026-11-13.json)，操作与边界见 [受控 Agent](../controlled-agent.md)。本报告使用规则演示和本地 HTTP 协议替身；真实供应商、人工语义支撑及正式负载未验收。

## 实际结果

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| SQLite | 139 通过、2 跳过、0 失败 | [JUnit](s4-2026-11-13/backend-sqlite.xml) |
| PostgreSQL 17/pgvector | 139 通过、2 跳过、0 失败 | [JUnit](s4-2026-11-13/backend-postgres.xml) |
| Ruff、Django check、迁移一致性 | 全部通过 | [checks CI](https://github.com/ha0018/KnowFlow/actions/runs/43353473323) |
| 前端 | TypeScript/Vite 构建及 2 项 SSE 协议测试通过 | 同一 checks CI 的 frontend job |
| 新增 S4 专项 | 50 项在两种数据库均通过，其中 31 条非法 schema，4 项实际 HTTP socket 协议测试 | [逐项矩阵](s4-2026-11-13/agent-test-matrix.json) |
| 浏览器 | 全部 3 个场景通过，页面脚本错误 0，390px 无横向溢出 | [本地运行记录及 spec 哈希](s4-2026-11-13/browser-verification.json)、[新截图](../demo/controlled-agent.png) |
| 完整部署 | 10 组业务检查通过，3 个 Agent 工具均经 Nginx 完成且引用可读 | [Compose CI](https://github.com/ha0018/KnowFlow/actions/runs/43353473392)、[原始报告](s4-2026-11-13/infrastructure-report.json) |
| 依赖停机恢复 | Redis、MinIO 各 1 项通过 | [原始报告](s4-2026-11-13/dependency-faults.json) |
| S3 回归 | 调参集 B0/B1/B2、安全矩阵 50 项及精确/HNSW 诊断通过 | [retrieval-quality CI](https://github.com/ha0018/KnowFlow/actions/runs/43353473337)、[安全矩阵](s4-2026-11-13/retrieval-security.json) |

SQLite 跳过两项仅适用 PostgreSQL 行锁的并发测试，PostgreSQL 实际执行它们。PostgreSQL 跳过 SQLite 本地备份及 portable cosine 实现两项，SQLite 实际执行它们。S4 新增 50 项没有跳过。

S3 本次仅运行既定 tuning 回归，没有重新调优或执行 holdout；原 S3 保留集报告保持不变。本地全量后端同样为 139 通过、2 跳过。浏览器覆盖原工作台链路、S3 逐题复核/导出，以及新增 S4 检索/摘要/统计/两种引用抽屉；使用本地真实 Uvicorn/Vite 与现有演示数据。

## S4 专项覆盖

31 条参数用例按测试中的稳定 ID 记录实际输入与结果，均断言调用子进程创建函数前拒绝：

| ID | 输入类别 |
| --- | --- |
| invalid-01–04 | Shell/Python/SQL/URL 非白名单工具 |
| invalid-05–07 | 检索伪造 tenant_id/user_id 及 URL 字段 |
| invalid-08–12 | top_k 为布尔/浮点/字符串/0/9 |
| invalid-13–16 | kb_ids 非数组、非字符串、非法 UUID、非法重复字符串 |
| invalid-17–18 | 空查询、数值查询 |
| invalid-19–23 | 重复 JSON 键、NaN、非 JSON、数组、null 参数 |
| invalid-24–26 | 摘要非法 UUID、数值 ID、额外文件 path |
| invalid-27–31 | 统计日期倒置/过长、非法分组/日期、伪造租户 |

其余 19 项覆盖三个工具的实际子进程闭环、跨租户引用拒绝、会话摘要范围、循环及第五次调用拒绝、整轮 Token 准入、真实子进程超时/取消、模型总期限、所有者停止及 meta 后立即断连、SSE 幂等与跨模式冲突、数据库 actor/run.kind 约束、撤权期间最终回答丢弃、旧统计历史与引用隐藏，以及四项原生 HTTP 协议验证。完整测试名称与两种数据库结果均保存在逐项矩阵中。

硬超时测试启动实际 Python 子进程，验证到期 kill 后不会写入延迟标记；取消测试验证真实进程终止。工具期限默认 10 秒、整轮活动执行默认 60 秒，测试使用更短可配置期限复现。终止后的数据库审计、结算及进程回收时间另计，测试耗时不作为生产延迟或正式性能结论。

HTTP 测试使用实际 socket 服务，验证严格 function-calling 请求、独立规划用量、并行建议在工具执行前拒绝、模型超时及更短总期限取消、未知用量结算；服务返回确定性响应。它证明协议和中断路径，不证明任何真实模型的自主规划质量。

## 部署与证据保全

本机没有 Docker。GitHub Linux runner 从空部署启动 PostgreSQL 17.8/pgvector 0.8.1、Redis 7.4.12、独立 Celery worker/beat、MinIO 和 Nginx/Vue，应用使用受限数据库账号；[镜像记录](s4-2026-11-13/images.json) 保留实际 ID。Agent 通过三个工具的进程闭环，未遗留 reserved 额度；原文上传/异步发布、RAG/幂等/引用、撤权、失败重建、丢投递补投及零额度拒绝均通过。

最终代码 CI 的四份归档均验证 GitHub 提供的 ZIP SHA-256 后，仅读取固定名称的报告；[JSON 汇总](controlled-agent-2026-11-13.json) 保存归档 ID、校验值和每份原始文件的 SHA-256。没有提交服务日志、凭据或临时签名下载链接。浏览器记录和专项矩阵由本地实际运行状态/原始 JUnit 派生，明确标记了来源修订。

## 验收边界

S4 的受控执行软件与自动验证已交付。真实供应商原生工具调用兼容性、多步任务选择/完成率、实际取消 usage、Token 估算校准、事实/引用语义支撑和拒答/注入效果仍待配置后验收。引用编号有效只证明来源存在且当前可见，不证明回答事实均被支撑。

此报告没有替代 A01–A15 的完整验收或正式并发压测。下一阶段为 S5 的成员/ACL 界面、解析隔离、数据清理、部署备份恢复与完整交付验收。
