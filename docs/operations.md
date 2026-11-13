# 部署、解析与数据运维

适用于 S5 交付版本。默认本地 SQLite 路径见 README；本节的联合备份和恢复使用 PostgreSQL、私有对象存储和 Docker Compose。命令从仓库根目录运行。验收状态以最终报告为准。

## 启动和升级

从 `.env.example` 创建 `.env`，配置随机 SECRET_KEY、迁移/应用数据库密码、对象存储密码、实际域名和模型 profile。部署使用受限的 knowflow_app 账号，迁移使用独立账号。不要把环境文件、备份和证书私钥提交到仓库。

```powershell
docker compose --env-file .env -f deploy/compose.yaml up --build -d --wait
docker compose --env-file .env -f deploy/compose.yaml exec backend uv run --no-sync python manage.py check
docker compose --env-file .env -f deploy/compose.yaml exec backend uv run --no-sync python manage.py seed_demo --password '自行设置的演示密码'
```

升级前创建备份，使用匹配代码版本的应用镜像，再执行迁移和就绪检查。S5 新增 documents 0004：上传对象租约/孤儿清理清单及其归属约束。部署元数据可设置 CODE_REVISION 为当前 Git SHA；备份对象清单记录该值与 Embedding profile。真实模型、更改向量维度和数据升级仍按模型接入说明操作。

应用容器采用只读根文件系统、256 MiB `/tmp` tmpfs、2 GiB 内存上限、256 个进程上限、移除 Linux capabilities 和 no-new-privileges。文件存储在私有对象存储中，应用仅使用临时目录处理输入。不同环境需要按实际并发调整容量并重新测量。

## 成员、角色和资料授权

管理员在“成员与角色”创建普通成员、分配角色、更新角色权限和停用/启用账号。不能通过成员接口提升管理员或停用当前账号。操作权限决定能否执行 Agent、评测等功能；资料读取另受知识库和文档 ACL 约束。状态/角色修改写审计并递增租户 ACL epoch。

有管理权限的用户可在“知识空间”打开知识库权限，整体保存私有/租户可见范围及用户/角色授权；两者在一个事务中更新。文档权限支持继承或进一步收紧，不能扩大知识库范围。知识库所有者也需要满足受限文档的 ACL，租户管理员保留管理能力。授权保存后重新检查当前权限，撤权影响检索、历史和引用。

普通 API 默认每账号 120 次/分钟，由 API_USER_RATE 配置。停止运行采用独立的 run-cancel 范围，每账号 60 次/分钟，避免普通额度耗尽后无法停止；会话/运行仍只限所有者。

## 解析资源隔离

PDF/DOCX 的结构解析、ZIP 目录检查及正文解码均在固定入口子进程中进行；HTTP 只检查基本类型、大小、签名和文本编码。子进程使用 Python isolated 模式，不加载应用设置、数据库、模型或存储凭据；Python 审计钩子拦截 socket、subprocess.Popen 等外部调用。

| 配置 | 默认 | 范围 |
| --- | --- | --- |
| PARSER_TIMEOUT_SECONDS | 15 秒 | 0.1–120 秒，父进程墙钟期限 |
| PARSER_CPU_SECONDS | 10 秒 | 1–120，Linux CPU 时间限制 |
| PARSER_MEMORY_MB | 512 MiB | 128–2048，Linux 地址空间限制 |
| PARSER_OUTPUT_BYTES | 16 MiB | 1 KiB–32 MiB，JSON 输出限制 |

Linux 子进程另限制文件输出、打开文件数并关闭 core dump。Windows 开发环境执行墙钟、输出和取消限制，CPU/地址空间限制仅在 Linux 验收。期限触发后 kill/wait，清理与操作系统回收耗时另计；原有任务 fencing、当前权限和发布检查仍生效，失败不会替换旧 active build。此机制限制文档解析资源，不提供任意代码的通用沙箱。

常见错误：PARSER_TIMEOUT、PARSER_RESOURCE_LIMIT、PARSER_OUTPUT_LIMIT、INVALID_DOCUMENT、OCR_REQUIRED、ENCRYPTED_PDF、EMPTY_FILE/EMPTY_TEXT。空文件在上传时返回明确错误；通过基本检查但结构无效的文件进入 FAILED 任务，错误码保留。Windows 文件句柄可能短暂延迟释放，父进程会重试清理；仍被占用时记录 parser_cleanup_deferred，保留原始停止原因。

## 孤儿对象与过期数据

默认只预览，先检查清单再决定是否应用：

```powershell
cd backend
uv run python manage.py cleanup_storage --output ../.runtime/storage-preview.json
uv run python manage.py cleanup_storage --apply --grace-days 7 --output ../.runtime/storage-applied.json
uv run python manage.py cleanup_expired
uv run python manage.py cleanup_expired --apply --parser-files
```

对象清理只处理已知租户下、由上传流程生成的 UUID 文件名，跳过其他命名空间和越界本地路径。每批最多 1,000 个对象，返回 next_cursor；CLI 用 `--after` 继续。应用模式第一次只将未引用对象登记为 candidate；对象修改时间和 first_unreferenced_at 均超过保留窗口后，后续应用才可删除。默认 7 天，最短 1 天。对象大小或修改时间变化会重新计算窗口。

上传在存储写入前取得一小时对象租约，落库前重新验证；清理与上传提交使用相同租户锁，活跃上传不能被清理。所有 DocumentVersion 引用的文件都保留，包括软删除文档和旧版本，避免破坏历史引用或备份。删除孤儿对象记录审计。业务版本、审计和费用账本不按此命令自动物理删除。

每小时任务清理过期 Session/JWT 并回收悬挂运行/额度预留；解析临时目录只清理固定前缀下超过 24 小时、经绝对路径检查的目录，跳过符号链接及仍占用的文件。自动对象删除默认关闭；设置 CLEANUP_STORAGE_ENABLED=true 后按 CLEANUP_STORAGE_GRACE_DAYS 执行，满批通过独立后续任务继续。大量对象仍应分批观察耗时和 next_cursor。

## PostgreSQL 与对象联合备份

```powershell
python scripts/deployment-backup.py backup --project knowflow --env-file .env --output ../private-backups/knowflow-snapshot.zip
```

脚本记录当前运行的 frontend/backend/worker/scheduler，停止这些应用写入者，拒绝迁移中的部署；导出私有对象、规范化业务行摘要和 PostgreSQL custom dump，最后恢复原先运行的服务。备份期间服务有维护窗口，运维需要保证没有其他直接数据库或对象写入者。

归档含 database.dump、objects.zip、manifest.json，逐项校验 SHA-256 与大小；所有版本原文件在导出时与数据库哈希核对。包含密码哈希、Token 与原文，应在私有目录加密保管，完整归档不作为 CI 报告上传。Linux 新归档以 0600 创建。当前对象快照上限 2 GiB、外层导入上限 4 GiB，较大部署需另行设计分片/增量备份。

## 恢复到全新环境

使用与备份匹配的代码、镜像和 Embedding profile，准备恢复环境文件（数据库/对象存储连接、密钥、域名等），保持原部署不变：

```powershell
python scripts/deployment-backup.py restore --archive ../private-backups/knowflow-snapshot.zip --env-file .env.recovery --new-project knowflow-recovery --port 8081 --report .runtime/restoration-report.json
```

目标 Compose project 必须与源不同，且不能已有容器或持久卷；目标对象空间必须为空。脚本先验证归档成员和校验值，在新数据库恢复 schema/数据，再核对业务行摘要、Embedding profile 和全部对象清单，随后导入文件并再次核验原文件 SHA-256，最后启动新应用。不会清空、覆盖或自动删除已有数据库/对象空间。失败时保留新目标供诊断。

恢复报告仅保存表计数、摘要和校验结果。检查 `/health/ready`，使用已有账号执行问答/引用/下载；更换 SECRET_KEY 会使原签名凭据失效，需重新登录。归档应来自受信部署，校验值用于检测损坏，不作为来源认证。SQLite 本地备份/恢复命令继续保留。

数据库备份格式参考 [PostgreSQL 17 pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html)。本项目额外暂停应用写入，以保证数据库快照与私有对象集合相互对应。

## HTTPS

在环境中设置证书绝对路径 TLS_CERT_FILE、TLS_KEY_FILE，以及实际 HTTPS_PORT（默认 443）。证书由部署环境提供，不写入镜像：

```powershell
docker compose --env-file .env -f deploy/compose.yaml -f deploy/compose.tls.yaml up -d --wait
```

Nginx 终止 TLS 1.2/1.3，转发正确的 X-Forwarded-Proto，保留 SSE 无缓冲及 90 秒读取超时；后端启用 HTTPS 重定向和 HSTS。内部 `/health/live`、`/health/ready` 免重定向，以支持 HTTP 容器探针。HTTP 入口重定向到标准 HTTPS 443；使用非标准端口时直接访问该 HTTPS 地址。

CI 使用单独信任的临时 localhost 证书，验证 HTTPS 首页、健康、未认证 API 和 HSTS；不代表已签发公网证书或完成真实域名上线。

## 复现验收

基础设施 workflow 从空环境启动并执行 verify_stack、verify_delivery、20 次依赖停机恢复、5 次真实 Worker 中断、联合备份/新环境恢复、持续并发和 HTTPS 检查。Worker 演练真实 SIGKILL 后人为提前租约到期，再由独立扫描器恢复，恢复时长以该注入点起算，不声称自然六分钟租约的生产恢复时长。

负载用独立容器中的 20 个 API/10 个检索用户持续请求 600 秒，使用 10,000 个自编合成 Chunk、两个租户及冻结哈希向量，保留全部失败和原始 JSONL。CI 显式提高 API 速率和合成租户额度以测量业务耗时，生产默认值不变。结果只适用于记录的实际硬件、并发与语料；真实供应商问答、文档时效和人工语义指标继续单独验收。
