/* Narrow AliRTC receive-only choreography from avatar-studio player.js at
 * 2e076b66ebd7b1db25d8c20914713ab7f4983f77; see THIRD_PARTY.md. No auto-connect. */
export const RTC_SDK_URL = '/vendor/aliyun-rtc-sdk-7.1.9.js';
export const RTC_SDK_INTEGRITY = 'sha256-gQAzg1HOYiodJeJplyR+VbCRNfRYuQ50iZWzV84m43Y=';
type Engine = {channel: string; userId: string; getOnlineRemoteUsers: () => string[]; getAudioTrack: (uid: string) => Promise<MediaStreamTrack | undefined>; getVideoTrack: (options: {userId: string; streamType: number}) => Promise<MediaStreamTrack | undefined>; subscribeRemoteMediaStream: (uid: string, videoTrack: number, video: boolean, audio: boolean) => unknown; muteAllRemoteAudioPlaying: (muted: boolean) => void; muteRemoteAudioPlaying: (uid: string, muted: boolean) => void; on: (event: string, callback: (...args: any[]) => void) => void; publishLocalAudioStream: (value: boolean) => Promise<unknown>; publishLocalVideoStream: (value: boolean) => Promise<unknown>; joinChannel: (token: string, uid: string) => Promise<unknown>; setRemoteViewConfig: (video: HTMLVideoElement, uid: string, type: number) => void; resumeRemoteAudioStream: (uid: string) => unknown; leaveChannel: () => Promise<unknown>; destroy: () => Promise<unknown>; [key: string]: unknown};
type Claim = {receiver_id: string; receiver_token: string; epoch: string | number; rtc: {sdk_url: string; sdk_integrity: string; token: string; user_id: string; channel_id: string; provider?: string}};
type Dependencies = {api: <T>(url: string, body?: unknown) => Promise<T>; epoch: () => number; ready: () => boolean; authenticated: () => boolean; changed: () => void};
const el = <T extends HTMLElement>(id: string) => document.getElementById(`receiver-${id}`) as T;
let sdkPromise: Promise<void> | null = null;
function loadSdk(url: string, integrity: string) {
  if (url !== RTC_SDK_URL || integrity !== RTC_SDK_INTEGRITY) throw new Error('SDK 配置不匹配');
  if (!sdkPromise) sdkPromise = new Promise<void>((resolve,reject) => {
    const script = document.createElement('script'); script.src = url; script.integrity = integrity; script.crossOrigin = 'anonymous'; script.referrerPolicy = 'no-referrer';
    const timeout = window.setTimeout(() => reject(new Error('SDK 加载超时')),10000);
    script.onload = () => {window.clearTimeout(timeout); resolve();}; script.onerror = () => {window.clearTimeout(timeout); reject(new Error('SDK 未安装或完整性校验失败'));}; document.head.append(script);
  });
  return sdkPromise;
}
export function setupReceiver(d: Dependencies) {
  const receiverId = crypto.randomUUID(), video = el<HTMLVideoElement>('video');
  let leaving: Promise<void> | null = null, startInFlight = false;
  let engine: Engine | null = null, claim: Claim | null = null, sessionId: string | null = null, attempted: string | null = null;
  let receiverEpoch = d.epoch(), lastFrameAt = 0, observedFrames = 0;
  let generation = 0, busy = false, joined = false, audioAllowed = false, blocked = false, heartbeatBusy = false, disposed = false;
  let tracks: Partial<Record<'audio'|'video', MediaStreamTrack>> = {}, frames = 0, baseFrames = 0, timer: number | null = null;
  let boundUid: string | null = null, sourceFault = false, subscriptionRequested = false;
  let audioSubscribed = false, videoSubscribed = false;
  let captureProtected = false;
  const notice = (s: string) => {el('status').textContent = `${s} · client_reported（浏览器上报，非媒体验收）`;};
  const rawFrames = () => typeof video.getVideoPlaybackQuality === 'function' ? video.getVideoPlaybackQuality().totalVideoFrames : 0;
  function protect() {
    if (!captureProtected) {
      const media = navigator.mediaDevices;
      if (!media) throw new Error('接收环境不支持采集保护');
      const deny = () => Promise.reject(new DOMException('接收器禁止本机采集','NotAllowedError'));
      for (const key of ['getUserMedia','getDisplayMedia']) Object.defineProperty(media,key,{value:deny,writable:false,configurable:false});
      captureProtected = true;
    }
  }
  function sourceFailure() {
    if (sourceFault) return;
    sourceFault = true; joined = audioAllowed = false;
    try {engine?.muteAllRemoteAudioPlaying(true);} catch (_) {}
    notice('音画来源歧义 / 来源改变：仅允许同一 Vidu 会话、频道中的唯一远端参与者；已关闭门并离开，不自动换人');
    void leave();
  }
  function acceptUid(uid: unknown) {
    if (sourceFault) return false;
    if (!claim || typeof uid !== 'string' || !uid || uid === claim.rtc.user_id || boundUid && boundUid !== uid) {sourceFailure(); return false;}
    boundUid ??= uid;
    return true;
  }
  function checkRoster() {
    if (!engine || !claim || sourceFault) return false;
    const users = engine.getOnlineRemoteUsers();
    if (!Array.isArray(users) || users.some(uid => typeof uid !== 'string' || !uid || uid === claim!.rtc.user_id) || new Set(users).size > 1) {sourceFailure(); return false;}
    if (users.length === 0) {if (boundUid && subscriptionRequested) sourceFailure(); return false;}
    return acceptUid(users[0]);
  }
  function subscribeBound() {
    if (!joined || !engine || !checkRoster() || !boundUid || subscriptionRequested) return;
    subscriptionRequested = true;
    try {engine.subscribeRemoteMediaStream(boundUid,1,true,true);} catch (_) {sourceFailure();}
  }
  async function refreshSourceTracks() {
    const e = engine, c = claim, gen = generation;
    if (!e || !c || !joined || !checkRoster() || !boundUid) return;
    if (e.channel !== c.rtc.channel_id || e.userId !== c.rtc.user_id) {sourceFailure(); return;}
    const uid = boundUid;
    const [audio, camera] = await Promise.all([e.getAudioTrack(uid),e.getVideoTrack({userId:uid,streamType:0})]);
    if (gen !== generation || engine !== e || boundUid !== uid || sourceFault) return;
    if (audio && (!(audio instanceof MediaStreamTrack) || audio.kind !== 'audio' || !audio.id) || camera && (!(camera instanceof MediaStreamTrack) || camera.kind !== 'video' || !camera.id)) {sourceFailure(); return;}
    if (tracks.video?.id !== camera?.id) {frames = observedFrames = 0; baseFrames = rawFrames(); lastFrameAt = 0;}
    tracks = {audio,video:camera};
    // Same-UID SDK getters, not whichever global RTCPeerConnection emitted last.
    // Video DOM must show exactly that participant's camera track; no mixed sender.
    if (video.srcObject) {
      if (!(video.srcObject instanceof MediaStream)) {sourceFailure(); return;}
      const videos = video.srcObject.getVideoTracks();
      if (videos.length && (!camera || videos.length !== 1 || videos[0].id !== camera.id)) {sourceFailure(); return;}
      if (video.srcObject.getAudioTracks().some(track => !audio || track.id !== audio.id)) {sourceFailure(); return;}
    }
  }

  function evidence() {
    const count = Math.max(frames,rawFrames()-baseFrames);
    if (count > observedFrames) {observedFrames = count; lastFrameAt = performance.now();}
    const visibleTrack = video.srcObject instanceof MediaStream ? video.srcObject.getVideoTracks() : [];
    const sameVideo = visibleTrack.length === 1 && visibleTrack[0].id === tracks.video?.id;
    const remoteVideo = !!(!sourceFault && videoSubscribed && sameVideo && tracks.video?.readyState === 'live' && !tracks.video.muted && video.videoWidth > 0 && video.videoHeight > 0 && video.readyState >= 2 && count > 0 && performance.now()-lastFrameAt < 5000);
    return {joined,remote_video:remoteVideo,audio_playback:!!(audioAllowed && !sourceFault && audioSubscribed && !blocked && tracks.audio?.readyState === 'live' && !tracks.audio.muted),autoplay_blocked:blocked};
  }
  function controls() {
    el<HTMLButtonElement>('audio').disabled = !engine || !boundUid || !audioSubscribed || !joined || sourceFault;
    el<HTMLButtonElement>('leave').disabled = !claim && !engine && !busy;
  }
  async function pulse() {
    if (!claim || !sessionId || heartbeatBusy || disposed) return;
    heartbeatBusy = true; const c = claim, sid = sessionId, gen = generation;
    try {
      if (joined) {subscribeBound(); await refreshSourceTracks();}
      if (gen !== generation || sourceFault) return;
      await d.api(`/api/performance-session/${sid}/receiver/heartbeat`,{receiver_id:receiverId,receiver_token:c.receiver_token,...evidence()});
      if (gen === generation) {const e = evidence(); notice(e.remote_video && e.audio_playback && e.joined ? '已上报接收就绪；仍需操作员观察确认' : blocked ? '浏览器阻止声音，发送门关闭' : '等待真实解码画面与声音播放许可');}
    } catch (_) {if (gen === generation) {notice('心跳未确认，发送门不可视为就绪；请停止并核查'); await leave(false);}}
    finally {heartbeatBusy = false; controls(); d.changed();}
  }
  async function leave(report = true) {
    if (leaving) return leaving;
    const old = engine, c = claim, sid = sessionId;
    try {old?.muteAllRemoteAudioPlaying(true);} catch (_) {}
    const cleanupGeneration = ++generation;
    engine = null; claim = null; joined = audioAllowed = false; blocked = false; tracks = {}; frames = 0; audioSubscribed = videoSubscribed = false;
    if (timer !== null) window.clearInterval(timer); timer = null;
    const cleanup = (async () => {
      // Assign the shared teardown promise before callbacks can re-enter sync/start.
      await Promise.resolve();
      if (report && c && sid && navigator.onLine) {
        try {await d.api(`/api/performance-session/${sid}/receiver/heartbeat`,{receiver_id:receiverId,receiver_token:c.receiver_token,joined:false,remote_video:false,audio_playback:false,autoplay_blocked:false});} catch (_) { /* TTL fails closed; do not leak tokens/errors. */ }
      }
      if (old) {try {await old.leaveChannel();} catch (_) {} try {await old.destroy();} catch (_) {}}
      if (generation === cleanupGeneration && engine === null) {video.srcObject = null; video.removeAttribute('src'); video.load();}
    })();
    leaving = cleanup;
    try {await cleanup;} finally {
      if (leaving === cleanup) {leaving = null; busy = false;}
      controls(); d.changed();
    }
  }

  async function start(sid: string) {
    if (!d.ready() || busy || startInFlight || leaving || claim || attempted === sid) return;
    startInFlight = true; attempted = sid; sessionId = sid; boundUid = null; sourceFault = subscriptionRequested = audioSubscribed = videoSubscribed = false; receiverEpoch = d.epoch(); busy = true; disposed = false; const gen = ++generation, auth = d.epoch(); controls(); d.changed(); notice('正在领取唯一接收者；不会自动重连');
    const valid = () => gen === generation && auth === d.epoch() && d.authenticated() && navigator.onLine;
    try {
      protect();
      const c = await d.api<Claim>(`/api/performance-session/${sid}/receiver/claim`,{receiver_id:receiverId});
      if (!valid()) return;
      if (c.receiver_id !== receiverId || !c.receiver_token || !c.rtc?.token || !c.rtc.user_id || typeof c.rtc.channel_id !== 'string' || !c.rtc.channel_id || c.rtc.provider && c.rtc.provider !== 'artc') throw new Error('接收授权无效');
      claim = c; timer = window.setInterval(() => void pulse(),2000);
      await loadSdk(c.rtc.sdk_url,c.rtc.sdk_integrity); if (!valid()) return;
      const factory = (window as unknown as {AliRtcEngine?: {getInstance: () => Engine; setLogLevel: (level: number) => void; AliRtcLogLevel: {NONE: number}; AliRtcRawDataStreamType: {AliRtcSdkStreamTypeCapture: number}}}).AliRtcEngine;
      if (!factory?.getInstance || typeof factory.setLogLevel !== 'function' || factory.AliRtcLogLevel?.NONE !== 5 || factory.AliRtcRawDataStreamType?.AliRtcSdkStreamTypeCapture !== 0) throw new Error('SDK 不支持安全日志配置');
      factory.setLogLevel(factory.AliRtcLogLevel.NONE);
      const e = factory.getInstance(); engine = e;
      const live = () => valid() && engine === e;
      for (const method of ['getOnlineRemoteUsers','getAudioTrack','getVideoTrack','subscribeRemoteMediaStream','muteAllRemoteAudioPlaying','muteRemoteAudioPlaying','setDefaultSubscribeAllRemoteAudioStreams','setDefaultSubscribeAllRemoteVideoStreams']) if (typeof e[method] !== 'function') throw new Error('SDK 缺少同源接收接口');
      e.on('remoteUserOnLineNotify',(uid: string) => {if (live() && acceptUid(uid)) {checkRoster(); subscribeBound();}});
      e.on('remoteUserOffLineNotify',(uid: string) => {if (live() && uid === boundUid) sourceFailure();});
      e.on('audioSubscribeStateChanged',(uid: string,_previous: number,next: number,_duration?: number,channel?: string) => {if (live() && acceptUid(uid)) {if (channel && channel !== c.rtc.channel_id) {sourceFailure(); return;} audioSubscribed = next === 3; if (!audioSubscribed) audioAllowed = false; controls(); void pulse();}});
      e.on('remoteAudioAutoPlayFail',(uid: string) => {if (live() && acceptUid(uid)) {blocked = true; audioAllowed = false; notice('浏览器阻止同一远端参与者声音，请显式开启'); controls(); void pulse();}});
      e.on('remoteAudioPlayError',(uid: string) => {if (live() && acceptUid(uid)) {blocked = true; audioAllowed = false; void pulse();}});
      e.on('videoSubscribeStateChanged',(uid: string,_previous: number,next: number,_duration?: number,channel?: string) => {if (live() && acceptUid(uid)) {if (channel && channel !== c.rtc.channel_id) {sourceFailure(); return;} videoSubscribed = next === 3; if (videoSubscribed) {e.setRemoteViewConfig(video,uid,1); void video.play().catch(() => {if (live()) {tracks.video = undefined; void pulse();}});} else {tracks.video = undefined;} void pulse();}});
      e.on('bye',() => {if (live()) {notice('远端断开；禁止自动重连'); void leave();}});
      // Do not auto-subscribe every participant, which could briefly play a second sender
      // before an event handler detects ambiguity. Only the latched UID is subscribed.
      for (const [key,value] of [['setChannelProfile','communication'],['setDefaultSubscribeAllRemoteAudioStreams',false],['setDefaultSubscribeAllRemoteVideoStreams',false]] as const) if (typeof e[key] === 'function') await (e[key] as (v: unknown) => Promise<void>).call(e,value);
      e.muteAllRemoteAudioPlaying(true);
      await e.publishLocalAudioStream(false); await e.publishLocalVideoStream(false); if (!valid()) return;
      baseFrames = rawFrames(); frames = observedFrames = 0; lastFrameAt = 0;
      if (typeof video.requestVideoFrameCallback === 'function') {const observe = () => {if (live()) {++frames; video.requestVideoFrameCallback(observe);}}; video.requestVideoFrameCallback(observe);}
      let timeout: number | undefined;
      try {await Promise.race([e.joinChannel(c.rtc.token,c.rtc.user_id),new Promise((_,reject) => {timeout = window.setTimeout(() => reject(new Error('RTC 超时')),20000);})]);} finally {window.clearTimeout(timeout);}
      if (!valid()) return;
      if (e.channel !== c.rtc.channel_id || e.userId !== c.rtc.user_id) {sourceFailure(); return;}
      joined = true; subscribeBound(); await pulse();
    } catch (_) {if (gen === generation) {notice('接收器未就绪或 SDK 未配置；请停止本场后核查，不自动重试'); await leave();}}
    finally {
      startInFlight = false;
      // A late claim/join from an old generation must never tear down a new receiver.
      if (!valid() && gen === generation && claim) await leave(false);
      if (gen === generation) busy = false;
      controls(); d.changed();
    }
  }
  el('audio').addEventListener('click', async () => {
    const e = engine, gen = generation;
    if (!e || !joined || !boundUid || !audioSubscribed || sourceFault || !checkRoster()) return;
    const uid = boundUid;
    try {await refreshSourceTracks(); if (sourceFault || !tracks.audio || !tracks.video || gen !== generation) return; e.muteRemoteAudioPlaying(uid,false); await Promise.resolve(e.resumeRemoteAudioStream(uid)); if (engine === e && gen === generation) {audioAllowed = true; blocked = false; await pulse();}}
    catch (_) {if (gen === generation) {audioAllowed = false; blocked = true; await pulse();}}
  });
  el('leave').addEventListener('click', () => {notice('已请求离开本机接收；云场仍须停止并回读'); void leave();});
  window.addEventListener('offline', () => {notice('浏览器离线，接收与发送门待核查'); void leave(false);});
  window.addEventListener('pagehide', () => {disposed = true; void leave();});
  return {start,leave,receiverId,canStart: (sid: string) => !busy && !startInFlight && !leaving && !claim && attempted !== sid,active: () => !!claim, sync: (sid: string | null, authenticated: boolean) => {if ((claim || engine || busy) && (!authenticated || !navigator.onLine || sid !== sessionId || receiverEpoch !== d.epoch())) void leave(false);},evidence};
}
