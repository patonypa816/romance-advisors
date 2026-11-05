"""Private, loopback-only control client; access-bearing URLs never enter logs."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp

DEFAULT_RUNTIME = Path(__file__).resolve().parents[2] / "ai-live-avatar/studio/data/runtime.json"

# Only these local messages may cross into the game UI. Studio reasons and
# transport exceptions can contain access-bearing URLs or provider details.
STARTUP_MESSAGES = {
    "startup_blocked": "数字人启动被阻断，请到数字人工作台查看会话状态。",
    "player_timeout": "数字人播放器连接等待超时，请检查唯一预览连接后重新启动。",
    "startup_timeout": "数字人启动等待超时，自动节目已停止，请检查数字人工作台。",
    "startup_failed": "数字人启动失败，自动节目已停止，请检查数字人工作台。",
    "creation_unknown": "数字人创建结果不明，禁止再次创建，请先核实云端会话。",
    "stop_unconfirmed": "数字人云端结束尚未确认，禁止再次创建，请先核实云端会话。",
    "runtime_invalid": "数字人本机连接记录无效，请先启动预览工作台。",
    "request_rejected": "数字人接口未接受当前操作，请检查预览模式、独占权限和会话状态。",
    "connection_unconfirmed": "数字人本机连接未确认，已停止自动推进。",
    "obs_required": "请以 OBS 接收模式启动 Studio，并打开 OBS 节目场景。",
    "preview_required": "请先以预览模式启动数字人工作台。",
    "session_in_use": "已有数字人会话尚未结束，请先在数字人工作台处理。",
    "generation_unconfirmed": "数字人场次标识未确认。",
    "claim_unconfirmed": "数字人独占权限未确认。",
    "service_closing": "数字人本机服务正在退出，请等待退出完成后重新启动工作台。",
    "session_used": "本场数字人会话已使用，请结束并保存后新建会话。",
    "replay_identity_mismatch": "固定对照的数字人或底图与原场次不一致，请检查后再开场。",
    "avatar_binding_invalid": "数字人形象绑定未确认，请在数字人工作台检查当前形象。",
    "credentials_missing": "已有语音与 Vidu 凭据配置未找到，请在数字人工作台检查配置。",
    "model_not_configured": "请检查百炼大脑模型配置及已有百炼凭据文件。",
    "model_worker_stopped": "大脑回复工作循环未运行，请检查数字人工作台。",
    "model_error": "请先处理百炼模型错误并手动恢复。",
    "previous_session_unconfirmed": "上一场云端结束尚未确认，禁止创建下一场，请先核实旧场记录。",
}


class AvatarClientError(RuntimeError):
    def __init__(self, code):
        self.code = code if code in STARTUP_MESSAGES else "startup_blocked"
        super().__init__(STARTUP_MESSAGES[self.code])


def check_startup_blocker(snapshot):
    studio = snapshot.get("studio", {})
    if studio.get("can_start") is True:
        return
    code, reason = studio.get("blocking_code"), studio.get("blocking_reason")
    if code == "player_not_connected":
        return
    if not code and reason in {None, "", "请等待工作台预览连接", "请先把播放地址加入 OBS 浏览器源"}:
        return
    # Compatibility with a Studio process that has not loaded blocking_code.
    if not code and reason == "上一场云端结束尚未确认，禁止创建下一场":
        code = "previous_session_unconfirmed"
    raise AvatarClientError(code)


class AvatarClient:
    def __init__(self, runtime_path=None, *, obs_output=False):
        self.obs_output = obs_output
        self.runtime_path = Path(runtime_path or os.environ.get("ROMANCE_AVATAR_RUNTIME", DEFAULT_RUNTIME))
        self.origin = ""
        self.control_url = ""
        self.player_url = ""
        self.owner_id = None
        self.generation = None
        self.http = None

    def discover(self):
        try:
            runtime = json.loads(self.runtime_path.read_text())
            urls = []
            for key, prefix in (("url", "/studio/"), ("player_url", "/player/")):
                value = runtime[key]
                parsed = urlsplit(value)
                if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
                        or parsed.username or parsed.password or parsed.query or parsed.fragment
                        or not parsed.path.startswith(prefix) or not parsed.path.endswith("/")):
                    raise ValueError()
                urls.append((value, f"http://127.0.0.1:{parsed.port}"))
            if urls[0][1] != urls[1][1]:
                raise ValueError()
            self.control_url, self.origin = urls[0]
            self.player_url = urls[1][0] + ("" if self.obs_output else "?preview=1")
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError(STARTUP_MESSAGES["runtime_invalid"]) from None

    async def request(self, path, *, body=None, query=None):
        if self.http is None:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        try:
            async with self.http.request("POST" if body is not None else "GET", self.control_url + path,
                                         json=body, params=query, headers={"Origin": self.origin},
                                         allow_redirects=False) as response:
                if response.status >= 300:
                    raise AvatarClientError("request_rejected")
                value = await response.json()
                if not isinstance(value, dict):
                    raise ValueError()
                return value
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise AvatarClientError("connection_unconfirmed") from None

    async def prepare_session(self, owner_id):
        self.owner_id = self.generation = None
        try:
            self.discover()
        except ValueError:
            raise AvatarClientError("runtime_invalid") from None
        snapshot = await self.snapshot()
        if snapshot.get("studio", {}).get("receiver_mode") != ("obs" if self.obs_output else "preview"):
            raise AvatarClientError("obs_required" if self.obs_output else "preview_required")
        if snapshot.get("session", {}).get("state") != "idle":
            if not snapshot.get("studio", {}).get("can_new"):
                raise AvatarClientError("session_in_use")
            await self.request("new", body={})
            snapshot = await self.snapshot()
        check_startup_blocker(snapshot)
        self.owner_id = owner_id
        # Capture the known generation before the awaited claim. If its HTTP
        # reply is lost or cancelled, stop can still address this same owner.
        self.generation = snapshot.get("session", {}).get("generation")
        if not self.generation:
            raise AvatarClientError("generation_unconfirmed")
        body = {"owner_id": owner_id}
        if self.obs_output:
            body["receiver_mode"] = "obs"
        claim = await self.request("external/claim", body=body)
        if claim.get("owner_id") != owner_id or claim.get("generation") != self.generation:
            raise AvatarClientError("claim_unconfirmed")
        self.generation = claim["generation"]
        separator = "?" if self.obs_output else "&"
        self.player_url += separator + "game_owner=" + quote(owner_id, safe="")
        return await self.snapshot()

    def owner(self):
        return {"owner_id": self.owner_id, "generation": self.generation}

    async def snapshot(self):
        return await self.request("state")

    async def start(self):
        return await self.request("start", body={})

    async def heartbeat(self):
        return await self.request("external/heartbeat", body=self.owner())

    async def external_state(self):
        return await self.request("external/state")

    def speech_body(self, record, text):
        body = {**self.owner(), "job_id": record["job_id"]}
        if record.get("mode") == "motion":
            body["mode"] = "motion"
        else:
            body["text"] = text
        for name in ("instruction", "plan", "expires_at", "finish_before"):
            if name == "instruction" and record.get("mode") == "motion":
                continue
            if name in record:
                body[name] = record[name]
        return body

    async def submit(self, record, text):
        return await self.request("external/jobs", body=self.speech_body(record, text))

    async def prepare(self, record, text):
        # PCM and word timing stay in Studio; preparing must never start delivery.
        return await self.request("external/jobs/prepare", body=self.speech_body(record, text))

    async def dispatch(self, record):
        # Reuse the immutable job ID, without resubmitting content or synthesizing.
        return await self.request("external/jobs/" + quote(record["job_id"], safe="") + "/dispatch",
                                  body=self.owner())

    async def job(self, record):
        return await self.request("external/jobs/" + quote(record["job_id"], safe=""), query=self.owner())

    async def cancel_job(self, record):
        return await self.request("external/jobs/" + quote(record["job_id"], safe="") + "/cancel", body=self.owner())

    async def stop(self):
        if self.owner_id is not None and self.generation is not None:
            await self.request("external/stop", body=self.owner())

    async def close(self):
        if self.http is not None:
            await self.http.close()
            self.http = None
