"""Operator-only real gift quantity check; memory only, never a game receipt."""
from __future__ import annotations

import math
import time

from .appearance import MAX_RECEIPTS, MAX_SAFE_INTEGER


class GiftQuantityProbe:
    GIFTS = {"463": ("小心心", 1), "164": ("抖音", -1)}

    def __init__(self, *, clock=time.time):
        self.clock = clock
        self.active = False
        self.room_id = ""
        self.started_at = None
        self.score = 50
        self.seen = set()
        self.groups = {}
        self.rows = self.empty_rows()
        self.reason = None

    @classmethod
    def eligible(cls, config, room_id):
        # This check is deliberately limited to the two catalog-verified one-
        # diamond gifts. Other configurations keep their normal live pathway.
        return (config.get("enabled") is False and config.get("quantity_verified") is False
                and bool(room_id) and config.get("room_id") == room_id
                and config.get("gift_directions") == {key: sign for key, (_, sign) in cls.GIFTS.items()})

    def begin(self, room_id, *, started_at=None):
        self.active = True
        self.room_id = room_id
        self.started_at = self.clock() if started_at is None else started_at
        self.score = 50
        self.seen.clear()
        self.groups.clear()
        self.reason = None
        self.rows = self.empty_rows()

    @classmethod
    def empty_rows(cls):
        return {key: {"gift_id": key, "name": name, "direction": sign,
                          "unit_diamonds": None, "cumulative_count": 0,
                          "accepted_count": 0, "last_increment": 0}
                     for key, (name, sign) in cls.GIFTS.items()}

    def stop(self):
        self.active = False

    def reset_sample(self):
        # Reset the displayed sample, not the collector connection. A new
        # packet can still carry the previous combo's cumulative repeatCount.
        self.started_at = self.clock()
        self.score = 50
        self.seen.clear()
        self.reason = None
        self.rows = self.empty_rows()

    def snapshot(self):
        # No viewer, source message or combo identifier leaves this object.
        return {"active": self.active, "started_at": self.started_at,
                "state": "receiving" if self.active and any(r["accepted_count"] for r in self.rows.values()) else "waiting" if self.active else "stopped",
                "score": self.score, "accepted_count": sum(row["accepted_count"] for row in self.rows.values()),
                "last_reason": self.reason,
                "gifts": [dict(row) for row in self.rows.values()]}

    @staticmethod
    def identifier(value):
        return isinstance(value, str) and 0 < len(value) <= 256 and bool(value.strip())

    def apply(self, event):
        def reject(reason):
            self.reason = reason
            return {"accepted": False, "reason": reason, "score": self.score, "delta": 0}

        if not self.active:
            return reject("probe_inactive")
        if (not isinstance(event, dict) or event.get("simulated") is True
                or event.get("type") != "gift" or event.get("platform") != "douyin"
                or event.get("roomId") != self.room_id):
            return reject("invalid_context")
        data, user = event.get("data"), event.get("user")
        if not isinstance(data, dict) or not isinstance(user, dict):
            return reject("unreliable_gift")
        now = self.clock()
        times = [event.get(key) for key in ("ts", "receivedAt")]
        if (data.get("sourceTimeKnown") is not True
                or any(type(value) not in (int, float) or not math.isfinite(value)
                       or not 0 < value <= (now + 5) * 1000 for value in times)
                or min(times) / 1000 < max(now - 45, self.started_at)
                or times[0] > times[1] + 5000):
            return reject("stale_gift")
        gift_id, group_id = data.get("giftId"), data.get("groupId")
        count, price = data.get("repeatCount"), data.get("diamondCount")
        if not isinstance(gift_id, str) or gift_id not in self.GIFTS:
            return reject("unconfigured_gift")
        if type(price) is not int or price != 1:
            return reject("one_diamond_required")
        if (not all(self.identifier(value) for value in (event.get("id"), user.get("id"), group_id))
                or group_id.strip() == "0" or type(count) is not int or not 0 < count <= MAX_SAFE_INTEGER):
            return reject("unreliable_gift")
        row = self.rows[gift_id]
        row["last_increment"] = 0
        if event["id"] in self.seen:
            return reject("duplicate_message")
        group = (user["id"], gift_id, group_id)
        if len(self.seen) >= MAX_RECEIPTS or (group not in self.groups and len(self.groups) >= MAX_RECEIPTS):
            return reject("receipt_limit")
        # repeatCount is cumulative within a combo. Its high-water mark consumes
        # end packets and out-of-order delivery, exactly as the game ledger does.
        previous = self.groups.get(group, 0)
        increment = max(0, count - previous)
        self.seen.add(event["id"])
        self.groups[group] = max(count, previous)
        row.update(unit_diamonds=1, cumulative_count=max(count, previous), last_increment=increment)
        if not increment:
            return reject("duplicate_progress")
        row["accepted_count"] += increment
        before = self.score
        self.score = min(100, max(0, before + row["direction"] * increment))
        self.reason = "accepted"
        return {"accepted": True, "reason": "accepted", "score": self.score,
                "delta": self.score - before, "increment": increment}
