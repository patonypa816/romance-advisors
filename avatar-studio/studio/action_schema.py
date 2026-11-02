"""Terra 动作计划的本地合同；通过校验不代表 Vidu 已按语义执行。"""
from __future__ import annotations

import math

MAX_ACTION_CHARS = 200  # 本地试验限制，不宣称是 Vidu 官方字符上限。
ACTION_POST_SEND_SECONDS = 1.0  # S/L/Q成功发送后的Type9最短间隔；不限制台词或PCM。
QUIET_DELAY_SECONDS = 1.5
# Q描述目标姿态，避免把“收回/恢复”再解释成需要反复执行的新动作。
QUIET_INSTRUCTION = "正常坐姿"
READING_GAZE = "目光落在镜头下方附近"
READING_EXPRESSION = "眼睛轻微眯起，神情专注辨认"
READING_LEAD_SECONDS = 1.0  # 本轮试验提前量；不是平台固定渲染延迟。
ANSWER_START_SECONDS = 1.0
SHORT_PERFORMANCE_LEAD_SECONDS = 0.5
CONTINUOUS_SECOND_S_SECONDS = 3.0
CONTINUOUS_SECOND_GE_SECONDS = 2.5
CONTINUOUS_MIN_AUDIO_SECONDS = 5.0
LONG_SPEECH_MAX_TEXT = 120
LONG_SPEECH_MAX_GROUPS = 6
LONG_SPEECH_GROUP_INTERVAL = 3.0
LONG_SPEECH_TAIL_SECONDS = 2.0
PARTS = frozenset({"head", "face", "left_arm", "right_arm", "both_arms", "torso", "whole_body"})
NODE_FIELDS = frozenset({"kind", "delay_seconds", "motions", "hold_seconds", "gaze", "expression"})
# 主持可发较长指令；限制单次动作节点，避免一条操作长时间占住调度器。
# 这只是本地主持任务的工作量边界，不改变观众的既有动作合同。
MAX_DIRECTOR_ACTIONS = 6


def _text(value: object, limit: int, label: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError(f"{label}必须为1至{limit}字的非空文字")
    return value.strip()


def _number(value: object, label: str) -> float:
    # bool 是 Python 的 int 子类，但不是合法的时间值。
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}必须为有限数值")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{label}必须为有限数值") from None
    if not math.isfinite(number):
        raise ValueError(f"{label}必须为有限数值")
    return number


def _ge(value: object, label: str) -> str:
    """只校验文字结构；连续变化可交给Vidu，发送密度由时间轴和发送器控制。"""
    return _text(value, 40, label)


def _validate_recovery(recovery: object, actions: list[dict], *, required=False) -> dict | None:
    if not actions and not required:
        if recovery is not None:
            raise ValueError("空actions的recovery必须为null，不额外发送状态帧或Q")
        return None
    if not isinstance(recovery, dict) or set(recovery) != {"gaze", "expression"}:
        raise ValueError("有动作或阅读前导时recovery必须且只能包含gaze和expression")
    return {"gaze": _ge(recovery["gaze"], "恢复gaze"),
            "expression": _ge(recovery["expression"], "恢复expression")}


def _instruction(active: str, gaze: str, expression: str) -> str:
    # Type9仍是文字接口；三行标签用于区分职责，并非Vidu新增的结构化API。
    # 统一字段内空白，避免模型的换行破坏这三行；G/E实际进入发送内容。
    pieces = [" ".join(part.split()).rstrip("。；;，, ") for part in (active, gaze, expression)]
    instruction = "\n".join(f"{label}：{value}。" for label, value in zip(("动作", "眼神", "表情"), pieces))
    if len(instruction) > MAX_ACTION_CHARS:
        raise ValueError("编译后的Type9指令超过本地200字限制")
    return instruction


def _validate_actions(actions: object) -> list[dict]:
    if not isinstance(actions, list):
        raise ValueError("actions必须为数组；无需动作时使用空数组")
    clean = []
    for position, node in enumerate(actions):
        if not isinstance(node, dict) or set(node) != NODE_FIELDS:
            raise ValueError("动作节点必须且只能包含kind、delay_seconds、motions、hold_seconds、gaze、expression")
        kind = node["kind"]
        if kind not in ("S", "L"):
            raise ValueError("模型只能规划S短动作或L长动作；Q由服务端生成")
        delay = _number(node["delay_seconds"], "delay_seconds")
        if (position == 0 and delay != 0) or (position > 0 and not ACTION_POST_SEND_SECONDS <= delay <= 30):
            raise ValueError("首节点间隔必须为0；后续节点间隔必须至少1秒且不超过30秒")
        hold = _number(node["hold_seconds"], "hold_seconds")
        if (kind == "S" and hold != 0) or (kind == "L" and not QUIET_DELAY_SECONDS < hold <= 30):
            raise ValueError("S的hold_seconds必须为0；L的完整窗口估计必须大于1.5且不超过30秒")
        raw_motions = node["motions"]
        if not isinstance(raw_motions, list) or not 1 <= len(raw_motions) <= (2 if kind == "S" else 1):
            raise ValueError("S节点只允许1或2个同时动作；L节点只允许1个动作")
        motions, resources = [], set()
        for motion in raw_motions:
            if not isinstance(motion, dict) or set(motion) != {"part", "description"}:
                raise ValueError("每个motion必须且只能包含part和description")
            part = motion["part"]
            if not isinstance(part, str) or part not in PARTS:
                raise ValueError("motion.part不在允许的身体部位中")
            if kind == "S" and part == "whole_body":
                raise ValueError("whole_body必须使用独立L节点")
            if kind == "L" and part not in {"whole_body", "torso"}:
                raise ValueError("L只允许whole_body或torso")
            description = _text(motion["description"], 70, "动作描述")
            occupied = {"left_arm", "right_arm"} if part == "both_arms" else {part}
            if resources & occupied or (resources and (part == "whole_body" or "whole_body" in resources)):
                raise ValueError("同一节点的身体部位存在资源冲突")
            resources.update(occupied)
            motions.append({"part": part, "description": description})
        # L 是独立完整窗口；后继发送时间不得落在这个估计窗口内。
        if clean and clean[-1]["kind"] == "L" and delay < clean[-1]["hold_seconds"]:
            raise ValueError("L后的节点间隔不得小于前一L的hold_seconds")
        clean.append({"kind": kind, "delay_seconds": delay, "motions": motions, "hold_seconds": hold,
                      "gaze": _ge(node["gaze"], "gaze"), "expression": _ge(node["expression"], "expression")})
    return clean


def _compile_validated(actions: list[dict], recovery: dict | None) -> list[dict]:
    timeline, at_seconds = [], 0.0
    for index, node in enumerate(actions, 1):
        at_seconds += node["delay_seconds"]
        descriptions = [motion["description"] for motion in node["motions"]]
        active = descriptions[0] if len(descriptions) == 1 else f"同时{descriptions[0]}，并{descriptions[1]}"
        instruction = _instruction(active, node["gaze"], node["expression"])
        timeline.append({"role": node["kind"], "kind": node["kind"], "index": index,
                         "at_seconds": at_seconds, "delay_seconds": node["delay_seconds"],
                         "motions": [dict(motion) for motion in node["motions"]],
                         "instruction": instruction, "hold_seconds": node["hold_seconds"],
                         "gaze": node["gaze"], "expression": node["expression"]})
    if actions:
        # 只在整组尾部补一次Q；L窗口只是估计，不能据此断言画面已完成。
        last = actions[-1]
        delay = QUIET_DELAY_SECONDS if last["kind"] == "S" else last["hold_seconds"]
        timeline.append({"role": "Q", "kind": "Q", "index": len(actions) + 1,
                         "at_seconds": at_seconds + delay, "delay_seconds": delay,
                         "motions": [], "instruction": _instruction(QUIET_INSTRUCTION, recovery["gaze"], recovery["expression"]),
                         "hold_seconds": 0.0, "gaze": recovery["gaze"], "expression": recovery["expression"]})
    return timeline


def compile_timeline(actions: list[dict], recovery: dict | None = None) -> list[dict]:
    """累计时间只作计划展示；调度器须从前一Type9实际成功发送时重新计时。"""
    actions = _validate_actions(actions)
    return _compile_validated(actions, _validate_recovery(recovery, actions))


def compile_short_performance_timeline(actions, recovery):
    """仅显式试验使用；保留正常校验，绝不截断或改写超出范围的动作。"""
    actions = _validate_actions(actions)
    if len(actions) != 1 or actions[0]["kind"] != "S" or len(actions[0]["motions"]) != 1:
        raise ValueError("短表演试验必须恰好一个S节点，且只含一个motion")
    recovery = _validate_recovery(recovery, actions)
    timeline = _compile_validated(actions, recovery)
    node = actions[0]
    pieces = [" ".join(node[key].split()).rstrip("。；;，, ") for key in ("gaze", "expression")]
    # GE只是本地时间线角色，线上仍为Type9文字；前导不夹带身体动作。
    lead = {"role": "GE", "kind": "GE", "index": 1, "at_seconds": 0.0,
            "delay_seconds": 0.0, "motions": [], "hold_seconds": 0.0,
            "instruction": "\n".join(f"{label}：{value}。" for label, value in zip(("眼神", "表情"), pieces)),
            "gaze": node["gaze"], "expression": node["expression"]}
    timeline[0]["delay_seconds"] = SHORT_PERFORMANCE_LEAD_SECONDS
    for index, entry in enumerate(timeline, 2):
        entry["index"] = index
        entry["at_seconds"] += SHORT_PERFORMANCE_LEAD_SECONDS
    return [lead, *timeline]


def short_performance_input_mode(item):
    """同一试验入口合同供入队、接收计划和TTS前复核共用。"""
    kind, source = item.get("input_kind", "viewer"), item.get("source")
    if kind == "viewer" and source is None:
        return "speech"
    if kind == "operator" and source in (None, "operator") and item.get("operator_mode") in ("once", "motion"):
        return "motion" if item["operator_mode"] == "motion" else "speech"
    raise ValueError("短表演试验仅开放模拟弹幕、一次安排和只做动作；主线与真实评论来源暂不接受")


def compile_continuous_speech_timeline(actions, recovery):
    """两组沿用单S编译；累计时间仅为试验计划，实际发送仍以成功写入为准。"""
    actions = _validate_actions(actions)
    if len(actions) != 2 or any(node["kind"] != "S" or len(node["motions"]) != 1 for node in actions):
        raise ValueError("连续语音试验必须恰好两个S节点，且每个只含一个motion")
    if actions[1]["delay_seconds"] != CONTINUOUS_SECOND_S_SECONDS:
        raise ValueError("连续语音试验第二个S的delay_seconds必须为3.0秒")
    recovery = _validate_recovery(recovery, actions)
    timeline = []
    for group_index, action in enumerate(actions, 1):
        # 单S编译仍要求首delay=0；跨组的固定间隔只在外层时间线表达。
        group = compile_short_performance_timeline([{**action, "delay_seconds": 0.0}], recovery)
        offset = (group_index - 1) * CONTINUOUS_SECOND_S_SECONDS
        for node in group:
            node.update(index=len(timeline) + 1, group_index=group_index,
                        at_seconds=node["at_seconds"] + offset)
            node["delay_seconds"] = node["at_seconds"] - (timeline[-1]["at_seconds"] if timeline else 0.0)
            if group_index == 2 and node["role"] == "GE":
                # 此定位以首PCM成功写入为零点；不能据计划声称渲染或播放完成。
                node["audio_at_seconds"] = CONTINUOUS_SECOND_GE_SECONDS
            timeline.append(node)
    return timeline


def continuous_speech_input_mode(item):
    """只开放本地模拟弹幕和主持一次安排，其他入口明确拒绝。"""
    if not isinstance(item, dict):
        raise ValueError("连续语音试验仅开放模拟弹幕和一次安排；主线、纯动作与真实评论来源暂不接受")
    kind, source = item.get("input_kind", "viewer"), item.get("source")
    if kind == "viewer" and source is None:
        return "speech"
    if kind == "operator" and source in (None, "operator") and item.get("operator_mode") == "once":
        return "speech"
    raise ValueError("连续语音试验仅开放模拟弹幕和一次安排；主线、纯动作与真实评论来源暂不接受")


def validate_continuous_speech_plan(plan):
    """正常计划校验后的附加合同；必须在收费TTS之前执行。"""
    if plan.get("reading") is not None or len(plan.get("segments", [])) != 1:
        raise ValueError("连续语音试验要求reading=null且恰好一段台词")
    part = plan["segments"][0]
    # 字数只控制单次TTS输入；实际是否达到5秒必须检查返回的PCM。
    _text(part.get("text"), 30, "text")
    part["timeline"] = compile_continuous_speech_timeline(part["actions"], part["recovery"])
    return plan


def validate_long_speech_group_interval(group_interval):
    if type(group_interval) not in (int, float) or group_interval not in (3.0, 5.0):
        raise ValueError("长文语音试验组间隔只能为3.0或5.0秒")
    return float(group_interval)


def validate_long_speech_actions(actions, *, group_interval=LONG_SPEECH_GROUP_INTERVAL):
    group_interval = validate_long_speech_group_interval(group_interval)
    actions = _validate_actions(actions)
    if not 1 <= len(actions) <= LONG_SPEECH_MAX_GROUPS or any(
            node["kind"] != "S" or len(node["motions"]) != 1 for node in actions):
        raise ValueError("长文语音试验需要1至6个S节点，且每个只含一个motion")
    if any(node["delay_seconds"] != group_interval for node in actions[1:]):
        raise ValueError(f"长文语音试验后续S的delay_seconds必须为{group_interval:.1f}秒")
    return actions


# 保留内部旧名称，外部调用使用公开校验入口。
_validate_long_speech_actions = validate_long_speech_actions


def compile_long_speech_timeline(actions, recovery, *, group_interval=LONG_SPEECH_GROUP_INTERVAL):
    """每组三帧独立复用单S基线；计划时刻不代替成功写入和PCM进度。"""
    actions = validate_long_speech_actions(actions, group_interval=group_interval)
    recovery = _validate_recovery(recovery, actions)
    timeline = []
    for group_index, action in enumerate(actions, 1):
        group = compile_short_performance_timeline([{**action, "delay_seconds": 0.0}], recovery)
        offset = (group_index - 1) * group_interval
        for node in group:
            node.update(index=len(timeline) + 1, group_index=group_index,
                        at_seconds=node["at_seconds"] + offset)
            node["delay_seconds"] = node["at_seconds"] - (timeline[-1]["at_seconds"] if timeline else 0.0)
            if group_index > 1 and node["role"] == "GE":
                # 音频零点是首PCM成功写入；实际调度还须等待上一组Q写入完成。
                node["audio_at_seconds"] = offset - SHORT_PERFORMANCE_LEAD_SECONDS
            timeline.append(node)
    return timeline


def long_speech_required_audio_seconds(actions, *, group_interval=LONG_SPEECH_GROUP_INTERVAL):
    """末S后留出Q的1.5秒与0.5秒自然语音余量；这是试验边界。"""
    actions = validate_long_speech_actions(actions, group_interval=group_interval)
    return (len(actions) - 1) * group_interval + LONG_SPEECH_TAIL_SECONDS


def validate_long_action_group_count(group_count):
    if type(group_count) is not int or group_count not in (1, 2):
        raise ValueError("长动作回放试验组数只能为整数1或2")
    return group_count


def _validate_long_action_replay_actions(actions, *, group_count=1):
    group_count = validate_long_action_group_count(group_count)
    actions = _validate_actions(actions)
    if len(actions) != group_count or any(action["kind"] != "L" for action in actions):
        raise ValueError(f"长动作回放试验必须恰好{group_count}个L节点")
    if group_count == 2:
        if any(action["motions"][0]["part"] != "whole_body" for action in actions):
            raise ValueError("双L回放的每个L必须使用一个whole_body motion")
        if actions[1]["delay_seconds"] < actions[0]["hold_seconds"] + 1.0:
            raise ValueError("第二L起点间距须至少为第一L窗口加1秒，为Q与GE各保留0.5秒")
    return actions


def compile_long_action_replay_timeline(actions, recovery, *, group_count=1):
    """每个L保留完整窗口与尾Q；双L之间另保留Q→GE→L的等待。"""
    actions = _validate_long_action_replay_actions(actions, group_count=group_count)
    recovery = _validate_recovery(recovery, actions)
    timeline, offset = [], 0.0
    for group_index, action in enumerate(actions, 1):
        offset += action["delay_seconds"]
        group = _compile_validated([{**action, "delay_seconds": 0.0}], recovery)
        pieces = [" ".join(action[key].split()).rstrip("。；;，, ") for key in ("gaze", "expression")]
        lead = {"role": "GE", "kind": "GE", "index": 1, "at_seconds": offset,
                "delay_seconds": 0.0, "motions": [], "hold_seconds": 0.0,
                "instruction": "\n".join(f"{label}：{value}。" for label, value in zip(("眼神", "表情"), pieces)),
                "gaze": action["gaze"], "expression": action["expression"]}
        for entry in group:
            entry["at_seconds"] += offset + SHORT_PERFORMANCE_LEAD_SECONDS
        for entry in [lead, *group]:
            entry.update(index=len(timeline)+1, group_index=group_index)
            entry["delay_seconds"] = entry["at_seconds"] - (timeline[-1]["at_seconds"] if timeline else 0.0)
            if group_index > 1 and entry["role"] == "GE":
                entry["audio_at_seconds"] = offset - SHORT_PERFORMANCE_LEAD_SECONDS
            timeline.append(entry)
    return timeline


def long_speech_input_mode(item):
    try:
        return continuous_speech_input_mode(item)
    except ValueError:
        raise ValueError("长文语音试验仅开放模拟弹幕和一次安排；主线、纯动作与真实评论来源暂不接受") from None


def _parse_long_speech_plan(body, *, allow_timeline, group_interval=LONG_SPEECH_GROUP_INTERVAL,
                            long_action_replay=False, long_action_group_count=1):
    group_interval = validate_long_speech_group_interval(group_interval)
    required = {"emotion", "observation", "reading", "segments"}
    optional = {"timeline"} if allow_timeline else set()
    if not isinstance(body, dict) or not required <= set(body) or set(body) - required - optional:
        raise ValueError("长文语音计划必须且只能包含emotion、observation、reading、segments及允许的内部timeline")
    if body["reading"] is not None:
        raise ValueError("长文语音试验要求reading=null且恰好一段台词")
    emotion = _text(body["emotion"], 24, "emotion")
    observation = _text(body["observation"], 300, "observation")
    segments = body["segments"]
    if not isinstance(segments, list) or len(segments) != 1:
        raise ValueError("长文语音试验要求reading=null且恰好一段台词")
    segment = segments[0]
    segment_fields = {"text", "instruction", "actions", "recovery"}
    if (not isinstance(segment, dict) or not segment_fields <= set(segment)
            or set(segment) - segment_fields - optional):
        raise ValueError("长文语音每段必须且只能包含text、instruction、actions、recovery及允许的内部timeline")
    text = _text(segment["text"], LONG_SPEECH_MAX_TEXT, "text")
    instruction = _text(segment["instruction"], 160, "语音instruction")
    actions = (_validate_long_action_replay_actions(segment["actions"], group_count=long_action_group_count)
               if long_action_replay else
               validate_long_speech_actions(segment["actions"], group_interval=group_interval))
    recovery = _validate_recovery(segment["recovery"], actions)
    timeline = (compile_long_action_replay_timeline(actions, recovery, group_count=long_action_group_count)
                if long_action_replay else
                compile_long_speech_timeline(actions, recovery, group_interval=group_interval))
    return {"emotion": emotion, "observation": observation, "reading": None,
            "segments": [{"text": text, "instruction": instruction, "actions": actions,
                          "recovery": recovery, "timeline": timeline}]}


def parse_long_speech_plan(body, *, group_interval=LONG_SPEECH_GROUP_INTERVAL):
    """独立外部合同；不经过普通模式30字parser，也不接受模型自传timeline。"""
    return _parse_long_speech_plan(body, allow_timeline=False, group_interval=group_interval)


def validate_long_speech_plan(plan, *, group_interval=LONG_SPEECH_GROUP_INTERVAL):
    """TTS前完整复核原始或内部计划；所有派生timeline均重新编译。"""
    clean = _parse_long_speech_plan(plan, allow_timeline=True, group_interval=group_interval)
    plan.clear()
    plan.update(clean)
    return plan


def parse_long_action_replay_plan(body, *, group_count=1):
    """单L或显式双L长文独立入口；原多S长文合同保持不变。"""
    return _parse_long_speech_plan(body, allow_timeline=False, long_action_replay=True,
                                   long_action_group_count=group_count)


def validate_long_action_replay_plan(plan, *, group_count=1):
    clean = _parse_long_speech_plan(plan, allow_timeline=True, long_action_replay=True,
                                    long_action_group_count=group_count)
    plan.clear()
    plan.update(clean)
    return plan


def validate_short_performance_plan(plan, *, motion_only=False):
    """在正常计划校验之后附加范围约束，显示实际试验时间线。"""
    if motion_only:
        plan["timeline"] = compile_short_performance_timeline(plan["actions"], plan["recovery"])
        return plan
    if plan.get("reading") is not None or len(plan.get("segments", [])) != 1:
        raise ValueError("短表演试验要求reading=null且恰好一段台词")
    part = plan["segments"][0]
    part["timeline"] = compile_short_performance_timeline(part["actions"], part["recovery"])
    return plan


def _reading_text(value: object) -> str:
    # 2至6字只是自然选段的倾向，校验只沿用语音通道的通用文字上限。
    return _text(value, 30, "reading.text")


def _validate_reading(reading: object, source_text: str | None) -> dict | None:
    if source_text is not None and not isinstance(source_text, str):
        raise ValueError("source_text必须为文字或None")
    if reading is None:
        # Terra按上下文整套选用或省略阅读，不以原弹幕长度决定是否强制阅读。
        return None
    if not isinstance(reading, dict) or set(reading) != {"text", "instruction"}:
        raise ValueError("reading必须且只能包含text和instruction")
    text = _reading_text(reading["text"])
    if source_text is not None and text not in source_text:
        raise ValueError("reading.text必须是本轮原弹幕的连续子串，不得扩写或添加前缀")
    return {"text": text, "instruction": _text(reading["instruction"], 160, "阅读语音instruction")}


def compile_reading_timeline(reading: dict, actions: list[dict], recovery: dict,
                             read_seconds: float | None = None) -> list[dict]:
    """阅读时长只取自PCM；预览时保留未知时间，不能冒充播放完成证据。"""
    if not isinstance(reading, dict):
        raise ValueError("阅读前导必须提供reading")
    _reading_text(reading.get("text"))
    actions = _validate_actions(actions)
    recovery = _validate_recovery(recovery, actions, required=True)
    if read_seconds is not None:
        read_seconds = _number(read_seconds, "read_seconds")
        if read_seconds <= 0:
            raise ValueError("read_seconds必须大于0，或在预览时为None")
    reading_motions = [{"part": "torso", "description": "上身轻微前倾"},
                       {"part": "head", "description": "头部自然向前探近"}]
    recovery_seconds = None if read_seconds is None else READING_LEAD_SECONDS + read_seconds
    timeline = [{"role": "R", "kind": "R", "index": 1, "at_seconds": 0.0,
                 "delay_seconds": 0.0, "motions": reading_motions,
                 "instruction": _instruction("同时上身轻微前倾，并头部自然向前探近", READING_GAZE, READING_EXPRESSION),
                 "hold_seconds": 0.0, "gaze": READING_GAZE, "expression": READING_EXPRESSION},
                {"role": "S0", "kind": "S0", "index": 2, "at_seconds": recovery_seconds,
                 "delay_seconds": recovery_seconds,
                 "motions": [{"part": "torso", "description": "收回前倾恢复正常坐姿"}],
                 "instruction": _instruction("收回前倾恢复正常坐姿", recovery["gaze"], recovery["expression"]),
                 "hold_seconds": 0.0, **recovery}]
    answer = _compile_validated(actions, recovery)
    if answer:
        answer[0]["delay_seconds"] = ANSWER_START_SECONDS
    else:
        # 即使回答没有主动动作，阅读引入的姿态也有S0与唯一的尾Q。
        answer = [{"role": "Q", "kind": "Q", "motions": [], "hold_seconds": 0.0,
                   "delay_seconds": QUIET_DELAY_SECONDS,
                   "instruction": _instruction(QUIET_INSTRUCTION, recovery["gaze"], recovery["expression"]),
                   **recovery}]
    at_seconds = recovery_seconds
    for index, node in enumerate(answer, 3):
        at_seconds = None if at_seconds is None else at_seconds + node["delay_seconds"]
        timeline.append({**node, "index": index, "at_seconds": at_seconds})
    return timeline


def parse_plan(body: dict, *, source_text: str | None = None) -> dict:
    """在任何收费语音合成前验证整轮计划，不接受旧action或模型自传Q。"""
    if not isinstance(body, dict) or set(body) - {"emotion", "observation", "reading", "segments", "id", "lease"}:
        raise ValueError("计划只接受emotion、observation、reading、segments及id/lease传输字段")
    if "reading" not in body:
        raise ValueError("计划必须包含reading")
    reading = _validate_reading(body["reading"], source_text)
    emotion = _text(body.get("emotion"), 24, "emotion")
    observation = _text(body.get("observation"), 300, "observation")
    segments = body.get("segments")
    if not isinstance(segments, list) or not 1 <= len(segments) <= 2:
        raise ValueError("每轮需要1至2个台词短句")
    clean = []
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict) or set(segment) != {"text", "instruction", "actions", "recovery"}:
            raise ValueError("每段必须且只能包含text、instruction、actions、recovery；不接受旧action结构")
        text = _text(segment["text"], 30, "text")
        instruction = _text(segment["instruction"], 160, "语音instruction")
        actions = _validate_actions(segment["actions"])
        has_reading = index == 0 and reading is not None
        recovery = _validate_recovery(segment["recovery"], actions, required=has_reading)
        timeline = (compile_reading_timeline(reading, actions, recovery) if has_reading
                    else _compile_validated(actions, recovery))
        clean.append({"text": text, "instruction": instruction, "actions": actions,
                      "recovery": recovery, "timeline": timeline})
    return {"emotion": emotion, "observation": observation, "reading": reading, "segments": clean}


def parse_game_motion_plan(body: dict) -> dict:
    """当前游戏纯动作合同；历史导演计划仍由旧解析器单独处理。"""
    if not isinstance(body, dict) or set(body) != {"emotion", "actions", "recovery"}:
        raise ValueError("纯动作计划必须且只能包含emotion、actions、recovery")
    emotion = _text(body["emotion"], 24, "emotion")
    if not isinstance(body["actions"], list) or not 1 <= len(body["actions"]) <= MAX_DIRECTOR_ACTIONS:
        raise ValueError("主持纯动作需要1至6个动作节点")
    actions = _validate_actions(body["actions"])
    recovery = _validate_recovery(body["recovery"], actions)
    return {"emotion": emotion, "actions": actions, "recovery": recovery,
            "timeline": _compile_validated(actions, recovery)}


def parse_director_plan(body: dict, *, mode: str) -> dict:
    """只供可信主持任务使用；不能按模型返回字段自动选取此权限分支。"""
    if mode not in ("once", "topic", "motion"):
        raise ValueError("未知主持模式")
    if mode == "motion":
        if not isinstance(body, dict) or set(body) != {"emotion", "observation", "actions", "recovery"}:
            raise ValueError("纯动作计划必须且只能包含emotion、observation、actions、recovery")
        emotion = _text(body["emotion"], 24, "emotion")
        observation = _text(body["observation"], 300, "observation")
        if not isinstance(body["actions"], list) or not 1 <= len(body["actions"]) <= MAX_DIRECTOR_ACTIONS:
            raise ValueError("主持纯动作需要1至6个动作节点")
        actions = _validate_actions(body["actions"])
        recovery = _validate_recovery(body["recovery"], actions)
        return {"emotion": emotion, "observation": observation, "actions": actions,
                "recovery": recovery, "timeline": _compile_validated(actions, recovery)}
    if not isinstance(body, dict) or set(body) != {"emotion", "observation", "reading", "segments"}:
        raise ValueError("主持台词计划必须且只能包含emotion、observation、reading、segments")
    if body["reading"] is not None:
        raise ValueError("主持指令不得使用reading，必须为null")
    parsed = parse_plan(body)
    if any(len(segment["actions"]) > MAX_DIRECTOR_ACTIONS for segment in parsed["segments"]):
        raise ValueError("主持每句最多6个动作节点")
    return parsed
