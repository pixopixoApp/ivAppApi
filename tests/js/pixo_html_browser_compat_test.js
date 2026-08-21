"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const listeners = new Map();
const nativeListeners = new Map();
const nativeCalls = [];
let rawCameraCalls = 0;
let reloadCalls = 0;

function addEventListener(name, callback) {
  const callbacks = listeners.get(name) || [];
  callbacks.push(callback);
  listeners.set(name, callbacks);
}

function removeEventListener(name, callback) {
  const callbacks = listeners.get(name) || [];
  listeners.set(name, callbacks.filter((candidate) => candidate !== callback));
}

function dispatchEvent(event) {
  event.stopImmediatePropagation = event.stopImmediatePropagation || function stop() {
    this.__stopped = true;
  };
  for (const callback of listeners.get(event.type) || []) {
    callback(event);
    if (event.__stopped) break;
  }
  return true;
}

class FakeEvent {
  constructor(type) { this.type = type; }
  stopImmediatePropagation() { this.__stopped = true; }
}

class FakeAnalyser {
  constructor() { this.fftSize = 1024; }
  getByteTimeDomainData(array) { array.fill(128); }
  getFloatTimeDomainData(array) { array.fill(0); }
  getByteFrequencyData(array) { array.fill(0); }
  getFloatFrequencyData(array) { array.fill(-100); }
}

class FakeAudioContext {
  createMediaStreamSource() { throw new Error("raw microphone stream reached AudioContext"); }
  createAnalyser() { return new FakeAnalyser(); }
  close() { return Promise.resolve(); }
}

const cameraTrack = {
  readyState: "live",
  listeners: new Map(),
  addEventListener(name, callback) { this.listeners.set(name, callback); },
  stop() {
    this.readyState = "ended";
    const callback = this.listeners.get("ended");
    if (callback) callback();
  },
};
const cameraStream = { getTracks() { return [cameraTrack]; } };

const root = {
  __PIXO_HTML_CONFIG__: {
    item_id: "html-browser-compat",
    bridge_version: 1,
    required_capabilities: ["motion", "microphoneLevel", "cameraStream", "haptics"],
    compatibility_profile: "browser-v1",
    restart_on_reactivate: true,
  },
  __motionCueHostShouldPlay: false,
  addEventListener,
  removeEventListener,
  dispatchEvent,
  Event: FakeEvent,
  AudioContext: FakeAudioContext,
  navigator: {
    mediaDevices: {
      async getUserMedia(constraints) {
        rawCameraCalls += 1;
        assert.ok(constraints.video);
        assert.equal(constraints.audio, false);
        return cameraStream;
      },
      async enumerateDevices() {
        return [
          { kind: "videoinput", deviceId: "camera" },
          { kind: "audioinput", deviceId: "microphone" },
        ];
      },
    },
  },
  document: {
    hidden: false,
    querySelectorAll() { return []; },
    addEventListener() {},
  },
  location: { reload() { reloadCalls += 1; } },
  PixoNative: {
    request(method, params) {
      nativeCalls.push({ method, params });
      if (method === "requestCapability") return Promise.resolve({ status: "granted" });
      if (method === "deviceInfo") {
        return Promise.resolve({
          platform: "android",
          bridge_version: 1,
          capabilities: ["motion", "microphoneLevel", "cameraStream", "haptics"],
        });
      }
      return Promise.resolve({ status: "active" });
    },
    receive() {},
    on(name, callback) {
      nativeListeners.set(name, callback);
      return function unsubscribe() { nativeListeners.delete(name); };
    },
    hasTransport() { return true; },
  },
};
root.window = root;

const sdkPath = path.resolve(__dirname, "../../scripts/pixo_html_host_sdk.js");
vm.runInNewContext(fs.readFileSync(sdkPath, "utf8"), { window: root });

function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

(async function run() {
  dispatchEvent({ type: "pixo:host-state", detail: { active: true, allowAudio: true } });
  assert.equal(root.__PIXO_HTML_HOST_SDK__.compatibilityProfile, "browser-v1");
  assert.equal(await root.DeviceMotionEvent.requestPermission(), "granted");

  let observedMotion = null;
  const onMotion = (event) => { observedMotion = event; };
  root.addEventListener("devicemotion", onMotion);
  await flush();
  nativeListeners.get("motion")({
    acceleration_x: 30,
    acceleration_y: 2,
    acceleration_z: 1,
    shake_score: 90,
  });
  assert.equal(observedMotion.accelerationIncludingGravity.x, 30);
  assert.equal(observedMotion.shake_score, 90);

  const microphoneStream = await root.navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: false },
    video: false,
  });
  assert.equal(rawCameraCalls, 0);
  const context = new root.AudioContext();
  const source = context.createMediaStreamSource(microphoneStream);
  const analyser = context.createAnalyser();
  source.connect(analyser);
  nativeListeners.get("microphoneLevel")({ rms: 0.25, volume_score: 25 });
  const samples = new Uint8Array(analyser.fftSize);
  analyser.getByteTimeDomainData(samples);
  const rms = Math.sqrt(samples.reduce((sum, value) => {
    const centered = (value - 128) / 128;
    return sum + centered * centered;
  }, 0) / samples.length);
  assert.ok(Math.abs(rms - 0.25) < 0.01);
  assert.equal(root.__PIXO_HTML_HOST_SDK__.trackedStreamCount(), 1);

  const camera = await root.navigator.mediaDevices.getUserMedia({ video: true, audio: false });
  assert.equal(camera, cameraStream);
  assert.equal(rawCameraCalls, 1);
  await assert.rejects(
    root.navigator.mediaDevices.getUserMedia({ video: true, audio: true }),
    /combined camera and microphone/,
  );

  assert.equal(root.navigator.vibrate(150), true);
  await flush();
  assert.ok(nativeCalls.some((call) => call.method === "haptic"));

  dispatchEvent({ type: "pixo:host-state", detail: { active: false, allowAudio: false } });
  await flush();
  assert.equal(microphoneStream.getTracks().length, 0);
  assert.equal(cameraTrack.readyState, "ended");
  assert.equal(root.__PIXO_HTML_HOST_SDK__.trackedStreamCount(), 0);

  dispatchEvent({ type: "pixo:host-state", detail: { active: true, allowAudio: true } });
  await flush();
  assert.equal(reloadCalls, 1);
})();
