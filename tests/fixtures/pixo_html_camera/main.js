(function installCameraLifecycleFixture() {
  "use strict";

  let acquiring = false;
  window.addEventListener("pixo:host-state", async function onHostState(event) {
    if (!event.detail.active || acquiring) return;
    acquiring = true;
    try {
      await PixoNative.requestCapability("cameraStream");
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "user" },
        audio: false,
      });
      document.querySelector("video").srcObject = stream;
    } finally {
      acquiring = false;
    }
  });
})();
