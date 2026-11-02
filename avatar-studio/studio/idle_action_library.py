"""Reviewed, avatar-scoped idle actions; cloud access is explicitly read-only.

A failed sync never edits the reviewed file or a running scheduler's deep copy.
The scheduler owns default-off enablement. Candidates are not visually verified.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import re
from pathlib import Path

import aiohttp

try:
    from . import credentials
    from .connection import client_session
    from .action_schema import _instruction
except ImportError:
    import credentials
    from connection import client_session
    from action_schema import _instruction


class IdleActionError(ValueError):
    """Safe error text: never includes credentials or upstream response bodies."""


_ID = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
DEFAULT_SETTINGS = {"min_interval_seconds": 15, "max_interval_seconds": 25}
MAX_RESPONSE = 1024 * 1024


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise IdleActionError("待机动作 ID 格式不正确")
    return value


def _text(value, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise IdleActionError("待机动作文字为空、过长或格式不正确")
    return value


def _number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
        raise IdleActionError("待机动作数值超出允许范围")
    return value


def _settings(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_SETTINGS):
        raise IdleActionError("待机频率设置格式不正确")
    result = {**DEFAULT_SETTINGS, **value}
    for item in result.values():
        _number(item, 5, 300)
    if result["min_interval_seconds"] > result["max_interval_seconds"]:
        raise IdleActionError("最短间隔不能大于最长间隔")
    return result


def _reviewed_instruction(value):
    value = _text(value)
    lines = value.split("\n")
    if len(lines) != 3:
        raise IdleActionError("待机指令必须为动作、眼神、表情三行")
    pieces = []
    for line, label in zip(lines, ("动作", "眼神", "表情")):
        prefix = label + "："
        if not line.startswith(prefix):
            raise IdleActionError("待机指令必须为动作、眼神、表情三行")
        pieces.append(_text(line[len(prefix):]))
    _instruction(*pieces)  # 复用实际Type9编译器的长度合同，保留已审阅原文。
    return value


def _action(value):
    if not isinstance(value, dict):
        raise IdleActionError("本地待机动作格式不正确")
    result = {"id": _id(value.get("id")), "name": _text(value.get("name"), 80),
              "instruction": _reviewed_instruction(value.get("instruction")),
              "recovery_instruction": _reviewed_instruction(value.get("recovery_instruction")),
              "hold_seconds": _number(value.get("hold_seconds", 3), 2, 4),
              "cooldown_seconds": _number(value.get("cooldown_seconds", 60), 0, 3600),
              "weight": _number(value.get("weight", 1), 0.01, 100)}
    digest = value.get("description_sha256")
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise IdleActionError("动作描述摘要格式不正确")
    result["description_sha256"] = digest
    for key, default in (("enabled", True), ("visual_verified", False)):
        item = value.get(key, default)
        if not isinstance(item, bool):
            raise IdleActionError("动作开关格式不正确")
        result[key] = item
    return result


class ViduIdleActionClient:
    """Fixed host, GET only, bounded bodies, no redirect and lazy credentials."""
    def __init__(self):
        self._api = None

    async def _get(self, path, params=None):
        # Paths are built only by methods below, never accepted from local config.
        try:
            if self._api is None:
                self._api = client_session()
            key = credentials.load_vidu()
            async with self._api.get("https://api.vidu.cn" + path, params=params,
                    headers={"Authorization": "Token " + key}, allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status != 200:
                    raise IdleActionError(f"待机动作库读取失败（HTTP {response.status}）")
                chunks, length = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    length += len(chunk)
                    if length > MAX_RESPONSE:
                        raise IdleActionError("待机动作库响应过大")
                    chunks.append(chunk)
                value = json.loads(b"".join(chunks))
                if not isinstance(value, dict):
                    raise IdleActionError("待机动作库响应格式不正确")
                return value
        except (IdleActionError, asyncio.CancelledError):
            raise
        except Exception:
            raise IdleActionError("待机动作库读取未完成，请检查网络或凭据") from None

    async def get_library(self, library_id):
        value = await self._get("/live/v1/action-libraries/" + _id(library_id))
        return {key: value[key] for key in ("id", "name", "avatar_id") if key in value}

    async def list_actions(self, library_id, page, page_size):
        value = await self._get("/live/v1/action-libraries/" + _id(library_id) + "/actions",
                                {"pager.page": page, "pager.pagesz": page_size})
        items = value.get("items")
        if not isinstance(items, list):
            raise IdleActionError("待机动作列表格式不正确")
        return {"total": value.get("total"), "items": [
            {key: row[key] for key in ("id", "name", "description") if key in row}
            if isinstance(row, dict) else None for row in items]}

    async def close(self):
        if self._api is not None:
            await self._api.close()


class IdleActionLibrary:
    def __init__(self, data: Path, client=None):
        self.path = Path(data) / "idle-action-library.json"
        self.client = client if client is not None else ViduIdleActionClient()
        self._lock = asyncio.Lock()
        self._verified = None
        self.sync_status = "not_synced"
        self.error = None
        self.state = None
        if self.path.exists():
            try:
                if self.path.stat().st_size > MAX_RESPONSE:
                    raise ValueError()
                raw = json.loads(self.path.read_text())
                if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("actions"), list):
                    raise ValueError()
                actions = [_action(row) for row in raw["actions"]]
                if len(actions) > 100 or len({row["id"] for row in actions}) != len(actions):
                    raise ValueError()
                self.state = {"version": 1, "library_id": _id(raw.get("library_id")),
                              "avatar_id": _id(raw.get("avatar_id")), "actions": actions,
                              "settings": _settings(raw.get("settings", {}))}
            except (OSError, ValueError, TypeError, KeyError):
                # 可选待机模块失效不应阻断工作台、语音或主讲启动。
                self.state = None
                self.sync_status = "review_required"
                self.error = "本地待机动作配置损坏，请保留并复核原文件；不影响语音和主讲"

    def public(self, avatar_id=None):
        result = copy.deepcopy(self.state or {"actions": [], "settings": DEFAULT_SETTINGS})
        for row in result["actions"]:
            row.pop("instruction", None)
            row.pop("recovery_instruction", None)
        result.update(configured=self.state is not None,
                      matches_avatar=self.state is not None and avatar_id == self.state["avatar_id"],
                      sync_status=self.sync_status, error=self.error)
        return result

    def snapshot(self, avatar_id):
        if not self.state or avatar_id != self.state["avatar_id"]:
            raise IdleActionError("待机动作库不适用于当前形象")
        if self.sync_status != "verified" or self._verified is None:
            raise IdleActionError("待机动作库尚未通过同步复核")
        return copy.deepcopy((self._verified["actions"], self._verified["settings"]))

    async def sync(self, avatar_id):
        async with self._lock:
            self.sync_status = "syncing"
            self.error = None
            try:
                if not self.state or avatar_id != self.state["avatar_id"]:
                    raise IdleActionError("待机动作库不适用于当前形象")
                library_id = self.state["library_id"]
                library = await self.client.get_library(library_id)
                if not isinstance(library, dict) or library.get("id") != library_id:
                    raise IdleActionError("待机动作库标识变化，请复核")
                if "avatar_id" in library and library["avatar_id"] != avatar_id:
                    raise IdleActionError("远端动作库形象不匹配，请复核")
                remote = {}
                total = None
                for page in range(100):
                    listing = await self.client.list_actions(library_id, page, 50)
                    if not isinstance(listing, dict) or not isinstance(listing.get("items"), list):
                        raise IdleActionError("待机动作列表格式不正确")
                    count = listing.get("total")
                    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 5000:
                        raise IdleActionError("待机动作分页格式不正确")
                    if total is not None and total != count:
                        raise IdleActionError("动作库同步期间发生变化，请重试复核")
                    total = count
                    for row in listing["items"]:
                        if not isinstance(row, dict):
                            raise IdleActionError("待机动作条目格式不正确")
                        action_id = _id(row.get("id"))
                        if action_id in remote:
                            raise IdleActionError("待机动作分页重复，请复核")
                        remote[action_id] = {"name": _text(row.get("name"), 80),
                                             "description": _text(row.get("description"), 2000)}
                    if len(remote) == total:
                        break
                    if not listing["items"] or len(remote) > total:
                        raise IdleActionError("待机动作分页不完整，请复核")
                else:
                    raise IdleActionError("待机动作分页过多，请复核")
                if set(remote) != {row["id"] for row in self.state["actions"]}:
                    raise IdleActionError("远端动作新增或删除，请人工复核本地快照")
                for row in self.state["actions"]:
                    actual = remote[row["id"]]
                    digest = hashlib.sha256(actual["description"].encode("utf-8")).hexdigest()
                    if actual["name"] != row["name"] or digest != row["description_sha256"]:
                        raise IdleActionError("远端动作名称或描述变化，请人工复核本地快照")
                self._verified = copy.deepcopy(self.state)
                self.sync_status = "verified"
            except asyncio.CancelledError:
                self.sync_status = "review_required"
                self.error = "待机动作同步已中断；不影响语音和主讲"
                raise
            except Exception:
                # Even injected clients may put credentials into exception messages.
                self.sync_status = "review_required"
                self.error = "待机动作库同步未通过，请复核网络、形象及远端动作；不影响语音和主讲"
            return self.public(avatar_id)

    def configure(self, settings, enabled_ids):
        if self.sync_status == "syncing":
            raise IdleActionError("同步进行中，请稍后设置")
        if self.state is None:
            raise IdleActionError("尚未配置待机动作库")
        if not isinstance(enabled_ids, list) or any(not isinstance(item, str) for item in enabled_ids):
            raise IdleActionError("动作启用列表格式不正确")
        known = {row["id"] for row in self.state["actions"]}
        if len(set(enabled_ids)) != len(enabled_ids) or not set(enabled_ids) <= known:
            raise IdleActionError("动作启用列表包含未知或重复 ID")
        candidate = copy.deepcopy(self.state)
        candidate["settings"] = _settings(settings)
        for row in candidate["actions"]:
            row["enabled"] = row["id"] in enabled_ids
        temporary = self.path.with_suffix(".json.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(candidate, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise IdleActionError("待机动作配置保存失败，原设置未更新") from None
        self.state = candidate
        if self.sync_status == "verified":
            self._verified = copy.deepcopy(candidate)
        return self.public(candidate["avatar_id"])

    async def close(self):
        await self.client.close()
