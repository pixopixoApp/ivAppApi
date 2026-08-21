/* Pinned Pixo Native Client v1. Kept with the publisher so reviewed packages
 * do not depend on an Android source checkout at build time. */
(function installPixoNativeClient(root) {
  "use strict";
  if (root.PixoNative && typeof root.PixoNative.request === "function") return;
  var pending = new Map(), listeners = new Map(), sequence = 1, disposed = false;
  function transport() {
    var raw = root.PixoNativeBridge || root.MotionCueNativeBridge;
    if (!raw || typeof raw.post !== "function") return null;
    return raw;
  }
  function error(message, details) {
    var result = new Error(message);
    result.code = "PIXO_NATIVE_REQUEST_FAILED";
    result.details = details || {};
    return result;
  }
  function emit(name, data, envelope) {
    var callbacks = listeners.get(name);
    if (callbacks) callbacks.forEach(function (callback) { try { callback(data || {}, envelope); } catch (_) {} });
    try { root.dispatchEvent(new CustomEvent("pixo:" + name, { detail: data || {} })); } catch (_) {}
  }
  function receive(raw) {
    var envelope = raw;
    try { if (typeof raw === "string") envelope = JSON.parse(raw); } catch (_) { return false; }
    if (!envelope || envelope.v !== 1) return false;
    if (envelope.kind === "event" && typeof envelope.name === "string") {
      emit(envelope.name, envelope.data || {}, envelope); return true;
    }
    if (envelope.kind !== "response" || envelope.id === undefined) return false;
    var entry = pending.get(String(envelope.id));
    if (!entry) return false;
    pending.delete(String(envelope.id)); root.clearTimeout(entry.timer);
    if (envelope.ok === false) entry.reject(error((envelope.error || {}).message || "Pixo native request failed.", envelope.error));
    else entry.resolve(envelope.result || {});
    return true;
  }
  root.__pixoNativeReceive = receive;
  function request(method, params, options) {
    if (disposed) return Promise.reject(error("Pixo native client has been disposed."));
    var channel = transport();
    if (!channel) return Promise.reject(error("Pixo native transport is unavailable."));
    var id = String(sequence++), timeout = Math.max(1000, Number((options || {}).timeoutMs) || 30000);
    return new Promise(function (resolve, reject) {
      var timer = root.setTimeout(function () {
        pending.delete(id); reject(error("Pixo native request timed out.", { method: method }));
      }, timeout);
      pending.set(id, { resolve: resolve, reject: reject, timer: timer });
      try {
        var result = channel.post(JSON.stringify({ v: 1, kind: "request", id: id, method: method, params: params || {}, action: method, payload: params || {} }));
        if (result && typeof result.then === "function") result.then(receive, function (reason) { receive({ v: 1, kind: "response", id: id, ok: false, error: { message: String(reason || "transport failed") } }); });
      } catch (reason) { receive({ v: 1, kind: "response", id: id, ok: false, error: { message: String(reason || "transport failed") } }); }
    });
  }
  var api = {
    version: 1, bridgeVersion: 1,
    capabilities: Object.freeze({ motion: true, microphoneLevel: true, cameraStream: true, haptics: true, mediaControl: true, deviceInfo: true }),
    request: request, receive: receive,
    hasTransport: function () { return !!transport(); },
    on: function (name, handler) { if (typeof handler !== "function") throw error("Pixo event handler is required."); var set = listeners.get(name) || new Set(); set.add(handler); listeners.set(name, set); return function () { set.delete(handler); if (!set.size) listeners.delete(name); }; },
    dispose: function () { disposed = true; pending.forEach(function (entry) { root.clearTimeout(entry.timer); entry.reject(error("Pixo native client was disposed.")); }); pending.clear(); listeners.clear(); },
    notifyRuntimeEvent: function (detail) { return request("runtimeEvent", detail || {}).then(function () { return true; }, function () { return false; }); },
    getDeviceInfo: function () { return request("deviceInfo"); },
    requestCapability: function (name) { return request("requestCapability", { name: name }, { timeoutMs: 120000 }); },
    startMotion: function () { return request("startMotion"); }, stopMotion: function () { return request("stopMotion"); },
    startMicrophoneLevel: function () { return request("startMicrophoneLevel"); }, stopMicrophoneLevel: function () { return request("stopMicrophoneLevel"); },
    vibrate: function (style) { return request("haptic", { style: style || "light" }); },
    setMediaPlayback: function (state) { return request("mediaControl", state || {}); }
  };
  // The trusted HTML Host SDK loads immediately after this client and replaces
  // the raw transport-facing object with its capability-filtered facade.  Keep
  // this one hand-off configurable; the Host SDK seals the public property.
  Object.defineProperty(root, "PixoNative", { value: Object.freeze(api), writable: false, configurable: true });
})(window);
