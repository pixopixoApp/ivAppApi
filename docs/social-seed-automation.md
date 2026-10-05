# 内部互动种子自动化接口

## 用途与边界

本功能只用于预上线 UI 验收。首期只允许 `social_seed` 账号给公开作品点赞、
取消点赞和发表顶级评论，不支持回复、评论点赞、关注或登录。

正式发布前必须在运营后台“全局配置 → 业务配置”关闭“内部互动预览”，并从
公开列表、详情、评论区和用户资料接口复核种子数据不可见。关闭开关不会删除
数据；推荐、排名、SEO、通知和真实运营统计始终不使用种子互动。

## 初始化账号

账号批次固定为 `prelaunch-v1`，用途固定为 `social_seed`。先试运行 5 个：

```sh
.venv/bin/python scripts/init_social_seed_accounts.py --count 5
```

验收昵称和头像后补齐到 100 个：

```sh
.venv/bin/python scripts/init_social_seed_accounts.py --count 100
```

脚本幂等，只补齐缺失账号或头像，最终校验批次恰好为 100 个账号。账号没有
登录凭据。头像由 DiceBear `10.x` 的 12 个 CC0 风格确定性混排生成，复制到
OSS 的 `internal/social-seed/prelaunch-v1/avatars/natural-v2/` 前缀，不依赖
运行时外链。

- 清单：`data/social-seed/prelaunch-v1-manifest.json`
- 许可证：`docs/licenses/dicebear-social-seed-styles-cc0.md`
- 清单字段：账号 ID、昵称、批次、头像 seed、版本、许可证、OSS key、SHA-256

## 鉴权和账号查询

所有接口都使用 ivapp 现有的 `X-Publish-Key`。机器人只能从账号查询接口获取
指定批次账号，不能使用后台发布账号，也不能创建登录会话。

```sh
curl --fail-with-body \
  -H "X-Publish-Key: $PIXOPIXO_PUBLISH_KEY" \
  "$PIXOPIXO_IVAPP_URL/internal/v1/social-seed/accounts?batch_id=prelaunch-v1"
```

查询参数：

- `batch_id`：必填，机器人必须把该值原样放入后续写请求。
- `cursor`：上页返回的不透明游标。
- `limit`：1–200，默认 50。
- `enabled_only`：默认 `true`。

响应按 `user_id` 稳定排序，只返回 `user_id`、`nickname`、`avatar_url`。

## 点赞和取消点赞

点赞是并发幂等操作，数据库仍以 `(video_id, user_id)` 唯一约束兜底。重复调用
不会重复增加计数。每次调用（包括无变化重放）都计入与普通账号相同的频率限制。

```sh
curl --fail-with-body -X PUT \
  -H "X-Publish-Key: $PIXOPIXO_PUBLISH_KEY" \
  -H "Content-Type: application/json" \
  --data "$SOCIAL_SEED_ACTOR_JSON" \
  "$PIXOPIXO_IVAPP_URL/internal/v1/social-seed/videos/$VIDEO_ID/like"
```

```sh
curl --fail-with-body -X DELETE \
  -H "X-Publish-Key: $PIXOPIXO_PUBLISH_KEY" \
  -H "Content-Type: application/json" \
  --data "$SOCIAL_SEED_ACTOR_JSON" \
  "$PIXOPIXO_IVAPP_URL/internal/v1/social-seed/videos/$VIDEO_ID/like"
```

`SOCIAL_SEED_ACTOR_JSON` 由机器人生成，内容包含 `actor_user_id` 和固定的
`batch_id`，密钥和账号 ID 不写入代码仓库。

## 发表评论

`idempotency_key` 对同一账号必须唯一，建议格式为
`<机器人任务ID>:<作品ID>:<序号>`。网络超时后使用完全相同的请求体和键重试；
服务返回原评论。相同键对应不同作品或正文时返回 `409`。

```sh
curl --fail-with-body -X POST \
  -H "X-Publish-Key: $PIXOPIXO_PUBLISH_KEY" \
  -H "Content-Type: application/json" \
  --data-binary "@$SOCIAL_SEED_COMMENT_FILE" \
  "$PIXOPIXO_IVAPP_URL/internal/v1/social-seed/videos/$VIDEO_ID/comments"
```

`social-seed-comment.json` 的结构：

```json
{
  "actor_user_id": "seed account id from the account API",
  "batch_id": "prelaunch-v1",
  "body": "A top-level comment of at most 280 Unicode characters.",
  "idempotency_key": "robot-run-42:video-123:1"
}
```

评论遵守普通账号相同的内容状态、屏蔽关系和频率限制。种子互动不产生站内通知。
机器人禁止直接写表、修改计数、绕过审核、使用发布账号或尝试评论回复。

## 预览开关

ivapp 内部接口：

- `GET /internal/v1/social-seed-preview`
- `PUT /internal/v1/social-seed-preview`

adminapi 代理接口：

- `GET /api/v1/settings/social-seed-preview`
- `PUT /api/v1/settings/social-seed-preview`

设置返回 `enabled`、`version`、`updated_by`、`updated_at`。数据库默认关闭；
admin 和 manager 可在运营后台修改。开启后公开展示计数为真实计数加种子计数，
并展示种子评论和资料；关闭后只返回真实数据。

## 校准与排障

计数校准会分别重算真实和种子计数：

```sh
curl --fail-with-body -X POST \
  -H "X-Publish-Key: $PIXOPIXO_PUBLISH_KEY" \
  "$PIXOPIXO_IVAPP_URL/internal/v1/social/reconcile"
```

排障顺序：

1. 用账号查询接口确认账号属于请求中的批次且 `enabled=true`。
2. `403` 时检查账号用途、批次、屏蔽关系；`429` 时按响应退避，不要换键重放。
3. 评论超时使用原幂等键重试；`409` 表示同一个键被用于不同请求。
4. 展示不一致时先读取预览开关，再执行计数校准并复查。
5. 头像异常时对照清单的 OSS key 和 SHA-256；不要改为 DiceBear 外链。

正式发布检查固定包含：关闭预览开关，确认公开接口无法访问种子账号、评论和
种子计数，同时确认推荐、排名、SEO、通知与真实统计未发生变化。
