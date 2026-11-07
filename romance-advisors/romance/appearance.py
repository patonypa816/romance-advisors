"""Appearance score accounting and durable episode-scoped gift receipts."""
from __future__ import annotations

MAX_SAFE_INTEGER = 2**53 - 1
MAX_RECEIPTS = 10000
ONE_DIAMOND_GIFT_DIRECTIONS = {"463": 1, "164": -1}

DEFAULT_BAND_STARTS = {"tired": 20, "normal": 40, "refined": 60, "stellar": 80}
APPEARANCE_BANDS = ((0, 0, "fish"), (1, 19, "rough"), (20, 39, "tired"),
                    (40, 59, "normal"), (60, 79, "refined"), (80, 100, "stellar"))
APPEARANCE_KEYS = ("fish", "rough", "tired", "normal", "refined", "stellar")
_DESCRIPTIONS = {
    "fish": "头像里，同一个人戴着遮住整张脸的银灰色鱼头，戴表的手托着鱼鳃。",
    "rough": "头像里，同一个人刘海湿塌，颈间挂着耳机，显得狼狈。",
    "tired": "头像里，同一个人头发翘起，穿着橄榄绿帽衫，显得疲惫。",
    "normal": "头像里，他留着日常刘海，穿着灰色卫衣，是咖啡馆里的普通打扮。",
    "refined": "头像里，同一个人把头发梳理整齐，露眉侧分，浅蓝衬衫叠穿白T，看起来清醒利落。",
    "stellar": "头像里，同一个人黑发蓬松，穿着黑色针织衫，在咖啡馆托腮，显得格外俊朗。",
}


def validate_band_starts(band_starts=None) -> dict:
    """Normalize optional overrides while keeping 50 normal and every tier nonempty."""
    if band_starts is None:
        return dict(DEFAULT_BAND_STARTS)
    if (not isinstance(band_starts, dict)
            or any(key not in DEFAULT_BAND_STARTS for key in band_starts)
            or any(type(value) is not int for value in band_starts.values())):
        raise ValueError("invalid_appearance_band_starts")
    starts = {**DEFAULT_BAND_STARTS, **band_starts}
    tired, normal, refined, stellar = (starts[key] for key in DEFAULT_BAND_STARTS)
    # Fish is exactly zero, rough starts at one; adjacent starts must differ.
    if not (1 < tired < normal <= 50 < refined < stellar <= 100):
        raise ValueError("invalid_appearance_band_starts")
    return starts


def appearance_bands(band_starts=None) -> tuple:
    starts = validate_band_starts(band_starts)
    ordered = (0, 1, *(starts[key] for key in DEFAULT_BAND_STARTS), 101)
    return tuple((ordered[index], ordered[index + 1] - 1, key)
                 for index, key in enumerate(APPEARANCE_KEYS))


def appearance_key(score: int, band_starts=None) -> str:
    if type(score) is not int or not 0 <= score <= 100:
        raise ValueError("invalid_appearance_score")
    for low, high, key in appearance_bands(band_starts):
        if low <= score <= high:
            return key
    raise ValueError("invalid_appearance_score")


score_tier = appearance_key

def appearance_snapshot(key: str, *, simulated=True) -> dict:
    """Return public, immutable-by-copy visible facts, with no score or gift IDs."""
    if key not in APPEARANCE_KEYS or type(simulated) is not bool:
        raise ValueError("invalid_appearance_key")
    return {"key": key, "avatar_key": ("simulated-" if simulated else "") + key,
            "description": _DESCRIPTIONS[key], "simulated": simulated,
            "portrait_url": f"/static/assets/appearance/{key}.jpg"}


def _identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and bool(value.strip())


def live_gift_readiness(config):
    """One server policy for enabling, starting and displaying real gift intake.

    The exact catalog pair allows a one-diamond live test without claiming that
    the collector's quantity semantics have already been observed on stream.
    """
    room = config.get("room_id")
    directions = config.get("gift_directions")
    valid = (isinstance(room, str) and 1 <= len(room) <= 32
             and room.isascii() and room.isdigit()
             and isinstance(directions, dict) and bool(directions)
             and all(_identifier(key) and type(sign) is int and sign in {-1, 1}
                     for key, sign in directions.items()))
    verified = config.get("quantity_verified") is True
    one_diamond = valid and directions == ONE_DIAMOND_GIFT_DIRECTIONS
    can_enable = bool(valid and (verified or one_diamond))
    mode = ("disabled" if config.get("enabled") is not True else
            "verified" if valid and verified else
            "one_diamond_test" if one_diamond else "unverified")
    return {"live_gift_mode": mode, "can_enable_live_gifts": can_enable}


class AppearanceLedger:
    def __init__(self, gift_directions: dict[str, int], *, room_id="offline-room",
                 episode_id="offline-episode", score=50, band_starts=None):
        if (not isinstance(gift_directions, dict) or not gift_directions
                or any(not _identifier(key) or type(value) is not int or value not in {-1, 1}
                       for key, value in gift_directions.items())
                or not _identifier(room_id) or not _identifier(episode_id)
                or type(score) is not int or not 0 <= score <= 100):
            raise ValueError("invalid_simulated_appearance_settings")
        self.band_starts = validate_band_starts(band_starts)
        self.gift_directions = dict(gift_directions)
        self.room_id, self.episode_id = room_id, episode_id
        self.score = score
        self.sequence = 0
        self.seen = set()
        self.groups = {}

    @property
    def target(self):
        return appearance_key(self.score, self.band_starts)

    def apply(self, event) -> dict:
        """Consume one trusted synthetic cumulative repeatCount, once.

        The explicit simulated marker is discarded by the real DyHub client.
        comboCount/groupCount never act as multipliers. Group progress consumes
        overflow too, so later end packets cannot restore discarded diamonds.
        """
        before = self.score

        def result(accepted, reason, **changes):
            return {"accepted": accepted, "reason": reason, "sequence": self.sequence,
                    "score_before": before, "score": self.score, "delta": self.score - before,
                    "overflow": 0, "diamonds": 0, "target": self.target, **changes}

        if not isinstance(event, dict) or event.get("simulated") is not True:
            return result(False, "simulation_required")
        data, user = event.get("data"), event.get("user")
        if (event.get("type") != "gift" or event.get("platform") != "douyin"
                or event.get("roomId") != self.room_id
                or event.get("episodeId", self.episode_id) != self.episode_id
                or not isinstance(data, dict) or not isinstance(user, dict)
                or not _identifier(event.get("id")) or not _identifier(user.get("id"))):
            return result(False, "invalid_context")
        gift_id, group_id = data.get("giftId"), data.get("groupId")
        price, count = data.get("diamondCount"), data.get("repeatCount")
        if (not _identifier(gift_id) or gift_id not in self.gift_directions
                or not _identifier(group_id) or group_id.strip() == "0"
                or type(price) is not int or not 0 < price <= MAX_SAFE_INTEGER
                or type(count) is not int or not 0 < count <= MAX_SAFE_INTEGER
                or price * count > MAX_SAFE_INTEGER):
            return result(False, "unreliable_gift")
        message = (self.room_id, event["id"])
        if message in self.seen:
            return result(False, "duplicate_message")
        group = ("douyin", self.room_id, user["id"], gift_id, group_id)
        previous = self.groups.get(group)
        if previous and previous["price"] != price:
            return result(False, "group_price_conflict")
        if len(self.seen) >= MAX_RECEIPTS or (previous is None and len(self.groups) >= MAX_RECEIPTS):
            return result(False, "receipt_limit")
        self.seen.add(message)
        total = price * count
        consumed = previous["diamonds"] if previous else 0
        diamonds = max(0, total - consumed)
        self.groups[group] = {"price": price, "diamonds": max(consumed, total)}
        if not diamonds:
            return result(False, "duplicate_progress")
        self.sequence += 1
        self.score = min(100, max(0, before + self.gift_directions[gift_id] * diamonds))
        return result(True, "accepted", diamonds=diamonds,
                      overflow=diamonds - abs(self.score - before))


class PersistentAppearanceService:
    """One game-owned ledger; one SQLite commit saves score and receipts together.

    Quantity verification is an operator assertion about the actual collector's
    cumulative repeatCount semantics. The exact one-diamond pair also permits
    an unverified live test. Missing prices/counters never default to 1.
    """
    def __init__(self, game, *, clock=None):
        import time
        self.game = game
        self.clock = clock or time.time

    def _state(self, *, writable=False):
        state = self.game.data.get("appearance") or {
            "config": None, "episode": None, "receipt_count": 0}
        return self._stage(state) if writable else state

    @staticmethod
    def _stage(state):
        import copy
        # Reads are constant-time. A write copies only containers and the tiny
        # mutable episode, rather than deep-copying every historical receipt.
        return {**state, "episode": copy.deepcopy(state["episode"]),
                "config": copy.deepcopy(state["config"]),
                "runtime": copy.deepcopy(state.get("runtime", {}))}

    def _commit(self, state, *, receipt=None, group=None):
        # Score/meta, immutable receipt, and consumed group high-water mark are
        # one transaction on the existing SQLite connection. Publish only after
        # COMMIT; game comment writes continue to carry only small meta fields.
        storage = self.game.storage
        meta = {k: v for k, v in self.game.data.items() if k not in storage.TABLES}
        meta["appearance"] = state
        storage.save_changes({}, meta=meta, appearance_receipt=receipt, appearance_group=group)
        self.game.data["appearance"] = state

    def settings(self):
        import copy
        config = copy.deepcopy(self._state()["config"]) or {
            "enabled": False, "room_id": "", "gift_directions": {},
            "quantity_verified": False, "version": 0}
        # Older saved settings have the original default boundaries implicitly.
        config["band_starts"] = validate_band_starts(config.get("band_starts"))
        return config

    get_config = settings

    def configure(self, config):
        if not isinstance(config, dict) or type(config.get("enabled", True)) is not bool:
            raise ValueError("invalid_appearance_settings")
        enabled = config.get("enabled", True)
        if not enabled:
            config = dict(config, room_id=config.get("room_id") or "unconfigured",
                          gift_directions=config.get("gift_directions") or {})
        if (not isinstance(config, dict) or not _identifier(config.get("room_id"))
                or not isinstance(config.get("gift_directions"), dict)
                or (enabled and not config["gift_directions"])
                or any(not _identifier(k) or type(v) is not int or v not in {-1, 1}
                       for k, v in config["gift_directions"].items())
                or type(config.get("quantity_verified", False)) is not bool):
            raise ValueError("invalid_appearance_settings")
        band_starts = validate_band_starts(config.get("band_starts"))
        state = self._state(writable=True)
        if state["episode"] and state["episode"].get("active"):
            raise ValueError("appearance_episode_active")
        state["config"] = {"enabled": enabled, "room_id": config["room_id"],
                           "gift_directions": dict(config["gift_directions"]),
                           "quantity_verified": config.get("quantity_verified", False),
                           "band_starts": band_starts,
                           "version": (state.get("config") or {}).get("version", 0) + 1}
        self._commit(state)
        return self.snapshot()

    def begin_episode(self, episode_id, *, simulated=False, owner_id=None):
        import copy
        state = self._state(writable=True)
        config = copy.deepcopy(state["config"])
        if config is not None:
            config["band_starts"] = validate_band_starts(config.get("band_starts"))
        episode = self.game.episode
        if (not config or not config.get("enabled", True) or type(simulated) is not bool or not _identifier(episode_id)
                or (owner_id is not None and not _identifier(owner_id))
                or not episode or episode["id"] != episode_id or episode["status"] != "ACTIVE"):
            raise ValueError("invalid_appearance_episode")
        if not simulated and live_gift_readiness(config)["live_gift_mode"] not in {"verified", "one_diamond_test"}:
            raise ValueError("appearance_quantity_unverified")
        current = state["episode"]
        if current and current["id"] == episode_id:
            if current["simulated"] != simulated or current.get("owner_id") != owner_id:
                raise ValueError("appearance_source_conflict")
            return self.snapshot()  # Refresh/start retries do not reset a ledger.
        state["episode"] = {"id": episode_id, "active": True, "simulated": simulated, "owner_id": owner_id,
                            "config": copy.deepcopy(config), "started_at": self.clock(),
                            "score": 50, "target": "normal", "sequence": 0, "receipt_count": 0}
        self._commit(state)
        return self.snapshot()

    def close_episode(self):
        state = self._state(writable=True)
        if state["episode"] and state["episode"].get("active"):
            state["episode"]["active"] = False
            self._commit(state)
        return self.snapshot()

    def save_state(self, **changes):
        """Persist coordinator state separately from trusted gift accounting."""
        state = self._state(writable=True)
        state.setdefault("runtime", {}).update(changes)
        self._commit(state)
        return self.snapshot()

    def snapshot(self):
        import copy
        state = self._state()
        episode = state["episode"] or {}
        return {"configured": bool(state["config"]), "config": self.settings() if state["config"] else None,
                "episode_id": episode.get("id"), "active": episode.get("active", False),
                "simulated": episode.get("simulated", False),
                "owner_id": episode.get("owner_id"),
                "operator_test": copy.deepcopy(episode.get("operator_test")),
                "score": episode.get("score", 50), "key": episode.get("target", "normal"),
                "description": _DESCRIPTIONS[episode.get("target", "normal")], "target": episode.get("target", "normal"),
                "sequence": episode.get("sequence", 0), "runtime": copy.deepcopy(state.get("runtime", {})),
                "receipt_count": state.get("receipt_count", 0)}

    def apply_operator_test(self, run_id, episode_id):
        """Credit the single fixed on-air test separately from paid gift receipts.

        The durable receipt and score commit together before any Studio work is
        scheduled. A lost HTTP response can be retried without credit or send.
        """
        import copy
        import uuid
        state = self._state()
        ep = state["episode"] or {}
        current = self.game.episode or {}
        if (not _identifier(run_id) or ep.get("owner_id") != run_id or ep.get("id") != episode_id
                or not ep.get("active") or ep.get("simulated") is not False
                or current.get("id") != episode_id or current.get("status") != "ACTIVE"):
            raise ValueError("operator_test_context_invalid")
        if ep.get("operator_test"):
            return dict(copy.deepcopy(ep["operator_test"]), accepted=False, reason="operator_test_already_applied")
        state = self._stage(state)
        ep = state["episode"]
        before = ep["score"]
        ep["score"] = min(100, before + 20)
        ep["target"] = appearance_key(ep["score"], ep["config"].get("band_starts"))
        ep["sequence"] += 1
        receipt = {"id": "operator_test_" + uuid.uuid4().hex, "source": "operator_test",
                   "run_id": run_id, "episode_id": episode_id, "at": self.clock(),
                   "accepted": True, "reason": "credited", "requested_delta": 20,
                   "score_before": before, "score": ep["score"], "delta": ep["score"] - before,
                   "target": ep["target"], "sequence": ep["sequence"]}
        ep["operator_test"] = receipt
        self._commit(state)
        return copy.deepcopy(receipt)

    def apply(self, event):
        import hashlib
        import json
        import math
        state = self._state()
        ep = state["episode"] or {}
        before = ep.get("score", 50)

        def result(accepted, reason, **changes):
            return {"accepted": accepted, "reason": reason, "score_before": before,
                    "score": ep.get("score", before), "delta": ep.get("score", before) - before,
                    "sequence": ep.get("sequence", 0), "target": ep.get("target", "normal"),
                    "diamonds": 0, "overflow": 0, **changes}

        current = self.game.episode
        if (not ep.get("active") or not current or current["status"] != "ACTIVE"
                or current["id"] != ep.get("id")):
            return result(False, "appearance_inactive")
        if not isinstance(event, dict):
            return result(False, "invalid_context")
        is_simulated = event.get("simulated") is True
        if is_simulated != ep["simulated"]:
            return result(False, "appearance_source_conflict")
        config = ep["config"]
        data, user = event.get("data"), event.get("user")
        if (event.get("type") != "gift" or event.get("platform") != "douyin"
                or event.get("roomId") != config["room_id"]
                or event.get("episodeId", ep["id"]) != ep["id"]
                or not isinstance(data, dict) or not isinstance(user, dict)
                or not _identifier(event.get("id")) or not _identifier(user.get("id"))):
            return result(False, "invalid_context")
        now = self.clock()
        times = [event.get(k) for k in ("ts", "receivedAt")]
        if (any(type(t) not in (int, float) or not math.isfinite(t) or t <= 0
                or t > (now + 5) * 1000 for t in times)
                or min(times) / 1000 < max(now - 45, ep["started_at"])
                or times[0] > times[1] + 5000
                or (not is_simulated and data.get("sourceTimeKnown") is not True)):
            return result(False, "stale_gift")
        gift_id, group_id = data.get("giftId"), data.get("groupId")
        price, count = data.get("diamondCount"), data.get("repeatCount")
        # Explicitly non-combinable single gifts can use the stable message ID.
        # Missing combo metadata and groupId="0" never take this fallback.
        if (group_id is None and data.get("combo") is False and count == 1
                and type(count) is int and data.get("groupCount") in (None, 1)
                and data.get("repeatEnd") is not False):
            group_id = "single:" + event["id"]
        if not isinstance(gift_id, str) or gift_id not in config["gift_directions"]:
            return result(False, "unconfigured_gift")
        if (not is_simulated and live_gift_readiness(config)["live_gift_mode"] == "one_diamond_test"
                and (type(price) is not int or price != 1)):
            # Never reinterpret missing, inflated, or boolean prices as one.
            # A verified catalog price is a constraint, not a fallback value.
            return result(False, "one_diamond_required")
        if (not _identifier(group_id) or group_id.strip() == "0"
                or type(price) is not int or not 0 < price <= MAX_SAFE_INTEGER
                or type(count) is not int or not 0 < count <= MAX_SAFE_INTEGER
                or price * count > MAX_SAFE_INTEGER):
            return result(False, "unreliable_gift")
        source = "simulated" if is_simulated else "dyhub"

        def digest(parts):
            return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()

        message = digest([source, config["room_id"], event["id"]])
        group = digest([source, "douyin", config["room_id"], user["id"], gift_id, group_id])
        if self.game.storage.appearance_receipt(message):
            return result(False, "duplicate_message")
        previous = self.game.storage.appearance_group(group)
        if previous and previous["episode_id"] != ep["id"]:
            return result(False, "old_episode_group")
        if previous and previous["price"] != price:
            return result(False, "group_price_conflict")
        state = self._stage(state)
        ep = state["episode"]
        total = price * count
        consumed = previous["diamonds"] if previous else 0
        diamonds = max(0, total - consumed)
        ep["receipt_count"] = ep.get("receipt_count", 0) + 1
        group_record = {"id": group, "episode_id": ep["id"], "price": price,
                                  "diamonds": max(consumed, total),
                                  "direction": config["gift_directions"][gift_id],
                                  "config_version": config["version"]}
        if diamonds:
            ep["sequence"] += 1
            ep["score"] = min(100, max(0, before + config["gift_directions"][gift_id] * diamonds))
            ep["target"] = appearance_key(ep["score"], config.get("band_starts"))
        receipt = result(bool(diamonds), "accepted" if diamonds else "duplicate_progress",
                         diamonds=diamonds, overflow=diamonds - abs(ep["score"] - before))
        state["receipt_count"] = state.get("receipt_count", 0) + 1
        self._commit(state, receipt=dict(receipt, id=message, group=group, episode_id=ep["id"]),
                     group=group_record)
        return receipt
