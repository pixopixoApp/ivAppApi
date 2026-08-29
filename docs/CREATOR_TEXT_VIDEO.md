# Web 文本生成互动内容：运行与上线手册

## 当前状态：软回滚

截至 2026-08-29，Web Creator 对用户只开放以下链路：

1. 登录并通过邀请码获得 Creator 权限。
2. 上传 MP4，完成归一化后进入互动分析、预览和发布。
3. 未登录用户仍可浏览和体验公开内容。

文本生成视频的 API、数据表、任务状态和历史记录全部保留，但 Website 不再暴露文本创建
或重新生成入口，ivapp 的新建入口也由 `CREATOR_TEXT_TO_VIDEO_ENABLED=false` 拒绝。
不要执行数据库降级或删除历史任务。

回滚前已经进入队列的文本任务允许继续排空。`CREATOR_VIDEO_GENERATION_ENABLED=false`
负责阻止 ivadmin 接受新任务；负责排空已有任务的 Worker 应保持运行，待活动任务归零后再由
运维停止。已经生成到 `review_source` 的历史草稿仍可在 Website 接受并继续互动分析；失败的
历史文本任务不能从 Website 重新生成，用户需取消后改用 MP4 新建。

## 保留的文本链路设计

以下设计仅供将来重新启用时参考。文本链路会先由 ivadmin 规划完整视频提示词和互动说明，
再由 Ark Seedance 生成源视频；用户确认源视频后，才进入与 MP4 相同的互动分析流程。

## 服务职责

- Website：当前提供登录、邀请码、MP4 输入、互动预览和发布；仅为历史文本任务保留源视频
  确认与进度展示。
- ivapp：用户权限、每日额度、创作会话、生成任务编排、源视频确认、30 天草稿清理。
- ivadmin：提示词规划、Ark 私有凭证、Seedance 异步任务、生成视频下载验收、互动分析。
- Media cache / OSS：生成视频保持私有；沿用已有内容寻址缓存和私有 OSS 抽象。

ivapp 不保存 Ark 或文本模型凭证。浏览器也不会接触任何模型凭证或 Ark 输出地址。

## 状态闭环

文本创建的主要状态如下：

```text
queued -> planning_prompt -> submitting_video -> generating_video
       -> ingesting_video -> preparing_preview -> review_source
       -> validate_video -> normalize_video -> sample_frames
       -> find_playable_moments -> compile_preview -> ready
       -> pending_review -> published
```

用户在 `review_source` 必须选择接受或重新生成。未接受的 ready / failed / cancelled
草稿在 30 天后由 Worker 清理；已接受源不会被该清理任务删除。

## 配置

ivadmin：

- `CREATOR_VIDEO_GENERATION_ENABLED=false`
- `CREATOR_VIDEO_ARK_API_KEY`
- `CREATOR_VIDEO_ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3`
- `CREATOR_VIDEO_MODEL=doubao-seedance-2-0-260128`
- `CREATOR_VIDEO_RATIO=9:16`
- `CREATOR_VIDEO_DURATION_SECONDS=10`
- `CREATOR_VIDEO_RESOLUTION=720p`
- `CREATOR_VIDEO_GENERATE_AUDIO=true`
- `CREATOR_VIDEO_WATERMARK=false`

ivapp：

- `CREATOR_TEXT_TO_VIDEO_ENABLED=false`
- `CREATOR_VIDEO_DAILY_QUOTA=3`
- `CREATOR_VIDEO_DRAFT_TTL_DAYS=30`

两个功能开关默认关闭。不要把 Ark Key 写入仓库、前端配置或部署日志。

## 将来重新启用的顺序

1. 申请并激活一个可调用 Seedance 2.0 的 Ark Key。
2. 使用 `ivadmin-api/backend/scripts/smoke_ark_video.py` 做 5 秒、480p 冒烟测试。
3. 在 ivapp 数据库执行 Alembic 升级。
4. 将成功验证的单个 Ark Key 注入 ivadmin secret；不要配置运行期双 Key 回退。
5. 先发布 ivadmin 并开启 `CREATOR_VIDEO_GENERATION_ENABLED`，验证私有接口和 Worker。
6. 再发布 ivapp 与 Website，最后开启 `CREATOR_TEXT_TO_VIDEO_ENABLED`。
7. 观察任务成功率、Ark 429/5xx、等待时长、额度扣减和 30 天清理任务。

再次软回滚时先关闭 ivapp 的文本入口，再关闭 ivadmin 新任务生成开关。MP4 上传链路与已
进入互动分析的任务不受影响；排空历史任务后再停 Worker；数据库字段和表保留，不执行降级
删除。

## 当前凭证验证结论

2026-08-29 的真实鉴权检查中，新项目现存 Key、旧项目 `ARK_API_KEY`，以及
`admin.pixopixo.cn` 线上 AI 视频模块当前保存的 Key 均被 Ark 返回
`401 AuthenticationError: API key status is not active`。线上模块在 2026-08-28 曾有成功的
Seedance 任务，但用同一 Key 查询该成功任务时也已返回 401；当前配置与部署备份中的 Key
相同，没有发现第二份运行时 Key。检查没有提交新视频任务，因此未产生生成费用。

上线前必须由运维在火山方舟控制台重新激活该 Key，或注入一个新的已激活 Key，并重新完成
冒烟测试；在此之前两个功能开关必须保持关闭。不要把已确认失效的 Key 复制到新环境。
