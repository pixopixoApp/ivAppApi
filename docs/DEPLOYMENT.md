# ivapp 部署与回滚

## 1. 目标环境

默认配置与 2026-08-08 的线上环境一致：

- SSH：`root@123.56.218.5:22`
- 项目：`/opt/play_video/ivapp`
- Compose project：`ivapp`
- API service：`api`
- 创作协调 Worker service：`worker`（单实例、全局并发 1；只调用 ivadmin 私有 API）
- 健康检查：`http://127.0.0.1:8100/health`
- Compose 命令：`docker-compose`（不是 `docker compose`）

服务器必须已有真实 `/opt/play_video/ivapp/.env` 和数据库/Redis `volumes/`。媒体切换严格按 [OSS 手册](OSS_MEDIA_MIGRATION.md) 执行。

## 2. 本地配置

```bash
cd /Users/clifford/Desktop/MotionCue/ivapp-backend
cp .deploy.env.example .deploy.env
```

只有目标主机、端口、路径等非敏感覆盖项放入 `.deploy.env`。应用密码、SMTP 密码、Google 配置和 Publish Key 始终只保存在服务器 `.env` 或后续接入的密钥系统中。

## 3. 发布前检查

```bash
./scripts/check.sh
./scripts/server_status.sh
./scripts/deploy.sh --dry-run
```

默认禁止从有未提交改动的工作树发布。确有需要时可显式传 `--allow-dirty`，但该发布将难以用 Git 精确追溯。

## 4. 正式发布

```bash
./scripts/deploy.sh
```

首次引入 `runtime_spec` 或明确需要重编译全部历史作品时使用：

```bash
./scripts/deploy.sh --backfill-runtime-specs
```

该开关会在切换 API 前依次执行 dry-run 与 apply。普通发布不会自动重编译旧作品，避免编译规则变化悄悄改变线上内容。

脚本顺序：

1. 本地 Python/Shell/Compose 基础检查。
2. SSH 预检线上目录、`.env`、`docker-compose`、`rsync`、`curl`。
3. 将代码同步到 `/opt/play_video/releases/ivapp/<UTC时间>-<Git短SHA>`。
4. 在 release 目录中读取线上 `.env`，先校验 Compose 并构建 `api` 镜像；此时线上容器未改变。
5. 把当前线上源码复制到 `/opt/play_video/backups/ivapp/<同一ID>`，并保存部署前 MySQL dump。
6. 同步新源码到线上目录；`.env`、`.git`、`volumes` 和缓存均被排除。
7. 使用新镜像执行 `alembic upgrade head`；可选显式回填历史 runtime spec。
8. 强制重建 `api`，轮询健康检查，再强制重建单实例 `worker`；不重建 MySQL、Redis、phpMyAdmin。
9. API 失败时自动恢复源码快照、重建旧镜像并重启 API。

纯源码变化可使用：

```bash
./scripts/deploy.sh --no-build
```

只有在确认 `requirements.txt`、Dockerfile、系统依赖、Compose 服务和 Python 依赖均未变化时才应使用；首次新增 worker 时不可使用。

## 5. 查看状态和日志

```bash
./scripts/server_status.sh
./scripts/server_status.sh --logs 200
```

日志可能包含邮箱或 token 前缀，只应在受信环境中查看，不应复制到公共工单。

## 6. 人工源码回滚

```bash
./scripts/rollback.sh --list
./scripts/rollback.sh 20260808T120000Z-abcdef123456
```

回滚会：

- 用指定快照恢复源码和 Compose 配置；
- 保留线上 `.env` 和全部 `volumes`；
- 从旧源码重建 API 镜像；
- 只重建 API 并检查健康状态。

## 7. 数据库恢复边界

源码回滚不会自动恢复数据库。原因是发布切换后可能已经产生新用户写入，自动覆盖数据库会造成额外数据丢失。

每次正式部署前会在对应 backup 目录保存 `database.sql.gz`。只有在确认新版本执行了不兼容迁移、评估停机窗口并备份当前故障现场后，才人工恢复。恢复步骤应由两人复核，且先在临时数据库验证 dump。

当前脚本没有备份 OSS 媒体和 Redis。应另外建立：

- `ivapp-media/v1/` 的跨 bucket 校验复制（对象永久保留）；
- MySQL 每日全量 + binlog/PITR；
- Redis AOF 备份（曝光数据可按业务容忍度决定）；
- 定期恢复演练和保留策略。

## 8. 安全注意事项

- 线上 `.env` 必须为 `0600`；正式发布会修正权限，但第一次发布前也应手工处理。
- 不要把服务器旧 `.git`、真实 `.env` 或 `volumes` 拷回本地仓库。
- 8101 的 phpMyAdmin 不应公开到互联网；优先绑定 `127.0.0.1` 或通过 VPN/SSH 隧道访问。
- ivapp 只保存访问 ivadmin 私有创作接口的 `CREATOR_INTERNAL_KEY`；Dify 与模型密钥仅属于 ivadmin，不得进入 ivapp 配置、响应、日志或源码。
- `X-Publish-Key` 只能通过 TLS 传输，并应定期轮换。
- release 和 backup 默认不自动删除；启用清理策略前必须同时满足保留数量、最近成功恢复点和异机备份要求。
