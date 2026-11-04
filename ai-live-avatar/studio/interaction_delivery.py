"""Trusted interaction jobs use the existing validated audio/motion sender.

Viewer names are speech data only. Every gesture comes from the local rule;
the existing second-stage planner may choose its speech anchor, not its motion.
"""
from __future__ import annotations

import copy
import re

from action_schema import parse_director_plan
from semantic_speech import parse_dialogue_semantic_plan


def is_motion_item(item):
    return ((item.get("input_kind") == "operator" and item.get("operator_mode") == "motion")
            or (item.get("input_kind") == "interaction" and item.get("interaction_mode") == "motion")
            or (item.get("input_kind") == "external_game" and item.get("external_mode") == "motion"))


def delivery_priority(item):
    # 原主持 > 原弹幕 > 主讲的相对顺序不变，只插入两种新互动。
    if item.get("input_kind") == "operator":
        return 4 if item.get("operator_mode") == "topic" else 0
    if item.get("unified_turn"):
        return 2
    return {"gift": 1, "welcome": 3}.get(item.get("interaction_kind"), 2)


def interaction_plan(job):
    motions = copy.deepcopy(job.get("motions", []))
    recovery = {"gaze": "自然看向镜头", "expression": "神情自然温和"}
    common = {"emotion": "温和"}
    if job.get("mode") == "motion":
        return {**common, "actions": [{"kind": "S", "delay_seconds": 0,
                "hold_seconds": 0, "motions": motions, "gaze": "看向镜头",
                "expression": "微笑致意"}], "recovery": recovery}
    text = job["text"]
    # 这只是第一阶段草案；真正发送前仍由原有词时间核验和动作定位检查。
    quote = re.search(r"[^\W_]+", text, re.UNICODE)
    if motions and quote is None:
        raise ValueError("带动作的感谢词需要至少一个可定位的文字短语")
    actions = [{"anchor": {"quote": quote.group()[:4], "occurrence": 1},
                "motions": motions, "gaze": "看向镜头", "expression": "微笑致意"}] if motions else []
    return {**common, "reading": None, "segments": [{"text": text,
            "instruction": "保持当前角色音色，自然温和地致谢或欢迎，清楚说完这一句。",
            "actions": actions, "recovery": recovery if actions else None}]}


def validate_interaction_plan(job):
    plan = interaction_plan(job)
    if job.get("mode") == "motion":
        return parse_director_plan(plan, mode="motion")
    return parse_dialogue_semantic_plan(plan, mode="once")


def compose_turn_plan(jobs, reply=None):
    """本地绑定已选批次的欢迎／感谢前导，模型不能遗漏它后仍记为已覆盖。

    前导最多一段，合并评论回复最多一段；只有普通单评论沿用原读评论行为。
    动作只来自已冻结礼物配置，其他合并礼物的动作取舍由队列明确记录。
    """
    if not jobs:
        if reply is None:
            raise ValueError("empty_turn")
        return copy.deepcopy(reply)
    if reply is None and len(jobs) == 1:
        return interaction_plan(jobs[0])
    # 欢迎在前、感谢在后，随后接主要评论；分批名单已在队列冻结。
    ordered = sorted(jobs, key=lambda j: j["kind"] != "welcome")
    text = "".join(j["text"] for j in ordered if j.get("mode") != "motion")
    if len(text) > 120:
        raise ValueError("interaction_prelude_too_long")
    motions = next((copy.deepcopy(j["motions"]) for j in jobs if j.get("motions")), [])
    if not text:
        raise ValueError("motion_gift_requires_separate_turn")
    prefix = interaction_plan({"mode": "speech", "text": text, "motions": motions})
    if reply is not None:
        if reply.get("reading") is not None or len(reply["segments"]) != 1:
            raise ValueError("mixed_turn_requires_one_reply_segment")
        prefix["emotion"] = reply["emotion"]
        prefix["segments"].extend(copy.deepcopy(reply["segments"]))
    return prefix
