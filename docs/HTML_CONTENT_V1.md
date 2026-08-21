# Android HTML 互动内容 V1

运营后台可以直接接收已有 HTML ZIP，不要求内容作者预先接入 Pixo Bridge，也不要求
ZIP 内包含 `pixo-html.json`。平台以确定性规则识别入口和所需能力，只修改临时 staging
副本并注入兼容层，不调用 LLM 分析或改写业务代码。ZIP 源包保存在私有 OSS，解压只在
临时目录进行；最终资源才会进入不可变 `public/html/{item_id}/{sha256}/...` 前缀，
不依赖服务器本地媒体目录。

[`HTML_CONTENT_OPERATOR_PROMPT.md`](HTML_CONTENT_OPERATOR_PROMPT.md) 仅用于从零生成新内容；
它不是手动上传已有 ZIP 的前置条件。

HTML 是 `published_videos` 中与 `runtime` 并列的普通作品类型。推荐、Following、作者页、详情、关注、屏蔽、举报、分享和去重播放量均复用现有体系；HTML 不进入 Runtime Spec 编译器。

## 两种接入方式

运营后台“手动上传”只要求 ZIP 中至少有一个 HTML 入口和一个视频资源。平台会忽略
`__MACOSX`、`.DS_Store` 和 AppleDouble 文件，列出所有入口候选，并根据静态扫描结果
生成平台自己的 manifest。ZIP 内已有的 `pixo-html.json` 不具备控制作品 ID、作者或权限
的能力。

CLI/自动发布流水线仍可使用显式 manifest。此时每个源目录必须包含 `pixo-html.json`：

每个源目录必须包含 `pixo-html.json`：

```json
{
  "item_id": "html_neon_balance_001",
  "entry": "index.html",
  "title": "Neon Balance",
  "description": "Tilt, clap and use the camera to interact",
  "bridge_version": 1,
  "required_capabilities": [
    "motion",
    "microphoneLevel",
    "cameraStream"
  ]
}
```

能力只能取 `motion`、`microphoneLevel`、`cameraStream`、`haptics`、`mediaControl`。未写 `user_id` 时，工具按 `sha256(item_id) % 100` 稳定分配 `html_creator_001` 至 `html_creator_100`。

页面必须先适配生命周期。首次申请摄像头应在 `active=true` 后进行，离开时 Host SDK 会停止所有已登记的 MediaStream；再次进入必须重新申请：

```js
window.addEventListener("pixo:host-state", async event => {
  if (!event.detail.active) return;
  await PixoNative.requestCapability("cameraStream");
  const stream = await navigator.mediaDevices.getUserMedia({
    video: { facingMode: "user" },
    audio: false
  });
  document.querySelector("video").srcObject = stream;
});
```

麦克风互动只允许音量 Bridge，不允许原始音频：

```js
await PixoNative.requestCapability("microphoneLevel");
await PixoNative.startMicrophoneLevel();
PixoNative.on("microphoneLevel", event => {
  console.log(event.volume_score, event.transient_score);
});
```

## 手动上传兼容层（browser-v1）

手动上传固定启用 `browser-v1`。它在不改写原业务脚本的前提下提供以下兼容：

- `DeviceMotionEvent`、`DeviceOrientationEvent` 和对应浏览器事件映射到 Pixo `motion`；
- 仅音量用途的 `getUserMedia({audio:true})`、`AudioContext.createMediaStreamSource()`、
  `AnalyserNode.getByteTimeDomainData()`/`getFloatTimeDomainData()` 映射到
  `microphoneLevel`，页面拿不到原始音频；
- `getUserMedia({video:true,audio:false})` 在申请 `cameraStream` 后调用真实摄像头；
- `navigator.vibrate()` 映射到 Pixo 触觉能力；
- `active=false` 时停止摄像头、运动和麦克风等级采样。兼容模式再次进入内容时重新加载
  页面，避免复用页面保存的旧流或旧监听。

以下能力无法在不改变产品语义或扩大隐私边界的情况下安全兼容：录音/`MediaRecorder`、
语音识别、`AudioWorklet`/原始音频处理、频域音频分析、屏幕采集，以及同时请求摄像头和
麦克风。扫描发现这些能力时仍可生成隔离预览包，但状态为 `review_required`，不能从当前
运营页面直接发布。

## 本地准备与自动检查

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-html-publisher.txt
.venv/bin/playwright install chromium

export PIXO_HTML_PUBLIC_BASE_URL=https://cdn.example.com/ivapp-media/v1/public/html
./scripts/pixo_html.py ./content/html_neon_balance_001 \
  --output /tmp/html_neon_balance_001-staged
```

该变量必须包含独占 HTML 对象目录，不能只填写 origin；工具不会再回退到历史 `/pixo/html` 前缀。

工具只修改临时 staging 副本。它会：

- 检查入口和 `<video>`，抽取 Base64 视频；手动上传还会确定性识别浏览器能力和不兼容项；
- 把本地绝对引用改为 `/ivapp-media/v1/public/html/{item_id}/{package_sha256}/...`；
- 在所有 HTML 业务脚本之前注入 Native Client、配置和 Host SDK；
- 拒绝 HTTP、`file://`、越界路径、未经批准的外部资源、外部 iframe、弹窗与 meta refresh；批准来源的脚本/样式/媒体会先 vendoring；
- 注入 CSP；内联业务脚本使用精确 SHA-256 白名单，不放开通用 `unsafe-inline`；
- 用 FFmpeg 解码每个视频的真实首帧，再用 Playwright 模拟 Bridge 检查页面资源、控制台、
  兼容能力调用、原始音频隔离和 MediaStream 释放。

`package_sha256` 同时绑定源文件、Native Client、Host SDK 和适配器 revision；工具升级不会在旧版本目录下悄悄覆盖文件。

## 上传、真机预览和发布

上传所需环境变量：

```bash
export PIXO_BACKEND_URL=https://api.example.com
export PIXO_PUBLISH_KEY=...

./scripts/pixo_html.py ./content/html_neon_balance_001 --upload
```

工具向 ivapp 申请逐文件精确 OSS 策略，不读取或保存阿里云 AccessKey。批准来源的第三方 JS/CSS/媒体会先 vendoring 到不可变包，JS/CSS 内部的静态 URL 与模块依赖也会递归归档；运行期 CSP 仍只允许 `self`。

CLI 上传只写不可变 OSS 目录，不写数据库。运营后台手动上传在准备完成后生成有时效签名的
二维码；Pixo Android 扫码后会解析同一套 `RemoteHtml` 描述并进入现有 HTML 播放器。
二维码只授权当前不可变版本的预览，不会把内容发布到 Feed。Android 构建的
`pixoHtmlTrustedOrigins` 必须包含该入口 origin。

真机完成运动、麦克风、摄像头、摄像头与音量并用、Next 释放、切后台释放后，保存验收文件：

```json
{
  "item_id": "html_neon_balance_001",
  "version": "<package-sha256>",
  "verified": true,
  "device_model": "Pixel 9",
  "tested_at": "2026-08-10T12:00:00Z",
  "checks": {
    "motion": true,
    "microphone_level": true,
    "camera_stream": true,
    "camera_and_microphone_together": true,
    "next_releases_resources": true,
    "background_releases_resources": true
  }
}
```

通过后才可持久化：

```bash
export PIXO_BACKEND_URL=https://api.example.com
export PIXO_PUBLISH_KEY=...
./scripts/pixo_html.py ./content/html_neon_balance_001 \
  --publish --android-approval ./android-approval.json
```

服务端会再次验证作者、能力白名单、受信 HTTPS origin、不可变 URL 路径和远端 HTML。相同 `item_id + version` 仅允许完全相同的幂等请求，且不能覆盖另一种内容类型。

## 虚拟作者

先预览固定账号，再幂等创建：

```bash
./scripts/seed_html_creators.py --dry-run
PIXO_BACKEND_URL=https://api.example.com PIXO_PUBLISH_KEY=... \
  ./scripts/seed_html_creators.py
```

账号 provider 为 `content_pool`、source 为 `admin`、头像为空，不创建登录凭据。它们仍是普通可展示、可关注的作者。

## 安全边界

Android 只对 `RemoteHtml` 且主页面位于构建时受信 origin/当前版本目录时注入 Bridge。WebView 禁止文件与 content 访问、混合内容、弹窗、外域导航和未批准资源域；Web 权限只可能放行已声明的 `VIDEO_CAPTURE`，`AUDIO_CAPTURE` 始终拒绝。HTML 模式不开放 Runtime 的 `cameraSignals`。

`RemoteHtml` 只使用内容包中固定版本的 Native Client 与 Host SDK，不注入旧 Runtime 兼容层。Runtime 与 HTML 共用按 Android 系统权限记录的品牌引导；每项权限在一次安装周期内最多展示一次，即使用户拒绝也不会因切换作品而重复展示。

生产环境需保持三处一致：后端 `HTML_TRUSTED_ORIGINS`、Android `pixoHtmlTrustedOrigins`、发布工具 `PIXO_HTML_PUBLIC_BASE_URL`。可选资源域同样要同时进入 Android `pixoHtmlAssetOrigins` 与发布命令的 `--asset-origin`。
