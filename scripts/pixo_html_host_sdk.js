(function installPixoHtmlHostSdk(root) {
  "use strict";

  const config = root.__PIXO_HTML_CONFIG__;
  const nativeClient = root.PixoNative;
  const allowedCapabilities = new Set([
    "motion",
    "microphoneLevel",
    "cameraStream",
    "haptics",
    "mediaControl",
  ]);

  if (!config || config.bridge_version !== 1 || !Array.isArray(config.required_capabilities)) {
    throw new Error("Pixo HTML host configuration is missing or invalid.");
  }
  if (!nativeClient || typeof nativeClient.request !== "function") {
    throw new Error("Pixo native client must load before the HTML Host SDK.");
  }

  const declared = new Set(config.required_capabilities);
  declared.forEach(function validateCapability(name) {
    if (!allowedCapabilities.has(name)) {
      throw new Error("Unsupported Pixo HTML capability: " + name);
    }
  });

  const browserCompatibility = config.compatibility_profile === "browser-v1";
  const restartOnReactivate = browserCompatibility && config.restart_on_reactivate === true;
  const bridgeMethods = Object.freeze({
    deviceInfo: null,
    requestCapability: null,
    startMotion: "motion",
    stopMotion: "motion",
    startMicrophoneLevel: "microphoneLevel",
    stopMicrophoneLevel: "microphoneLevel",
    haptic: "haptics",
    mediaControl: "mediaControl",
  });
  const trackedStreams = new Set();
  const microphoneCompatibilityStates = new Map();
  const analyserCompatibilityStates = new WeakMap();
  const syntheticSensorEvents = new WeakSet();
  const sensorListeners = new Map([
    ["devicemotion", new Set()],
    ["deviceorientation", new Set()],
    ["deviceorientationabsolute", new Set()],
  ]);
  const nativeAddEventListener = root.addEventListener.bind(root);
  const nativeRemoveEventListener = typeof root.removeEventListener === "function"
    ? root.removeEventListener.bind(root)
    : function noopRemoveEventListener() {};
  let hostActive = root.__motionCueHostShouldPlay === true;
  let hasBeenActive = hostActive;
  let explicitMotionActive = false;
  let explicitMicrophoneActive = false;
  let compatibilityMotionActive = false;
  let compatibilityMotionStarting = null;
  let compatibilityMotionUnsubscribe = null;
  let compatibilityMicrophoneActive = false;
  let compatibilityMicrophoneStarting = null;
  let compatibilityMicrophoneUnsubscribe = null;

  function pixoError(message, name, code) {
    const error = new Error(message);
    error.name = name || "Error";
    error.code = code || "PIXO_HTML_ERROR";
    return error;
  }

  function capabilityError(name) {
    return pixoError(
      "HTML content did not declare capability: " + name,
      "NotAllowedError",
      "PIXO_CAPABILITY_NOT_DECLARED",
    );
  }

  function unsupportedError(name) {
    return pixoError(
      "Pixo browser compatibility does not expose " + name + ".",
      "NotSupportedError",
      "PIXO_BROWSER_API_UNSUPPORTED",
    );
  }

  function inactiveError() {
    return pixoError(
      "The Pixo HTML page is not active.",
      "InvalidStateError",
      "PIXO_PAGE_INACTIVE",
    );
  }

  function requireCapability(name) {
    if (!declared.has(name)) throw capabilityError(name);
  }

  function request(method, params, options) {
    if (!Object.prototype.hasOwnProperty.call(bridgeMethods, method)) {
      return Promise.reject(capabilityError(method));
    }
    const capability = bridgeMethods[method];
    if (capability && !declared.has(capability)) {
      return Promise.reject(capabilityError(capability));
    }
    return nativeClient.request(method, params || {}, options);
  }

  function capabilityWasGranted(result) {
    const status = result && typeof result === "object" ? result.status : "";
    return status === "granted" || status === "active";
  }

  function requireGranted(result, capability) {
    if (!capabilityWasGranted(result)) {
      throw pixoError(
        "Pixo capability is unavailable: " + capability,
        "NotAllowedError",
        "PIXO_CAPABILITY_UNAVAILABLE",
      );
    }
    return result;
  }

  const capabilities = {};
  allowedCapabilities.forEach(function exposeDeclaredCapability(name) {
    capabilities[name] = declared.has(name);
  });

  const safeClient = {
    version: 1,
    bridgeVersion: 1,
    capabilities: Object.freeze(capabilities),
    request,
    receive: nativeClient.receive ? nativeClient.receive.bind(nativeClient) : undefined,
    on: nativeClient.on.bind(nativeClient),
    hasTransport: nativeClient.hasTransport
      ? nativeClient.hasTransport.bind(nativeClient)
      : function hasTransport() { return true; },
    getDeviceInfo: function getDeviceInfo() {
      return request("deviceInfo").then(function restrictDeviceInfo(info) {
        const result = info && typeof info === "object" ? Object.assign({}, info) : {};
        const available = new Set(Array.isArray(result.capabilities) ? result.capabilities : []);
        result.platform = result.platform || "android";
        result.bridge_version = 1;
        result.capabilities = Array.from(declared).filter(function isAvailable(name) {
          return available.has(name);
        });
        result.supportsMotion = declared.has("motion") && result.supportsMotion !== false;
        result.supportsMicrophoneLevel = declared.has("microphoneLevel") &&
          result.supportsMicrophoneLevel !== false;
        result.supportsCameraStream = declared.has("cameraStream") &&
          result.supportsCameraStream !== false;
        result.supportsCameraPermission = result.supportsCameraStream;
        result.supportsCameraSignals = false;
        result.supportsHaptics = declared.has("haptics") && result.supportsHaptics !== false;
        result.supportsMediaControl = declared.has("mediaControl") &&
          result.supportsMediaControl !== false;
        return result;
      });
    },
    requestCapability: function requestCapability(name) {
      if (!declared.has(name)) return Promise.reject(capabilityError(name));
      if (!hostActive) return Promise.reject(inactiveError());
      return request("requestCapability", { name }, { timeoutMs: 120000 });
    },
  };

  if (declared.has("motion")) {
    safeClient.startMotion = function startMotion() {
      if (!hostActive) return Promise.reject(inactiveError());
      return request("startMotion").then(function rememberExplicitMotion(result) {
        explicitMotionActive = true;
        return result;
      });
    };
    safeClient.stopMotion = function stopMotion() {
      explicitMotionActive = false;
      compatibilityMotionActive = false;
      return request("stopMotion");
    };
  }
  if (declared.has("microphoneLevel")) {
    safeClient.startMicrophoneLevel = function startMicrophoneLevel() {
      if (!hostActive) return Promise.reject(inactiveError());
      return request("startMicrophoneLevel").then(function rememberExplicitMicrophone(result) {
        explicitMicrophoneActive = true;
        return result;
      });
    };
    safeClient.stopMicrophoneLevel = function stopMicrophoneLevel() {
      explicitMicrophoneActive = false;
      compatibilityMicrophoneActive = false;
      return request("stopMicrophoneLevel");
    };
  }
  if (declared.has("haptics")) {
    safeClient.vibrate = function vibrate(style) {
      if (!hostActive) return Promise.reject(inactiveError());
      return request("haptic", { style: style || "light" });
    };
  }
  if (declared.has("mediaControl")) {
    safeClient.setMediaPlayback = function setMediaPlayback(state) {
      return request("mediaControl", state && typeof state === "object" ? state : {});
    };
  }

  function createEvent(type, properties) {
    let event;
    try {
      event = typeof root.Event === "function" ? new root.Event(type) : { type };
    } catch (_) {
      event = { type };
    }
    Object.keys(properties || {}).forEach(function defineEventProperty(name) {
      try {
        Object.defineProperty(event, name, {
          value: properties[name],
          enumerable: true,
          configurable: true,
        });
      } catch (_) {
        try { event[name] = properties[name]; } catch (_) {}
      }
    });
    return event;
  }

  function dispatchTrackEvent(listeners, type) {
    const event = createEvent(type, {});
    const callback = listeners.get(type);
    if (typeof callback === "function") {
      try { callback(event); } catch (_) {}
    }
  }

  function microphoneLevel(data) {
    const candidates = [
      data && data.rms,
      data && data.peak,
      typeof (data && data.volume_score) === "number" ? data.volume_score / 100 : null,
      typeof (data && data.score) === "number" ? data.score / 100 : null,
    ];
    const value = candidates.find(function firstNumber(candidate) {
      return typeof candidate === "number" && Number.isFinite(candidate);
    });
    return Math.max(0, Math.min(1, value || 0));
  }

  function stopCompatibilityMicrophoneIfIdle() {
    if (microphoneCompatibilityStates.size || explicitMicrophoneActive) return;
    if (compatibilityMicrophoneUnsubscribe) {
      compatibilityMicrophoneUnsubscribe();
      compatibilityMicrophoneUnsubscribe = null;
    }
    if (compatibilityMicrophoneActive) {
      compatibilityMicrophoneActive = false;
      request("stopMicrophoneLevel").catch(function ignoreStopFailure() {});
    }
  }

  function endCompatibilityMicrophoneStream(stream) {
    const state = microphoneCompatibilityStates.get(stream);
    if (!state || state.ended) return;
    state.ended = true;
    state.track.readyState = "ended";
    microphoneCompatibilityStates.delete(stream);
    trackedStreams.delete(stream);
    dispatchTrackEvent(state.trackListeners, "ended");
    stopCompatibilityMicrophoneIfIdle();
  }

  function createCompatibilityMicrophoneStream() {
    const trackListeners = new Map();
    const state = { ended: false, level: 0, track: null, trackListeners };
    const track = {
      kind: "audio",
      id: "pixo-microphone-level",
      label: "Pixo microphone level",
      enabled: true,
      muted: false,
      readyState: "live",
      stop: function stop() { endCompatibilityMicrophoneStream(stream); },
      addEventListener: function addTrackListener(name, callback) {
        if (typeof callback === "function") trackListeners.set(name, callback);
      },
      removeEventListener: function removeTrackListener(name, callback) {
        if (trackListeners.get(name) === callback) trackListeners.delete(name);
      },
    };
    state.track = track;
    const stream = {
      id: "pixo-microphone-level-stream",
      get active() { return !state.ended; },
      getTracks: function getTracks() { return state.ended ? [] : [track]; },
      getAudioTracks: function getAudioTracks() { return state.ended ? [] : [track]; },
      getVideoTracks: function getVideoTracks() { return []; },
      addEventListener: function addStreamListener() {},
      removeEventListener: function removeStreamListener() {},
    };
    microphoneCompatibilityStates.set(stream, state);
    trackedStreams.add(stream);
    return stream;
  }

  function ensureCompatibilityMicrophone() {
    if (!hostActive) return Promise.reject(inactiveError());
    requireCapability("microphoneLevel");
    if (compatibilityMicrophoneActive) {
      return Promise.resolve(createCompatibilityMicrophoneStream());
    }
    if (!compatibilityMicrophoneStarting) {
      compatibilityMicrophoneStarting = safeClient.requestCapability("microphoneLevel")
        .then(function startGrantedMicrophone(result) {
          requireGranted(result, "microphoneLevel");
          if (!compatibilityMicrophoneUnsubscribe) {
            compatibilityMicrophoneUnsubscribe = nativeClient.on(
              "microphoneLevel",
              function updateCompatibilityMicrophone(data) {
                const level = microphoneLevel(data);
                microphoneCompatibilityStates.forEach(function updateState(state) {
                  if (!state.ended) state.level = level;
                });
              },
            );
          }
          return request("startMicrophoneLevel");
        })
        .then(function microphoneStarted() {
          compatibilityMicrophoneActive = true;
        })
        .finally(function clearMicrophoneStart() {
          compatibilityMicrophoneStarting = null;
        });
    }
    return compatibilityMicrophoneStarting.then(createCompatibilityMicrophoneStream);
  }

  function setNodeMethod(node, name, replacement) {
    try {
      Object.defineProperty(node, name, {
        value: replacement,
        writable: true,
        configurable: true,
      });
    } catch (_) {
      try { node[name] = replacement; } catch (_) {}
    }
  }

  function fillByteTimeDomain(array, level) {
    const amplitude = Math.max(0, Math.min(127, Math.round(level * 128)));
    for (let index = 0; index < array.length; index += 1) {
      array[index] = 128 + (index % 2 === 0 ? -amplitude : amplitude);
    }
  }

  function fillFloatTimeDomain(array, level) {
    const amplitude = Math.max(0, Math.min(1, level));
    for (let index = 0; index < array.length; index += 1) {
      array[index] = index % 2 === 0 ? -amplitude : amplitude;
    }
  }

  function installAudioContextCompatibility(Context) {
    if (!Context || !Context.prototype || Context.prototype.__pixoLevelCompatibility) return;
    const originalCreateSource = Context.prototype.createMediaStreamSource;
    const originalCreateAnalyser = Context.prototype.createAnalyser;
    if (typeof originalCreateSource !== "function" || typeof originalCreateAnalyser !== "function") return;

    Context.prototype.createMediaStreamSource = function createMediaStreamSource(stream) {
      const state = microphoneCompatibilityStates.get(stream);
      if (!state) return originalCreateSource.call(this, stream);
      if (state.ended) throw pixoError("Microphone stream has ended.", "InvalidStateError");
      return {
        context: this,
        connect: function connect(destination) {
          analyserCompatibilityStates.set(destination, state);
          return destination;
        },
        disconnect: function disconnect() {},
      };
    };

    Context.prototype.createAnalyser = function createAnalyser() {
      const analyser = originalCreateAnalyser.call(this);
      const originalByteTime = typeof analyser.getByteTimeDomainData === "function"
        ? analyser.getByteTimeDomainData.bind(analyser)
        : null;
      const originalFloatTime = typeof analyser.getFloatTimeDomainData === "function"
        ? analyser.getFloatTimeDomainData.bind(analyser)
        : null;
      const originalByteFrequency = typeof analyser.getByteFrequencyData === "function"
        ? analyser.getByteFrequencyData.bind(analyser)
        : null;
      const originalFloatFrequency = typeof analyser.getFloatFrequencyData === "function"
        ? analyser.getFloatFrequencyData.bind(analyser)
        : null;
      if (originalByteTime) {
        setNodeMethod(analyser, "getByteTimeDomainData", function getByteTimeDomainData(array) {
          const state = analyserCompatibilityStates.get(analyser);
          if (!state) return originalByteTime(array);
          fillByteTimeDomain(array, state.ended ? 0 : state.level);
        });
      }
      if (originalFloatTime) {
        setNodeMethod(analyser, "getFloatTimeDomainData", function getFloatTimeDomainData(array) {
          const state = analyserCompatibilityStates.get(analyser);
          if (!state) return originalFloatTime(array);
          fillFloatTimeDomain(array, state.ended ? 0 : state.level);
        });
      }
      if (originalByteFrequency) {
        setNodeMethod(analyser, "getByteFrequencyData", function getByteFrequencyData(array) {
          if (analyserCompatibilityStates.has(analyser)) {
            throw unsupportedError("microphone frequency analysis");
          }
          return originalByteFrequency(array);
        });
      }
      if (originalFloatFrequency) {
        setNodeMethod(analyser, "getFloatFrequencyData", function getFloatFrequencyData(array) {
          if (analyserCompatibilityStates.has(analyser)) {
            throw unsupportedError("microphone frequency analysis");
          }
          return originalFloatFrequency(array);
        });
      }
      return analyser;
    };

    try {
      Object.defineProperty(Context.prototype, "__pixoLevelCompatibility", {
        value: true,
        configurable: false,
      });
    } catch (_) {}
  }

  function sensorListenerCount() {
    let count = 0;
    sensorListeners.forEach(function countListeners(listeners) { count += listeners.size; });
    return count;
  }

  function dispatchSyntheticSensorEvent(type, properties) {
    const event = createEvent(type, properties);
    syntheticSensorEvents.add(event);
    try { root.dispatchEvent(event); } catch (_) {}
  }

  function stopCompatibilityMotionIfIdle() {
    if (sensorListenerCount() || explicitMotionActive) return;
    if (compatibilityMotionUnsubscribe) {
      compatibilityMotionUnsubscribe();
      compatibilityMotionUnsubscribe = null;
    }
    if (compatibilityMotionActive) {
      compatibilityMotionActive = false;
      request("stopMotion").catch(function ignoreStopFailure() {});
    }
  }

  function ensureCompatibilityMotion() {
    if (!browserCompatibility || !declared.has("motion") || !sensorListenerCount()) {
      return Promise.resolve();
    }
    if (!hostActive) return Promise.reject(inactiveError());
    if (compatibilityMotionActive) return Promise.resolve();
    if (!compatibilityMotionStarting) {
      compatibilityMotionStarting = safeClient.requestCapability("motion")
        .then(function startGrantedMotion(result) {
          requireGranted(result, "motion");
          if (!compatibilityMotionUnsubscribe) {
            compatibilityMotionUnsubscribe = nativeClient.on("motion", function emitMotion(data) {
              const acceleration = {
                x: Number(data && data.acceleration_x) || 0,
                y: Number(data && data.acceleration_y) || 0,
                z: Number(data && data.acceleration_z) || 0,
              };
              if ((sensorListeners.get("devicemotion") || new Set()).size) {
                dispatchSyntheticSensorEvent("devicemotion", {
                  acceleration,
                  accelerationIncludingGravity: acceleration,
                  rotationRate: null,
                  interval: Number(data && data.interval) || 16,
                  shake_score: Number(data && data.shake_score) || 0,
                });
              }
              const orientation = {
                alpha: Number(data && data.alpha) || 0,
                beta: Number(data && data.beta) || 0,
                gamma: Number(data && data.gamma) || 0,
                absolute: false,
              };
              if ((sensorListeners.get("deviceorientation") || new Set()).size) {
                dispatchSyntheticSensorEvent("deviceorientation", orientation);
              }
              if ((sensorListeners.get("deviceorientationabsolute") || new Set()).size) {
                dispatchSyntheticSensorEvent(
                  "deviceorientationabsolute",
                  Object.assign({}, orientation, { absolute: true }),
                );
              }
            });
          }
          return request("startMotion");
        })
        .then(function motionStarted() { compatibilityMotionActive = true; })
        .finally(function clearMotionStart() { compatibilityMotionStarting = null; });
    }
    return compatibilityMotionStarting;
  }

  ["devicemotion", "deviceorientation", "deviceorientationabsolute"].forEach(
    function blockRawBrowserSensorEvent(name) {
      nativeAddEventListener(name, function blockEvent(event) {
        if (
          !syntheticSensorEvents.has(event) &&
          event &&
          typeof event.stopImmediatePropagation === "function"
        ) {
          event.stopImmediatePropagation();
        }
      }, true);
    },
  );

  if (browserCompatibility && declared.has("motion")) {
    root.addEventListener = function addCompatibleEventListener(name, callback, options) {
      nativeAddEventListener(name, callback, options);
      const listeners = sensorListeners.get(String(name).toLowerCase());
      if (listeners && callback) {
        listeners.add(callback);
        ensureCompatibilityMotion().catch(function ignoreCompatibilityStartFailure() {});
      }
    };
    root.removeEventListener = function removeCompatibleEventListener(name, callback, options) {
      nativeRemoveEventListener(name, callback, options);
      const listeners = sensorListeners.get(String(name).toLowerCase());
      if (listeners && callback) {
        listeners.delete(callback);
        stopCompatibilityMotionIfIdle();
      }
    };

    function BrowserMotionEvent() {}
    BrowserMotionEvent.requestPermission = function requestMotionPermission() {
      return safeClient.requestCapability("motion").then(function browserPermission(result) {
        return capabilityWasGranted(result) ? "granted" : "denied";
      });
    };
    function BrowserOrientationEvent() {}
    BrowserOrientationEvent.requestPermission = BrowserMotionEvent.requestPermission;
    try {
      Object.defineProperty(root, "DeviceMotionEvent", {
        value: BrowserMotionEvent,
        writable: false,
        configurable: false,
      });
      Object.defineProperty(root, "DeviceOrientationEvent", {
        value: BrowserOrientationEvent,
        writable: false,
        configurable: false,
      });
    } catch (_) {
      try { root.DeviceMotionEvent.requestPermission = BrowserMotionEvent.requestPermission; } catch (_) {}
      try { root.DeviceOrientationEvent.requestPermission = BrowserMotionEvent.requestPermission; } catch (_) {}
    }
  } else {
    ["DeviceMotionEvent", "DeviceOrientationEvent"].forEach(function hideRawSensor(name) {
      try {
        Object.defineProperty(root, name, {
          value: undefined,
          writable: false,
          configurable: false,
        });
      } catch (_) {}
    });
  }

  [
    "Accelerometer",
    "Gyroscope",
    "LinearAccelerationSensor",
    "AbsoluteOrientationSensor",
    "RelativeOrientationSensor",
    "Magnetometer",
    "SpeechRecognition",
    "webkitSpeechRecognition",
  ].forEach(function hideUnsupportedBrowserCapability(name) {
    try {
      Object.defineProperty(root, name, {
        value: undefined,
        writable: false,
        configurable: false,
      });
    } catch (_) {}
  });

  const mediaDevices = root.navigator && root.navigator.mediaDevices;
  if (mediaDevices && typeof mediaDevices.getUserMedia === "function") {
    const originalGetUserMedia = mediaDevices.getUserMedia.bind(mediaDevices);
    mediaDevices.getUserMedia = function guardedGetUserMedia(constraints) {
      const requested = constraints && typeof constraints === "object" ? constraints : {};
      const wantsAudio = Boolean(requested.audio);
      const wantsVideo = Boolean(requested.video);
      if (wantsAudio && wantsVideo) {
        return Promise.reject(unsupportedError("combined camera and microphone capture"));
      }
      if (wantsAudio) {
        if (!browserCompatibility) {
          return Promise.reject(capabilityError("rawMicrophoneAudio"));
        }
        return ensureCompatibilityMicrophone();
      }
      if (!wantsVideo) {
        return Promise.reject(new TypeError("Pixo HTML getUserMedia requires audio or video."));
      }
      try {
        requireCapability("cameraStream");
        if (!hostActive) throw inactiveError();
      } catch (error) {
        return Promise.reject(error);
      }
      const permission = browserCompatibility
        ? safeClient.requestCapability("cameraStream").then(function cameraPermission(result) {
            return requireGranted(result, "cameraStream");
          })
        : Promise.resolve();
      return permission
        .then(function openCamera() {
          return originalGetUserMedia({ video: requested.video, audio: false });
        })
        .then(function track(stream) {
          trackedStreams.add(stream);
          try {
            stream.getTracks().forEach(function observeTrack(track) {
              track.addEventListener("ended", function forgetEndedStream() {
                const hasLiveTrack = stream.getTracks().some(function isLive(candidate) {
                  return candidate.readyState !== "ended";
                });
                if (!hasLiveTrack) trackedStreams.delete(stream);
              }, { once: true });
            });
          } catch (_) {}
          return stream;
        });
    };
    if (typeof mediaDevices.enumerateDevices === "function") {
      const originalEnumerateDevices = mediaDevices.enumerateDevices.bind(mediaDevices);
      mediaDevices.enumerateDevices = function guardedEnumerateDevices() {
        if (!declared.has("cameraStream")) return Promise.resolve([]);
        return originalEnumerateDevices().then(function onlyVideoInputs(devices) {
          return devices.filter(function isCamera(device) { return device.kind === "videoinput"; });
        });
      };
    }
    if (typeof mediaDevices.getDisplayMedia === "function") {
      mediaDevices.getDisplayMedia = function rejectDisplayCapture() {
        return Promise.reject(unsupportedError("displayCapture"));
      };
    }
  }

  if (root.navigator) {
    ["getUserMedia", "webkitGetUserMedia", "mozGetUserMedia"].forEach(
      function installLegacyCaptureApi(name) {
        const value = browserCompatibility && mediaDevices
          ? function compatibleLegacyGetUserMedia(constraints, onSuccess, onError) {
              mediaDevices.getUserMedia(constraints).then(onSuccess, onError);
            }
          : undefined;
        try {
          Object.defineProperty(root.navigator, name, {
            value,
            writable: false,
            configurable: false,
          });
        } catch (_) {
          try { root.navigator[name] = value; } catch (_) {}
        }
      },
    );
  }

  if (browserCompatibility) {
    installAudioContextCompatibility(root.AudioContext);
    if (root.webkitAudioContext !== root.AudioContext) {
      installAudioContextCompatibility(root.webkitAudioContext);
    }
    if (root.navigator && declared.has("haptics")) {
      const compatibleVibrate = function compatibleVibrate(pattern) {
        if (!hostActive) return false;
        const durations = Array.isArray(pattern) ? pattern : [pattern];
        const longest = Math.max.apply(null, durations.map(function duration(value) {
          return Math.max(0, Number(value) || 0);
        }));
        const style = longest >= 120 ? "heavy" : longest >= 50 ? "medium" : "light";
        safeClient.requestCapability("haptics")
          .then(function vibrateWhenGranted(result) {
            if (capabilityWasGranted(result)) return safeClient.vibrate(style);
            return null;
          })
          .catch(function ignoreHapticFailure() {});
        return true;
      };
      try {
        Object.defineProperty(root.navigator, "vibrate", {
          value: compatibleVibrate,
          writable: false,
          configurable: false,
        });
      } catch (_) {
        try { root.navigator.vibrate = compatibleVibrate; } catch (_) {}
      }
    }
  }

  function stopTrackedStreams() {
    Array.from(trackedStreams).forEach(function stopStream(stream) {
      try {
        stream.getTracks().forEach(function stopTrack(track) {
          try { track.stop(); } catch (_) {}
        });
      } catch (_) {}
    });
    trackedStreams.clear();
  }

  function pauseMedia() {
    try {
      root.document.querySelectorAll("video,audio").forEach(function pauseNode(node) {
        try { node.pause(); } catch (_) {}
      });
    } catch (_) {}
  }

  function stopNativeSampling() {
    explicitMotionActive = false;
    explicitMicrophoneActive = false;
    compatibilityMotionActive = false;
    compatibilityMicrophoneActive = false;
    if (declared.has("motion")) {
      nativeClient.request("stopMotion", {}).catch(function ignoreStopFailure() {});
    }
    if (declared.has("microphoneLevel")) {
      nativeClient.request("stopMicrophoneLevel", {}).catch(function ignoreStopFailure() {});
    }
  }

  function notifyChildFrames(detail) {
    try {
      root.document.querySelectorAll("iframe").forEach(function notifyFrame(frame) {
        try {
          frame.contentWindow.dispatchEvent(new root.CustomEvent("pixo:host-state", { detail }));
        } catch (_) {}
      });
    } catch (_) {}
  }

  function deactivate() {
    hostActive = false;
    pauseMedia();
    stopTrackedStreams();
    if (compatibilityMotionUnsubscribe) {
      compatibilityMotionUnsubscribe();
      compatibilityMotionUnsubscribe = null;
    }
    if (compatibilityMicrophoneUnsubscribe) {
      compatibilityMicrophoneUnsubscribe();
      compatibilityMicrophoneUnsubscribe = null;
    }
    stopNativeSampling();
    notifyChildFrames({ active: false, allowAudio: false });
  }

  nativeAddEventListener("pixo:host-state", function handleHostState(event) {
    const detail = event && event.detail ? event.detail : {};
    if (detail.active === true) {
      const shouldRestart = restartOnReactivate && hasBeenActive && !hostActive;
      hostActive = true;
      hasBeenActive = true;
      notifyChildFrames({ active: true, allowAudio: detail.allowAudio === true });
      if (shouldRestart && root.location && typeof root.location.reload === "function") {
        Promise.resolve().then(function restartCompatibilityPage() {
          try { root.location.reload(); } catch (_) {}
        });
        return;
      }
      ensureCompatibilityMotion().catch(function ignoreCompatibilityStartFailure() {});
      return;
    }
    deactivate();
  });
  nativeAddEventListener("pagehide", deactivate);
  root.document.addEventListener("visibilitychange", function handleVisibility() {
    if (root.document.hidden) deactivate();
  });

  Object.freeze(safeClient);
  Object.defineProperty(root, "PixoNative", {
    value: safeClient,
    writable: false,
    configurable: false,
    enumerable: true,
  });
  Object.defineProperty(root, "MotionCueNative", {
    value: safeClient,
    writable: false,
    configurable: false,
    enumerable: true,
  });
  Object.defineProperty(root, "__PIXO_HTML_HOST_SDK__", {
    value: Object.freeze({
      version: 1,
      compatibilityProfile: browserCompatibility ? "browser-v1" : "strict",
      isActive: function isActive() { return hostActive; },
      trackedStreamCount: function trackedStreamCount() { return trackedStreams.size; },
      deactivate,
    }),
    writable: false,
    configurable: false,
  });
})(window);
