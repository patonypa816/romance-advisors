"""Finite performance tasks on the existing Studio preparation/sender chain.

No media transport or business priority lives here. A task owns its internal
blocks; applications own when to submit, pause, resume or cancel that task.
"""
from __future__ import annotations

import copy
import contextlib
import hashlib
import json
import uuid

from semantic_speech import (MAX_TEXT, _normalized, _occurrences, _continuous_quote,
                             parse_game_semantic_plan, validate_dialogue_actions, _normalize_input_actions)
from action_schema import _validate_recovery

TERMINAL = {"completed", "cancelled", "failed", "unknown"}
DEFAULT_BLOCK_CHARS = 80  # A speech-chunk target, not a provider limit.
DEFAULT_GE = {"gaze": "注视镜头", "expression": "自然平静"}


def _expression_defaults(body):
    """Fill optional performance hints before the unchanged shared compiler.

    These are requested targets for the current front-facing avatar, not an
    observation of its pose. Explicit values still pass the original checks.
    """
    if not isinstance(body, dict):
        return body
    body = copy.deepcopy(body)
    body.setdefault("emotion", "自然")  # Compatibility metadata for old history.
    parts = body.get("segments", [body])
    if not isinstance(parts, list):
        return body
    for part in parts:
        if not isinstance(part, dict):
            continue
        actions = part.get("actions")
        if not isinstance(actions, list):
            continue
        for action in actions:
            if isinstance(action, dict):
                for key, value in DEFAULT_GE.items():
                    action.setdefault(key, value)
        if actions:
            if part.get("recovery") is None:
                part["recovery"] = dict(DEFAULT_GE)
            elif isinstance(part["recovery"], dict):
                for key, value in DEFAULT_GE.items():
                    part["recovery"].setdefault(key, value)
        else:
            part.setdefault("recovery", None)
    return body


def split_text(text, limit=DEFAULT_BLOCK_CHARS, protected=()):
    """Lossless natural boundaries, then bounded technical splits if necessary.

    Protected spans are action quotes in this exact source, before TTS. Never
    split a quote or move an action to a neighbouring occurrence.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("台词不能为空")
    if type(limit) is not int or not 20 <= limit <= MAX_TEXT:
        raise ValueError("block_chars需为20至120；这是本地单块配置")
    result, start = [], 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            candidates = range(start + 1, end + 1)
            valid = [p for p in candidates if not any(a < p < b for a, b in protected)
                     and text[start:p].strip()]
            if not valid:
                raise ValueError("动作锚点跨越单块容量；请缩短锚点或调整block_chars")
            # Prefer a complete sentence, then a clause/word. Tiny tails stay
            # with their sentence when there is room in the current block.
            floor = start + limit // 3
            ends = [p for p in valid if p >= floor and text[p - 1] in "。！？!?；;\n"]
            ends = ends or [p for p in valid if p >= floor and text[p - 1] in "，,、：: \t"]
            end = ends[-1] if ends else valid[-1]
        if not text[start:end].strip():
            # Whitespace has no standalone speech. Keep it with the last block
            # when the underlying parser's non-whitespace capacity allows it.
            if result and not text[start:].strip():
                a, _, previous = result[-1]
                result[-1] = (a, len(text), previous + text[start:])
                break
            raise ValueError("连续空白无法形成独立语音块；请调整稿件排版")
        result.append((start, end, text[start:end]))
        start = end
    assert "".join(part[2] for part in result) == text
    return result


def chunk_plan(body, limit=DEFAULT_BLOCK_CHARS):
    """Normalize a complete semantic performance into original short contracts."""
    body = _expression_defaults(body)
    if not isinstance(body, dict) or set(body) != {"emotion", "segments"}:
        raise ValueError("表演计划只接受emotion、segments")
    segments = body["segments"]
    if not isinstance(segments, list) or not 1 <= len(segments) <= 200:
        raise ValueError("需要有限且非空的segments")
    plans = []
    for segment in segments:
        if not isinstance(segment, dict) or set(segment) != {"text", "instruction", "actions", "recovery"}:
            raise ValueError("每段只接受text、instruction、actions、recovery")
        text = segment["text"]
        if not isinstance(text, str) or len(text) > 16000:
            raise ValueError("单项任务台词超过本地容量")
        actions = validate_dialogue_actions(_normalize_input_actions(segment["actions"]), text)
        _validate_recovery(segment["recovery"], actions)
        positions = [i for i, char in enumerate(text) if _normalized(char)]
        located = []
        for action in actions:
            anchor = action["anchor"]
            offset = _occurrences(_normalized(text), anchor["quote"])[anchor["occurrence"] - 1]
            if not _continuous_quote(text, offset, anchor["quote"]):
                raise ValueError("动作锚点不是连续原文")
            located.append((positions[offset], positions[offset + len(anchor["quote"]) - 1] + 1, action))
        if any(a > b for a, b in zip([x[0] for x in located], [x[0] for x in located][1:])):
            raise ValueError("跨块动作锚点需按台词顺序排列，不能静默改变动作顺序")
        for start, end, block in split_text(text, limit, [(a, b) for a, b, _ in located]):
            local = []
            for a, b, action in located:
                if start <= a and b <= end:
                    value = copy.deepcopy(action)
                    prefix = _normalized(text[start:a])
                    # Count matches by start position, including overlaps.
                    starts = _occurrences(_normalized(block), action["anchor"]["quote"])
                    value["anchor"]["occurrence"] = starts.index(len(prefix)) + 1
                    local.append(value)
            part = {"text": block, "instruction": segment["instruction"], "actions": local,
                    "recovery": segment["recovery"] if local else None}
            plan = parse_game_semantic_plan({"emotion": body["emotion"], "segments": [part]}, mode="once")
            # The original parser trims presentation whitespace. A fixed script
            # needs the exact original source, so restore it after validation.
            plan["segments"][0]["text"] = block
            plans.append(plan)
    if sum(len(p["segments"][0]["text"]) for p in plans) > 16000:
        raise ValueError("任务总台词超过16000字的本地容量")
    return plans


class PerformanceTasks:
    def __init__(self, studio):
        self.studio = studio
        self.tasks = {}

    def reset(self):
        self.tasks = {}

    def checkpoint(self):
        # Diagnostic persistence never gates playback, cancellation or stop.
        with contextlib.suppress(OSError, ValueError, TypeError):
            path = self.studio.current.out / "performance-tasks.json"
            path.write_text(json.dumps(self.public(), ensure_ascii=False, indent=2))

    def unfinished(self):
        return any(t["status"] not in TERMINAL for t in self.tasks.values())

    def first(self):
        return next((t for t in self.tasks.values() if t["status"] not in TERMINAL
                     and not t["pause_requested"] and not t["cancel_requested"]), None)

    def submit(self, body):
        if not isinstance(body, dict) or set(body) - {"request_id", "kind", "text", "instruction", "auto_actions", "context", "block_chars"}:
            raise ValueError("通用表演输入字段不正确")
        ident, kind, text = body.get("request_id"), body.get("kind"), body.get("text")
        if not isinstance(ident, str) or not 1 <= len(ident) <= 100:
            raise ValueError("request_id需为1至100字")
        if kind not in {"intent", "script", "motion"} or not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise ValueError("kind需为intent/script/motion，text需为非空且不超过16000字")
        auto = body.get("auto_actions", False)
        if type(auto) is not bool or (kind != "script" and "auto_actions" in body):
            raise ValueError("auto_actions仅用于固定稿")
        context = body.get("context", {})
        if not isinstance(context, dict) or len(json.dumps(context, ensure_ascii=False)) > 12000:
            raise ValueError("context需为有限的表达上下文对象")
        instruction = body.get("instruction", "自然清晰，正常语速，按语义停顿。")
        if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 160:
            raise ValueError("instruction需为1至160字")
        limit = body.get("block_chars", DEFAULT_BLOCK_CHARS)
        if type(limit) is not int or not 20 <= limit <= MAX_TEXT:
            raise ValueError("block_chars需为20至120")
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if ident in self.tasks:
            t = self.tasks[ident]
            if t["fingerprint"] != fingerprint:
                raise ValueError("同一request_id不能用于不同内容")
            return self.view(t)
        if len(self.tasks) >= 200:
            raise ValueError("本场任务记录已满；结束本场后新建，不自动丢弃去重记录")
        t = {"request_id": ident, "task_id": uuid.uuid4().hex, "kind": kind, "text": text,
             "instruction": instruction, "auto_actions": auto, "context": copy.deepcopy(context),
             "block_chars": limit, "fingerprint": fingerprint, "status": "accepted", "plans": None,
             "sent_blocks": 0, "sent_text": "", "item_id": None, "future_item_id": None,
             "pause_requested": False, "cancel_requested": False, "error": None}
        if kind == "script":
            t["plans"] = chunk_plan({"emotion": "自然", "segments": [{"text": text,
                "instruction": instruction, "actions": [], "recovery": None}]}, limit)
        self.tasks[ident] = t
        self.checkpoint()
        return self.view(t)

    def item(self, t):
        return next((c for c in self.studio.current.comments if c["id"] == t["item_id"]), None)

    def task_for(self, c):
        return self.tasks.get(c.get("performance_request_id"))

    def view(self, t):
        c = self.item(t)
        sending = bool(c and c.get("delivery_started") and c["status"] not in {"sent", "error", "cancelled"})
        partial = bool(t["sent_blocks"] or (c and (c.get("delivery_started") or c.get("motion_receipts"))))
        return {key: copy.deepcopy(t[key]) for key in
                ("request_id", "task_id", "kind", "status", "sent_blocks", "sent_text", "pause_requested", "cancel_requested", "error")} | {
            "total_blocks": len(t["plans"]) if t["plans"] is not None else None,
            "current_item_id": t["item_id"], "prepared_next_item_id": t["future_item_id"],
            "sending": sending, "partial_delivery": partial,
            "planned_text": ("".join(p.get("segments", [{}])[0].get("text", "") for p in t["plans"])
                             if t["plans"] is not None else None),
            "blocks": copy.deepcopy(t["plans"]),
            "progress_basis": "successful_send_and_handoff_not_confirmed_playback"}

    def public(self):
        return [self.view(t) for t in self.tasks.values()]

    def sync(self):
        for t in self.tasks.values():
            if t["status"] in TERMINAL:
                continue
            c = self.item(t)
            if c and c["status"] == "sent":
                t["sent_blocks"] += 1
                t["sent_text"] += c.get("answer", "")
                t["item_id"], t["future_item_id"] = t["future_item_id"], None
                c = self.item(t)
            if c and c["status"] in {"error", "cancelled", "expired"}:
                t["error"] = c.get("error") or c["status"]
                t["status"] = ("cancelled" if t["cancel_requested"] else
                               "unknown" if c.get("delivery_started") else "failed")
                # A conditional future block must not survive a predecessor's
                # failure, including one still returning from model/TTS.
                self.cancel_unsent(t)
                continue
            if t["cancel_requested"] and not c:
                t["status"] = "cancelled"
            elif t["plans"] is not None and t["sent_blocks"] == len(t["plans"]):
                t["status"] = "completed"
            elif t["pause_requested"] and not (c and c.get("delivery_started")):
                t["status"] = "paused"
            elif c:
                t["status"] = "playing" if c.get("delivery_started") else "preparing"
            else:
                t["status"] = "accepted"

    def cancel_unsent(self, t):
        for c in self.studio.current.comments:
            if c.get("performance_request_id") != t["request_id"] or c.get("delivery_started"):
                continue
            if c["status"] in {"sent", "error", "cancelled", "expired"}:
                continue
            c.update(cancel_requested=True, status="cancelled")
            self.studio.current.discard_prepared(c)
            worker = getattr(self.studio.automation, "reply_task", None)
            if worker and getattr(worker, "pipeline_item", None) is c:
                worker.cancel()

    def control(self, ident, action):
        self.sync()
        t = self.tasks.get(ident)
        if t is None:
            raise ValueError("未知request_id")
        if action not in {"pause", "resume", "cancel"}:
            raise ValueError("只接受pause/resume/cancel")
        if t["status"] in TERMINAL:
            return self.view(t)
        if action == "resume":
            t["pause_requested"] = False
        elif action == "pause":
            t["pause_requested"] = True
        else:
            t["cancel_requested"] = True
            self.cancel_unsent(t)
        self.sync()
        self.checkpoint()
        return self.view(t)

    def stop(self):
        for ident in list(self.tasks):
            self.control(ident, "cancel")

    def is_current(self, c):
        t = self.task_for(c)
        return bool(t and t["status"] not in TERMINAL and not t["cancel_requested"]
                    and c["id"] in (t["item_id"], t["future_item_id"]) and not c.get("cancel_requested"))

    def delivery_gate(self, c):
        self.sync()
        t = self.task_for(c)
        return bool(self.is_current(c) and t is self.first() and t["item_id"] == c["id"]
                    and c["performance_block"] == t["sent_blocks"] and not t["pause_requested"])

    def next_item(self):
        self.sync()
        t = self.first()
        if not t:
            return None
        c = self.item(t)
        future = False
        index = t["sent_blocks"]
        if c:
            if c["status"] == "pending":
                return c
            if not c.get("delivery_started") or t["plans"] is None or index + 1 >= len(t["plans"]):
                return None
            old_future = next((i for i in self.studio.current.comments if i["id"] == t["future_item_id"]), None)
            if old_future:
                return old_future if old_future["status"] == "pending" else None
            future, index = True, index + 1
        c = self.studio.enqueue_comment(t["text"], "表演任务", source="performance", input_kind="operator",
            operator_mode="motion" if t["kind"] == "motion" else "once",
            performance_request_id=t["request_id"], performance_block=index)
        t["future_item_id" if future else "item_id"] = c["id"]
        if not future:
            t["status"] = "preparing"
        return c

    async def prepare_plan(self, c, ai, brain_context):
        t = self.task_for(c)
        if not self.is_current(c):
            raise ValueError("任务已撤销")
        if t["plans"] is None:
            context = {key: copy.deepcopy(brain_context[key]) for key in ("character", "operator_settings") if key in brain_context}
            raw = await ai.generate_performance(t["kind"], t["text"], context, t["context"], t["instruction"])
            # Best-effort raw evidence; no diagnostics write can grant delivery.
            with contextlib.suppress(Exception):
                clean = self.studio.current.diagnostics.clean(raw)
                (self.studio.current.out / ("performance-raw-" + t["task_id"] + ".json")).write_text(
                    json.dumps(clean, ensure_ascii=False, indent=2))
            if not self.is_current(c):
                raise ValueError("任务已撤销")
            if t["kind"] == "motion":
                from action_schema import parse_game_motion_plan
                plan = parse_game_motion_plan(_expression_defaults(raw))
                plan.pop("timeline", None)
                t["plans"] = [plan]
            else:
                t["plans"] = chunk_plan(raw, t["block_chars"])
        plan = copy.deepcopy(t["plans"][c["performance_block"]])
        if t["kind"] == "script" and t["auto_actions"]:
            text = plan["segments"][0]["text"]
            raw = await ai.generate_performance("script", text, brain_context, t["context"], t["instruction"])
            if not self.is_current(c):
                raise ValueError("任务已撤销")
            fixed = chunk_plan(raw, t["block_chars"])
            if len(fixed) != 1 or fixed[0]["segments"][0]["text"] != text:
                raise ValueError("固定稿编排必须保留本段原文和分段")
            plan = fixed[0]
            t["plans"][c["performance_block"]] = copy.deepcopy(plan)
        self.checkpoint()
        return plan
