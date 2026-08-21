# OSS 媒体存储与稳定切换手册

## 最终边界

- 只有 `ivapp` 配置 `ALIYUN_OSS_ACCESS_KEY_ID/SECRET`；Android、浏览器、ivadmin 和 HTML 工具只拿到短时、精确到对象键/大小/MIME/SHA-256 的 PostObject 策略或私有下载 URL。
- 复用现有 bucket 与 CDN 域名，但所有新对象只写 `ivapp-media/v1/`。禁止写入历史 `motioncue/`、`pixo/html/` 等目录。
- OSS 对象永久保留。更新、下架、注销、上传 session 过期均只解除数据库绑定，不删除对象；bucket 不得为该前缀配置生命周期清理。
- 最终态 API/Worker/ivadmin 不挂载持久媒体盘。分析时只在 `/tmp` 建任务目录；源视频保留在媒体 OSS，业务 JSON 直接入 ivadmin 数据库，抽帧和中间工作区不会上传至 Pixo OSS，任务完成后自动清理。切换观察期通过单独 Compose override 暂时挂载旧盘。
- 内部上传会话按“用途 + 目标 + 上下文 + 相对路径 + 大小 + SHA-256”生成稳定幂等键。已完成会话不重复上传；未完成/过期会话只重签同一 ingress 对象。

## 对象目录

```text
ivapp-media/v1/
├── ingress/client/{session_id}/{object_id}.mp4
├── ingress/internal/{session_id}/{object_id}.{ext}
├── private/creator-sources/{shard}/{object_id}/source.mp4
├── private/admin-runs/{run_id}/sources/{object_id}/source.mp4
├── private/admin-runs/{run_id}/versions/{version}/{snapshot_id}/{relative_path}
├── private/admin-runs/{run_id}/publish-inputs/{version}/{snapshot_id}/{relative_path}
├── private/imports/{batch_id}/{object_id}/{legacy_relative_path}
├── public/avatars/{shard}/{object_id}.{ext}
├── public/runtime/{item_id}/{publication_id}/single.mp4
├── public/runtime/{item_id}/{publication_id}/clips/{clip_id}.mp4
└── public/html/{item_id}/{package_sha256}/{relative_path}
```

对象键不含用户 ID。业务归属、当前版本和历史发布由 MySQL 中的 `media_objects`、`media_upload_sessions`、`published_media_assets`、`html_packages` 等表维护。

## OSS/CDN 前置配置

1. 保留现有 bucket 和 CDN，加 CDN 回源路径 `ivapp-media/v1/`。
2. bucket CORS 只允许后台 Web 的正式 HTTPS Origin 使用 `POST`、`GET`、`HEAD`，允许 `Content-Type`/所需 `x-oss-*` 头并暴露 `ETag`；不开放 `PUT` 和 credentials。Android 原生请求不依赖 CORS，但仍只使用 HTTPS 精确策略。
3. `ivapp-media/v1/public/**` 允许 CDN 公读；其余对象保持 private。
4. 不给 `ivapp-media/v1/**` 配置生命周期删除、覆盖同步或同名改写。
5. 首次部署前确认 Compose 使用的共享网络已存在：

```bash
docker network inspect pixo-backend >/dev/null 2>&1 || docker network create pixo-backend
```

6. 生产设置：

```env
MEDIA_STORAGE_MODE=oss
MEDIA_READ_FALLBACK_LOCAL=true
ALIYUN_OSS_REGION=cn-beijing
ALIYUN_OSS_BUCKET=<existing-bucket>
ALIYUN_OSS_ACCESS_KEY_ID=<ivapp-only>
ALIYUN_OSS_ACCESS_KEY_SECRET=<ivapp-only>
ALIYUN_OSS_PUBLIC_BASE_URL=https://<existing-cdn-domain>
OSS_ROOT_PREFIX=ivapp-media/v1
PIXO_HTML_PUBLIC_BASE_URL=https://<existing-cdn-domain>/ivapp-media/v1/public/html
HTML_TRUSTED_ORIGINS=https://<existing-cdn-domain>
```

`ALIYUN_OSS_PUBLIC_BASE_URL` 必须是无路径、无查询参数的 HTTPS origin；对象路径由 ivapp 统一追加，避免 CDN 基地址意外混入历史目录。

ivadmin 只设置 `MEDIA_STORAGE_MODE=oss`、`IVAPP_BASE_URL`、`IVAPP_PUBLISH_KEY`，绝不能配置阿里云 AccessKey。

## 上线步骤

### 1. 加法部署

先备份 MySQL，部署 ivapp revision `20260811_0007` 与 ivadmin 新字段。此时保持 `MEDIA_STORAGE_MODE=local`，并使用读写过渡挂载，验证旧播放与管理链路不变：

```bash
# 分别在 ivapp-backend 与 ivadmin-api 目录执行
docker-compose -f docker-compose.yml -f docker-compose.media-transition.yml up -d
```

不要在 local 阶段只启动主 Compose；主 Compose 是最终无媒体盘形态。

### 2. 全量盘点（默认只读）

```bash
mkdir -p migration-manifests
docker-compose -f docker-compose.yml -f docker-compose.media-migration.yml run --rm api \
  python -m scripts.migrate_media_to_oss \
  --batch-id ivapp-20260811 \
  --manifest /migration-manifests/ivapp-20260811.json
```

在 ivadmin 目录执行：

```bash
mkdir -p migration-manifests
docker-compose -f docker-compose.yml -f docker-compose.media-migration.yml run --rm api \
  python -m scripts.migrate_runs_to_oss \
  --batch-id ivadmin-20260811 \
  --manifest /migration-manifests/ivadmin-20260811.json
```

核对文件数、总字节数和逐文件 SHA-256。两个脚本都会枚举孤儿文件；不会因为数据库没有引用而跳过。还必须确认 manifest 中 `preflight.ok=true`：数据库引用的缺失文件、越出媒体根目录的路径以及符号链接都会列出并阻止 `--apply`，避免卸载旧盘后才暴露坏引用。

### 3. 迁移（可断点重跑）

进入短维护窗：在网关暂停 Creator/后台上传与发布入口，等待在途任务结束，停止 ivapp worker，并停止 ivadmin API（其进程内分析 worker 也会随之停止）。ivapp API 暂时保留，供稍后的 ivadmin 迁移申请 OSS 策略：

```bash
# ivapp 目录
docker-compose -f docker-compose.yml -f docker-compose.media-transition.yml stop worker

# ivadmin 目录
docker-compose -f docker-compose.yml -f docker-compose.media-transition.yml stop api
```

先只给 **ivapp** 的盘点命令加 `--apply`。该脚本直接使用 ivapp 独占的 AK，把 `MEDIA_ROOT` 全量迁移并写好 Runtime、头像和 Creator 绑定。完成并抽检后，将 **ivapp** 的 `.env` 改成 `MEDIA_STORAGE_MODE=oss`、`MEDIA_READ_FALLBACK_LOCAL=true`，用只读回退挂载仅启动 API；worker 此时仍保持停止：

```bash
docker-compose -f docker-compose.yml -f docker-compose.media-fallback.yml up -d api
```

随后给 **ivadmin** 的盘点命令加 `--apply`。ivadmin 不持有 AK，它必须在 ivapp 已进入 OSS 模式后，通过仍在线的 ivapp API 获取精确上传策略。迁移完成后把 ivadmin `.env` 改为 `MEDIA_STORAGE_MODE=oss`，再以只读回退挂载启动 ivadmin API：

```bash
docker-compose -f docker-compose.yml -f docker-compose.media-fallback.yml up -d api
```

两个脚本都会先写 `private/imports` 或规范化私有目录，再在 OSS 内复制公开对象、验证大小和 SHA-256 元数据并写数据库绑定。目标对象禁止覆盖；同一批次和同一内容清单重跑会复用同一个上传会话与对象。脚本绝不删除本地或 OSS 文件，零字节占位文件和无主视频的 Story 也会进入清单。

完成标准：

- manifest 文件数与源目录 `find` 统计一致；
- manifest 总字节数与源目录一致；
- `preflight.ok=true` 且不存在未解析数据库引用或符号链接；
- 所有识别到的 Runtime、Story、头像、Creator source 都有 `media_object_id`；
- 所有孤儿均有 `migration_import` 对象；
- 随机抽检与全量脚本验证的 SHA-256 一致。

### 4. 写流量切换

确认 ivapp 与 ivadmin 都已处于 OSS 模式后，先发布 ivadmin-web，再发布 Android，最后恢复写入口并启动 ivapp worker：

```bash
# ivapp 目录
docker-compose -f docker-compose.yml -f docker-compose.media-fallback.yml up -d worker
```

新 Android 创作者上传没有 multipart 回退；若签名策略不可用会明确失败，不会把视频落到应用服务器。

HTML 工具必须配置 `PIXO_BACKEND_URL`、`PIXO_PUBLISH_KEY` 和新的 `PIXO_HTML_PUBLIC_BASE_URL`。工具不再读取 OSS AccessKey，会把 Base64 视频和批准来源的三方 JS/CSS/媒体 vendoring 后逐文件直传。

### 5. 读流量观察

初期保留 `MEDIA_READ_FALLBACK_LOCAL=true`，观察：

- OSS 初始化/完成成功率和 SHA 校验失败数；
- CDN 2xx/206、回源失败和首帧耗时；
- ivadmin 临时目录峰值、快照耗时和进程退出后的目录残留；
- Creator 的上传、分析、预览、发布完整链路；
- HTML 包的摄像头/麦克风/运动真机释放。

确认所有旧 URL 已绑定 OSS 后设置 `MEDIA_READ_FALLBACK_LOCAL=false`，只用主 Compose 重新创建服务，彻底卸载旧媒体盘：

```bash
docker-compose -f docker-compose.yml up -d --force-recreate
```

迁移专用 override 以后只在审计或补迁时使用。

### 6. 回滚

写切换前可切回上一镜像和 `MEDIA_STORAGE_MODE=local`，并恢复 `media-transition` 读写挂载。产生第一笔 OSS-only 新写入后，不再把全站切回 local（新对象本来就不在旧盘）；应回滚到首个 OSS 兼容镜像并继续使用 OSS，或仅回退 Web/Android。不要回滚数据库、不要删除 OSS。观察期结束后本地盘只做离线归档，不再作为在线回滚依赖。

## 未来换 bucket/节点

保持对象键 `ivapp-media/v1/...` 不变，将整个前缀按 ETag/大小/SHA 校验复制到新 bucket；切 CDN 回源并更新 ivapp 的 bucket/region 配置即可。数据库保存的是对象键和稳定 CDN URL体系，不需要重新设计目录或混合迁移历史业务前缀。
