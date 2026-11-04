(function () {
  "use strict";
  var video = document.getElementById("remote");
  var standby = document.getElementById("standby");
  var preview = new URLSearchParams(window.location.search).get("preview") === "1";
  var gameOwner = new URLSearchParams(window.location.search).get("game_owner");
  var audioControl = document.getElementById("audioControl"), remoteAudioUsers = new Set();
  var takeoverControl = document.getElementById("takeoverControl"), takeoverGeneration = null, takeoverBusy = false;
  audioControl.hidden = !preview;
  audioControl.disabled = true;
  // The SDK owns the separate audio element. Keep the video muted to avoid
  // duplicate sound; resume SDK audio inside an actual iframe click gesture.
  audioControl.addEventListener("click", function () {
    if (!engine || !remoteAudioUsers.size) return;
    try {
      remoteAudioUsers.forEach(function (uid) { engine.resumeRemoteAudioStream(uid); });
      audioControl.textContent = "已请求播放声音 · 可再次点击";
    } catch (_) { audioControl.textContent = "声音未开启，请再次点击"; }
  });
  var clientId = window.crypto && typeof window.crypto.randomUUID === "function"
    ? window.crypto.randomUUID() : "obs-" + Date.now() + "-" + Math.random().toString(36).slice(2);
  var engine = null, generation = null, attempted = null, claimed = false, claimAttempt = "unseen";
  var joined = false, failed = false, exiting = false, heartbeatBusy = false, leaving = null;
  var tracks = {}, callbackFrames = 0, qualityBase = 0, readyDeadline = 0;
  var captureBlocked = false;
  var terminal = ["idle", "ended", "error", "stopping", "stop_unconfirmed", "creation_unknown"];

  takeoverControl.addEventListener("click", async function () {
    if (!preview || claimed || takeoverBusy || takeoverGeneration === null) return;
    var requestedGeneration = takeoverGeneration;
    takeoverBusy = true; takeoverControl.disabled = true;
    try {
      // 只能由操作者在未开场时明确接管；后台再次检查场次与创建状态，
      // 防止页面显示按钮后恰好开场的竞态。普通轮询绝不自动抢占。
      var claim = await post("claim", { client_id: clientId, receiver_mode: "preview",
        takeover: true, generation: requestedGeneration, game_owner: gameOwner });
      if (claim.granted && claim.generation === requestedGeneration) {
        generation = claim.generation; claimed = true; failed = false;
        claimAttempt = JSON.stringify(generation);
        takeoverControl.hidden = true; takeoverGeneration = null;
        notice("本页已连接，等待工作台启动数字人");
      } else { notice(claim.reason || "本场状态已变化，请查看工作台状态"); }
    } catch (_) { notice("未能切换预览连接，请稍后重试"); }
    finally { takeoverBusy = false; takeoverControl.disabled = false; }
  });

  // 拒绝所有本机采集，即使 SDK 意外请求也不能打开麦克风、摄像头或屏幕。
  try {
    var media = navigator.mediaDevices;
    if (media) {
      var deny = function () { return Promise.reject(new DOMException("播放器禁止本机采集", "NotAllowedError")); };
      ["getUserMedia", "getDisplayMedia"].forEach(function (name) {
        Object.defineProperty(media, name, { value: deny, writable: false, configurable: false });
      });
      captureBlocked = ["getUserMedia", "getDisplayMedia"].every(function (name) {
        var descriptor = Object.getOwnPropertyDescriptor(media, name);
        return descriptor && descriptor.value === deny && !descriptor.writable && !descriptor.configurable;
      });
    }
  } catch (_) { captureBlocked = false; }

  // 沿用已验收链路的远端轨道观察；不复制媒体，不编码，也不建立上传缓存。
  var OriginalPC = window.RTCPeerConnection;
  if (OriginalPC) {
    function ReceivePC() {
      var args = Array.prototype.slice.call(arguments);
      var pc = Reflect.construct(OriginalPC, args);
      pc.addEventListener("track", function (event) {
        if (generation !== null && claimed && !failed) tracks[event.track.kind] = event.track;
      });
      return pc;
    }
    ReceivePC.prototype = OriginalPC.prototype;
    Object.setPrototypeOf(ReceivePC, OriginalPC);
    window.RTCPeerConnection = ReceivePC;
  }

  function notice(message) { standby.textContent = message; standby.hidden = false; }
  function post(path, data) {
    return fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data), cache: "no-store" })
      .then(function (response) { if (!response.ok) throw Error("本机接口未确认"); return response.json(); });
  }
  function rawFrames() {
    if (typeof video.getVideoPlaybackQuality === "function") {
      var quality = video.getVideoPlaybackQuality();
      if (quality && Number.isFinite(quality.totalVideoFrames)) return quality.totalVideoFrames;
    }
    return Number(video.webkitDecodedFrameCount) || 0;
  }
  function evidence() {
    var frames = Math.max(callbackFrames, rawFrames() - qualityBase, 0);
    return { client_id: clientId, generation: generation,
      audio_live: !!(tracks.audio && tracks.audio.readyState === "live"),
      video_live: !!(tracks.video && tracks.video.readyState === "live" && video.videoWidth > 0 && video.videoHeight > 0 && video.readyState >= 2 && frames > 0),
      frames: frames };
  }
  if (typeof video.requestVideoFrameCallback === "function") {
    var observeFrame = function () {
      if (claimed && !failed) callbackFrames += 1;
      if (!exiting) video.requestVideoFrameCallback(observeFrame);
    };
    video.requestVideoFrameCallback(observeFrame);
  }
  function releasePayload() { return evidence(); }
  async function leave() {
    if (leaving) return leaving;
    leaving = (async function () {
      var old = engine;
      engine = null;
      if (claimed) post("release", releasePayload()).catch(function () {});
      claimed = false; joined = false; tracks = {};
      remoteAudioUsers.clear(); audioControl.disabled = true;
      audioControl.textContent = "开启声音";
      if (old) {
        try { await old.leaveChannel(); } catch (_) {}
        try { await old.destroy(); } catch (_) {}
        // AliRTC 的单例销毁后需要短暂释放，避免下一代取得仍在通话中的实例。
        await new Promise(function (resolve) { setTimeout(resolve, 800); });
      }
      video.srcObject = null;
      video.removeAttribute("src");
      video.load();
    })();
    try { await leaving; } finally { leaving = null; }
  }
  async function fault(message) {
    if (failed || generation === null || exiting) return;
    var faultGeneration = generation;
    failed = true;
    notice(message + "，请在工作台结束本场后重新开始");
    try { await post("fault", { client_id: clientId, generation: generation, message: message }); } catch (_) {}
    if (generation === faultGeneration && failed) await leave();
  }
  async function optional(name, value) {
    if (typeof engine[name] === "function") await engine[name](value);
  }
  async function connect(session) {
    // 记录尝试先于任何 await：接口超时也不能悄悄再次入会。
    attempted = session.generation; generation = session.generation; failed = false;
    try {
      if (!captureBlocked || !OriginalPC || !window.AliRtcEngine) throw Error("播放器采集保护或 RTC 环境未就绪");
      var response = await fetch("rtc?client_id=" + encodeURIComponent(clientId) + "&generation=" + encodeURIComponent(generation), { cache: "no-store" });
      if (!response.ok) throw Error("未能取得本场接收授权");
      var rtc = await response.json();
      if (!rtc.token || !rtc.user_id || (rtc.provider && rtc.provider !== "artc")) throw Error("本场 RTC 接收信息不完整");
      engine = window.AliRtcEngine.getInstance();
      var currentEngine = engine;
      engine.on("audioSubscribeStateChanged", function (uid, previous, next) {
        if (engine === currentEngine && next === 3 && !failed) {
          remoteAudioUsers.add(uid); audioControl.disabled = false;
        }
      });
      engine.on("remoteAudioAutoPlayFail", function (uid) {
        if (engine === currentEngine && preview) {
          remoteAudioUsers.add(uid); audioControl.disabled = false;
          audioControl.textContent = "浏览器已拦截声音，请点击开启";
        }
      });
      engine.on("remoteAudioPlayError", function () {
        if (engine === currentEngine && preview) audioControl.textContent = "声音播放异常，请点击重试";
      });
      engine.on("videoSubscribeStateChanged", function (uid, previous, next) {
        if (engine && next === 3 && !failed) {
          engine.setRemoteViewConfig(video, uid, 1);
          video.play().catch(function () {});
        }
      });
      engine.on("bye", function () { if (engine === currentEngine && claimed) fault("远端音画连接已断开"); });
      await optional("setChannelProfile", "communication");
      await engine.publishLocalAudioStream(false);
      await engine.publishLocalVideoStream(false);
      await optional("setDefaultSubscribeAllRemoteAudioStreams", true);
      await optional("setDefaultSubscribeAllRemoteVideoStreams", true);
      callbackFrames = 0; qualityBase = rawFrames();
      var timer;
      try {
        await Promise.race([engine.joinChannel(rtc.token, rtc.user_id), new Promise(function (_, reject) {
          timer = setTimeout(function () { reject(Error("RTC 入会超时")); }, 20000);
        })]);
      } finally { clearTimeout(timer); }
      readyDeadline = Date.now() + 25000;
      notice("正在接收数字人画面和声音");
    } catch (_) { await fault("播放器连接未能就绪"); }
  }
  async function pulse() {
    if (!claimed || failed || exiting || heartbeatBusy) return;
    heartbeatBusy = true;
    try {
      var proof = evidence();
      await post("heartbeat", proof);
      if (!claimed || proof.generation !== generation || failed) return;
      if (!joined && proof.audio_live && proof.video_live) {
        await post("joined", proof);
        if (claimed && proof.generation === generation && !failed) { joined = true; standby.hidden = true; }
      } else if (joined && (!proof.audio_live || !proof.video_live)) {
        await fault("远端音视频轨道已中断");
      } else if (!joined && readyDeadline && Date.now() > readyDeadline) {
        await fault("未确认远端音视频和真实解码帧");
      }
    } catch (_) { notice("本机连接中断，等待工作台确认结束"); }
    finally { heartbeatBusy = false; }
  }
  async function poll() {
    try {
      var response = await fetch("state", { cache: "no-store" });
      if (!response.ok) throw Error();
      var data = await response.json(), session = data.session || {};
      if (leaving) await leaving;
      if (claimed && (session.generation !== generation || (session.state !== "idle" && terminal.indexOf(session.state) !== -1))) {
        await leave(); generation = null; readyDeadline = 0;
        notice("等待工作台启动数字人");
      }
      // 待机也领取身份，让控制台能在创建付费会话前确认 OBS 页面在线。
      var claimKey = JSON.stringify(session.generation == null ? null : session.generation);
      if (!claimed && !takeoverBusy && claimAttempt !== claimKey && (session.state === "idle" || terminal.indexOf(session.state) === -1)) {
        var claim = await post("claim", { client_id: clientId, receiver_mode: preview ? "preview" : "obs", game_owner: gameOwner });
        claimAttempt = claimKey;
        // 显式接管可能先于这个普通请求返回，不让迟到的拒绝提示覆盖成功状态。
        if (!claimed) {
          if (claim.granted && claim.generation === session.generation) {
            generation = claim.generation; claimed = true; failed = false;
            notice("等待工作台启动数字人");
          } else {
            notice(claim.reason || "播放器未取得本场画面，请检查是否已有另一个播放页");
          }
        }
      }
      takeoverGeneration = preview && !claimed && data.can_takeover === true ? session.generation : null;
      takeoverControl.hidden = takeoverGeneration === null;
      if (claimed && session.generation != null && session.rtc_available && terminal.indexOf(session.state) === -1 && attempted !== session.generation) {
        await connect(session);
      }
      // joined 回应偶发丢失时，仅同步已被服务端接受且本地仍真实就绪的状态，不再次入会。
      if (claimed && !failed && data.player && data.player.joined && evidence().audio_live && evidence().video_live) {
        joined = true; standby.hidden = true;
      }
    } catch (_) { notice("本机工作台暂时无法连接"); }
    finally { if (!exiting) setTimeout(poll, 1500); }
  }
  function onExit() {
    if (exiting) return;
    exiting = true;
    if (claimed) navigator.sendBeacon("release", new Blob([JSON.stringify(releasePayload())], { type: "application/json" }));
  }
  window.addEventListener("pagehide", onExit);
  setInterval(pulse, 3000);
  window.addEventListener("DOMContentLoaded", poll);
})();
