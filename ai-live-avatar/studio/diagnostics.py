"""Passive, bounded diagnostic view; never replay or interpret logged content."""
from collections import deque
import json
from pathlib import Path
import re
import time


_PRIVATE = re.compile(r"token|authorization|password|secret|api.?key|credential|image_uri|pcm|audio_data", re.I)


def scrub(value):
    """Defence in depth; protocol extraction below is the primary allowlist."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"redacted_bytes": len(value)}
    if isinstance(value, dict):
        return {str(k)[:80]: scrub(v) for k, v in value.items() if not _PRIVATE.search(str(k))}
    if isinstance(value, (tuple, list)):
        return [scrub(v) for v in value[:80]]
    if isinstance(value, str):
        value = re.sub(r"(?i)data:[^\s]+", "[data removed]", value)
        value = re.sub(r"(?i)(?:https?|wss?)://[^\s<>\"']+", "[URL removed]", value)
        value = re.sub(r"(?i)\b(?:Bearer|Token)\s+[^\s,;]+", "[credential removed]", value)
        value = re.sub(r"(?i)(?:api[_-]?key|token|secret|password|authorization)\s*[=:]\s*[^\s,;]+", "[credential removed]", value)
        value = re.sub(r"\b(?:vda_|sk-)[A-Za-z0-9_-]+", "[credential removed]", value)
        return value[:2000]
    return value if isinstance(value, (int, float, bool)) or value is None else type(value).__name__


def frame_fields(frame, size, clean=scrub):
    """Keep all frame types visible, while extracting only known safe values."""
    if not isinstance(frame, dict):
        return {"details": {"frame_bytes": size, "shape": type(frame).__name__}}
    result = {k: clean(frame[k]) for k in ("type", "seq_id") if isinstance(frame.get(k), (int, str))}
    details = {"frame_bytes": size, "fields": [clean(str(k)) for k in list(frame)[:40]]}
    for key in ("live_id", "conn_id"):
        if isinstance(frame.get(key), (str, int)):
            details[key] = clean(frame[key])
    payload = frame.get("payload")
    if isinstance(payload, dict):
        details["payload_fields"] = [clean(str(k)) for k in list(payload)[:40]]
        for section, allowed in {
            "text_msg": ("content", "msg_id", "id", "timestamp"),
            "prompt_operation_ack": ("success", "error_code", "error_msg"),
            "conn_init_ack": ("success", "error_code", "error_msg", "server_timestamp"),
            "hangup": ("hangup_reason", "reason"),
            "error": ("source", "code", "message"),
        }.items():
            part = payload.get(section)
            if isinstance(part, dict):
                safe = {k: clean(part[k]) for k in allowed if isinstance(part.get(k), (str, int, float, bool))}
                details[section] = safe
                if section == "text_msg":
                    if "content" in safe:
                        result["content"] = safe["content"]
                    if "msg_id" in safe or "id" in safe:
                        result["msg_id"] = safe.get("msg_id", safe.get("id"))
        # Some platform error variants put these fields directly in payload.
        for k in ("source", "code", "message", "hangup_reason", "reason"):
            if isinstance(payload.get(k), (str, int, float, bool)):
                details[k] = clean(payload[k])
    result["details"] = details
    return result


class Diagnostics:
    def __init__(self, out, limit=300):
        self.path = Path(out) / "diagnostics.jsonl"
        self.run_id = Path(out).name
        self.records = deque(maxlen=min(300, max(1, limit)))
        self.latest_id = 0
        self.zero = time.monotonic()
        self.secrets = set()
        self.closed = False
        self.disk_failed = False
        self.counts = dict(out_total=0, in_total=0, type9=0, type10=0, audio_bytes=0)

    def register_secret(self, value):
        if isinstance(value, str) and len(value) >= 8:
            self.secrets.add(value)

    def clean(self, value):
        # Replace full known secrets before truncation, including nested strings.
        if isinstance(value, dict):
            return {self.clean(str(k))[:80]: self.clean(v) for k, v in value.items()
                    if not _PRIVATE.search(str(k))}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value[:80]]
        if isinstance(value, str):
            for secret in sorted(self.secrets, key=len, reverse=True):
                value = value.replace(secret, "[credential removed]")
        return scrub(value)

    def record(self, direction, kind, **fields):
        self.latest_id += 1
        row = dict(id=self.latest_id, time=time.time(), elapsed_ms=round((time.monotonic()-self.zero)*1000),
                   direction=direction, kind=kind, **self.clean(fields))
        self.records.append(row)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as target:
                target.write(json.dumps(row, ensure_ascii=True)+"\n")
        except (OSError, UnicodeError):
            # Logging must never turn a successful socket send into a failed send.
            if not self.disk_failed:
                self.disk_failed = True
                self.latest_id += 1
                self.records.append(dict(id=self.latest_id, time=time.time(), elapsed_ms=row["elapsed_ms"],
                                         direction="local", kind="diagnostics_disk_write_failed",
                                         details={"message": "磁盘日志不完整，当前页面仍可回读最近记录"}))
        return row

    def frame(self, direction, status, frame, size, **context):
        values = frame_fields(frame, size, self.clean)
        values["details"].update(self.clean(context))
        self.record(direction, "ws_"+status, **values)
        if direction == "in" or status == "sent":
            self.counts["in_total" if direction == "in" else "out_total"] += 1
            if direction == "out" and frame.get("type") in (9, 10):
                self.counts["type"+str(frame["type"])] += 1

    def snapshot(self, after=0):
        return dict(run_id=self.run_id, latest_id=self.latest_id,
                    first_id=self.records[0]["id"] if self.records else self.latest_id+1,
                    records=[dict(r) for r in self.records if r["id"] > after],
                    counts=dict(self.counts), closed=self.closed)

    def export(self):
        rows = {}
        try:
            with self.path.open(encoding="utf-8") as source:
                for line in source:
                    try:
                        row = json.loads(line)
                        rows[row["id"]] = self.clean(row)
                    except (ValueError, KeyError, TypeError):
                        continue
        except (OSError, UnicodeError):
            pass
        rows.update({r["id"]: r for r in self.records})
        return {**self.snapshot(self.latest_id), "records": [rows[i] for i in sorted(rows)],
                "disk_complete": not self.disk_failed}
