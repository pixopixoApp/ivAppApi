# Android Creator：AI 来源与 Branch Story 上线手册

## 已实现范围

Android Creator 的来源与玩法已经解耦：

1. 用户上传视频，或使用 5 Credits 生成 5 秒 AI 来源。
2. 来源准备完成后只进入确认/玩法选择，不会自动启动互动分析。
3. `AI Auto Interactions` 沿用原有分析能力和完整互动类型。
4. `Branch Story` 使用 A 开场、B 成功、C 未达成三段视频。B/C 均为 5 秒；
   每个 AI 结局 5 Credits，最多一侧可免费复用原视频片尾。
5. 用户手动切换互动类型时，Android 使用服务端下发的确定性选项立即预览；
   保存也由确定性编译器完成，不调用 AI、不扣 Credits。
6. Story 必须完整体验 B、C 两条路径后才能保存修改或发布。

新任务的视频提供方固定为官方 `MiniMax-Hailuo-2.3` 公共 API。供应商生成 6 秒
768P 素材，ivadmin 入库前确定性裁切、转码为产品要求的 5 秒 9:16 来源。历史 Ark
任务仍可查询或排空，新任务不会回退到 Ark，也不会把模型凭证发给 Android、浏览器
或 ivapp。

## 计费和任务语义

- 新用户一次性获得 5 Credits。
- 受邀用户首次在 Android 登录后，邀请人一次性获得 10 Credits。
- AI 来源：5 秒，预留 5 Credits，视频准备成功后结算；失败或取消后释放。
- Story 结局：每个 5 秒，分别预留和结算 5 Credits。一侧失败不会影响另一侧，
  只重试失败的一侧。
- Credits 余额是唯一用量限制；没有隐藏的每日 3 次配额。
- 请求 ID、任务和生成结果均持久化。离开页面不会取消任务；重新打开后会继续查询。
- 已成功的 Story 结局立即视为持久结果，不会因另一侧失败而被草稿清理器删除。

## Story 播放规则

- 不复用片尾时，A 为完整来源视频。
- 复用片尾时，切点两侧各至少保留 1 秒；A 为切点前部分，B 或 C 为片尾。
- A 到达分支点后暂停，输入能力准备完成后给用户完整 4 秒操作时间。
- 成功立即播放 B；未达成或超时播放 C。
- B、C 播放结束后结束体验，不回到上一个互动点。

## 服务职责

- Android：页面、即时互动替换、本地预览、草稿恢复与服务端任务重连。
- ivapp：用户权限、Credits、创作会话、Story 计划、分支状态、预览确认和发布。
- ivadmin：MiniMax 私有凭证、任务提交/查询、视频下载验收、5 秒归一化、切片和首帧。
- Media cache / OSS：保存规范化来源、A/B/C 片段和发布素材。

## 配置

ivadmin 必需配置：

- `CREATOR_INTERNAL_KEY`：与 ivapp 完全一致的随机内部密钥。
- `CREATOR_VIDEO_GENERATION_ENABLED=true`
- `CREATOR_BRANCH_STORY_ENABLED=true`
- `CREATOR_VIDEO_GENERATION_WORKER_ENABLED=true`
- `CREATOR_VIDEO_MINIMAX_API_KEY`：从部署密钥存储注入。
- `CREATOR_VIDEO_MINIMAX_BASE_URL=https://api.minimaxi.com`
- `CREATOR_VIDEO_MODEL=MiniMax-Hailuo-2.3`
- `CREATOR_VIDEO_DURATION_SECONDS=5`
- `CREATOR_VIDEO_RESOLUTION=768P`
- `MEDIA_STORAGE_MODE=oss`：Story 首帧必须通过私有对象存储生成短时下载地址。

`CREATOR_VIDEO_ARK_API_KEY` 只用于排空历史任务，可以留空。启动检查会拒绝模型、
时长、分辨率或 MiniMax Key 缺失的半启用部署，避免用户进入一个必然失败的入口。

ivapp 必需配置：

- `IVADMIN_BASE_URL`：ivadmin 内网地址。
- `CREATOR_INTERNAL_KEY`：与 ivadmin 相同。
- `CREATOR_TEXT_TO_VIDEO_ENABLED=true`
- `CREATOR_BRANCH_STORY_ENABLED=true`
- `CREATOR_VIDEO_DRAFT_TTL_DAYS=30`

`CREATOR_VIDEO_DAILY_QUOTA` 已废弃，保留字段仅用于旧客户端响应兼容。

## 上线顺序

1. 备份数据库，并分别升级 ivadmin、ivapp 的数据库结构。
2. 将 PixoMini 已验证的 MiniMax 服务端 Key 注入 ivadmin；不要复制进仓库或客户端。
3. 先发布 ivadmin，确认 Worker、共享媒体缓存、私有 OSS 和内部接口正常。
4. 在隔离测试账号上做一次真实 5 秒来源和一次带首帧的 5 秒 Story 生成。
5. 验证失败退款、重复请求、B/C 两路播放、三段素材发布和审核状态。
6. 发布 ivapp 和 Android，先开启文本来源，再开启 Branch Story。
7. 观察生成耗时、失败率、429/5xx、Credits 对账、媒体备份和 CDN 发布状态。

本次代码验收只使用模拟响应和本地媒体，没有调用真实 MiniMax API。真实生成冒烟测试由
持有生产 Key 的发布人员执行。

## 回滚

先关闭 ivapp 的 `CREATOR_BRANCH_STORY_ENABLED` 和文本来源入口，阻止新任务；再让 ivadmin
排空已经进入队列的任务。已成功来源、结局、版本和发布内容继续可用。不要通过数据库降级
删除用户任务或媒体。
