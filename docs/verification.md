# 验证记录

日期：2026-11-06；环境：Windows、Python 3.12.13、Node.js 22.21.0。完整 Python 依赖固定于 `backend/uv.lock`，前端固定于 `frontend/package-lock.json`。

| 检查 | 实际结果 |
| --- | --- |
| `manage.py migrate`、`check` | SQLite 全部迁移成功，系统检查无问题 |
| `makemigrations --check --dry-run` | 模型与已提交迁移一致 |
| `ruff check`、`ruff format --check` | 通过 |
| `pytest -q` | SQLite 45 项通过；增加队列/存储停机健康检查及发布失败补投测试 |
| PostgreSQL 后端 CI | 44 项通过、1 项跳过；跳过仅适用 SQLite 的本地备份测试 |
| `npm run build` | TypeScript 检查与 Vite 生产构建通过 |
| `npm test` | 2 条 SSE 客户端协议测试通过（UTF-8 边界、CRLF 拆包、合包、心跳） |
| `npx playwright test` | 1 条浏览器端完整业务测试通过 |
| 本地备份恢复 | 自动化测试验证 SQLite integrity_check、文档条数及原文件 SHA-256 |
| 20 题开发集 Dense/Hybrid | 两配置均完整运行；原始逐题结果见 evals/reports，使用 demo provider，不代表语义质量 |
| 完整 Compose CI | 从空卷启动完整服务组合，八组业务检查全部通过 |
| 真实依赖故障演练 | Redis/MinIO 分别停机时返回 unready，活性保持，重启后就绪恢复 |

浏览器实际操作包括：登录 → 检索退款问题 → 流式问答 → 打开引用原文 → 上传 Markdown 并等待发布 → 删除测试文档 → 执行评估 → 检查 390px 手机布局。没有页面脚本错误，手机页面没有横向溢出。

已保存截图：`docs/demo/workspace.png`、`answer-reference.png`、`mobile.png`。浏览器测试会产生少量真实演示会话及评估记录；测试上传的文档软删除，保留审计记录。

本机没有 Docker。完整容器组合已在 GitHub Linux runner 实际运行：PostgreSQL 17.8、pgvector 0.8.1、Redis 7.4.12、独立 Celery worker/beat、源码构建的 MinIO、Nginx/Vue。应用使用无 schema CREATE 权限的独立账号。

部署链路验证包括授权文件上传/下载、真实 worker 发布、pgvector/FTS、经 Nginx 的 SSE/幂等/引用、撤权后整个历史正文隐藏、失败构建保留旧版，以及一次被故意丢弃的投递由 beat 自动恢复。详细结果见 [容器 CI](https://github.com/ha0018/KnowFlow/actions/runs/42160359237) 与 [已保存报告](benchmarks/infrastructure-2026-11-06.json)。

真实模型供应商没有配置，不声明真实 TTFT、Token、费用或回答质量。

项目技术方案的 A01–A15 是后续完整验收目标；本次有限测试没有替代 100 条保留集、20 次故障循环、10,000 Chunk 压测或生产部署认证。
