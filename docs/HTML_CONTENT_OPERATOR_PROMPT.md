# Pixo 离线 HTML 互动内容生成提示词（V1）

将以下整段提示词给内容制作工具或运营同事。产物请打成 ZIP 后在 ivadmin 的“HTML 内容”页面上传；不要在 ZIP 中放 `pixo-html.json` 或 `pixo-host/`，平台会生成并注入它们。

```text
你要生成一个可在 Pixo Android App 中离线运行的 HTML 互动内容包。

交付格式：一个 ZIP。根目录优先放 index.html；所有图片、字体、音频、视频和第三方 JavaScript/CSS 都必须放在 ZIP 内，并使用相对路径引用。不要使用任何网络请求、CDN、外部脚本、外部 iframe、HTTP/HTTPS URL、file:// URL、绝对本地路径或弹窗。不要创建 pixo-host/ 目录；这是平台保留目录。

页面应适配竖屏手机，并在初始状态展示静态说明和开始按钮。不要假定页面一打开就已经有摄像头、麦克风或运动权限。所有媒体资源须是本地相对路径；视频推荐 H.264/AAC MP4，并可存在多个文件。

Pixo 原生桥接 API（平台会在你的业务脚本之前注入）：

1) 查询/请求能力
   const info = await PixoNative.getDeviceInfo();
   await PixoNative.requestCapability("motion");
   await PixoNative.requestCapability("microphoneLevel");
   await PixoNative.requestCapability("cameraStream");
   await PixoNative.requestCapability("haptics");
   await PixoNative.requestCapability("mediaControl");
   只能请求本内容实际使用的能力；请求前应由用户点击开始互动。

2) 运动（不能使用 DeviceMotionEvent、DeviceOrientationEvent 或浏览器传感器 API）
   await PixoNative.requestCapability("motion");
   await PixoNative.startMotion();
   const offMotion = PixoNative.on("motion", (event) => {
     // event.beta / event.gamma：倾斜角
     // event.acceleration_x / acceleration_y / acceleration_z
     // event.acceleration_magnitude / event.shake_score
   });
   // 离开互动时：await PixoNative.stopMotion(); offMotion();

3) 麦克风音量（不提供录音或原始音频；绝不能请求 audio getUserMedia）
   await PixoNative.requestCapability("microphoneLevel");
   await PixoNative.startMicrophoneLevel();
   const offMic = PixoNative.on("microphoneLevel", (event) => {
     // event.volume_score: 0~100
     // event.peak_score: 0~100
     // event.transient_score: 突发声强，可作拍手提示
   });
   // 离开互动时：await PixoNative.stopMicrophoneLevel(); offMic();

4) 摄像头（使用标准 MediaStream，但必须先请求 cameraStream；只允许 video，audio 必须为 false）
   await PixoNative.requestCapability("cameraStream");
   const stream = await navigator.mediaDevices.getUserMedia({
     video: { facingMode: "user" },
     audio: false,
   });
   document.querySelector("video").srcObject = stream;
   // 页面收到 inactive 时不要保存旧 stream；重新 active 后由用户重新请求。

5) 触觉与媒体
   await PixoNative.requestCapability("haptics");
   await PixoNative.vibrate("light"); // light、medium 或 heavy
   await PixoNative.requestCapability("mediaControl");
   await PixoNative.setMediaPlayback({ play: true, muted: false, volume: 0.8 });

6) 生命周期
   window.addEventListener("pixo:host-state", (event) => {
     const active = event.detail && event.detail.active === true;
     if (!active) {
       document.querySelectorAll("video,audio").forEach((node) => node.pause());
       // 停止你自己保存的动画/定时器；平台会停止已登记的摄像头流与原生采样。
     }
   });

禁止：PixoNative.startCameraSignals、定位、文件、蓝牙、NFC、录音保存、语音识别、getUserMedia({audio:true})、外部网络资源。不要把 Android 对象或权限实现暴露给页面。

请输出完整可运行文件，不要只输出代码片段；确保所有引用路径相对且存在。
```

## 平台负责的事

- ZIP 会先直传私有 OSS；解压、适配和校验只发生在临时目录，不会作为服务器本地文件保存。
- 平台注入固定版本的 `PixoNative` 客户端和 Host SDK，改写本地绝对路径/Base64 视频，并把最终所有文件上传到不可变公共 OSS 目录。
- 平台拦截未声明能力、原始音频、外域跳转、外部 iframe、混合内容和未打包资源。
- Android 只在页面真正调用能力时走品牌引导/系统权限；已授权的 App 权限不会重复弹系统框。
