# ivapp 后端全盘分析

审计时间：2026-08-08（Asia/Shanghai）

线上来源：`root@182.92.102.61:/opt/play_video/ivapp`

线上源提交：`f0d4882d166589875d42af76bfb83fe9d6917fde`

本地安全基线：Git `main` 根提交 `689d180`

## 1. 结论摘要

- 这是一个单体 FastAPI 服务，使用 MySQL 保存账号、作品、游标与埋点，Redis 保存用户曝光集合，本地卷保存视频和头像。
- 线上健康检查正常；API、MySQL、Redis、phpMyAdmin 均在 Docker Compose 项目 `ivapp` 中运行。
- App 协议使用 HTTP 200 + `head.status` 表达业务结果：`0` 成功、`100` 通用失败、`101` 登录/身份失败。响应没有机器可读错误码或用户文案。
- 推荐 `/video` 已实现“服务端隐式游标 + 环形列表”，客户端不传 cursor；但游标按 token 保存，`ssid` 目前只回显，不参与推荐。所有游客共享 `anonymous` 游标。
- `/following` 是关注用户列表；首页关注作品流是独立的 `/following_feed`。`/my_videos` 和 `/user_videos` 已存在。
- 代码声明 30 种互动类型；线上作品实际出现 18 种。线上出现只证明服务端曾下发，不等同于双端交互成功率已通过自动化验证。
- 项目没有测试目录、CI、Alembic 迁移、结构化错误协议、自动数据备份或正式发布/回滚流程。
- 审计发现若干必须优先处理的问题：源码曾包含真实凭据、App 头像上传调用错误、Google 校验出网阻断、phpMyAdmin 以 root 权限公开监听、Story 的 `on_end` 在发布解析时被丢弃。

## 2. 运行架构

```text
iOS / Android
    │  App 协议 JSON、媒体 GET
    ▼
FastAPI / Uvicorn :8100
    ├── MySQL 8.4       用户、token、作品、游标、验证码、关注、埋点
    ├── Redis           impression:{user_id} 曝光集合
    ├── SMTP            邮箱验证码
    ├── Google JWKS     Google ID Token 公钥校验
    └── /volumes/media  mp4 与头像

ivadmin / 内部工具
    └── /internal/v1/*，使用 X-Publish-Key

phpMyAdmin :8101
    └── MySQL root 管理入口
```

线上组件：

| 组件 | 当前形态 | 备注 |
|---|---|---|
| API | Python 3.12 / FastAPI / Uvicorn | 容器 `ivapp-api-1`，源码只读挂载 |
| MySQL | `mysql:8.4` | 数据在 `./volumes/mysql`，未映射数据库端口 |
| Redis | `redis:latest` | AOF，数据在 `./volumes/redis` |
| phpMyAdmin | `phpmyadmin:5.2-apache` | 宿主机 `0.0.0.0:8101` |
| 媒体 | FastAPI `FileResponse` | `./volumes/media`，公开读取 |
| 编排 | Docker Compose v2.40.2 | 服务器命令是独立的 `docker-compose` |

服务器审计时约有 43 GiB 可用磁盘、2.1 GiB available 内存、无 swap。API 和 phpMyAdmin 的 8100/8101 均监听全部 IPv4/IPv6 地址。

## 3. 源码地图

| 文件 | 职责 |
|---|---|
| `app/main.py` | FastAPI 生命周期、路由注册、健康检查、协议鉴权异常 |
| `app/config.py` | 环境变量配置 |
| `app/models.py` | 7 张 SQLAlchemy 业务表 |
| `app/db.py` | Engine、Session、启动时建表和手写迁移 |
| `app/routers/feed.py` | 邮箱/Google 登录、推荐、详情、曝光、埋点 |
| `app/routers/user.py` | 资料、生日、删号、关注、作者作品和关注流 |
| `app/routers/admin.py` | 内部用户管理、内容发布、媒体、日志 |
| `app/protocol_video.py` | timeline/story 到 App v1.0 播放协议的转换 |
| `app/protocol_envelope.py` | `head/body` 响应封装和 status |
| `app/auth_user.py` | token 认证与 JSON body 重放 |
| `app/feed_rank.py` | 教学置顶、已看沉底、环形分页 |
| `app/impressions.py` | Redis 曝光集合 |
| `app/avatar_storage.py` | 头像校验、落盘与路径解析 |
| `app/mail.py` | SMTP 验证码 |

## 4. API 能力盘点

| 边界 | 接口 |
|---|---|
| 无登录 | `GET /health`、`POST /send_code`、`/verify`、`/google_login`、`/video`、`/video_detail` |
| 登录用户 | `POST /track`、`/impression`、`/profile`、`/profile_update`、`/avatar`、`/user_profile`、`/birthday`、`/deactivate`、`/follow`、`/unfollow`、`/following`、`/followers`、`/user_videos`、`/my_videos`、`/following_feed` |
| 内部管理 | `/internal/v1/users*`、`/internal/v1/publish`、`/internal/v1/videos*`、`/internal/v1/logs` |
| 公开媒体 | `/media/avatars/{filename}`、`/media/{video_id}.mp4`、`/media/{item_id}/{clip_id}.mp4` |

需要明确的产品语义：

- `/following`：某个用户关注的主播列表。
- `/followers`：某个用户的粉丝列表。
- `/following_feed`：当前登录用户所关注作者发布的作品流。
- `/my_videos`：当前登录用户作品。
- `/user_videos`：指定作者作品。
- `/video_detail`：只返回作品与作者基本展示字段，没有当前登录用户是否关注作者等 viewer state；若详情页需要该状态，仍需并发请求 `/user_profile` 或扩展详情响应。
- `/follow`、`/unfollow` 成功 body 为空；客户端可以乐观更新数字，但失败时必须回滚，且本地计数不能作为最终一致性来源。

## 5. 分页和推荐行为

| 接口 | 当前分页行为 | 影响 |
|---|---|---|
| `/video` | 请求仅有 `limit`；服务端用 `RecommendCursor(token)` 保存位置并环形取数 | 可连续请求，但没有结束；内容池刷完后重复 |
| `/following`、`/followers` | 只有 `limit` | 永远只能拿最新首批，无法加载下一页 |
| `/user_videos`、`/my_videos`、`/following_feed` | 只有 `limit` | 永远只能拿首批，无法加载下一页 |
| 管理用户列表 | `limit + offset` | 可分页 |
| 管理埋点 | `limit + after_id` | 可增量拉取 |

`/video` 的真实逻辑：

1. `feed_weight DESC, id ASC` 形成池，停用作者被过滤。
2. 未看教学片优先；Redis 已曝光内容沉底。
3. 从 `recommend_cursors.token` 读取数字游标。
4. 一批最多返回 `min(limit, 池大小)`，更新游标并循环。

关键限制：

- `ssid` 没有用于推荐、游标或曝光；它只在响应头中回显，缺失时随机生成。
- 登录用户以 token 区分游标；重新登录换 token 后游标重置。
- 游客全部解析为 token=`anonymous`，共享同一个全局游标，请求会互相推进。
- 排序池变化时，旧数字游标可能导致跳过或重复。
- 并发请求更新同一游标没有锁或原子更新，可能发生丢失更新。
- 客户端可以在临近列表尾部预取下一批并持续滑动，但必须按 `item_id` 去重，并接受池刷完后重复；这不是有 `has_more` 的有限分页。

建议：推荐流短期可保留服务端游标，但至少把游客键改为稳定 `ssid`，并返回 `next_cursor`/`has_more` 或明确 `is_circular`；普通关注/作品列表应加入基于 `(created_at,id)` 的 cursor 分页。

## 6. 互动类型审计

代码在 `app/protocol_video.py` 中定义 30 种检测默认值。2026-08-08 对生产 `published_videos.timeline` 做只读统计，共发现 40 个互动点、18 种类型：

| 已在线上作品中出现 | 次数 |
|---|---:|
| `tap` | 10 |
| `mic_blow` | 6 |
| `camera_motion` | 4 |
| `tilt_left` | 3 |
| `shake` | 2 |
| `swipe_right` | 2 |
| `swipe_up` | 2 |
| `drag_right` | 1 |
| `erase` | 1 |
| `hold_charge` | 1 |
| `hold_still` | 1 |
| `mic_clap` | 1 |
| `mic_level` | 1 |
| `rapid_tap` | 1 |
| `rotate` | 1 |
| `swipe_down` | 1 |
| `swipe_left` | 1 |
| `tilt_right` | 1 |

代码已默认实现、但生产内容未出现的 12 种：

`double_tap`、`hold`、`drag_left`、`drag_up`、`drag_down`、`scrub_left`、`scrub_right`、`scrub_up`、`scrub_down`、`pinch`、`draw_circle`、`mic_quiet`。

边界说明：

- 后端只生成检测参数，不会验证 iOS/Android 是否真正完成动作识别。
- `camera_motion` 虽然已有 4 个线上互动点，仍需结合客户端能力判断是否“明确不支持”；单凭下发记录不能证明摄像头体验成功。
- 未知 gesture 不会被拒绝：响应仍保留未知 `type`，但 detection 会回退为 `tap` 默认值，容易造成静默错配。
- 所有互动都被强制 `pause_video=true`，反馈动画、震动和音效也是统一固定值。

## 7. 数据模型

| 表 | 用途 | 主要问题 |
|---|---|---|
| `published_videos` | single/story 内容、作者、权重、教学标记 | timeline JSON；没有作者外键 |
| `recommend_cursors` | token 到环形游标 | 不过期、不清理；游客共享 |
| `analytics_logs` | 互动埋点原文 | `data` 无长度限制和结构约束 |
| `email_codes` | 邮箱验证码 | 明文保存；无错误尝试次数限制 |
| `users` | 登录身份与资料 | provider+subject 唯一；软删无执行任务 |
| `user_tokens` | 登录会话 | token 明文保存；没有外键 |
| `follows` | 关注关系 | 没有外键；统计包含失效关系 |

审计时线上规模：21 个作品、10 个用户、8 个有效/存量 token、2 条关注、50 条验证码记录、1,885 条埋点、41 个推荐游标。21 个作品中 18 个 single、3 个 story，当前没有作品标记为教学片。

## 8. 关键风险与缺口

### P0：应先处理

1. **凭据进入源码和旧 Git 历史。** 线上 `.env.example` 与 `app/config.py` 曾包含数据库和 SMTP 实值；服务器 `.env` 权限为 `0644`。本地新仓库已脱敏且不保留旧 `.git`，但线上凭据必须轮换，旧仓库历史也应视为已泄漏。
2. **App 头像上传当前会报错。** `routers/user.py` 对同步函数 `save_user_avatar()` 使用了 `await`，并传入不存在的 `file=` 参数；管理端头像路由的调用方式才与函数签名一致。
3. **Google 登录依赖当前不可达的外网。** 服务器能解析 Google，但 IPv4 443 连接超时、IPv6 不可用；`google-auth` 默认请求超时很长且可能尝试多个地址。请求还没有显式 timeout/circuit breaker，容易耗尽工作线程。
4. **phpMyAdmin root 管理面公开。** 8101 监听所有地址，并以 MySQL root 凭据连接；应立即限制为回环/安全组白名单/VPN，优先下线公网入口。

### P1：近期工程化

1. **没有测试和 CI。** 头像调用错误、Story 字段丢失等无法在发布前被发现。
2. **没有正式数据库迁移。** API 启动执行 `create_all + ALTER/DROP`；多实例启动存在竞态，回滚代码无法回滚 schema。
3. **部署前没有完整数据恢复链。** 新脚本会保留源码快照和 MySQL dump，但媒体、Redis、异机备份和恢复演练仍需补齐。
4. **Story `on_end` 被丢弃。** 协议转换器支持 clip `on_end`，但 `_parse_story()` 只保留每个 clip 的 `timeline`，发布时会删除 `on_end`。
5. **普通列表没有下一页。** 关注、粉丝、作者作品、我的作品和关注流都只能取第一批。
6. **协议错误不可诊断。** `status=100/101 + {}` 无法区分限流、SMTP 失败、参数错、账号停用、Google 网络错；FastAPI 422/500 又是另一种结构。
7. **发码事务顺序不合理。** 验证码先写库并提交，再发 SMTP；邮件发送失败时仍会触发发送间隔，用户短期重试只能收到通用 status 100。
8. **上传占用和一致性风险。** 发布接口把所有视频完整读进内存，无总大小限制；旧媒体先删除、新媒体再写、最后提交 DB，任一步失败都可能产生文件/数据库不一致。
9. **账号计划删除没有执行器。** 只写 `scheduled_delete_at`，没有定时硬删、取消删除或审计流程；Google 账号也无法走邮箱验证码删号。
10. **依赖不可复现。** Python 依赖只有下限，Redis 使用 `latest`；未来重建可能得到不同版本。

### P2：持续改进

- 数据表没有外键；停用/缺失用户的关注关系和计数可能残留。
- `/follow` 只检查用户存在，不检查对方是否启用。
- `/track.data` 没有长度上限、JSON schema 或幂等键。
- `head.ver` 响应实际优先回显客户端版本，与“服务端版本”字段定义不一致。
- 默认请求模型包含示例 token 字符串，缺少 token 时不应生成类似真实凭证的默认值。
- 没有请求 ID、指标、追踪、告警、日志轮转或敏感信息统一脱敏。
- 静态 Publish Key 缺少轮换、权限分级和审计，且需要 TLS 才能安全传输。

## 9. 建议开发顺序

1. 轮换数据库、SMTP、Publish Key；把 `.env` 改为 `0600`；限制 8101，并确认 8100 只通过 TLS 入口暴露。
2. 修复 `/avatar` 和 Story `on_end`，为所有纯函数、认证、分页和上传错误增加测试。
3. 为 Google 验证设置 3–5 秒连接/读取超时、失败熔断和明确错误码，并解决服务器到 Google JWKS 的受控出网。
4. 引入 Alembic；启动阶段不再执行破坏性 DDL；建立 schema 前向/回退策略。
5. 定义错误枚举和 `message/retry_after_seconds`，统一 422/500 到协议层；优先改善发码限流与外部服务故障提示。
6. 给所有列表加入 cursor；明确推荐流是无限环形还是有限页，并用 `ssid` 隔离游客会话。
7. 将媒体上传改为流式临时文件 + 校验 + 原子 rename，限制单文件/总大小，并为 DB/文件设计补偿事务。
8. 固定依赖版本，接入 CI、镜像扫描、监控告警和定期异机备份恢复演练。

## 10. 当前可维护基线

- 新本地仓库没有 remote，也没有继承服务器 `.git`。
- 真实 `.env`、数据库卷、Redis 卷、媒体和缓存从未拉到本地。
- `app/config.py` 与 `.env.example` 已移除硬编码实值。
- `scripts/check.sh` 提供语法/Compose 基础检查。
- `scripts/deploy.sh` 提供预检、不可变 release、源码快照、构建、API 单服务重建、健康检查与自动源码回滚。
- `scripts/server_status.sh` 和 `scripts/rollback.sh` 提供状态检查与人工回滚入口。

后续任何业务修改建议从新增失败用例开始，并保持“业务代码提交”和“部署/运维变更提交”可独立回滚。
