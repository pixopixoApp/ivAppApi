# ivapp

维护入口：[项目全盘分析](docs/PROJECT_ANALYSIS.md) · [部署与回滚](docs/DEPLOYMENT.md)

临时 C 端 FastAPI：接收 ivadmin 发布、教学置顶 + `feed_weight` 排序的推荐 Feed、Redis 曝光沉底去重；邮箱验证码登录。

自带 **独立 MySQL**（库名 `ivapp`）与 **Redis**，**不与 ivadmin 共用**；MySQL / Redis **不对宿主机映射端口**，仅 compose 内网。

只允许两种挂载：**代码 `:ro`**，**数据 `./volumes/...`**。禁止 Docker named volume。

| 数据 | 位置 |
|------|------|
| 发布视频等 | `./volumes/media/`（整目录挂到容器 `/volumes`） |
| 用户头像 | `./volumes/media/avatars/`（相对 URL `/media/avatars/...`） |
| MySQL datadir | `./volumes/mysql/` |
| Redis AOF/data | `./volumes/redis/` |
| 代码（只读） | `./app` |

导出 MySQL：

```bash
docker-compose exec mysql \
  bash -c 'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --all-databases' > ./volumes/mysql-dump.sql
```

宿主机端口统一 **810x**：API `8100`，phpMyAdmin `8101`。

模块：`routers/feed.py`（公开：`/send_code`、`/verify`、`/google_login`、`/video`、`/video_detail`；需登录：`/track`、`/impression`）、`routers/user.py`（需登录：资料/生日/停用、关注与粉丝、作品列表与关注流）、`routers/admin.py`（用户读/写/停用/头像、发布、Feed 权重、曝光池调试、logs、媒体直出）。

API 镜像构建对齐 ivadmin：Debian 用中科大源，pip 用清华源（见 `apt.sources` / `Dockerfile`）。

## 启动

```bash
cd ivapp
cp .env.example .env   # 若还没有
mkdir -p volumes/mysql volumes/media volumes/redis
docker-compose down
docker-compose up -d --build
```

- API：http://localhost:8100/health
- phpMyAdmin：http://localhost:8101/

表在 API 启动时 `create_all` 自动建。

## 登录与用户身份

一人一种登录（邮箱或 Google）。概念：

| 概念 | 说明 |
|------|------|
| `user_id` | 稳定用户 id（`users` 表），关注 / 发布作者用它 |
| `provider` + `subject` | 登录身份（邮箱：`email` + 邮箱；Google：`google` + Google `sub`），UNIQUE |
| `enabled` | 是否启用；`false` 时其名下视频对 Feed/单拉不可见 |
| `nickname` | 昵称，可空串 |
| `avatar_url` | 头像**相对路径**（如 `/media/avatars/{user_id}.png`），可空串；文件落在 `MEDIA_ROOT/avatars/` |
| `birthday` | 生日 `YYYY-MM-DD`，可空串（未设置时登录回 `needs_birthday=true`）；保存时须满 13 岁 |
| `source` | 创建来源：`app`=真实用户（邮箱登录），`admin`=管理后台创建；创建后不可改 |
| `deletion_requested_at` / `scheduled_delete_at` | App 删号申请时间与计划删除时间（默认申请后 30 天）；申请时立刻 `enabled=false` |
| `token` | 会话凭证，每次 verify / google_login 会换新，挂在 `user_tokens.user_id`；用户停用后 token 失效 |

1. `POST /send_code`：发 6 位验证码（默认 **10 分钟**；同邮箱 **60 秒**限流）；登录与删号共用
2. `POST /verify`：邮箱登录，返回 `token`、`user_id`、`email`、`expires_at`、`needs_birthday`（token 默认 **30 天**）；已停用账号不可登录
3. `POST /google_login`：Google 登录，`body.id_token`；成功回包与 `/verify` 同形；需配置 `GOOGLE_CLIENT_ID`
4. **浏览**：`/video`、`/video_detail` 游客可访问；`/track`、`/impression` 与 user 路由需有效 token（无效 → `status=101`）
5. **必须登录**（`routers/user.py`）：`/profile`、`/profile_update`、`/avatar`、`/birthday`、`/deactivate`、`/user_profile`、`/follow`、`/unfollow`、`/following`、`/followers`、`/user_videos`、`/my_videos`、`/following_feed`

> 用户资料写入内核 `apply_user_update` 由管理端 upsert / 头像上传与 App `/profile_update`、`/avatar` 共用；App 仅允许改昵称/头像；生日走 `/birthday`（年龄门）；删号走 `/deactivate`（邮箱验证码；Google 账号本期不可走邮箱删号）。

无数据库外键；仅 UNIQUE / INDEX。

| 环境变量 | 说明 |
|----------|------|
| `SMTP_HOST` | 由环境配置；置空则只打日志 |
| `SMTP_PORT` | 默认 `465`（SSL） |
| `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` | 发件账号；源码不提供真实默认值 |
| `SMTP_SSL` / `SMTP_TLS` | 465 用 SSL（`SMTP_SSL=true`，`SMTP_TLS=false`） |
| `CODE_TTL_SECONDS` | 验证码有效期，默认 600 |
| `TOKEN_TTL_DAYS` | token 有效期，默认 30 |
| `SEND_CODE_INTERVAL_SECONDS` | 发码间隔，默认 60 |
| `ACCOUNT_DELETION_BUFFER_DAYS` | 删号缓冲天数，默认 30（写 `scheduled_delete_at`） |
| `GOOGLE_CLIENT_ID` | Google Web Client ID；`/google_login` 校验 id_token 的 audience；空则接口返回 status=100 |
| `REDIS_URL` | Redis 连接串，默认 `redis://redis:6379/0`；曝光去重池 `impression:{user_id}` |
| `GOOGLE_CLIENT_SECRET` | 可选占位（本期 ID Token 路径不使用） |

## 接口

### 1. 用户读 / upsert / 停用（管理）

Header: `X-Publish-Key: <PUBLISH_KEY>`

**列表** `GET /internal/v1/users?source=admin&enabled=true&q=小明&limit=50&offset=0`

可选：`source`（`app`/`admin`）、`enabled`、`q`（昵称模糊）、`limit`（默认 50，1–200）、`offset`（默认 0）。按 `created_at` 倒序。返回：`{ items, total, limit, offset }`。

**读取** `GET /internal/v1/users/{user_id}` → `{ user_id, provider, subject, enabled, nickname, avatar_url, source, created_at }`。不存在 → `404`。

**批量读取** `POST /internal/v1/users/batch`

```json
{ "user_ids": ["u1", "u2"] }
```

最多 200 个。返回：`{ "items": [ ...同上... ], "missing": ["未找到的 id"] }`（保序去重）。

**Upsert** `POST /internal/v1/users`

```json
{
  "user_id": "u1",
  "provider": "email",
  "subject": "a@b.com",
  "enabled": true,
  "nickname": "小明",
  "avatar_url": "/media/avatars/u1.png"
}
```

按 `user_id` 插入或覆盖；新建时 `source=admin`（邮箱自注册为 `app`）；`nickname` / `avatar_url` 可选（不传则更新时保持原值；传 `""` 可清空）。`avatar_url` 须为相对路径（`/` 开头）或空串。`provider+subject` 冲突 → `409`。

**停用** `POST /internal/v1/users/{user_id}/deactivate` → `{ "user_id", "enabled": false }`。不存在 → `404`。

**上传头像** `POST /internal/v1/users/{user_id}/avatar`（multipart `file`：jpg/png/webp，≤2MB）→ 写入 `MEDIA_ROOT/avatars/{user_id}.{ext}`，更新 `avatar_url`，返回用户信息。不存在 → `404`。

直出头像：`GET /media/avatars/{user_id}.{ext}`（无需 Publish-Key）。

停用后：该作者名下视频从 `/video`、`/video_detail` 不可见（存量无 `user_id` 的视频不受影响）。

### 2. 发布（ivadmin → ivapp）

`POST /internal/v1/publish`（multipart）

Header: `X-Publish-Key: <PUBLISH_KEY>`

公共字段：

| 字段 | 说明 |
|------|------|
| `video_id` | 发布单元幂等键（**item_id** / run_id） |
| `version` | 分析版本 |
| `user_id` | **必填**，作者 `users.user_id`（须存在且 `enabled`） |
| `content_mode` | `single`（默认）或 `story` |
| `feed_weight` | 可选 int；越大 Feed 越靠前。新建默认 `0`；更新时不传则保持原值 |
| `is_tutorial` | 可选 bool；教学片。新建默认 `false`；更新不传保持原值；`true` 时清其它教学标记 |

**single（兼容现状）**

| 字段 | 说明 |
|------|------|
| `timeline` | timeline JSON 字符串 |
| `video` | 一个 mp4 文件 |

落盘：`MEDIA_ROOT/{video_id}.mp4`，相对 URL：`/media/{video_id}.mp4`。

**story**

| 字段 | 说明 |
|------|------|
| `story` | story JSON 字符串：`{ "entry_clip_id", "clips": { "<clip_id>": { "timeline": {...} } } }` |
| `clips` | **多个** mp4；每个文件名必须是 `{clip_id}.mp4`，且与 `story.clips` 的键一一对应 |

落盘：`MEDIA_ROOT/{video_id}/{clip_id}.mp4`，相对 URL：`/media/{video_id}/{clip_id}.mp4`。  
返回的 `video_url` 为**入口 clip**路径。`entry_clip_id` 必须在 `clips` 内。

返回：`{ "video_id", "version", "video_url", "user_id", "content_mode", "updated" }`。用户不存在或已停用 → `400`。

直出：`GET /media/{video_id}.mp4`（single）；`GET /media/{video_id}/{clip_id}.mp4`（story）。

> 本期仅 ivapp；ivadmin 对接 story 表单可后续再改。

### 2b. 查询已发布视频信息

单条：`GET /internal/v1/videos/{video_id}`  
批量：`POST /internal/v1/videos/batch`

Header: `X-Publish-Key: <PUBLISH_KEY>`

单条返回：`{ "video_id", "version", "video_url", "user_id", "content_mode", "feed_weight", "is_tutorial", "created_at", "updated_at" }`（不含 timeline/story；存量 `user_id` 可为 `null`，`content_mode` 默认 `single`，`feed_weight` 默认 `0`，`is_tutorial` 默认 `false`）。不存在 → `404`。

改运营字段（主路径）：`POST /internal/v1/videos/{video_id}/feed`

```json
{ "is_tutorial": true }
```

也可 `{ "feed_weight": 10 }` 或两者一起。至少传一项。`is_tutorial=true` 时全站其它片自动取消教学标记。返回同上元数据。

调试曝光池：`GET /internal/v1/users/{user_id}/impressions` → `{ "user_id", "count", "video_ids": [...] }`（Redis Set 全量）。用户不存在 → `404`；存在但池空 → `count=0`。

批量请求：

```json
{ "video_ids": ["demo1", "demo2"] }
```

最多 200 个。返回：`{ "items": [ ...同上... ], "missing": ["未找到的 id"] }`。

### 2c. 取消发布

`DELETE /internal/v1/videos/{video_id}`

Header: `X-Publish-Key: <PUBLISH_KEY>`

删除 `published_videos` 记录及对应媒体：single 删 `{video_id}.mp4`；story 删整目录 `{video_id}/`。返回：`{ "video_id", "deleted": true }`。不存在 → `404`。埋点日志保留。

### 3. 发验证码

`POST /send_code`

```json
{
  "head": { "act": "send_code", "ver": "1.2" },
  "body": { "email": "user@example.com" }
}
```

登录前接口，**不需要** `head.token`。成功：`status=0`；失败：`status=100`。

### 4. 验码拿 token

`POST /verify`

```json
{
  "head": { "act": "verify", "ver": "1.2" },
  "body": { "email": "user@example.com", "code": "123456" }
}
```

成功：`body` 含 `token`、`user_id`、`email`、`expires_at`、`needs_birthday`（生日未设置时为 `true`）。验证码错误/过期：`status=101`。

### 4b. Google 登录拿 token

`POST /google_login`

Android 用 Credential Manager / Google Sign-In 取得 **ID Token**（`serverClientId` = 环境变量 `GOOGLE_CLIENT_ID` 的 Web Client ID），再提交：

```json
{
  "head": { "act": "google_login", "ver": "1.2" },
  "body": { "id_token": "<Google_ID_TOKEN_JWT>" }
}
```

成功：`body` 与 `/verify` 同形（`token`、`user_id`、`email`、`expires_at`、`needs_birthday`）；`email` 取自 Google claims，可能为空串。  
用户按 `provider=google` + `sub` 建档；与邮箱账号**不自动合并**。  
校验失败/停用：`status=101`；未配置 `GOOGLE_CLIENT_ID`：`status=100`。容器需能访问 Google JWKS（出网）。

### 5. App 拉视频列表（游客可访问）

`POST /video`

可不带 `head.token`（游客用 `anonymous` 游标）。`body.limit` 可选，默认 10。

**排序（MVP）**

```text
S0 = sort_by(feed_weight DESC, id ASC)   # 停用作者不入池
T  = is_tutorial 那一条（全站至多 1）
S  = T + (S0 \ T)                        # 教学未看时占序列首位
登录: F = unseen(S) + seen(S)             # seen 来自 Redis impression:{user_id}；已看后教学也会沉底
游客: F = S
环形 cursor 取 limit（RecommendCursor，按 token）
```

- 客户端视口起播后调 `POST /impression` 记已看；不下发即记已看
- 已看沉底、不删除；刷完可循环再看到
- Redis 不可用时：登录 Feed **降级为仅权重序（不沉底）**并打 warning；`/impression` 返回 `status=100`

成功：`body.items[]`，每项为发布单元：

```json
{
  "item_id": "demo1",
  "user_id": "u1",
  "nickname": "小明",
  "avatar_url": "/media/avatars/u1.png",
  "video": [
    {
      "video_id": "demo1",
      "video": "/media/demo1.mp4",
      "interactions": [
        {
          "id": "action_001",
          "type": "tap",
          "description": "",
          "offset_time_ms": 1000,
          "pause_video": true,
          "detection": {},
          "feedback": {},
          "on_success": { "action": "continue" },
          "on_miss": { "action": "continue" }
        }
      ]
    }
  ]
}
```

App **v1.0**：无 `transition`、无 `miss_behavior`；每个 interaction 必有 `on_success` / `on_miss`。

- **单视频**：`video.length === 1`
- **Story**：`video` 多段；入口 clip（源 `entry_clip_id`）排在数组第一位；story `outcomes` / clip `on_end` 映射为 Action（**不改** story 源结构）：

| story 源 | v1.0 |
|----------|------|
| `outcomes.*.action` = `goto` + `clip_id` | `{"action":"jump_video","target_video_id":"...","timing":"immediate"}` |
| `outcomes.*.action` = `continue` | `{"action":"continue"}` |
| `outcomes.*.action` = `replay` | `{"action":"restart_video"}` |
| outcomes 其它 / 缺失 / `end` / `timeout` | `{"action":"continue"}` |
| clip `on_end` = `{ "action":"goto", "clip_id" }` | 该 clip 带 `on_end`：`jump_video` + `timing: immediate` |
| 无 clip `on_end` | 协议不输出 `on_end`（播完无跳转） |

`on_success` ← `outcomes.success`；`on_miss` ← `outcomes.fail`（忽略 `timeout`）。  
无 outcomes 的互动：两边均为 `continue`。互动跳转与播完跳转的 `jump_video.timing` 一律 `immediate`。

`detection` 按交互类型（`gesture`）查默认表（含 `response_window_ms`、`min_duration_ms`、`min_volume_score` 等类型专属字段）。  
若源有合法 `gate_end_ms`（≥ `gate_at_ms`），则 **只覆盖** `response_window_ms = gate_end_ms - gate_at_ms`；有 `region` 时覆盖 `place`。未知类型回退为 `tap` 默认。

停用作者的发布单元不入池；池为空：`status=100`。

### 6. App 按 item_id 单拉（游客可访问）

`POST /video_detail`

可不带 `head.token`。`body: { "video_id": "..." }` — 此处 `video_id` 语义为**发布单元 item_id**（与 publish 的 `video_id` 相同）。  
成功：`body.items` 为**单元素数组**（条目与 `/video` 同形）。不存在或作者已停用：`status=100`，`body={}`。

### 7. App 埋点（需登录）

`POST /track`

需有效 `head.token`。`body.video_id` + `body.data`。成功：`status=0`。

### 7b. App 曝光上报（需登录）

`POST /impression`

需有效 `head.token`。`body: { "video_id": "<item_id>" }` → Redis `SADD impression:{user_id}`（幂等）。  
成功：`status=0`；视频不存在或 Redis 不可用：`status=100`；无效 token：`status=101`。

### 8. 个人资料 / 生日 / 停用（需登录）

**查询** `POST /profile` — body 可空 → `body: { user_id, nickname, avatar_url, email, enabled, following_count, follower_count }`

**编辑** `POST /profile_update` — `body: { "nickname"?, "avatar_url"? }`（相对路径或空串）→ 成功回资料同形。非法头像路径 → `status=100`。

**上传头像** `POST /avatar` — multipart：`token` + `file`（jpg/png/webp，≤2MB）→ 落盘并更新 `avatar_url`，成功回资料同形（`act=avatar`）。非法文件 `status=100`；无效 token `status=101`。

**保存生日（年龄门）** `POST /birthday` — `body: { "birthday": "YYYY-MM-DD" }`  
- 满 13 岁（UTC）：写库 → `{ birthday, needs_birthday: false, passed: true }`  
- 未满 13：不写库 → `status=100`，`{ birthday: "", needs_birthday: true, passed: false }`  
- 非法日期 → `status=100`

**删除账号** `POST /deactivate` — 先 `POST /send_code`（登录邮箱），再 `body: { "code": "123456" }`  
→ `enabled=false`、清全部 token、写 `deletion_requested_at` / `scheduled_delete_at`（默认 +30 天）→ `{ scheduled_delete_at }`。验证码错误或非邮箱账号 → `status=100`。本期不做到期硬删与取消删号。

**他人公开资料** `POST /user_profile` — `body: { "user_id" }` → `body: { user_id, nickname, avatar_url, enabled, following_count, follower_count, is_following }`（**不含邮箱**）。不存在或已停用 → `status=100`。

与管理端共用 `apply_user_update` / `save_user_avatar`；App **不能**经 profile_update 改 `provider` / `subject` / `enabled`。

### 9. 关注 / 粉丝 / 作品列表

均需有效 `head.token`（解析出当前 `user_id`），否则 `status=101`。

**关注** `POST /follow` — `body: { "user_id": "<对方>" }`  
**取消** `POST /unfollow` — 同上  
**关注列表** `POST /following` — `body: { "user_id"?, "limit": 50 }`（`user_id` 空=自己）→ `body.items[{ user_id, nickname, avatar_url, created_at }]`  
**粉丝列表** `POST /followers` — 同上形  

**他人作品** `POST /user_videos` — `body: { "user_id", "limit"? }` → `body.items` 与 `/video` 同形；作者不存在/停用 → `status=100`  
**我的作品** `POST /my_videos` — `body: { "limit"? }` → 同形  
**关注流** `POST /following_feed` — `body: { "limit"? }` → 我所关注作者的公开作品（按发布时间倒序）

不能关注自己 / 对方不存在 → `status=100`；已关注再 follow、未关注再 unfollow → 幂等 `status=0`。
目标用户不存在或已停用时查 following/followers → `status=100`。

### 10. 拉取埋点

`GET /internal/v1/logs`

Header: `X-Publish-Key: <PUBLISH_KEY>`

必填查询参数 `video_id`。

## curl 示例

```bash
# 发码（无 SMTP 时看 API 日志里的 code=）
curl -s -X POST http://localhost:8100/send_code \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"send_code","ver":"1.2"},"body":{"email":"user@example.com"}}'

# 验码（把 CODE 换成实际值；成功后拿 body.token 与 body.user_id）
curl -s -X POST http://localhost:8100/verify \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"verify","ver":"1.2"},"body":{"email":"user@example.com","code":"CODE"}}'

curl -s -X POST http://localhost:8100/google_login \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"google_login","ver":"1.2"},"body":{"id_token":"GOOGLE_ID_TOKEN"}}'

curl -s -X POST http://localhost:8100/birthday \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"birthday","ver":"1.2","token":"TOKEN"},"body":{"birthday":"2000-01-01"}}'

curl -s -X POST http://localhost:8100/follow \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"follow","ver":"1.2","token":"TOKEN"},"body":{"user_id":"OTHER_USER_ID"}}'

curl -s -X POST http://localhost:8100/profile \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"profile","ver":"1.2","token":"TOKEN"},"body":{}}'

curl -s -X POST http://localhost:8100/user_profile \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"user_profile","ver":"1.2","token":"TOKEN"},"body":{"user_id":"OTHER_USER_ID"}}'

curl -s -X POST http://localhost:8100/profile_update \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"profile_update","ver":"1.2","token":"TOKEN"},"body":{"nickname":"小明"}}'

curl -s -X POST http://localhost:8100/avatar \
  -F 'token=TOKEN' \
  -F 'file=@./avatar.png;type=image/png'

curl -s -X POST http://localhost:8100/following \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"following","ver":"1.2","token":"TOKEN"},"body":{}}'

curl -s -X POST http://localhost:8100/following \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"following","ver":"1.2","token":"TOKEN"},"body":{"user_id":"OTHER_USER_ID"}}'

curl -s -X POST http://localhost:8100/followers \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"followers","ver":"1.2","token":"TOKEN"},"body":{}}'

curl -s -X POST http://localhost:8100/user_videos \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"user_videos","ver":"1.2","token":"TOKEN"},"body":{"user_id":"OTHER_USER_ID"}}'

curl -s -X POST http://localhost:8100/my_videos \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"my_videos","ver":"1.2","token":"TOKEN"},"body":{}}'

curl -s -X POST http://localhost:8100/following_feed \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"following_feed","ver":"1.2","token":"TOKEN"},"body":{}}'

curl -s -X POST http://localhost:8100/unfollow \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"unfollow","ver":"1.2","token":"TOKEN"},"body":{"user_id":"OTHER_USER_ID"}}'

# 删号：先发码到登录邮箱，再带 code
curl -s -X POST http://localhost:8100/send_code \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"send_code","ver":"1.2"},"body":{"email":"user@example.com"}}'

curl -s -X POST http://localhost:8100/deactivate \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"deactivate","ver":"1.2","token":"TOKEN"},"body":{"code":"CODE"}}'

# Feed / 单拉（游客可无 token）；埋点仍需 token
curl -s -X POST http://localhost:8100/video \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"video","ver":"1.2"},"body":{}}'

curl -s -X POST http://localhost:8100/video_detail \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"video_detail","ver":"1.2"},"body":{"video_id":"demo1"}}'

curl -s -X POST http://localhost:8100/track \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"track","ver":"1.2","token":"TOKEN"},"body":{"video_id":"demo1","data":"tap_ok"}}'

curl -s -X POST http://localhost:8100/impression \
  -H "Content-Type: application/json" \
  -d '{"head":{"act":"impression","ver":"1.2","token":"TOKEN"},"body":{"video_id":"demo1"}}'

# 管理：upsert / 读取 / 停用用户
curl -s -X POST http://localhost:8100/internal/v1/users \
  -H "X-Publish-Key: dev-publish-key" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u1","provider":"email","subject":"author@example.com","enabled":true,"nickname":"作者","avatar_url":"/media/avatars/u1.png"}'

curl -s "http://localhost:8100/internal/v1/users?source=admin&enabled=true&limit=50&offset=0" \
  -H "X-Publish-Key: dev-publish-key"

curl -s "http://localhost:8100/internal/v1/users/u1" \
  -H "X-Publish-Key: dev-publish-key"

curl -s -X POST http://localhost:8100/internal/v1/users/batch \
  -H "X-Publish-Key: dev-publish-key" \
  -H "Content-Type: application/json" \
  -d '{"user_ids":["u1","u2"]}'

curl -s -X POST http://localhost:8100/internal/v1/users/u1/deactivate \
  -H "X-Publish-Key: dev-publish-key"

curl -s -X POST http://localhost:8100/internal/v1/users/u1/avatar \
  -H "X-Publish-Key: dev-publish-key" \
  -F 'file=@./avatar.png;type=image/png'

curl -s "http://localhost:8100/media/avatars/u1.png" -o /tmp/avatar-out.png

curl -s -X POST http://localhost:8100/internal/v1/publish \
  -H "X-Publish-Key: dev-publish-key" \
  -F 'video_id=demo1' \
  -F 'version=1' \
  -F 'content_mode=single' \
  -F 'user_id=u1' \
  -F 'timeline={"interactions":[{"gesture":"tap","gate_at_ms":1000,"hint":"点一下"}]}' \
  -F 'video=@./demo.mp4;type=video/mp4'

# Story 发布示例（clips 文件名 = {clip_id}.mp4）
curl -s -X POST http://localhost:8100/internal/v1/publish \
  -H "X-Publish-Key: dev-publish-key" \
  -F 'video_id=story1' \
  -F 'version=1' \
  -F 'content_mode=story' \
  -F 'user_id=u1' \
  -F 'story={"entry_clip_id":"intro","clips":{"intro":{"timeline":{"interactions":[{"gesture":"tap","gate_at_ms":1000,"hint":"选路","outcomes":{"success":{"action":"goto","clip_id":"good"},"fail":{"action":"goto","clip_id":"bad"}}}]}},"good":{"timeline":{"interactions":[]}},"bad":{"timeline":{"interactions":[]}}}}' \
  -F 'clips=@./intro.mp4;type=video/mp4;filename=intro.mp4' \
  -F 'clips=@./good.mp4;type=video/mp4;filename=good.mp4' \
  -F 'clips=@./bad.mp4;type=video/mp4;filename=bad.mp4'

curl -s "http://localhost:8100/internal/v1/videos/demo1" \
  -H "X-Publish-Key: dev-publish-key"

curl -s -X POST http://localhost:8100/internal/v1/videos/demo1/feed \
  -H "X-Publish-Key: dev-publish-key" \
  -H "Content-Type: application/json" \
  -d '{"feed_weight":10,"is_tutorial":true}'

curl -s "http://localhost:8100/internal/v1/users/u1/impressions" \
  -H "X-Publish-Key: dev-publish-key"

curl -s -X POST http://localhost:8100/internal/v1/videos/batch \
  -H "X-Publish-Key: dev-publish-key" \
  -H "Content-Type: application/json" \
  -d '{"video_ids":["demo1","demo2"]}'

curl -s -X DELETE "http://localhost:8100/internal/v1/videos/demo1" \
  -H "X-Publish-Key: dev-publish-key"

curl -s "http://localhost:8100/internal/v1/logs?video_id=demo1&limit=20" \
  -H "X-Publish-Key: dev-publish-key"
```
