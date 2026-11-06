# S1：真实基础设施验收

本阶段验证部署组件与权限/任务闭环，模型保持 `demo`。验证命令创建带 `verify-` 前缀的独立租户及资料，应在专门的验收环境运行。

## 启动与验证

1. 复制 `.env.example` 为 `.env`，生成并填入随机 `SECRET_KEY`、`POSTGRES_PASSWORD`、`POSTGRES_APP_PASSWORD`、`MINIO_ROOT_PASSWORD`。
2. 将 `ALLOWED_HOSTS` 设置为 `localhost,127.0.0.1,backend,frontend`，本地 HTTP 验收保留 `SECURE_SSL_REDIRECT=false`。
3. 在已安装 Docker Compose 的环境执行：

```powershell
docker compose --env-file .env -f deploy/compose.yaml build
docker compose --env-file .env -f deploy/compose.yaml up -d --wait --wait-timeout 240
docker compose --env-file .env -f deploy/compose.yaml exec -T backend uv run --no-sync python manage.py verify_stack --report /tmp/infrastructure-report.json
docker compose --env-file .env -f deploy/compose.yaml exec -T backend cat /tmp/infrastructure-report.json
python scripts/verify-infrastructure.py
```

最后一个脚本会依次停止并恢复 Redis、MinIO，确认就绪检查返回 503、活性仍返回 200，并保存 `.runtime/dependency-faults.json`。不要对正在使用的环境执行此故障演练。

`verify_stack` 检查：依赖就绪、独立 worker、pgvector 版本、应用账号拒绝 DDL、跨租户数据库写入被拒、真实后台上传/发布、私有对象与授权下载、授权检索、经 Nginx 的 SSE 与幂等、撤权隐藏整个历史正文、失败重建保留旧版本、beat 补投一次模拟丢失的消息。

验收报告不含密码、密钥或原文，保留运行修订、检查状态、组件版本、资源 ID 和耗时。报告耗时用于排查，不作为技术方案的业务性能验收。

## 数据库账号

`knowflow` 用于数据库初始化和迁移；后端、worker、scheduler 使用 `knowflow_app`，只有业务表读写、序列使用及 schema USAGE 权限。应用角色没有 superuser/createdb/createrole/bypassrls 或 schema CREATE 权限。迁移密码不注入应用容器；迁移创建的后续表由 default privileges 自动授予应用角色读写。

对之前已经创建的持久卷，初始化脚本不会自动重跑。备份后可显式执行以下幂等脚本，为现有数据库增加/更新应用角色；不删除数据：

```powershell
docker compose --env-file .env -f deploy/compose.yaml exec postgres sh /docker-entrypoint-initdb.d/10-app-role.sh
```

## 镜像与源码

基础镜像固定 digest，清单见 `deploy/images.lock.json`。MinIO 上游改为源码分发，旧镜像拉取已不可用；现在从固定 release 对应 commit 构建，远程源码包验证 SHA-256，镜像包含上游许可证和来源标签。服务端代码未修改。

参考：[MinIO 上游分发说明](https://github.com/minio/minio#source-only-distribution)、[Compose 就绪依赖](https://docs.docker.com/compose/how-tos/startup-order/)、[Celery 配置](https://docs.celeryq.dev/en/stable/userguide/configuration.html)、[S3 存储配置](https://django-storages.readthedocs.io/en/stable/backends/amazon-S3.html)。

## CI 证据

`.github/workflows/infrastructure.yaml` 在干净的 GitHub runner 上构建整个栈，执行上述业务和故障检查，并上传 `infrastructure-verification` artifact（报告、镜像 ID、容器状态、服务日志）。运行完成前本阶段仍标记为“实现中”，通过后再更新开发计划与验证记录。
