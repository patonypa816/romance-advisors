"""One lazy owned media component and one ephemeral client-reported receiver.

This runtime is never a cloud-session discovery/adoption API. Only an ID committed
by on_created can be reconciled. No provider is constructed at import/startup.
"""
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import os
import re
import secrets
import time
import hashlib
from uuid import uuid4
from .media_http import MediaHTTP,validate_secret,validate_workspace,validate_proxy,BailianPlanner
from .performance_contract import PerformanceError

SDK_URL='/vendor/aliyun-rtc-sdk-7.1.9.js'
SDK_INTEGRITY='sha256-gQAzg1HOYiodJeJplyR+VbCRNfRYuQ50iZWzV84m43Y='

@dataclass(frozen=True)
class MediaConfig:
    api_key: str=field(repr=False)
    tts_key: str=field(repr=False)
    workspace: str
    avatar_id: str
    model: str
    voice: str
    persona: str
    artifact_dir: Path=Path('/tmp/hell-inc-media')
    audio_hosts: tuple=()
    proxy: str|None=field(default=None,repr=False)

    def __post_init__(self):
        validate_secret(self.api_key);validate_secret(self.tts_key);validate_workspace(self.workspace);validate_proxy(self.proxy)
        if not Path(self.artifact_dir).is_absolute():raise ValueError('media_directory_must_be_absolute')
        if not re.fullmatch(r'[0-9]{1,64}',self.avatar_id):raise ValueError('avatar_id_required')
        for value in (self.model,self.voice,self.persona):
            if not isinstance(value,str) or not value.strip() or len(value)>2000 or '\x00' in value:raise ValueError('runtime_voice_persona_required')

    @classmethod
    def from_env(cls):
        # No CSV, key-prefix heuristics, placeholder expansion, or old asset default.
        def configured(primary,existing):
            # Explicit primary (including empty) overrides legacy deployment names.
            return os.environ[primary] if primary in os.environ else os.getenv(existing)
        values=(configured('HELL_INC_VIDU_KEY','VIDU_API_KEY'),configured('HELL_INC_BAILIAN_KEY','DASHSCOPE_API_KEY'),
                configured('HELL_INC_BAILIAN_WORKSPACE','BAILIAN_WORKSPACE_ID'),
                os.getenv('HELL_INC_AVATAR_ID'),os.getenv('HELL_INC_PLANNER_MODEL'),os.getenv('HELL_INC_TTS_VOICE'),os.getenv('HELL_INC_VOICE_PERSONA'))
        if any(not value for value in values):raise PerformanceError('real_media_not_configured')
        return cls(*values,artifact_dir=Path(os.getenv('HELL_INC_MEDIA_DIR','/tmp/hell-inc-media')),
                   audio_hosts=tuple(x for x in os.getenv('HELL_INC_AUDIO_HOSTS','').split(',') if x),proxy=os.getenv('HTTPS_PROXY') or None)



def empty_receiver():
    return {'claimed':False,'healthy':False,'joined':False,'remote_video':False,'audio_playback':False,'autoplay_blocked':False,'evidence_source':'client_reported'}


class RealMediaRuntime:
    RECEIVER_TTL=10.0
    def __init__(self,settings,*,component_factory=None,http=None,config=None,clock=time.monotonic):
        self.settings=settings;self.component_factory=component_factory;self.http=http;self.config=config;self.clock=clock
        self.component=None;self.session_id=None;self.epoch=str(uuid4())
        self.receiver_id=None;self.receiver_token=None;self.receiver_deadline=0
        self.receiver=empty_receiver();self.operator_confirmed=False;self.paused=True;self.needs_pause_sync=False
        self.owned_live_id=None;self.tasks=set();self._adapter=None;self.persist=None

    @property
    def real_enabled(self):return self.settings.real_media_enabled is True
    @property
    def paid_enabled(self):return self.settings.allow_paid_media is True

    def default_media(self):
        return {'state':'not_started','owned_live_id':None,'creation_attempted':False,'cloud_end_confirmed':False,'receiver':empty_receiver(),'operator_confirmed':False,'real_enabled':self.real_enabled,'paid_enabled':self.paid_enabled}

    def view(self,media=None):
        result={**self.default_media(),**(media or {})}
        result.update(real_enabled=self.real_enabled,paid_enabled=self.paid_enabled)
        if self.session_id:
            receiver=dict(self.receiver);receiver['healthy']=self.receiver_healthy()
            result.update(receiver=receiver,operator_confirmed=self.operator_confirmed)
        else:
            result.update(receiver=empty_receiver(),operator_confirmed=False)
        return result

    def _setup(self):
        if self.config is None:self.config=MediaConfig.from_env()
        if self.http is None:self.http=MediaHTTP(workspace=self.config.workspace,proxy=self.config.proxy,audio_hosts=self.config.audio_hosts)

    def ensure_configured(self):
        if not self.real_enabled or not self.paid_enabled:raise PerformanceError('real_media_disabled')
        # Validation only. Construction does not issue a request.
        if self.config is None:self.config=MediaConfig.from_env()
        if self.component_factory is None:
            sdk=Path(self.settings.frontend_dir)/'vendor'/'aliyun-rtc-sdk-7.1.9.js'
            if not getattr(self.settings,'rtc_connect_origins',()) or not sdk.is_file() or hashlib.sha256(sdk.read_bytes()).hexdigest()!='8100338351ce622a1d25e26997247e55b09135f458b90e748995b357ce26e376':
                raise PerformanceError('rtc_transport_not_configured')

    async def start(self,session_id,on_created,persist):
        self.ensure_configured()
        if self.component is not None or self.session_id is not None:raise PerformanceError('media_already_attempted')
        self._setup();self.session_id=session_id;self.persist=persist;self.epoch=str(uuid4());self.paused=True
        out=Path(self.config.artifact_dir)/session_id/self.epoch
        out.mkdir(parents=True,exist_ok=False,mode=0o700)
        os.chmod(out,0o700);os.chmod(out.parent,0o700)
        if self.component_factory is None:
            from .avatar_transport.motion_component import MotionComponent
            factory=MotionComponent
        else:factory=self.component_factory
        async def created(live_id,safe_metadata):
            # Complete DB ownership before allowing the component to open WSS.
            await on_created(live_id,{'state':'connecting'})
            self.owned_live_id=live_id
        self.component=factory(self.http,out,Path('UNUSED'),on_created=created,recording=False)
        await self.component.start(self.config.api_key,avatar_id=self.config.avatar_id)
        task=asyncio.create_task(self._monitor(self.component,self.persist,self.epoch));self.tasks.add(task);task.add_done_callback(self.tasks.discard)

    async def _monitor(self,component,persist,epoch):
        previous=None
        while self.component is component and self.epoch==epoch:
            p=component.public();state=p.get('state')
            mapped={'waiting_stage':'waiting_receiver','ready':'ready','sending':'ready','draining':'ready','ended':'ended','error':'unknown','stop_unconfirmed':'unknown','creation_unknown':'unknown','stopping':'stopping','creating':'creating','starting':'creating','connecting':'connecting'}.get(state,'unknown')
            ended=p.get('cloud_end_confirmed') is True
            if mapped!=previous:
                try:await persist({'state':mapped,'cloud_end_confirmed':ended})
                except Exception:
                    self.paused=True;component.latch_stop();return
                previous=mapped
            if mapped in ('ended','unknown'):return
            await asyncio.sleep(.1)

    def receiver_healthy(self):
        if self.receiver_id and self.clock()>=self.receiver_deadline:
            if not self.paused or self.operator_confirmed:self.needs_pause_sync=True
            self.paused=True;self.operator_confirmed=False
        r=self.receiver
        return bool(self.receiver_id and self.clock()<self.receiver_deadline and r['joined'] and r['remote_video'] and r['audio_playback'] and not r['autoplay_blocked'])

    def ready(self,session_id):
        return bool(self.real_enabled and self.paid_enabled and session_id==self.session_id and self.owned_live_id and self.component is not None and self.component.public().get('ready') and self.receiver_healthy() and self.operator_confirmed and not self.paused)

    def assert_ready(self,session_id):
        if not self.ready(session_id):raise PerformanceError('real_media_not_ready')

    def validate(self,context):
        if not isinstance(context,dict):raise PerformanceError('execution_context_required')
        self.assert_ready(context.get('session_id'))
        if context.get('epoch',self.epoch)!=self.epoch:raise PerformanceError('runtime_epoch_changed')

    def pause(self):
        if not self.paused:self.needs_pause_sync=True
        self.paused=True

    def resume(self,session_id):
        if session_id!=self.session_id:raise PerformanceError('runtime_not_owned')
        self.paused=False
        try:self.assert_ready(session_id)
        except BaseException:self.paused=True;raise
        self.needs_pause_sync=False

    def claim(self,session_id,receiver_id):
        if session_id!=self.session_id or not self.owned_live_id or self.component is None:raise PerformanceError('rtc_not_ready')
        if self.receiver_id is not None and self.receiver_id!=receiver_id:raise PerformanceError('receiver_already_claimed')
        rtc=self.component.rtc()
        if not rtc or not all(rtc.get(k) for k in ('token','user_id')):raise PerformanceError('rtc_not_ready')
        self.receiver_id=receiver_id;self.receiver_token=secrets.token_urlsafe(32)
        self.receiver_deadline=0;self.receiver={**empty_receiver(),'claimed':True};self.operator_confirmed=False;self.paused=True
        return {'receiver_id':receiver_id,'receiver_token':self.receiver_token,'epoch':self.epoch,'rtc':{**rtc,'sdk_url':SDK_URL,'sdk_integrity':SDK_INTEGRITY}}

    def heartbeat(self,session_id,data):
        if session_id!=self.session_id or data['receiver_id']!=self.receiver_id or not self.receiver_token or not secrets.compare_digest(data['receiver_token'],self.receiver_token):raise PerformanceError('receiver_claim_invalid')
        self.receiver_healthy()  # Latch previous lease expiry BEFORE extending it.
        self.receiver.update({k:data[k] for k in ('joined','remote_video','audio_playback','autoplay_blocked')})
        self.receiver_deadline=self.clock()+self.RECEIVER_TTL
        if not self.receiver_healthy():self.operator_confirmed=False;self.paused=True
        elif self.component is not None:self.component.joined()
        return self.view()

    def confirm(self,session_id,receiver_id):
        if session_id!=self.session_id or receiver_id!=self.receiver_id or not self.receiver_healthy():raise PerformanceError('receiver_not_healthy')
        self.operator_confirmed=True

    def adapter(self,persist_preparation):
        self._setup()
        if self._adapter is None:
            from .real_performance import RealPerformanceAdapter
            self._adapter=RealPerformanceAdapter(self,self.http,BailianPlanner(self.http,self.config.tts_key,self.config.workspace,self.config.model),
                self.config.tts_key,self.config.workspace,Path(self.config.artifact_dir),persist_preparation)
        return self._adapter

    async def stop(self,session_id,owned_live_id):
        self.pause();self.operator_confirmed=False
        if self.component is not None and self.session_id==session_id:
            self.component.latch_stop()
            try:
                p=await asyncio.wait_for(self.component.stop(),timeout=110)
                return {'state':'ended' if p.get('cloud_end_confirmed') is True else 'unknown','cloud_end_confirmed':p.get('cloud_end_confirmed') is True}
            except Exception:return {'state':'unknown','cloud_end_confirmed':False}
        # No component after restart: GET readback only, never reconnect or adopt.
        return await self.reconcile(session_id,owned_live_id)

    async def reconcile(self,session_id,owned_live_id):
        self.pause()
        if not owned_live_id:return {'state':'unknown','cloud_end_confirmed':False,'reconciliation_required':'provider_console'}
        if not isinstance(owned_live_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',owned_live_id):raise PerformanceError('invalid_owned_live_id')
        self._setup()
        async with self.http.get('https://api.vidu.cn/live/v1/lives/'+owned_live_id,headers={'Authorization':'Token '+self.config.api_key}) as response:
            if response.status!=200:return {'state':'unknown','cloud_end_confirmed':False}
            value=await response.json();live=value.get('live') or {}
            ended=str(live.get('status',live.get('state',''))).lower()=='ended'
            return {'state':'ended' if ended else 'unknown','cloud_end_confirmed':ended}

    async def release_ended(self):
        for task in tuple(self.tasks):task.cancel()
        if self.tasks:await asyncio.gather(*tuple(self.tasks),return_exceptions=True)
        self.pause();self.receiver_id=None;self.receiver_token=None;self.receiver=empty_receiver();self.operator_confirmed=False
        self.session_id=None;self.owned_live_id=None;self.component=None;self._adapter=None;self.epoch=str(uuid4());self.needs_pause_sync=False

    async def shutdown(self):
        self.pause()
        # Existing owned cloud cleanup is permitted even if paid opt-in was revoked.
        if self.component is not None:
            result=await self.stop(self.session_id,self.owned_live_id)
            if self.persist:
                try:await self.persist(result)
                except Exception:pass
        for task in tuple(self.tasks):task.cancel()
        if self.tasks:await asyncio.gather(*tuple(self.tasks),return_exceptions=True)
        if self.http is not None and hasattr(self.http,'close'):await self.http.close()
        self.receiver_token=None
