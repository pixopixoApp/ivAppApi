# ivapp backend

Pixo App 与运营后台共用的 FastAPI 后端。项目保留现有 App `head/body` 协议和全部旧 URL，同时补充标准 Bearer 鉴权、新的 REST 能力、可迁移数据库结构与独立创作协调 Worker。

## 服务结构

```text
iOS / Android
  ├─ 旧 App API：POST /video、/profile ...（head/body，兼容 Bearer）
  └─ 新 REST API：/api/v1/*（JSON/Multipart + Bearer）
                         │
                FastAPI API :8100
                  ├─ MySQL 8.4
                  ├─ Redis 7.4
                  └─ OSS ivapp-media/v1（ivapp 独占 AccessKey）
                         │
                creator worker（并发 1）
                  └─ ivadmin 私有任务 API → ivcore

运营后台 / 内部工具
  └─ /internal/v1/* + X-Publish-Key
```

Compose 服务为 `api`、`worker`、`mysql`、`redis`、`phpmyadmin`。phpMyAdmin 仅绑定 `127.0.0.1:8101`，不可直接公网访问。

## 播放协议：持久化是唯一真相

远程 HTML 是并列的 `content_type=html`，不进入 Runtime Spec。它使用受信 HTTPS 不可变内容包、Bridge v1 和 Android 的受控 WebView；本地适配、Playwright 校验、OSS 上传、真机验收与最终发布见 [HTML 互动内容 V1](docs/HTML_CONTENT_V1.md)。

`published_videos.timeline` 保留可编辑的源数据；最终 App 播放数据写入：

- `runtime_spec`：完整的 clip、互动、检测参数和结果动作。
- `runtime_spec_version`：当前为 `1.0`。

发布顺序固定为：校验 source → 编译完整 runtime spec → 校验 runtime spec → 原子写入发布记录。`/video`、`/video_detail`、作品列表只读取持久化的 runtime spec，不会在响应时补默认值、覆盖参数或从 timeline 临时生成。

当前 `1.0` 编译规则：

- 所有互动点持久化 `pause_video=true`。
- 所有互动点持久化 `detection.response_window_ms=0`，即暂停后无限等待用户互动。
- `mic_level`、`mic_blow`：`min_volume_score=55`、`min_duration_ms=300`。
- `mic_clap`：`min_volume_score=55`。
- `mic_quiet` 保持安静语义：`max_volume_score=20`、`min_duration_ms=1000`。
- 未知 gesture 编译失败，不再静默回退为 tap。
- Story 的 clip `on_end` 在发布解析和 runtime spec 中完整保留。

缺少或损坏 runtime spec 的作品不会下发，并记录 error 日志。运营审计：

```bash
curl -H 'X-Publish-Key: ...' http://localhost:8100/internal/v1/runtime-specs/audit
```

历史作品必须显式迁移：

```bash
./scripts/backfill_runtime_specs.sh --dry-run
./scripts/backfill_runtime_specs.sh --apply
```

批量回填是全有或全无；任一作品无法编译时不会修改任何作品。单条显式重编译为 `POST /internal/v1/videos/{video_id}/runtime-spec/recompile`。未来修改编译规则时必须升级 `runtime_spec_version` 并显式重编译，旧作品不会自动变化。

## 协议兼容与鉴权

旧 App API 的 URL、请求 `head/body`、响应 `head/body` 和业务状态 `0/100/101` 保持兼容。

所有需要登录的旧接口同时接受：

```http
Authorization: Bearer <token>
```

若同一请求同时带 Bearer 与 `head.token`，两者必须一致，否则返回 `status=101`。新 `/api/v1/creator/*` 只接受 Bearer；内部接口继续使用 `X-Publish-Key`。

## Feed 与列表分页

- 推荐 `/video` 是无限循环流。请求支持可选不透明 `body.cursor`；响应包含 `next_cursor`、`has_more=true`、`is_circular=true`。
- 旧客户端不传 cursor 时，登录用户按稳定 `user_id` 保存服务端位置，游客按 `ssid` 隔离；不再共享全局 anonymous 游标。
- `/following`、`/followers`、`/user_videos`、`/my_videos`、`/following_feed` 使用签名 keyset cursor，响应包含 `next_cursor` 与 `has_more`。
- cursor 不允许客户端解析或拼接，不能跨列表复用。

Feed item 新增：

- `play_count`：去重登录用户播放数；同一用户/作品只计一次，在 `/impression` 成功时记录。
- `is_following`：当前登录用户是否关注作者。
- `viewer_following_author`、`following`：与 `is_following` 同值的双端过渡兼容字段。

`/follow`、`/unfollow` 成功 body 继续为空，客户端可乐观 `+1/-1`，失败时回滚。

## 账号与资料修复

- `/profile`、`/user_profile`、`/profile_update` 支持最长 80 字符的 `bio`。
- `/verify` 返回 `birthday`、`needs_birthday`、`is_under_13`。
- 合法生日无论是否满 13 岁都会持久化；`/birthday` 的 `passed` 表示是否满 13 岁，未满时不再丢失生日。
- App `/avatar` 已改为正确的同步存储调用，支持旧 form token 与 Bearer。
- `/follow` 拒绝关注已停用用户。
- Google token 校验有明确的 5 秒网络超时，并区分无效 token 与 Google 服务暂不可用。

登录验证码 purpose 为 `login`；账号注销验证码 purpose 为 `deactivate`。两者不共享验证码或频控：

```text
POST /send_code                 # 登录验证码
POST /deactivate/send_code      # 需登录，注销验证码
POST /deactivate                # 只接受 deactivate purpose
```

SMTP 发送成功后才提交验证码；发送失败会回滚，不会消耗冷却时间。频控响应会附加 `error_code=CODE_RATE_LIMITED`、友好 `message` 和 `retry_after_seconds`。

## App 更新与创作者权限

App 更新策略保存在 `app_versions`：

```text
POST /api/v1/app-updates/check
PUT  /internal/v1/app-versions/{ios|android}
GET  /internal/v1/app-versions/{ios|android}
```

创作者权限为永久 grant，可通过一次性邀请码或运营审核获得：

```text
GET  /api/v1/creator/access
POST /api/v1/creator/invites/redeem
POST /api/v1/creator/applications

POST /internal/v1/creator/invites
GET  /internal/v1/creator/applications
POST /internal/v1/creator/applications/{user_id}/decision
```

邀请码仅在创建响应中返回明文，数据库只保存 SHA-256；兑换使用行锁且每码只能成功一次。

## 创作、预览与发布

```text
POST /api/v1/creator/uploads/init                    协商本机断点续传（旧版回退 OSS）
HEAD/PATCH /api/v1/creator/uploads/{session_id}/source 查询偏移/续传分片
POST /api/v1/creator/uploads/{session_id}/finalize   校验 SHA-256/时长并原子固化
GET  /api/v1/creator/uploads/{upload_id}             查询统一编码与备份状态
POST /api/v1/creator/creations                       创建异步任务
GET  /api/v1/creator/creations/active                恢复最近未发布会话
GET  /api/v1/creator/creations/{id}                  查询进度/预览
POST /api/v1/creator/creations/{id}/versions          排队生成新版本（FIFO）
POST /api/v1/creator/creations/{id}/retry
POST /api/v1/creator/creations/{id}/cancel
POST /api/v1/creator/creations/{id}/publish          confirm=true + version/title/description
DELETE /api/v1/creator/published/{id}                作者软删除
POST /api/v1/creator/published/{id}/restore          作者恢复
```

每个用户最多一个活跃创作会话；同一会话可连续提交调整请求，版本严格 FIFO。协调 Worker 串行提交和轮询 ivadmin，全局并发为 1。进度阶段固定为：

1. `validate_video`
2. `normalize_video`
3. `sample_frames`
4. `find_playable_moments`
5. `compile_preview`

ivapp 不包含 Dify 或模型凭证，也不分析视频。Worker 只通过 `X-Creator-Internal-Key` 调用 ivadmin 私有任务 API；ivadmin/ivcore 是分析、模型配置与 Dify 调用的唯一所有者。

ivapp 与 ivadmin 将同一个宿主机目录挂载为 `/data/media-cache`。上传原片只写一次本机内容寻址存储；ivadmin 直接读取该文件并产出经过完整解码验收的 `mobile-v1` 播放版，分析、预览和发布只引用播放版。原片与播放版分别保留，并由后台任务异步备份到私有 OSS；正常链路不再通过 OSS 在两个服务间搬运视频。

任务 ready 后返回已经持久化的预览 runtime spec；只有用户预览后显式提交 `confirm=true` 才创建 `published_videos`。私有预览优先使用短期鉴权 CDN，正式发布会把播放版复制为不可变公开对象，预热 CDN 后再原子切换 runtime spec。

媒体统一进入 `ivapp-media/v1/`。客户端、ivadmin 与 HTML 工具不持有 OSS AccessKey；完整目录、全量迁移、灰度和回滚见 [OSS 媒体存储与稳定切换手册](docs/OSS_MEDIA_MIGRATION.md)。

## 数据库迁移

项目使用 Alembic，API 容器启动命令先执行：

```bash
alembic upgrade head
```

FastAPI 生命周期不再执行 `create_all` 或手写 `ALTER TABLE`。首个 revision 能以非破坏方式接管现有生产 schema，并创建新增表/字段。

## 本地开发与验证

```bash
cp .env.example .env
mkdir -p volumes/mysql volumes/redis
docker-compose up -d --build

python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
./scripts/check.sh
```

服务入口：API `http://localhost:8100/health`，phpMyAdmin 通过本机 `http://127.0.0.1:8101` 访问。

## 部署

常规发布：

```bash
./scripts/deploy.sh
```

首次上线持久化 runtime spec，或首次切换到 ExperienceSpec v1.1 时必须显式执行：

```bash
./scripts/deploy.sh --backfill-runtime-specs
```

脚本会先做本地检查、远端源码快照和 MySQL dump，再运行 Alembic；指定 backfill 时先 dry-run、再 apply，把历史 Runtime 内容从原始 timeline/story 重编译到当前协议版本；随后切换 API、健康检查并重建 Worker。生产 `.env` 和 `volumes` 不会上传、下载或进入源码备份。

详细运维说明见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)，原始现状审计见 [docs/PROJECT_ANALYSIS.md](docs/PROJECT_ANALYSIS.md)。
