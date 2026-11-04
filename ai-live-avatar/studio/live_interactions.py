"""离线礼物/进场规则与有界队列；不调用模型，不拥有任何直播连接。"""
from __future__ import annotations

from collections import Counter, OrderedDict
import copy
import hashlib
import math
import secrets
import string
import time
import uuid

from action_schema import PARTS


MAX_PENDING = 120
MAX_SEEN = 4096
MAX_GROUPS = 2048
MAX_COOLDOWNS = 2048
# 本地接收策略，不表示平台已承诺这些重放/连击时限。
MESSAGE_REPLAY_SECONDS = 120
GROUP_REPLAY_SECONDS = 600
MAX_SPEECH_LENGTH = 120
GIFT_TTL_SECONDS = 120
WELCOME_TTL_SECONDS = 60
WELCOME_BATCH_SIZE = 3
PREPARATION_TTL_SECONDS = 120
MAX_PREPARATION_ATTEMPTS = 2
MAX_GIFT_SELECTION = 3
MAX_CANDIDATE_GIFTS = 12
MAX_WELCOME_SNAPSHOTS = 16
MAX_SETTLED_RESERVATIONS = 128
PARTIAL_WELCOME_COOLDOWN_SECONDS = 15
NON_RETRYABLE_SELECTION_REASONS = {"selection_skipped", "selection_failed", "snapshot_expired"}
NO_GROUP_MIN_COOLDOWN_SECONDS = 15
MAX_SAFE_INTEGER = 2**53 - 1


def _rule(name, action, part="right_arm", aliases=()):
    return {"enabled": True, "id": "", "name": name, "aliases": list(aliases),
            "part": part, "action": action, "mode": "speech",
            "template": "谢谢{nickname}送来的{gift}。", "cooldown_seconds": 15}


DEFAULT_INTERACTIONS = {
    "gift_enabled": True, "welcome_enabled": True, "gift_fallback_enabled": True,
    # 没有核实过的礼物 ID 不写入配置。空 ID 规则只做精确名称/别名匹配。
    "gift_rules": [
        _rule("小心心", "轻轻点头致谢", "head"),
        _rule("玫瑰", "右手轻放胸前致谢", aliases=("鲜花",)),
        _rule("棒棒糖", "右手轻轻竖起大拇指致谢"),
        _rule("大啤酒", "右手轻抬空手向镜头致意"),
        _rule("加油鸭", "右手轻握拳做一次加油手势"),
        _rule("比心", "右手在胸前小幅做一个指尖比心"),
        _rule("热气球", "右手向上轻轻挥手致意", aliases=("飞机",)),
        _rule("嘉年华", "双手适度展开致谢", "both_arms", ("烟花",)),
    ],
    "gift_fallback": _rule("其他礼物", "轻轻点头致谢", "head"),
    "welcome_template": "欢迎{nickname}来到直播间。",
    # 0：同 UID 本场仅一次；正数：经过该间隔后可再次欢迎。
    "welcome_cooldown_seconds": 0,
}


def _identity(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    if isinstance(value, int) and value.bit_length() > 850:
        return None
    value = str(value).strip()
    return value if 0 < len(value) <= 256 else None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer(value):
    return value if type(value) is int and 0 <= value <= MAX_SAFE_INTEGER else None


def _text(value, limit, label, *, empty=False):
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是文字")
    value = " ".join(value.split())
    if len(value) > limit or (not empty and not value):
        raise ValueError(f"{label}长度不符合要求")
    return value


def _template(value, label):
    # 占位符源码不等于实际台词；这里只限制配置体积，120 字合同在展开后检查。
    value = _text(value, 4096, label)
    try:
        fields = list(string.Formatter().parse(value))
    except ValueError:
        raise ValueError(f"{label}占位符不合法") from None
    if any(field is not None and (field not in {"nickname", "gift"} or spec or conversion)
           for _, field, spec, conversion in fields):
        raise ValueError(f"{label}只允许{{nickname}}和{{gift}}占位符")
    return value


def _clean_rule(value):
    required = {"enabled", "id", "name", "aliases", "part", "action", "mode", "template", "cooldown_seconds"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("礼物规则字段不完整或包含未知字段")
    if type(value["enabled"]) is not bool:
        raise ValueError("规则enabled必须为布尔值")
    if not isinstance(value["part"], str) or value["part"] not in PARTS - {"whole_body"}:
        raise ValueError("规则part不在允许的身体部位中")
    if value["mode"] not in ("speech", "motion"):
        raise ValueError("规则mode只能为speech或motion")
    aliases = value["aliases"]
    if not isinstance(aliases, list) or len(aliases) > 12:
        raise ValueError("规则aliases须为最多12项数组")
    aliases = [_text(item, 64, "礼物别名") for item in aliases]
    cooldown = _number(value["cooldown_seconds"])
    if cooldown is None or not 0 <= cooldown <= 3600:
        raise ValueError("规则冷却须为0至3600秒")
    action = _text(value["action"], 70, "礼物动作", empty=True)
    if value["mode"] == "motion" and not action:
        raise ValueError("仅动作模式必须配置动作")
    return {"enabled": value["enabled"], "id": _text(value["id"], 256, "礼物ID", empty=True),
            "name": _text(value["name"], 64, "礼物名称"), "aliases": list(dict.fromkeys(aliases)),
            "part": value["part"], "action": action, "mode": value["mode"],
            # 仅动作模式不使用感谢词，允许保留空值或尚未编辑好的草稿。
            "template": (_template(value["template"], "礼物感谢模板")
                         if value["mode"] == "speech" else copy.deepcopy(value["template"])),
            "cooldown_seconds": cooldown}


def validate_settings(body):
    """接受完整设置或顶层局部设置，返回独立副本；配置错误明确拒绝。"""
    if not isinstance(body, dict) or set(body) - set(DEFAULT_INTERACTIONS):
        raise ValueError("互动设置包含未知字段或格式不正确")
    clean = copy.deepcopy(DEFAULT_INTERACTIONS)
    clean.update(copy.deepcopy(body))
    for key in ("gift_enabled", "welcome_enabled", "gift_fallback_enabled"):
        if type(clean[key]) is not bool:
            raise ValueError(f"{key}必须为布尔值")
    if not isinstance(clean["gift_rules"], list) or len(clean["gift_rules"]) > 40:
        raise ValueError("礼物规则最多40条")
    clean["gift_rules"] = [_clean_rule(rule) for rule in clean["gift_rules"]]
    clean["gift_fallback"] = _clean_rule(clean["gift_fallback"])
    ids, names = set(), set()
    for rule in clean["gift_rules"]:
        if rule["id"]:
            if rule["id"] in ids:
                raise ValueError("礼物ID不能重复")
            ids.add(rule["id"])
        else:
            keys = {rule["name"], *rule["aliases"]}
            if names & keys:
                raise ValueError("按名称匹配的礼物规则不能重名")
            names.update(keys)
    clean["welcome_template"] = _template(clean["welcome_template"], "欢迎模板")
    cooldown = _number(clean["welcome_cooldown_seconds"])
    if cooldown is None or not 0 <= cooldown <= 86400:
        raise ValueError("欢迎冷却须为0至86400秒")
    clean["welcome_cooldown_seconds"] = cooldown
    return clean


class InteractionQueue:
    def __init__(self, settings=None, *, clock=time.time, viewer_key=None):
        self.settings = validate_settings({} if settings is None else settings)
        self._clock = clock
        self._salt = secrets.token_bytes(16)
        self._pending = OrderedDict()
        self._seen = OrderedDict()
        self._groups, self._welcome_seen, self._cooldowns = {}, {}, {}
        self._counts = Counter()
        self._last_reason = ""
        self._saturated = set()
        self._viewer_key = viewer_key
        self._welcome_snapshots = OrderedDict()
        self._reservations = {}
        self._settled = OrderedDict()
        self._occupied = set()
        self._welcome_recent = {}
        self._representative_offset = 0

    def _key(self, *parts):
        digest = hashlib.sha256(self._salt)
        for part in parts:
            encoded = str(part).encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
        return digest.hexdigest()

    def _result(self, status, reason, **fields):
        self._counts[status] += 1
        self._last_reason = reason
        return {"status": status, "reason": reason, **fields}

    def _cleanup(self, now):
        for reservation_id, record in list(self._reservations.items()):
            if not record.get("started") and record["job"]["expires_at"] <= now:
                # 不知道超时前是否发过信号时，保守终止，不自动重复付费生成或重播。
                self._settle(reservation_id, success=False, sent_any=True,
                             reason="preparation_timeout", now=now)
        for key, job in list(self._pending.items()):
            if job["expires_at"] <= now:
                del self._pending[key]
                self._counts["expired"] += 1
        for key, until in list(self._cooldowns.items()):
            if until <= now:
                del self._cooldowns[key]
        for key, until in list(self._seen.items()):
            if until <= now:
                del self._seen[key]
        reserved_ids = {item["id"] for record in self._reservations.values() for item in record["items"]}
        for key, group in list(self._groups.items()):
            # 待处理组始终保留；重连/暂停清待办后仍保留有限时间的去重记录。
            if group["job_id"] not in self._pending and group["job_id"] not in reserved_ids and group["expires_at"] <= now:
                del self._groups[key]
        for key, until in list(self._welcome_recent.items()):
            if until <= now:
                del self._welcome_recent[key]
        for token, snapshot in list(self._welcome_snapshots.items()):
            if any(key not in self._pending for key in snapshot["ids"]):
                del self._welcome_snapshots[token]
        cooldown = self.settings["welcome_cooldown_seconds"]
        if cooldown:
            for key, previous in list(self._welcome_seen.items()):
                if previous + cooldown <= now:
                    del self._welcome_seen[key]
        # saturated 只表示当前正在施加的容量限制，不是永久错误开关。
        for label, mapping, limit in (("groups", self._groups, MAX_GROUPS),
                                      ("cooldowns", self._cooldowns, MAX_COOLDOWNS)):
            if len(mapping) < limit:
                self._saturated.discard(label)

    def configure(self, settings, *, preserve_reservation_ids=()):
        clean = validate_settings(settings)
        self.settings = clean
        # 关闭规则时不能继续发送旧待办；已见过的消息仍保留，防止重放。
        self.clear("settings_changed", preserve_reservation_ids=preserve_reservation_ids)
        return copy.deepcopy(clean)

    def clear(self, reason, reset_seen=False, *, preserve_reservation_ids=()):
        # 仅调用方确认已开始发送的轮次可以保留；预备下一轮照常取消。
        # 换场/停场永远使旧结果失效，白名单不能越过这个边界。
        allowed = preserve_reservation_ids if isinstance(preserve_reservation_ids, (list, tuple, set, frozenset)) else ()
        preserved = {key for key in allowed if isinstance(key, str)}
        if reset_seen or reason in ("new_session", "session_stop", "service_closed"):
            preserved.clear()
        cancelled = {key for key in self._reservations if key not in preserved}
        count = len(self._pending) + sum(len(self._reservations[key]["items"]) for key in cancelled)
        self._pending.clear()
        for reservation_id in cancelled:
            record = self._reservations.pop(reservation_id)
            self._record_settled(reservation_id, {"status": "cancelled", "reason": "cleared", "job": record["job"]})
        self._occupied = {item["meta"]["viewer_key"] for record in self._reservations.values()
                          for item in record["items"] if item["kind"] == "welcome"}
        self._welcome_snapshots.clear()
        if reset_seen:
            self._seen.clear()
            self._groups.clear()
            self._welcome_seen.clear()
            self._cooldowns.clear()
            self._saturated.clear()
            self._welcome_recent.clear()
        self._counts["cleared"] += count
        self._last_reason = reason if isinstance(reason, str) and len(reason) <= 80 else "cleared"
        return {"status": "cleared", "reason": self._last_reason, "count": count,
                "preserved_reservations": len(self._reservations)}

    def _capacity(self, mapping, limit, label):
        if len(mapping) < limit:
            return True
        # 连击窗口内不强行淘汰，避免迟到进度再次触发；到期后自动恢复。
        self._saturated.add(label)
        return False

    def _remember_message(self, message_key, now):
        # 消息缓存是最近 N 条/有限时间的重放保护；整场欢迎身份另行保存。
        while len(self._seen) >= MAX_SEEN:
            self._seen.popitem(last=False)
        self._seen[message_key] = now + MESSAGE_REPLAY_SECONDS

    def _match(self, gift_id, name):
        for rule in self.settings["gift_rules"]:
            if rule["id"] and rule["id"] == gift_id:
                return rule
        for rule in self.settings["gift_rules"]:
            if not rule["id"] and name in {rule["name"], *rule["aliases"]}:
                return rule
        fallback = self.settings["gift_fallback"]
        return fallback if self.settings["gift_fallback_enabled"] and fallback["enabled"] else None

    def ingest(self, event, now=None):
        now = _number(self._clock() if now is None else now)
        if now is None or now <= 0:
            return self._result("ignored", "invalid_now")
        self._cleanup(now)
        if not isinstance(event, dict) or event.get("platform") != "douyin" or event.get("type") not in ("gift", "member"):
            return self._result("ignored", "unsupported_event")
        kind = "gift" if event["type"] == "gift" else "welcome"
        if not self.settings[kind + "_enabled"]:
            return self._result("ignored", "disabled")
        user, data = event.get("user"), event.get("data")
        if not isinstance(user, dict) or not isinstance(data, dict):
            return self._result("ignored", "invalid_event")
        message, room, uid = _identity(event.get("id")), _identity(event.get("roomId")), _identity(user.get("id"))
        if message is None or room is None or uid is None:
            return self._result("ignored", "invalid_identity")
        timestamps = [_number(event.get(key)) for key in ("ts", "receivedAt")]
        if any(value is None or value <= 0 for value in timestamps):
            return self._result("ignored", "invalid_timestamp")
        event_at, received_at = [value / 1000 for value in timestamps]
        if max(event_at, received_at) > now + 5 or event_at > received_at + 5:
            return self._result("ignored", "future_timestamp")
        ttl = GIFT_TTL_SECONDS if kind == "gift" else WELCOME_TTL_SECONDS
        expires_at = min(now, event_at, received_at) + ttl
        if expires_at <= now:
            return self._result("expired", "event_expired")
        message_key = self._key(room, kind, message)
        viewer_key = self._viewer_key(room, uid) if self._viewer_key else self._key(room, uid)
        if message_key in self._seen:
            return self._result("duplicate", "duplicate_message")
        nickname = user.get("nickname")
        nickname = " ".join(nickname.split()) if isinstance(nickname, str) else "观众"
        nickname = nickname or "观众"
        common = {"kind": kind, "id": uuid.uuid4().hex, "nickname": nickname,
                  "expires_at": expires_at, "created_at": now,
                  "meta": {"event_key": message_key, "viewer_key": viewer_key, "room_key": self._key(room)}}
        if kind == "welcome":
            return self._welcome(common, user, data, message_key, viewer_key, now)
        return self._gift(common, data, message_key, viewer_key, now)

    def _welcome(self, job, user, data, message_key, viewer_key, now):
        level = _integer(user.get("payGradeLevel"))
        if type(data.get("action")) is not int or data["action"] != 1 or data.get("isSetToAdmin") is True:
            return self._result("ignored", "not_confirmed_entry")
        if "isSetToAdmin" in data and type(data["isSetToAdmin"]) is not bool:
            return self._result("ignored", "invalid_admin_flag")
        if level is None or level <= 10:
            return self._result("ignored", "level_not_above_10")
        self._remember_message(message_key, now)
        cooldown = self.settings["welcome_cooldown_seconds"]
        previous = self._welcome_seen.get(viewer_key)
        if previous is not None and (cooldown == 0 or now < previous + cooldown):
            return self._result("duplicate", "welcome_already_seen")
        if viewer_key in self._occupied:
            return self._result("duplicate", "welcome_reserved")
        if self._welcome_recent.get(viewer_key, 0) > now:
            return self._result("duplicate", "welcome_recently_sent")
        if any(item["kind"] == "welcome" and item["meta"]["viewer_key"] == viewer_key for item in self._pending.values()):
            return self._result("duplicate", "welcome_pending")
        if len(self._pending) >= MAX_PENDING:
            return self._result("ignored", "queue_full")
        text = self.settings["welcome_template"].format(nickname=job["nickname"], gift="")
        if len(text) > MAX_SPEECH_LENGTH:
            # 昵称不是资格条件；不能因为一个长名字丢掉整位合格入场者。
            text = "欢迎刚才进来的朋友，进来坐坐。"
        job.update(text=text,
                   template=self.settings["welcome_template"], mode="speech", rule=None, motions=[])
        job["meta"]["pay_grade_level"] = level
        self._pending[job["id"]] = job
        return self._result("queued", "welcome_queued", id=job["id"], kind="welcome")

    def _gift(self, job, data, message_key, viewer_key, now):
        gift_id = _identity(data.get("giftId"))
        gift_id = gift_id if gift_id != "0" else None
        raw_name = data.get("giftName")
        name = " ".join(raw_name.split()) if isinstance(raw_name, str) else ""
        if not gift_id and not name:
            return self._result("ignored", "invalid_gift")
        rule = self._match(gift_id, name)
        if rule is None:
            return self._result("ignored", "unmatched_gift")
        if not rule["enabled"]:
            return self._result("ignored", "gift_rule_disabled")
        self._remember_message(message_key, now)
        group_id = _identity(data.get("groupId"))
        group_id = group_id if group_id != "0" else None
        group_key = self._key(job["meta"]["room_key"], viewer_key, gift_id, group_id) if gift_id and group_id else None
        counts = {key: value for key in ("repeatCount", "comboCount", "groupCount")
                  if (value := _integer(data.get(key))) is not None}
        if group_key in self._groups:
            group = self._groups[group_key]
            group["expires_at"] = now + GROUP_REPLAY_SECONDS
            for key, value in counts.items():
                group["counts"][key] = max(group["counts"].get(key, 0), value)
            queued = self._pending.get(group["job_id"])
            if queued is not None:
                queued["meta"]["counts"] = dict(group["counts"])
                queued["meta"]["quantity"] = group["counts"].get("repeatCount")
                if data.get("repeatEnd") is True:
                    queued["meta"]["repeat_end"] = True
            return self._result("merged" if queued else "duplicate", "gift_group_seen", id=group["job_id"])
        if group_key:
            if not self._capacity(self._groups, MAX_GROUPS, "groups"):
                return self._result("ignored", "gift_group_capacity")
            # 即使本次因冷却/队满而略过，也记住该组，重放不能重新触发。
            self._groups[group_key] = {"job_id": job["id"], "counts": dict(counts),
                                       "expires_at": now + GROUP_REPLAY_SECONDS}
        cooldown_key = self._key(viewer_key, gift_id or name)
        if self._cooldowns.get(cooldown_key, 0) > now:
            return self._result("ignored", "gift_cooldown", quantity_verified=False)
        if len(self._pending) >= MAX_PENDING:
            return self._result("ignored", "queue_full")
        gift = name or rule["name"]
        text = rule["template"].format(nickname=job["nickname"], gift=gift) if rule["mode"] == "speech" else ""
        if len(text) > MAX_SPEECH_LENGTH:
            return self._result("ignored", "speech_text_too_long", text_length=len(text), limit=MAX_SPEECH_LENGTH)
        duration = max(rule["cooldown_seconds"], NO_GROUP_MIN_COOLDOWN_SECONDS if not group_key else 0)
        if duration:
            if cooldown_key not in self._cooldowns and not self._capacity(self._cooldowns, MAX_COOLDOWNS, "cooldowns"):
                return self._result("ignored", "dedupe_capacity")
            self._cooldowns[cooldown_key] = now + duration
        job.update(text=text,
                   mode=rule["mode"], rule=copy.deepcopy(rule),
                   motions=[{"part": rule["part"], "description": rule["action"]}] if rule["action"] else [])
        job["meta"].update(gift_id=gift_id, gift_name=name, group_key=group_key,
                           counts=counts, quantity=counts.get("repeatCount"),
                           quantity_verified=False, group_verified=bool(group_key),
                           quantity_note="unverified_source_counts" if group_key else "missing_group_unverified",
                           repeat_end=data.get("repeatEnd") if type(data.get("repeatEnd")) is bool else None)
        self._pending[job["id"]] = job
        return self._result("queued", "gift_queued", id=job["id"], kind="gift",
                            quantity_verified=job["meta"]["quantity_verified"])

    def has_pending(self, kind=None):
        self._cleanup(self._clock())
        return any(kind is None or item["kind"] == kind for item in self._pending.values())

    def _welcome_summary(self, ids):
        items = [self._pending[key] for key in ids]
        offset = self._representative_offset % len(items)
        representatives = []
        # 完整成员留在本地；只选能自然读出、不挤爆台词的少量代表名。
        for item in items[offset:] + items[:offset]:
            name = item["nickname"]
            if 0 < len(name) <= 24 and name != "观众" and not any(ord(char) < 32 for char in name):
                representatives.append(item)
            if len(representatives) >= WELCOME_BATCH_SIZE:
                break
        names = [item["nickname"] for item in representatives]
        collective = len(names) < len(items)
        if collective:
            prefix = "、".join(names)
            text = (f"欢迎{prefix}，还有刚才进来的各位朋友，进来坐坐。" if prefix
                    else "欢迎刚才进来的各位朋友，进来坐坐。")
        else:
            text = items[0]["template"].format(nickname="、".join(names), gift="")
            if len(text) > MAX_SPEECH_LENGTH:
                text = "欢迎" + "、".join(names) + "，进来坐坐。"
        return {"count": len(items), "names": names, "text": text, "collective": collective,
                "representative_viewer_keys": [item["meta"]["viewer_key"] for item in representatives]}

    def snapshot_candidates(self, now=None):
        """有界摘要；欢迎 token 绑定快照原成员，不随新入场扩大。"""
        now = _number(self._clock() if now is None else now)
        if now is None:
            return {"gift": [], "welcome": None}
        self._cleanup(now)
        gifts = []
        if self.settings["gift_enabled"]:
            for item in self._pending.values():
                if item["kind"] != "gift":
                    continue
                gifts.append({"id": item["id"], "nickname": item["nickname"][:32],
                              "viewer_key": item["meta"]["viewer_key"], "text": item["text"],
                              "mode": item["mode"], "motions": copy.deepcopy(item["motions"]),
                              "gift_name": item["meta"]["gift_name"][:80],
                              "group_verified": item["meta"]["group_verified"],
                              "repeat_end": item["meta"]["repeat_end"]})
                if len(gifts) >= MAX_CANDIDATE_GIFTS:
                    break
        ids = tuple(key for key, item in self._pending.items()
                    if item["kind"] == "welcome" and self.settings["welcome_enabled"])
        welcome = None
        if ids:
            for token, snapshot in self._welcome_snapshots.items():
                if snapshot["ids"] == ids:
                    welcome = {"id": token, **snapshot["summary"]}
                    break
            if welcome is None:
                token = uuid.uuid4().hex
                summary = self._welcome_summary(ids)
                self._welcome_snapshots[token] = {"ids": ids, "summary": summary}
                while len(self._welcome_snapshots) > MAX_WELCOME_SNAPSHOTS:
                    self._welcome_snapshots.popitem(last=False)
                welcome = {"id": token, **summary}
        return copy.deepcopy({"gift": gifts, "welcome": welcome})

    def _reserve(self, items, now, summary=None, welcome_id=None):
        job = copy.deepcopy(items[0])
        reservation_id = uuid.uuid4().hex
        for item in items:
            self._pending.pop(item["id"])
            item["meta"]["preparation_attempts"] = item["meta"].get("preparation_attempts", 0) + 1
        job.update(id=reservation_id, reservation_id=reservation_id,
                   expires_at=now + PREPARATION_TTL_SECONDS)
        job["meta"]["sources"] = copy.deepcopy(items)
        job["meta"]["source_ids"] = [item["id"] for item in items]
        if job["kind"] == "welcome":
            summary = copy.deepcopy(summary)
            job.update(text=summary["text"], nickname="、".join(summary["names"]))
            job["meta"].update(members=[{"id": item["id"], "nickname": item["nickname"], **item["meta"]}
                                         for item in items],
                               batch_size=len(items), welcome_id=welcome_id,
                               collective=summary["collective"], names=summary["names"],
                               representative_viewer_keys=summary["representative_viewer_keys"])
            self._occupied.update(item["meta"]["viewer_key"] for item in items)
            self._representative_offset += max(1, len(summary["names"]))
        elif len(items) > 1:
            spoken = [item for item in items if item["mode"] == "speech"]
            text = "".join(item["text"] for item in spoken)
            if spoken and (len(text) > MAX_SPEECH_LENGTH or len(spoken) != len(items)):
                names = list(dict.fromkeys(item["nickname"] for item in items
                                           if len(item["nickname"]) <= 24))[:3]
                text = ("谢谢" + "、".join(names) + "，也谢谢各位朋友送来的礼物，感谢大家的支持。" if names
                        else "谢谢各位朋友送来的礼物，感谢大家的支持。")
            # 合并轮只选一个原配置动作，其他组明确记作合并取舍。
            chosen = next((item for item in items if item["motions"]), None)
            job.update(text=text, mode="speech" if spoken else "motion",
                       motions=copy.deepcopy(chosen["motions"]) if chosen else [])
            job["meta"].update(gift_ids=[item["id"] for item in items],
                               selected_motion_source_id=chosen["id"] if chosen else None,
                               merged_motion_source_ids=[item["id"] for item in items
                                                         if item["motions"] and item is not chosen])
        self._reservations[reservation_id] = {"job": copy.deepcopy(job), "items": items, "started": False}
        self._counts["reserved"] += len(items)
        self._counts["popped"] += len(items)
        return job

    def reserve_selection(self, *, gift_ids=None, welcome_id=None, now=None):
        """原子校验并冻结选择；任一选择失效时整次选择拒绝。"""
        now = _number(self._clock() if now is None else now)
        if now is None:
            return []
        self._cleanup(now)
        gift_ids = [] if gift_ids is None else gift_ids
        if (not isinstance(gift_ids, (list, tuple)) or len(gift_ids) > MAX_GIFT_SELECTION
                or any(not isinstance(key, str) for key in gift_ids)
                or len(set(gift_ids)) != len(gift_ids)):
            return []
        gifts = [self._pending.get(key) for key in gift_ids]
        if gifts and (not self.settings["gift_enabled"] or any(not item or item["kind"] != "gift" for item in gifts)):
            return []
        snapshot = None
        if welcome_id is not None:
            if not isinstance(welcome_id, str) or not self.settings["welcome_enabled"]:
                return []
            snapshot = self._welcome_snapshots.get(welcome_id)
            if not snapshot or any(key not in self._pending for key in snapshot["ids"]):
                return []
        jobs = []
        if gifts:
            jobs.append(self._reserve(gifts, now))
        if snapshot:
            items = [self._pending[key] for key in snapshot["ids"]]
            jobs.append(self._reserve(items, now, snapshot["summary"], welcome_id))
        return jobs

    @staticmethod
    def _reservation_id(job_or_id):
        value = job_or_id.get("reservation_id", job_or_id.get("id")) if isinstance(job_or_id, dict) else job_or_id
        return value if isinstance(value, str) else None

    def reservation_active(self, job_or_id):
        self._cleanup(self._clock())
        return self._reservation_id(job_or_id) in self._reservations

    def mark_started(self, job_or_id):
        """首个信号发送前提交；准备期限只约束开始发送，不能截断已开始的整轮。"""
        self._cleanup(self._clock())
        record = self._reservations.get(self._reservation_id(job_or_id))
        if record is None:
            return False
        record["started"] = True
        return True

    def _record_settled(self, reservation_id, result):
        self._settled[reservation_id] = result
        while len(self._settled) > MAX_SETTLED_RESERVATIONS:
            self._settled.popitem(last=False)

    def _settle(self, reservation_id, *, success, sent_any, reason, now):
        record = self._reservations.pop(reservation_id, None)
        if record is None:
            return copy.deepcopy(self._settled.get(reservation_id, {"status": "stale", "reason": "unknown_reservation"}))
        released = 0
        for item in record["items"]:
            if item["kind"] == "welcome":
                key = item["meta"]["viewer_key"]
                self._occupied.discard(key)
                if success:
                    self._welcome_seen[key] = now
                elif sent_any:
                    self._welcome_recent[key] = now + PARTIAL_WELCOME_COOLDOWN_SECONDS
            if (not success and not sent_any and reason not in NON_RETRYABLE_SELECTION_REASONS
                    and item["expires_at"] > now
                    and item["meta"]["preparation_attempts"] < MAX_PREPARATION_ATTEMPTS
                    and len(self._pending) < MAX_PENDING):
                self._pending[item["id"]] = item
                released += 1
        status = "completed" if success else "partial" if sent_any else "failed"
        self._counts[status] += len(record["items"])
        self._counts["released"] += released
        result = {"status": status, "reason": reason, "released": released,
                  "count": len(record["items"]), "sent_any": bool(sent_any), "success": bool(success),
                  "reservation_id": reservation_id}
        # 完整冻结绑定保留在本地供诊断，不进入下一轮模型摘要。
        self._record_settled(reservation_id, {**result, "job": record["job"]})
        return result

    def finish(self, job_or_id, *, success, sent_any, reason=""):
        """整轮必要信号发完后调用；同一 reservation 重复结算不改变记录。"""
        now = self._clock()
        self._cleanup(now)
        reservation_id = self._reservation_id(job_or_id)
        if reservation_id is None:
            return {"status": "stale", "reason": "unknown_reservation"}
        reason = reason if isinstance(reason, str) and len(reason) <= 80 else "finished"
        result = self._settle(reservation_id, success=bool(success and sent_any),
                              sent_any=bool(sent_any), reason=reason, now=now)
        result.pop("job", None)
        return result

    def pop(self, kind, now=None):
        """兼容入口：取出只是预留，finish 成功才记为完成欢迎。"""
        if not isinstance(kind, str) or kind not in ("gift", "welcome"):
            return None
        candidates = self.snapshot_candidates(now=now)
        if kind == "gift":
            ids = [candidates["gift"][0]["id"]] if candidates["gift"] else []
            jobs = self.reserve_selection(gift_ids=ids, now=now)
        else:
            token = candidates["welcome"]["id"] if candidates["welcome"] else None
            jobs = self.reserve_selection(welcome_id=token, now=now) if token else []
        return jobs[0] if jobs else None

    def public(self):
        self._cleanup(self._clock())
        gifts = sum(item["kind"] == "gift" for item in self._pending.values())
        return {"settings": copy.deepcopy(self.settings), "pending_count": len(self._pending),
                "gift_pending": gifts, "welcome_pending": len(self._pending) - gifts,
                "counts": dict(self._counts), "last_reason": self._last_reason,
                "seen_count": len(self._seen), "group_count": len(self._groups),
                "welcome_seen_count": len(self._welcome_seen), "cooldown_count": len(self._cooldowns),
                "reserved_count": len(self._reservations), "welcome_occupied_count": len(self._occupied),
                "settled_count": len(self._settled),
                "saturated": sorted(self._saturated)}
