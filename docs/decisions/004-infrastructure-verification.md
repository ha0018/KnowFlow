# ADR 004：用独立容器环境验证部署链路

状态：采用。

本机未安装 Docker。GitHub Actions 的 SQLite/PostgreSQL 测试矩阵已通过，但该测试使用 inline 任务及本地文件，不能证明 Redis、独立 worker、MinIO 和 Nginx 可联合工作。因此增加独立的 Compose 工作流，从空卷启动，实际执行业务链路与依赖故障恢复，并保留报告和日志。

迁移账号保留 DDL 能力；运行账号仅获业务数据读写权限，迁移密码不进入应用环境。发布失败由数据库清单恢复，生产者连接使用有限超时，worker 独立重连。存储客户端同样限制连接和读取超时，使依赖不可用时就绪检查能及时返回 503。

原 MinIO 镜像在实际 Registry 检查中无法拉取。上游已转为源码分发，因此固定 release 对应的 commit 和源码包 SHA-256 构建 MinIO，基础镜像固定 digest，并缓存构建结果。保留上游许可证与来源信息，不替换存储协议或服务实现。[上游说明](https://github.com/minio/minio#source-only-distribution)

首次部署演练还暴露了运行目录权限问题：`uv sync` 在构建时由 root 写入缓存，应用切换普通用户后无法打开 `/home/knowflow/.cache/uv/sdists-v9/.git`，迁移退出。修复将应用目录和运行缓存归属统一移交给普通用户，并在 Dockerfile 的 `USER` 切换后实际运行 Python 验证。这个发现来自容器演练，不是生产事故。

所有验证使用独立样本租户和演示模型，测试凭据每次随机生成。组件联调成功后仍需单独进行真实模型质量、并发、压力、备份恢复及生产运行验收。
