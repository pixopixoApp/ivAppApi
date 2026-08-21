"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const listeners = new Map();
const nativeCalls = [];
const mediaNodes = [{ paused: false, pause() { this.paused = true; } }];
const track = {
  readyState: "live",
  listeners: new Map(),
  addEventListener(name, callback) { this.listeners.set(name, callback); },
  stop() {
    this.readyState = "ended";
    const callback = this.listeners.get("ended");
    if (callback) callback();
  },
};
const stream = { getTracks() { return [track]; } };
let rawGetUserMediaCalls = 0;

function addEventListener(name, callback) {
  const callbacks = listeners.get(name) || [];
  callbacks.push(callback);
  listeners.set(name, callbacks);
}

function dispatchEvent(event) {
  (listeners.get(event.type) || []).forEach((callback) => callback(event));
}

const documentListeners = new Map();
const root = {
  __PIXO_HTML_CONFIG__: {
    item_id: "html-test",
    bridge_version: 1,
    required_capabilities: ["motion", "microphoneLevel", "cameraStream"],
  },
  __motionCueHostShouldPlay: false,
  addEventListener,
  dispatchEvent,
  navigator: {
    webkitGetUserMedia() {
      throw new Error("legacy capture API must not be reached");
    },
    mediaDevices: {
      async getUserMedia(constraints) {
        rawGetUserMediaCalls += 1;
        assert.equal(constraints.audio, false);
        return stream;
      },
      async enumerateDevices() {
        return [
          { kind: "videoinput", deviceId: "camera" },
          { kind: "audioinput", deviceId: "microphone" },
        ];
      },
      async getDisplayMedia() {
        throw new Error("raw display API must not be reached");
      },
    },
  },
  DeviceMotionEvent: function DeviceMotionEvent() {},
  webkitSpeechRecognition: function SpeechRecognition() {},
  document: {
    hidden: false,
    querySelectorAll() { return mediaNodes; },
    addEventListener(name, callback) { documentListeners.set(name, callback); },
  },
  PixoNative: {
    request(method, params) {
      nativeCalls.push({ method, params });
      if (method === "deviceInfo") {
        return Promise.resolve({
          platform: "android",
          bridge_version: 1,
          capabilities: ["motion", "cameraStream", "haptics"],
          supportsMotion: true,
          supportsCameraStream: true,
        });
      }
      return Promise.resolve({ status: "active" });
    },
    receive() {},
    on() { return function unsubscribe() {}; },
    hasTransport() { return true; },
  },
};
root.window = root;

const sdkPath = path.resolve(__dirname, "../../scripts/pixo_html_host_sdk.js");
vm.runInNewContext(fs.readFileSync(sdkPath, "utf8"), { window: root });

(async function run() {
  assert.deepEqual(
    Object.keys(root.PixoNative.capabilities).filter((name) => root.PixoNative.capabilities[name]),
    ["motion", "microphoneLevel", "cameraStream"],
  );
  assert.equal(root.PixoNative.vibrate, undefined);
  assert.equal(root.DeviceMotionEvent, undefined);
  assert.equal(root.webkitSpeechRecognition, undefined);
  assert.equal(root.navigator.webkitGetUserMedia, undefined);
  await assert.rejects(root.PixoNative.requestCapability("motion"), /not active/);
  await assert.rejects(root.PixoNative.startMotion(), /not active/);
  await assert.rejects(
    root.PixoNative.request("haptic", { style: "light" }),
    /did not declare/,
  );

  dispatchEvent({ type: "pixo:host-state", detail: { active: true, allowAudio: true } });
  await root.PixoNative.startMotion();
  await root.PixoNative.startMicrophoneLevel();
  const info = await root.PixoNative.getDeviceInfo();
  assert.equal(Array.from(info.capabilities).join(","), "motion,cameraStream");
  assert.equal(info.supportsCameraSignals, false);
  assert.equal(info.supportsHaptics, false);

  const acquired = await root.navigator.mediaDevices.getUserMedia({ video: true, audio: false });
  assert.equal(acquired, stream);
  assert.equal(root.__PIXO_HTML_HOST_SDK__.trackedStreamCount(), 1);
  await assert.rejects(
    root.navigator.mediaDevices.getUserMedia({ audio: true }),
    /rawMicrophoneAudio/,
  );
  assert.equal(rawGetUserMediaCalls, 1);
  assert.deepEqual(
    await root.navigator.mediaDevices.enumerateDevices(),
    [{ kind: "videoinput", deviceId: "camera" }],
  );
  await assert.rejects(root.navigator.mediaDevices.getDisplayMedia(), /displayCapture/);

  dispatchEvent({ type: "pixo:host-state", detail: { active: false, allowAudio: false } });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(track.readyState, "ended");
  assert.equal(root.__PIXO_HTML_HOST_SDK__.trackedStreamCount(), 0);
  assert.equal(mediaNodes[0].paused, true);
  assert.ok(nativeCalls.some((call) => call.method === "stopMotion"));
  assert.ok(nativeCalls.some((call) => call.method === "stopMicrophoneLevel"));

  await assert.rejects(
    root.navigator.mediaDevices.getUserMedia({ video: true, audio: false }),
    /not active/,
  );
  assert.equal(rawGetUserMediaCalls, 1);
})();
