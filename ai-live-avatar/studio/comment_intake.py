"""真实评论的有界内存候选池；只有上层门禁可以把选中评论送入回复队列。"""
from __future__ import annotations

from collections import Counter, OrderedDict, deque
import hashlib
import math
import secrets
import time
import unicodedata
import uuid


FUTURE_TOLERANCE_SECONDS = 5
MAX_REASON_CHARS = 160


def _reason(value: object) -> str:
    # 异常对象、过长模型输出不能直接进入公开状态，防止整段工具响应外泄。
    if not isinstance(value, str):
        return "invalid_reason"
    text = " ".join(value.split())
    return text if 0 < len(text) <= MAX_REASON_CHARS else "invalid_reason"


def _identity(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    value = str(value)
    return value if 0 < len(value) <= 256 and value.strip() else None


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


class CandidatePool:
    """同步方法供单一事件循环使用，不负责联网、持久化或回复入队。

    消息身份以房间隔离；作者身份仅保留每个池随机盐的散列。固定消息 ID
    在有界去重窗口内一直有效，同作者同文本的重复窗口为 ttl_seconds。
    """

    def __init__(self, clock=time.time, ttl_seconds=45, max_candidates=60,
                 max_history=100, dedup_limit=4096):
        ttl = _finite(ttl_seconds)
        if ttl is None or ttl <= 0:
            raise ValueError("ttl_seconds必须为有限正数")
        for name, value in (("max_candidates", max_candidates), ("max_history", max_history),
                            ("dedup_limit", dedup_limit)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name}必须为正整数")
        self.clock = clock
        self.ttl_seconds = ttl
        self.max_candidates = max_candidates
        self.max_history = max_history
        self.dedup_limit = dedup_limit
        self.version = 0
        self._salt = secrets.token_bytes(32)
        self._candidates = OrderedDict()
        self._history = deque(maxlen=max_history)
        self._dedup = OrderedDict()
        self._deliveries = OrderedDict()
        self._counts = Counter()
        self._lease = None
        self._last_now = None

    def _now(self):
        now = _finite(self.clock())
        if now is None:
            raise ValueError("候选池时钟必须返回有限秒数")
        # 本机校时回拨不能让已等待的评论重新变年轻。
        self._last_now = now if self._last_now is None else max(now, self._last_now)
        return self._last_now

    def _hash(self, *pieces):
        digest = hashlib.blake2b(key=self._salt, digest_size=20)
        for piece in pieces:
            encoded = piece.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
        return digest.hexdigest()

    def _public(self, candidate, now):
        return {key: candidate[key] for key in ("id", "text", "nickname", "viewer_key")} | {
            "age_seconds": round(max(0, now - candidate["oldest_at"]), 3),
            "expires_at": candidate["expires_at"],
        }

    def viewer_key(self, room_id, user_id):
        """三类候选共享同一匿名身份，不能按可能重名的昵称合并观众。"""
        return self._hash("viewer", str(room_id), str(user_id))

    def _record(self, status, reason, candidate=None, now=None):
        self._counts[status] += 1
        row = {"status": status, "reason": _reason(reason), "at": now}
        if candidate is not None:
            row.update(self._public(candidate, now))
        self._history.append(row)

    def _finish_batch(self, status, reason, now):
        lease, self._lease = self._lease, None
        if lease:
            for candidate_id in lease["ids"]:
                candidate = self._candidates.pop(candidate_id, None)
                if candidate:
                    self._record(status, reason, candidate, now)

    def _cleanup(self, now):
        # 一个租约是一份固定候选清单；清单过期后整批作废，不能沿用旧模型答案。
        if self._lease and now >= self._lease["expires_at"]:
            self._finish_batch("expired", "batch_expired", now)
        for candidate_id, candidate in list(self._candidates.items()):
            if now >= candidate["expires_at"]:
                self._candidates.pop(candidate_id)
                self._record("expired", "candidate_expired", candidate, now)
        for key, entry in list(self._dedup.items()):
            if entry["until"] is not None and now >= entry["until"]:
                self._dedup.pop(key)

    def _remember(self, key, candidate_id, until):
        self._dedup[key] = {"id": candidate_id, "until": until}
        self._dedup.move_to_end(key)
        while len(self._dedup) > self.dedup_limit:
            self._dedup.popitem(last=False)

    def _reject(self, reason, now, status="error", candidate_id=None):
        self._record(status, reason, now=now)
        return {"status": status, "reason": reason, "id": candidate_id}

    def ingest(self, event: dict) -> dict:
        now = self._now()
        self._cleanup(now)
        if not isinstance(event, dict):
            return self._reject("invalid_event", now)
        if event.get("platform") != "douyin" or event.get("type") != "chat":
            return self._reject("unsupported_event", now)
        data, user = event.get("data"), event.get("user")
        if not isinstance(data, dict) or not isinstance(user, dict):
            return self._reject("invalid_event", now)
        text = data.get("content")
        # 原文过长直接拒绝，不能截短后让模型误解观众原意。
        if not isinstance(text, str) or not 1 <= len(text) <= 160 or not text.strip():
            return self._reject("invalid_text", now)
        message_id, room_id, user_id = (_identity(event.get("id")),
                                        _identity(event.get("roomId")), _identity(user.get("id")))
        if message_id is None or room_id is None or user_id is None:
            return self._reject("invalid_identity", now)
        timestamps = [_finite(event.get(key)) for key in ("ts", "receivedAt")]
        if any(value is None or value <= 0 for value in timestamps):
            return self._reject("invalid_timestamp", now)
        event_at, received_at = [value / 1000 for value in timestamps]
        if max(event_at, received_at) > now + FUTURE_TOLERANCE_SECONDS:
            return self._reject("future_timestamp", now)
        oldest_at = min(now, event_at, received_at)
        expires_at = oldest_at + self.ttl_seconds
        if now >= expires_at:
            return self._reject("old_timestamp", now, "expired")
        if event_at > received_at + FUTURE_TOLERANCE_SECONDS:
            return self._reject("inconsistent_timestamp", now)
        viewer_key = self.viewer_key(room_id, user_id)
        normalized = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
        message_key = ("message", self._hash(room_id, message_id))
        author_key = ("author", self._hash(viewer_key, normalized))
        for key, reason in ((message_key, "duplicate_message"), (author_key, "duplicate_author_text")):
            if key in self._dedup:
                duplicate_id = self._dedup[key]["id"]
                self._dedup.move_to_end(key)
                # 即使作者文本重复，也记住这次的新消息ID，防止稍后回放。
                self._remember(message_key, duplicate_id, None)
                return self._reject(reason, now, "duplicate", duplicate_id)
        nickname = user.get("nickname")
        nickname = " ".join(nickname.split())[:16] if isinstance(nickname, str) else "观众"
        candidate = {"id": uuid.uuid4().hex, "text": text, "nickname": nickname or "观众",
                     "viewer_key": viewer_key, "oldest_at": oldest_at,
                     "received_at": now, "expires_at": expires_at, "status": "candidate"}
        if len(self._candidates) >= self.max_candidates:
            # 筛选中的条目不可被新流量改变；优先淘汰等待候选。
            evict_id = next((key for key, value in self._candidates.items()
                             if value["status"] == "candidate"), None)
            if evict_id is None:
                self._remember(message_key, candidate["id"], None)
                self._remember(author_key, candidate["id"], now + self.ttl_seconds)
                self._record("evicted", "capacity_all_selecting", candidate, now)
                return {"status": "evicted", "reason": "capacity_all_selecting", "id": candidate["id"]}
            evicted = self._candidates.pop(evict_id)
            self._record("evicted", "capacity", evicted, now)
        self._candidates[candidate["id"]] = candidate
        self._remember(message_key, candidate["id"], None)
        self._remember(author_key, candidate["id"], now + self.ttl_seconds)
        self._record("candidate", "accepted", candidate, now)
        return {"status": "candidate", "reason": "accepted", "id": candidate["id"]}

    def lease_batch(self, max_items=12) -> dict | None:
        now = self._now()
        self._cleanup(now)
        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
            raise ValueError("max_items必须为正整数")
        if self._lease or not self._candidates:
            return None
        selected = list(self._candidates.values())[:min(max_items, 12)]
        self._lease = {"batch_id": uuid.uuid4().hex, "version": self.version,
                       "ids": [item["id"] for item in selected],
                       "expires_at": min(item["expires_at"] for item in selected)}
        for item in selected:
            item["status"] = "selecting"
            self._record("selecting", "batch_leased", item, now)
        return {"batch_id": self._lease["batch_id"], "version": self.version,
                "candidates": [self._public(item, now) for item in selected]}

    def resolve(self, batch_id, selected_id: str | None, reason: str) -> dict:
        now = self._now()
        self._cleanup(now)
        if not self._lease or batch_id != self._lease["batch_id"]:
            return {"status": "error", "reason": "stale_batch", "selected": None}
        if selected_id is not None and (not isinstance(selected_id, str) or selected_id not in self._lease["ids"]):
            self._finish_batch("error", "invalid_selected_id", now)
            return {"status": "error", "reason": "invalid_selected_id", "selected": None}
        lease, self._lease = self._lease, None
        chosen = None
        for candidate_id in lease["ids"]:
            candidate = self._candidates.pop(candidate_id)
            status = "selected" if candidate_id == selected_id else "not_selected"
            self._record(status, reason, candidate, now)
            if status == "selected":
                chosen = self._public(candidate, now)
                self._deliveries[candidate_id] = candidate
                while len(self._deliveries) > self.max_history:
                    self._deliveries.popitem(last=False)
        return {"status": "selected" if chosen else "not_selected", "reason": _reason(reason), "selected": chosen}

    def failed(self, batch_id, reason) -> dict:
        now = self._now()
        self._cleanup(now)
        if not self._lease or batch_id != self._lease["batch_id"]:
            return {"status": "error", "reason": "stale_batch", "selected": None}
        self._finish_batch("error", reason, now)
        return {"status": "error", "reason": _reason(reason), "selected": None}

    def release_batch(self, batch_id):
        """本轮选了其他类别时归还评论；未被选择不等于内容不适合。"""
        now = self._now()
        self._cleanup(now)
        if not self._lease or self._lease["batch_id"] != batch_id:
            return False
        for ident in self._lease["ids"]:
            if ident in self._candidates:
                self._candidates[ident]["status"] = "candidate"
        self._lease = None
        return True

    def resolve_many(self, batch_id, selected_ids, reason):
        """一轮可回答同一人的补充或相似问题，仍消费一次固定候选清单。"""
        now = self._now()
        self._cleanup(now)
        if not self._lease or self._lease["batch_id"] != batch_id:
            return []
        if (not isinstance(selected_ids, list) or len(selected_ids) > 3
                or len(set(selected_ids)) != len(selected_ids)
                or any(ident not in self._lease["ids"] for ident in selected_ids)):
            raise ValueError("invalid_selected_ids")
        lease, self._lease = self._lease, None
        chosen = {}
        for ident in lease["ids"]:
            item = self._candidates.pop(ident, None)
            if item is None:
                continue
            selected = ident in selected_ids
            self._record("selected" if selected else "not_selected", reason, item, now)
            if selected:
                chosen[ident] = self._public(item, now)
                self._deliveries[ident] = item
        while len(self._deliveries) > self.max_history:
            self._deliveries.popitem(last=False)
        return [chosen[ident] for ident in selected_ids if ident in chosen]

    def clear(self, reason) -> dict:
        now = self._now()
        self._cleanup(now)
        count = len(self._candidates)
        for candidate in self._candidates.values():
            self._record("cleared", reason, candidate, now)
        self._candidates.clear()
        self._lease = None
        self._deliveries.clear()
        self.version += 1
        return {"status": "cleared", "reason": _reason(reason), "count": count, "version": self.version}

    def mark_delivery(self, id, status, reason) -> dict:
        now = self._now()
        self._cleanup(now)
        if not isinstance(id, str) or status not in ("enqueued", "not_enqueued"):
            return {"status": "error", "reason": "invalid_delivery", "id": None}
        candidate = self._deliveries.pop(id, None)
        if candidate is None:
            return {"status": "error", "reason": "unknown_delivery", "id": id}
        self._record(status, reason, candidate, now)
        return {"status": status, "reason": _reason(reason), "id": id}

    def snapshot(self) -> dict:
        now = self._now()
        self._cleanup(now)
        return {"version": self.version, "counts": dict(self._counts),
                "candidate_count": sum(item["status"] == "candidate" for item in self._candidates.values()),
                "selecting_count": sum(item["status"] == "selecting" for item in self._candidates.values()),
                "dedup_count": len(self._dedup), "history_count": len(self._history),
                "active_batch": bool(self._lease),
                "candidates": [self._public(item, now) | {"status": item["status"]}
                               for item in self._candidates.values()],
                "history": [dict(item) for item in list(self._history)[-30:]]}
