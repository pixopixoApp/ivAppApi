# 接口文档：上报已看（/seen）

> 面向 App / 客户端开发。用于把**用户真正浏览或播放过**的视频上报给服务端，
> 服务端据此写入「已看」去重池，推荐接口 `/video` 下次不再重复下发这些内容。

> **适用前提**：App 端强制登录。因此本文档以**登录态**为准——请求只需携带有效 `token`，
> `ssid` 无需传（详见第 2.1 与第 5 节）。

## 1. 基本信息

| 项 | 值 |
|----|----|
| 方法 | `POST` |
| 生产地址 | `https://api.pixopixo.com/seen` |
| 备用地址（Web 同源） | `https://pixopixo.com/game-api/seen` |
| 鉴权 | **需携带登录 `token`**（请求体 `head.token` 或 `Authorization: Bearer`）；App 强制登录场景必带 |
| Content-Type | `application/json` |
| 幂等性 | 是。同一 `video_id` 重复上报无副作用 |

## 2. 请求

### 2.1 Head（协议头）

```json
{
  "act": "seen",
  "ver": "1.2",
  "time": "2026-09-17 16:11:55",
  "token": "<用户 token>"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `act` | string | 建议传 | 应为 `"seen"`（不传时服务端默认 `"video"`，建议显式传 `"seen"`） |
| `ver` | string | 建议传 | 客户端版本号（如 `"1.2"`）；不传默认 `"1.2"` |
| `time` | string | 否 | 客户端时间，格式 `YYYY-MM-DD HH:MM:SS`；服务端不校验，响应 `time` 为服务端时间 |
| `token` | string | **是** | 登录用户的 token；也可用请求头 `Authorization: Bearer <token>` 代替 |
| `ssid` | string | 否 | 登录态下**不需要**；仅游客场景才需要稳定 ssid（见第 5 节） |

> **说明**：App 强制登录，去重键为 `user:seen:{user_id}`（由 `token` 解析得到，与服务端
> 之前是否收到 `ssid` 无关）。因此 App 只需保证 `token` 正确即可，`ssid` 可省略。
> 若 `token` 无效/过期，请求会被当作游客处理，去重也将失效——请确保登录态有效。

### 2.2 Body

```json
{
  "video_id": "00d8c82c-414a-4ff1-b24e-471b3df35c29"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `video_id` | string | 是 | 发布单元 `item_id`（即 `/video` 返回的 `body.items[].item_id`），长度 1~128 |

### 2.3 完整请求示例

**App（登录用户，推荐用法）：**

```bash
curl -X POST https://api.pixopixo.com/seen \
  -H 'Content-Type: application/json' \
  -d '{
    "head": {"act": "seen", "ver": "1.2", "token": "<用户 token>"},
    "body": {"video_id": "00d8c82c-414a-4ff1-b24e-471b3df35c29"}
  }'
```

或使用标准 Bearer 头：

```bash
curl -X POST https://api.pixopixo.com/seen \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <用户 token>' \
  -d '{
    "head": {"act": "seen", "ver": "1.2"},
    "body": {"video_id": "00d8c82c-414a-4ff1-b24e-471b3df35c29"}
  }'
```

**游客（非 App 场景，仅 Web/未登录）：**

```bash
curl -X POST https://api.pixopixo.com/seen \
  -H 'Content-Type: application/json' \
  -d '{
    "head": {"act": "seen", "ver": "1.2", "ssid": "a1b2c3d4e5f60718"},
    "body": {"video_id": "00d8c82c-414a-4ff1-b24e-471b3df35c29"}
  }'
```

## 3. 响应

响应结构与其它接口一致，`head.status` 表示结果。

### 3.1 成功

```json
{
  "head": {
    "act": "seen",
    "status": 0,
    "ver": "1.2",
    "time": "2026-09-17 16:11:55",
    "ssid": "a1b2c3d4e5f60718"
  },
  "body": {}
}
```

`head.ssid` 为服务端确认/回填的会话 ID。**客户端应保存该值并用于后续请求**
（后续未带 ssid 时可省，但建议始终带上从首次响应拿到的 ssid）。

### 3.2 失败

```json
{
  "head": {
    "act": "seen",
    "status": 100,
    "ver": "1.2",
    "time": "2026-09-17 16:11:58",
    "ssid": "a1b2c3d4e5f60718"
  },
  "body": {}
}
```

| status | 含义 | 客户端处理 |
|--------|------|-----------|
| `0` | 成功 | 正常 |
| `100` | `video_id` 为空 / 视频不存在 / 已删除 / 未过审 / 未开启分发 / CDN 未就绪 / Redis 暂不可用 | 忽略该次上报即可（不阻塞播放），可稍后重试 |

> 说明：`/seen` 失败**不影响视频播放**。客户端可将其视为「尽力而为」的上报（fire-and-forget）。

## 4. 调用时机建议

服务端推荐在**用户真正看到 / 开始播放**某条视频时上报：

- 进入视频详情/播放页，且**视频开始播放**时 → 上报该 `item_id`；
- 或视频在信息流中**实际曝光**（进入可视区域并停留一定时长，如 ≥1s）→ 上报。

**不要**在 `/video` 拿到 20 条时就把整页都上报——只有真正被消费的内容才应上报，
这样「没看过的视频」下次仍会被推荐。

## 5. 关于 token 与 ssid

- **登录用户（App 场景）**：去重键为 `user:seen:{user_id}`（由 `token` 解析），TTL 约 7 天，
  **与 ssid 无关**。App 只需保证 `token` 有效、正确传递即可，`ssid` 可省略。
- **游客（非 App 场景）**：去重键为 `user:seen:guest:{ssid}`，TTL 约 24 小时；
  若游客不带 ssid，服务端会为每次请求随机生成新 ssid，去重失效。

> 对 App 而言：**关键是带上有效的 `token`**。若 token 缺失或失效，请求会被当作游客，
> 去重行为将不可控（依赖 ssid），可能出现重复推荐。

## 6. 与 /video、/impression 的关系（去重来源说明）

推荐去重池是 `user:seen:{user_id}`，其写入来源有三：

| 来源 | 写入内容 |
|------|----------|
| `/video` 的"曝光即标记"（**当前默认开启**） | 返回的**整页**视频 |
| `/seen`（本接口） | 单条（客户端上报真正看过的） |
| `/impression` | 单条（客户端上报真正播放的）；**服务端现已同步写入 `user:seen`** |

> 注意（当前默认行为）：`/video` 会把整页标为已看，导致用户"没看到的"也被排除、
> 下次不再推荐。要改为"只标记真正看过的"，需关闭曝光即标记
> （服务端配置 `FEATURE_SEEN_CLIENT_REPORT=true`）。

**切换顺序（服务端配合，务必遵守）**：
1. 先确认客户端上报可靠 —— `/seen` **或** `/impression` 有持续流量；
2. 再开启 `FEATURE_SEEN_CLIENT_REPORT=true`，下线 `/video` 的曝光即标记。

> 在此之前，客户端上报**不会冲突**（幂等），且已生效于 `user:seen`（与曝光标记叠加）。

## 7. 相关接口

| 接口 | 说明 |
|------|------|
| `POST /video` | 拉取推荐列表（返回 `items[].item_id`） |
| `POST /seen` | **本接口**：上报已看 |
| `POST /impression` | 需登录：上报"真正播放"；现已同步写入推荐去重池 `user:seen` |
| `POST /track` | 需登录：埋点上报（自由 `data` 字符串） |
