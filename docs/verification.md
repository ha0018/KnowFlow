# 验证记录

2026-11-13 的 S5 原始证据见 [S5 报告](benchmarks/delivery-2026-11-13.md)：基线 Linux SQLite/PostgreSQL 各 170 通过、2 跳过，Windows 168 通过、4 跳过，四个浏览器场景通过。新增 31 项基线测试覆盖成员/角色/ACL、解析子进程/Linux 限制、上传租约/保留期清理、归档损坏及恢复保护。160 项授权、50/20 份文件、10 次重投、10 次版本故障、20 次取消/断开、20 个追踪样本通过；20 次依赖故障恢复和五次真实 Worker SIGKILL 恢复通过，后者从人为提前租约到期起计。联合快照恢复到全新部署，93 文件/35 表摘要匹配且十组业务通过，临时受信 localhost 证书的 HTTPS 四项通过。末次扫描窗口修正及回归记录在报告。

实际十分钟持续并发测量保留全部 11,858 个请求：20 API 用户的 P95 为 1,816.70 ms、10 检索用户为 2,601.22 ms，全部成功但两项延迟均未通过。CI 绿色表示验证/测量成功执行，不表示所有目标通过。[A01–A15 状态](acceptance.md) 保留性能未达标与真实模型/人工待验收项；[演示](demo/walkthrough.md) 附约 2 分 24 秒真实录屏。

S5 最终回归包含新增的 90 秒心跳/30 秒扫描边界：Linux SQLite/PostgreSQL 均 171 通过、2 跳过（S5 新增共 32 项），前端构建/SSE 与 S3 调参/安全回归通过；原始 JUnit 和运行链接见 S5 报告。本机迁移/系统检查通过，清理命令仅 dry-run，最新后端 ready、前端 HTTP 200。

最终修订的完整容器回归通过；第二轮十分钟负载的 12,374 次请求全部成功，API/检索 P95 1,744.66/2,496.95 ms，两项延迟仍未达标。首次基线与最终回归均原样归档，没有择优替换。另从空 SQLite 执行 README 迁移和 seed_demo，8 份文档全部 READY；独立服务的十项实际 HTTP 业务检查通过，见 S5 空环境报告。

2026-11-13 的 S4 记录及原始 JUnit 见 [S4 报告](benchmarks/controlled-agent-2026-11-13.md)：SQLite/PostgreSQL 各 139 通过、2 跳过，Ruff/系统检查/迁移一致性、前端生产构建和 2 项 SSE 协议测试通过；三个浏览器场景全部通过。新增 50 项 Agent 专项测试包含 31 条非法参数，以及越权、四次执行上限、重复循环、真实子进程硬超时/取消、原生 HTTP 超时/取消及结算。完整容器十组业务检查和 Redis/MinIO 两项故障恢复通过；S3 调参、安全矩阵及索引诊断回归通过。新截图：[受控 Agent](demo/controlled-agent.png)。使用规则演示和实际 socket 协议替身，没有真实供应商质量结论。

2026-11-10 的 S3 记录及原始证据见 [S3 报告](benchmarks/retrieval-quality-2026-11-10.md)：SQLite/PostgreSQL 各 89 通过、2 跳过；前端构建与协议测试通过；浏览器业务和 S3 查看逐题/复核保存/导出/手机布局两场景通过，临时额度已恢复。固定 60 题保留集 B0/B1/B2 各组错误 0；独立 50 项安全矩阵、两种规模/选择率的真实 HNSW 诊断与完整容器九组业务/两项故障恢复通过。

S3 使用演示模型，人工语义指标保持 N/A，没有替代真实模型及正式负载验收。新截图：[评测配置](demo/retrieval-quality.png)、[逐题复核](demo/quality-case-review.png)。以下各节保留 2026-11-06 的 S1/S2 历史基线。

日期：2026-11-06；环境：Windows、Python 3.12.13、Node.js 22.21.0。完整 Python 依赖固定于 `backend/uv.lock`，前端固定于 `frontend/package-lock.json`。

| 检查 | 实际结果 |
| --- | --- |
| `manage.py migrate`、`check` | SQLite 全部迁移成功，系统检查无问题 |
| `makemigrations --check --dry-run` | 模型与已提交迁移一致 |
| `ruff check`、`ruff format --check` | 通过 |
| SQLite 后端 CI | 74 项通过、2 项跳过；跳过仅适用 PostgreSQL 的两个并发测试 |
| PostgreSQL 后端 CI | 75 项通过、1 项跳过；跳过仅适用 SQLite 的本地备份测试 |
| `npm run build` | TypeScript 检查与 Vite 生产构建通过 |
| `npm test` | 2 条 SSE 客户端协议测试通过（UTF-8 边界、CRLF 拆包、合包、心跳） |
| `npx playwright test` | 1 条浏览器端完整业务测试通过 |
| 本地备份恢复 | 自动化测试验证 SQLite integrity_check、文档条数及原文件 SHA-256 |
| 20 题开发集 Dense/Hybrid | 两配置均完整运行；原始逐题结果见 evals/reports，使用 demo provider，不代表语义质量 |
| 完整 Compose CI | 从空卷启动完整服务组合，八组业务检查全部通过 |
| 真实依赖故障演练 | Redis/MinIO 分别停机时返回 unready，活性保持，重启后就绪恢复 |

浏览器实际操作包括：登录 → 检索退款问题 → 流式问答 → 打开引用原文 → 上传 Markdown 并等待发布 → 删除测试文档 → 执行评估 → 检查生成/向量服务 → 修改并恢复每日额度 → 检查 390px 手机布局。没有页面脚本错误，手机页面没有横向溢出。

已保存截图：`docs/demo/workspace.png`、`answer-reference.png`、`mobile.png`。浏览器测试会产生少量真实演示会话及评估记录；测试上传的文档软删除，保留审计记录。

S2 新增 [模型与额度界面截图](demo/models-and-quota.png)。[自动检查](https://github.com/ha0018/KnowFlow/actions/runs/42177509836) 覆盖独立 endpoint/key、schema 维度、429/Retry-After、断流不重放、总超时、空回复、取消结算、未知用量/价格、过期预留、跨租户关联及授权历史。真实 PostgreSQL 并发用例验证不会超额准入或重复结算。

[S2 容器验收](https://github.com/ha0018/KnowFlow/actions/runs/42177509880) 九组业务检查及 Redis/MinIO 两项故障恢复全部通过，包含新增用量账本、预留无遗留和零额度请求拒绝。原始报告已 [保存到仓库](benchmarks/model-resources-2026-11-06.json)。

另通过实际 HTTP socket 启动受控协议服务验证探测命令、认证、SSE 和向量超时；该服务返回确定性测试结果，不是实际 LLM/供应商。

本机没有 Docker。完整容器组合已在 GitHub Linux runner 实际运行：PostgreSQL 17.8、pgvector 0.8.1、Redis 7.4.12、独立 Celery worker/beat、源码构建的 MinIO、Nginx/Vue。应用使用无 schema CREATE 权限的独立账号。

部署链路验证包括授权文件上传/下载、真实 worker 发布、pgvector/FTS、经 Nginx 的 SSE/幂等/引用、撤权后整个历史正文隐藏、失败构建保留旧版，以及一次被故意丢弃的投递由 beat 自动恢复。详细结果见 [容器 CI](https://github.com/ha0018/KnowFlow/actions/runs/42160359237) 与 [已保存报告](benchmarks/infrastructure-2026-11-06.json)。

真实模型供应商没有配置，不声明真实 TTFT、Token、费用或回答质量。

以上 S1/S2 历史基线的有限测试不替代完整验收。后续 S3/S5 已交付 100 条分层题（40 调参/60 保留）、20 次依赖故障及 10,000 Chunk 负载测量，结果和边界见页首报告；没有完成生产部署认证或全部真实模型/人工验收。
