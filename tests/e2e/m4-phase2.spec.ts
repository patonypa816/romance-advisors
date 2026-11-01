import { test, expect } from '../../frontend/node_modules/@playwright/test/index.mjs';

// Deliberately MOCKED receiver/API evidence; never RTC, cloud, audio or paid acceptance.
test('mocked real-media receiver fails closed on autoplay and keeps stop/readback distinct', async ({ page }, testInfo) => {
  const errors: string[] = [], outbound: string[] = [], heartbeats: any[] = [], commands: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('request', r => {if (new URL(r.url()).hostname !== '127.0.0.1') outbound.push(r.url());});
  await page.addInitScript(() => {
    const w = window as any; w.__mockRtc = {events:{},publish:[],sdk:[],audioFails:false};
    class FakePC extends EventTarget {
      constructor() {super(); w.__mockRtc.pc = this;}
    }
    w.RTCPeerConnection = FakePC;
    Object.defineProperty(HTMLVideoElement.prototype,'videoWidth',{get:()=>640,configurable:true});
    Object.defineProperty(HTMLVideoElement.prototype,'videoHeight',{get:()=>360,configurable:true});
    Object.defineProperty(HTMLMediaElement.prototype,'readyState',{get:()=>4,configurable:true});
    HTMLMediaElement.prototype.play = async function() {};
    HTMLVideoElement.prototype.requestVideoFrameCallback = function(callback: any) {return window.setTimeout(()=>callback(performance.now(),{}),40);};
    const engine: any = {
      on:(event:string,fn:any)=>{w.__mockRtc.events[event]=fn;},
      publishLocalAudioStream:async(v:boolean)=>{w.__mockRtc.publish.push(['audio',v]);},
      publishLocalVideoStream:async(v:boolean)=>{w.__mockRtc.publish.push(['video',v]);},
      setChannelProfile:async()=>{},setDefaultSubscribeAllRemoteAudioStreams:async()=>{},setDefaultSubscribeAllRemoteVideoStreams:async()=>{},
      setRemoteViewConfig:()=>{},
      joinChannel:async()=>{
        const pc = new w.RTCPeerConnection();
        for (const kind of ['audio','video']) {const track:any=new EventTarget(); track.kind=kind;track.readyState='live';track.muted=false;const event:any=new Event('track');event.track=track;pc.dispatchEvent(event);}
        w.__mockRtc.events.audioSubscribeStateChanged('remote',0,3);
        w.__mockRtc.events.videoSubscribeStateChanged('remote',0,3);
      },
      resumeRemoteAudioStream:async()=>{if(w.__mockRtc.audioFails) throw new Error('mock autoplay denied');},
      leaveChannel:async()=>{},destroy:async()=>{},
    };
    w.AliRtcEngine={getInstance:()=>engine,AliRtcLogLevel:{NONE:5},setLogLevel:(level:number)=>{w.__mockRtc.logLevel=level;}};
    // The pinned loader/SRI is inspected, not weakened in production. No SDK download.
    const interceptSdk=(node:Node|string) => {
      if (!(node instanceof HTMLScriptElement) || new URL(node.src).pathname !== '/vendor/aliyun-rtc-sdk-7.1.9.js') return false;
      if (node.integrity !== 'sha256-gQAzg1HOYiodJeJplyR+VbCRNfRYuQ50iZWzV84m43Y=') throw new Error('Mock requires the exact production SDK pin');
      w.__mockRtc.sdk.push({src:node.getAttribute('src'),integrity:node.integrity});
      setTimeout(()=>node.dispatchEvent(new Event('load')),0);return true;
    };
    // ParentNode.append is native and does not call a JS override of appendChild.
    // Cover the actual production loader's document.head.append(script) boundary.
    const originalAppend=Element.prototype.append;
    Element.prototype.append=function(...nodes:(Node|string)[]) {
      const remaining=nodes.filter(node=>!interceptSdk(node));
      if (remaining.length) originalAppend.apply(this,remaining);
    };
    const original=Node.prototype.appendChild;
    Node.prototype.appendChild=function<T extends Node>(node:T):T {
      if(interceptSdk(node)) return node;
      return original.call(this,node) as T;
    };
  });
  let revision=0, session:any=null, holdReadback=false;
  let releaseReadback: (()=>void) | undefined;
  const media:any={state:'not_started',owned_live_id:null,creation_attempted:false,cloud_end_confirmed:false,
    real_enabled:false,paid_enabled:false,operator_confirmed:false,
    receiver:{claimed:false,healthy:false,joined:false,remote_video:false,audio_playback:false,autoplay_blocked:false,evidence_source:'client_reported'}};
  const snapshot=()=>({revision:++revision,server_time:new Date().toISOString(),session,media});
  await page.route('**/api/**',async route=>{
    const req=route.request(), path=new URL(req.url()).pathname, body=req.method()==='POST'?req.postDataJSON():null;
    let result:any;
    if(path==='/api/session') result={authenticated:true};
    else if(path==='/api/health') result={status:'ok',mode:'local-test',world_id:'mocked-receiver-only'};
    else if(path==='/api/players') result={revision:++revision,players:[]};
    else if(path==='/api/interviews') result={revision:++revision,interviews:[]};
    else if(path==='/api/competitions') result={revision:++revision,competitions:[]};
    else if(path==='/api/performances') result={...snapshot(),performances:[]};
    else if(path==='/api/performance-session') {
      if(body) {session={session_id:body.session_id,mode:'real',status:'paused',avatar_aspect:body.avatar_aspect,media_verified:false};commands.push('create_local');}
      result=snapshot();
    } else if(path.endsWith('/media/start')) {
      expect(body.confirm_paid).toBe(true);commands.push('start_mock');media.state='waiting_receiver';media.creation_attempted=true;media.owned_live_id='owned-mock';result=snapshot();
    } else if(path.endsWith('/receiver/claim')) {
      media.receiver.claimed=true;
      result={receiver_id:body.receiver_id,receiver_token:'ephemeral-receiver-mock',epoch:'mock-epoch',rtc:{provider:'artc',user_id:'mock-user',token:'ephemeral-rtc-mock',sdk_url:'/vendor/aliyun-rtc-sdk-7.1.9.js',sdk_integrity:'sha256-gQAzg1HOYiodJeJplyR+VbCRNfRYuQ50iZWzV84m43Y='}};
    } else if(path.endsWith('/receiver/heartbeat')) {
      heartbeats.push(body);Object.assign(media.receiver,{joined:body.joined,remote_video:body.remote_video,audio_playback:body.audio_playback,autoplay_blocked:body.autoplay_blocked});
      media.receiver.healthy=body.joined&&body.remote_video&&body.audio_playback&&!body.autoplay_blocked;
      if(!media.receiver.healthy) {media.operator_confirmed=false;if(session)session.status='paused';}
      else media.state='ready';result=snapshot();
    } else if(path.endsWith('/media/confirm-ready')) {expect(media.receiver.healthy).toBe(true);media.operator_confirmed=true;commands.push('confirm_mock');result=snapshot();}
    else if(path.endsWith('/resume')) {session.status='active';commands.push('resume_mock');result=snapshot();}
    else if(path.endsWith('/pause')) {session.status='paused';commands.push('pause_only');result={...snapshot(),stop_status:'unstarted_cancelled'};}
    else if(path.endsWith('/media/stop')) {media.state='unknown';media.cloud_end_confirmed=false;session.status='paused';commands.push('stop_mock_unknown');result=snapshot();}
    else if(path.endsWith('/media/reconcile')) {
      if(holdReadback) {commands.push('get_reconcile_held');result=snapshot();await new Promise<void>(resolve=>{releaseReadback=resolve;});}
      else {media.state='ended';media.cloud_end_confirmed=true;commands.push('get_reconcile_mock');result=snapshot();}
    }
    else {throw new Error(`Unexpected mocked route ${path}`);}
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(result)});
  });
  await page.goto('/');
  await page.locator('.real-media-controls > summary').click();
  await expect(page.locator('#performance-media-state')).toContainText('禁用');
  await expect(page.locator('#performance-session-real-create')).toBeDisabled();
  expect(commands).toEqual([]);
  media.real_enabled=true;media.paid_enabled=true;
  await page.locator('#performance-refresh').click();
  await expect(page.locator('#performance-session-real-create')).toBeEnabled();
  await page.locator('#performance-session-real-create').click();
  await expect(page.locator('#performance-media-start')).toBeDisabled();
  await expect(page.locator('#performance-aspect')).toHaveValue('');
  await page.locator('#performance-aspect').selectOption('16:9');
  await page.locator('#performance-paid-confirm').check();
  await page.locator('#performance-media-start').click();
  await expect(page.locator('#performance-media-start')).toBeDisabled();
  await expect(page.locator('#performance-session-close')).toBeDisabled();
  await page.locator('#receiver-start').click();
  await expect.poll(()=>page.evaluate(()=>(window as any).__mockRtc.sdk.length)).toBe(1);
  await expect(page.locator('#receiver-audio')).toBeEnabled();
  await expect.poll(()=>heartbeats.some(h=>h.joined)).toBe(true);
  await expect(page.locator('#performance-media-confirm-ready')).toBeDisabled();
  await page.evaluate(()=>{(window as any).__mockRtc.audioFails=true;});
  await page.locator('#receiver-audio').click();
  await expect.poll(()=>heartbeats.some(h=>h.autoplay_blocked&&!h.audio_playback)).toBe(true);
  await expect(page.locator('#receiver-status')).toContainText('client_reported');
  await expect(page.locator('#performance-session-resume')).toBeDisabled();
  await page.evaluate(()=>{(window as any).__mockRtc.audioFails=false;});
  await page.locator('#receiver-audio').click();
  await expect.poll(()=>heartbeats.some(h=>h.joined&&h.remote_video&&h.audio_playback&&!h.autoplay_blocked)).toBe(true);
  await expect(page.locator('#performance-media-confirm-ready')).toBeEnabled();
  await page.locator('#performance-media-confirm-ready').click();
  await page.locator('#performance-session-resume').click();
  await expect(page.locator('#performance-session-pause')).toBeEnabled();
  await page.locator('#performance-session-pause').click();
  expect(commands).not.toContain('stop_mock_unknown');
  await expect(page.locator('#performance-media-state')).toContainText('云场结束回读 未确认');
  holdReadback=true;
  await page.locator('#performance-media-reconcile').click();
  await expect.poll(()=>commands.includes('get_reconcile_held')).toBe(true);
  await expect(page.locator('#performance-media-stop')).toBeEnabled();
  await page.locator('#performance-media-stop').click();
  await expect(page.locator('#performance-media-state')).toContainText('unknown');
  holdReadback=false;releaseReadback?.();
  await expect(page.locator('#performance-session-close')).toBeDisabled();
  await expect(page.locator('#performance-media-start')).toBeDisabled();
  await page.locator('#performance-media-reconcile').click();
  await expect(page.locator('#performance-media-state')).toContainText('ended');
  await expect(page.locator('#performance-session-close')).toBeEnabled();
  const evidence=await page.evaluate(()=>({mock:{publish:(window as any).__mockRtc.publish,sdk:(window as any).__mockRtc.sdk,logLevel:(window as any).__mockRtc.logLevel},storage:JSON.stringify({...localStorage,...sessionStorage})}));
  expect(evidence.mock.logLevel).toBe(5);
  expect(evidence.mock.publish).toEqual([['audio',false],['video',false]]);
  expect(evidence.mock.sdk).toEqual([{src:'/vendor/aliyun-rtc-sdk-7.1.9.js',integrity:'sha256-gQAzg1HOYiodJeJplyR+VbCRNfRYuQ50iZWzV84m43Y='}]);
  expect(evidence.storage).not.toContain('ephemeral-');
  await page.screenshot({path:testInfo.outputPath('m4-phase2-mocked-receiver-ended-desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await page.locator('#performance-aspect').selectOption('4:3');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.screenshot({path:testInfo.outputPath('m4-phase2-mocked-receiver-ended-mobile.png'),fullPage:true});
  expect(outbound).toEqual([]);expect(errors).toEqual([]);
});
