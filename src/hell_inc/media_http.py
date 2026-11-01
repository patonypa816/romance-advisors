"""Narrow lazy HTTPS/WSS transport. Never resolve opaque network-secret strings.

No ambient netrc/default Authorization, no redirect following, and no credential
forwarding to signed download URLs. Import/construct performs no I/O.
"""
import ipaddress
import json
import re
from urllib.parse import urlsplit, urlunsplit
import aiohttp


def validate_secret(value):
    if not isinstance(value,str) or not value or len(value)>8192 or any(c in value for c in '\r\n\x00'):
        raise ValueError('invalid_opaque_credential')
    return value


def validate_workspace(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?',value):
        raise ValueError('invalid_workspace')
    return value


def validate_url(url,allowed_hosts):
    if not isinstance(url,str) or len(url)>8192 or any(c in url for c in '\r\n\x00\\'):
        raise ValueError('unapproved_media_url')
    p=urlsplit(url)
    if p.scheme not in ('https','wss') or p.username or p.password or p.fragment or p.port not in (None,443):
        raise ValueError('unapproved_media_url')
    host=p.hostname
    if not host or host not in set(allowed_hosts) or host=='localhost' or host.endswith(('.localhost','.local')):
        raise ValueError('unapproved_media_host')
    try:
        if not ipaddress.ip_address(host).is_global:raise ValueError('private_media_host')
    except ValueError as exc:
        if str(exc)=='private_media_host':raise
        # DNS names are exact operator-approved destinations, never caller-derived.
        if re.fullmatch(r'[0-9.]+',host):raise ValueError('invalid_media_host') from None
    return url


def scrub_url(url):
    try:
        p=urlsplit(url)
        return urlunsplit((p.scheme,p.hostname or '',p.path,'',''))
    except (ValueError,TypeError):return '[redacted-url]'


def validate_proxy(proxy):
    if proxy is None:return None
    if not isinstance(proxy,str) or any(c in proxy for c in '\r\n\x00'):raise ValueError('invalid_proxy')
    p=urlsplit(proxy)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ('','/'):
        raise ValueError('invalid_proxy')
    return proxy


class _NoRedirectSession(aiohttp.ClientSession):
    # aiohttp 3.14.4 _ws_connect calls self.request without allow_redirects.
    # Override the shared request boundary so HTTP Upgrade cannot follow a 30x.
    async def _request(self,method,url,**kwargs):
        kwargs['allow_redirects']=False
        return await super()._request(method,url,**kwargs)


class MediaHTTP:
    def __init__(self,session=None,*,workspace,proxy=None,audio_hosts=()):
        self.workspace=validate_workspace(workspace)
        self.api_host=f'{workspace}.cn-beijing.maas.aliyuncs.com'
        self.audio_hosts=frozenset(audio_hosts)
        self.proxy=validate_proxy(proxy)
        self.session=session
        self._owns=session is None

    def _session(self):
        if self.session is None:
            self.session=_NoRedirectSession(trust_env=False,timeout=aiohttp.ClientTimeout(total=120),cookie_jar=aiohttp.DummyCookieJar())
        return self.session

    def _kwargs(self,url,kwargs,websocket=False):
        validate_url(url,{'api.vidu.cn',self.api_host}|self.audio_hosts)
        p=urlsplit(url)
        if websocket and p.scheme!='wss':raise ValueError('wss_required')
        if not websocket and p.scheme!='https':raise ValueError('https_required')
        headers=dict(kwargs.pop('headers',{}) or {})
        for key in headers:
            if key.lower() in ('cookie','proxy-authorization'):raise ValueError('forbidden_media_header')
        authkeys=[k for k in headers if k.lower()=='authorization']
        if len(authkeys)>1:raise ValueError('duplicate_authorization')
        if p.hostname in self.audio_hosts and p.hostname not in ('api.vidu.cn',self.api_host):
            # Never leak caller/session Authorization to a signed media host.
            for key in authkeys:headers.pop(key)
        elif authkeys:
            value=headers[authkeys[0]]
            prefix='Token ' if p.hostname=='api.vidu.cn' else 'Bearer '
            if not isinstance(value,str) or not value.startswith(prefix):raise ValueError('wrong_authorization_scheme')
            validate_secret(value[len(prefix):])
        if kwargs.get('ssl') is False:raise ValueError('tls_verification_required')
        kwargs.update(headers=headers,proxy=self.proxy,ssl=True)
        if not websocket:kwargs['allow_redirects']=False
        kwargs.setdefault('timeout',aiohttp.ClientWSTimeout(ws_close=5) if websocket else aiohttp.ClientTimeout(total=120))
        return kwargs

    def request(self,method,url,**kwargs):
        kwargs=self._kwargs(url,kwargs)
        if method.upper() not in ('GET','POST'):raise ValueError('unsupported_media_method')
        # Explicit empty headers are safe because this wrapper owns a client with
        # no default credentials. Injected sessions must meet the same contract.
        self._no_ambient_auth()
        return self._session().request(method,url,**kwargs)

    def get(self,url,**kwargs):return self.request('GET',url,**kwargs)
    def post(self,url,**kwargs):return self.request('POST',url,**kwargs)

    def _no_ambient_auth(self):
        if self.session is not None and any(k.lower() in ('authorization','cookie','proxy-authorization') for k in getattr(self.session,'headers',{})):
            raise ValueError('ambient_authorization_forbidden')

    async def ws_connect(self,url,**kwargs):
        self._no_ambient_auth()
        if isinstance(self.session,aiohttp.ClientSession) and not isinstance(self.session,_NoRedirectSession):
            raise ValueError('websocket_redirect_policy_required')
        kwargs=self._kwargs(url,kwargs,True)
        return await self._session().ws_connect(url,**kwargs)

    async def close(self):
        if self._owns and self.session is not None:await self.session.close()


class PlannerOutputError(ValueError):
    """Only successful-response model content, never headers/error response bodies."""
    def __init__(self,candidate):
        self.candidate=candidate
        super().__init__('planner_output_json_invalid')


class BailianPlanner:
    """One explicitly requested non-thinking structured call; no retries.

    Output tokens and request time are bounded independently of the response byte
    safety limit. These are not a monetary cap or a cancellation/refund guarantee.
    Pinned Studio bailian_ai.py 2e076b66 also sends top-level enable_thinking=False
    and bounded max_tokens; direct HTTP (not SDK extra_body) is intentional.
    """
    def __init__(self,http,api_key,workspace,model,*,max_tokens=2048,timeout_seconds=60):
        self.http,self.api_key=http,validate_secret(api_key)
        self.workspace=validate_workspace(workspace)
        if not isinstance(model,str) or not model or len(model)>128:raise ValueError('model_not_configured')
        if type(max_tokens) is not int or not 1<=max_tokens<=4096:
            raise ValueError('planner_token_limit_invalid')
        if type(timeout_seconds) is not int or not 1<=timeout_seconds<=120:
            raise ValueError('planner_timeout_invalid')
        self.model=model
        self.max_tokens,self.timeout_seconds=max_tokens,timeout_seconds

    async def complete_json(self,prompt,data):
        endpoint=f'https://{self.workspace}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions'
        request={'model':self.model,'messages':[{'role':'system','content':prompt},{'role':'user','content':json.dumps(data,ensure_ascii=False,allow_nan=False)}], 'response_format':{'type':'json_object'},'temperature':0.2,'enable_thinking':False,'max_tokens':self.max_tokens}
        async with self.http.post(endpoint,headers={'Authorization':'Bearer '+self.api_key,'Content-Type':'application/json'},json=request,timeout=aiohttp.ClientTimeout(total=self.timeout_seconds)) as response:
            if response.status!=200:
                error=RuntimeError('planner_http_error');error.status=response.status;raise error
            raw=await response.content.read(1048577)
            if len(raw)>1048576:raise ValueError('planner_response_too_large')
            try:
                result=json.loads(raw);model_text=result['choices'][0]['message']['content']
                if not isinstance(model_text,str):raise ValueError()
            except (ValueError,KeyError,IndexError,TypeError):raise ValueError('planner_response_invalid') from None
            request_id=result.get('id')
            if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',request_id):request_id=None
            raw_usage=result.get('usage')
            usage={k:v for k,v in (raw_usage if isinstance(raw_usage,dict) else {}).items() if k in ('prompt_tokens','completion_tokens','total_tokens') and type(v) is int and v>=0}
            candidate={'model_text':model_text,'request_id':request_id,'usage':usage}
            try:
                output=json.loads(model_text)
                if not isinstance(output,dict):raise ValueError()
            except (ValueError,TypeError):raise PlannerOutputError(candidate) from None
            return {'output':output,'request_id':request_id,'usage':usage}

