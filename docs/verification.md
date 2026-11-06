# 本机验证记录

日期：2026-11-06；环境：Windows、Python 3.12.13、Node.js 22.21.0。完整 Python 依赖固定于 `backend/uv.lock`，前端固定于 `frontend/package-lock.json`。

| 检查 | 实际结果 |
| --- | --- |
| `manage.py migrate`、`check` | SQLite 全部迁移成功，系统检查无问题 |
| `makemigrations --check --dry-run` | 模型与已提交迁移一致 |
| `ruff check`、`ruff format --check` | 通过 |
| `pytest -q` | 42 项通过；涵盖租户隔离、ACL、任务重复/租约/fencing、发布竞争、解析边界、流式、JWT、工具、评估、备份、模型协议及健康检查 |
| `npm run build` | TypeScript 检查与 Vite 生产构建通过 |
| `npm test` | 2 条 SSE 客户端协议测试通过（UTF-8 边界、CRLF 拆包、合包、心跳） |
| `npx playwright test` | 1 条浏览器端完整业务测试通过 |
| 本地备份恢复 | 自动化测试验证 SQLite integrity_check、文档条数及原文件 SHA-256 |
| 20 题开发集 Dense/Hybrid | 两配置均完整运行；原始逐题结果见 evals/reports，使用 demo provider，不代表语义质量 |

浏览器实际操作包括：登录 → 检索退款问题 → 流式问答 → 打开引用原文 → 上传 Markdown 并等待发布 → 删除测试文档 → 执行评估 → 检查 390px 手机布局。没有页面脚本错误，手机页面没有横向溢出。

已保存截图：`docs/demo/workspace.png`、`answer-reference.png`、`mobile.png`。浏览器测试会产生少量真实演示会话及评估记录；测试上传的文档软删除，保留审计记录。

Docker、PostgreSQL、Redis、MinIO 在本机没有运行，远程 CI 尚未执行。Compose 配置和数据库专用迁移已提供，但不把配置存在当作集成验收成功。真实模型供应商没有配置，不声明真实 TTFT、Token、费用或回答质量。

项目技术方案的 A01–A15 是后续完整验收目标；本次有限测试没有替代 100 条保留集、20 次故障循环、10,000 Chunk 压测或生产部署认证。
