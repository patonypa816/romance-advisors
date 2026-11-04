"""只读固定素材，重排动作间距；本模块不接触凭据、TTS或网络。"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
from pathlib import Path
import re
import wave

from action_schema import (parse_long_speech_plan, long_speech_required_audio_seconds,
                           parse_long_action_replay_plan)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"c[0-9]{4,12}", value):
        raise ValueError("固定回放评论编号无效")
    return value


class FixedReplay:
    RATE = 24000

    def __init__(self, source_run: Path, comment_id="c0001", group_interval=5.0, *, action_kind="S",
                 long_action_group_count=1, first_q_after_seconds=None, second_l_at_seconds=None):
        self.source_run = Path(source_run).resolve(strict=True)
        self.comment_id = _identifier(comment_id)
        # 5秒只属于本次固定素材对照，不扩大正常长文模式的默认合同。
        if isinstance(group_interval, bool) or group_interval != 5.0:
            raise ValueError("固定回放只允许5秒组间距")
        if action_kind not in {"S", "L"}:
            raise ValueError("固定回放只支持多组S或L分组对照")
        if type(long_action_group_count) is not int or long_action_group_count not in (1, 2):
            raise ValueError("固定长动作回放只支持1或2组L")
        if action_kind != "L" and long_action_group_count != 1:
            raise ValueError("双L分组只适用于L固定回放")
        if first_q_after_seconds is not None and (
                type(first_q_after_seconds) not in (int, float) or first_q_after_seconds != 3.0
                or action_kind != "L" or long_action_group_count != 2):
            raise ValueError("提前Q对照只允许双L模式的第一Q在L1后3秒发送")
        if second_l_at_seconds is not None and (
                type(second_l_at_seconds) not in (int, float) or second_l_at_seconds != 7.36
                or action_kind != "L" or long_action_group_count != 2 or first_q_after_seconds != 3.0):
            raise ValueError("语义位置对照只允许双L及第一Q为3秒时，将第二L设在7.36秒")
        self.action_kind = action_kind
        self.long_action_group_count = long_action_group_count
        self.first_q_after_seconds = first_q_after_seconds
        self.second_l_at_seconds = second_l_at_seconds
        # L没有组间距；属性使用默认值只为兼容公共传输构造，实际由L窗口计时。
        self.group_interval = float(group_interval) if action_kind == "S" else 3.0
        self._source_hashes = {}
        ident = self.comment_id
        plan_name = f"plans/{ident}.json"
        self._original_plan = self._read(plan_name, 100_000)
        original = json.loads(self._original_plan)
        if not isinstance(original, dict) or original.get("comment_id") != ident:
            raise ValueError("原计划评论编号不匹配")
        if set(original) - {"comment_id", "comment", "emotion", "observation", "reading", "segments"}:
            raise ValueError("原计划含未知字段")
        raw_plan = {key: copy.deepcopy(original[key]) for key in ("emotion", "observation", "reading", "segments")}
        for part in raw_plan["segments"]:
            part.pop("timeline", None)
        canonical = parse_long_speech_plan(raw_plan)
        source_part = canonical["segments"][0]

        folder = f"tts/{ident}-1"
        result = json.loads(self._read(f"{folder}/result.json", 100_000))
        sent = json.loads(self._read(f"{folder}/component-send.json", 200_000))
        receipt = json.loads(self._read("component-receipt.json", 1_000_000))
        variant = json.loads(self._read("variant.json", 100_000))
        self.pcm = self._read(f"{folder}/sample.pcm", self.RATE * 2 * 30)
        self.wav = self._read(f"{folder}/sample.wav", self.RATE * 2 * 30 + 100_000)
        self.pcm_sha256, self.wav_sha256 = _sha(self.pcm), _sha(self.wav)
        self.audio_seconds = len(self.pcm) / (self.RATE * 2)
        if (result.get("status") != "ok" or result.get("text") != source_part["text"]
                or result.get("pcm_sha256") != self.pcm_sha256
                or result.get("wav_sha256") != self.wav_sha256
                or not self.pcm or len(self.pcm) % 2):
            raise ValueError("原音频状态、台词或哈希核验失败")
        for seconds in (result.get("audio_seconds"), sent.get("audio_seconds")):
            if (isinstance(seconds, bool) or not isinstance(seconds, (float, int))
                    or not math.isfinite(seconds) or abs(seconds - self.audio_seconds) > .001):
                raise ValueError("原音频时长与PCM字节不一致")
        # 原WAV可能保留流式占位长度；读取实际数据，并与PCM逐字节比较。
        with wave.open(io.BytesIO(self.wav)) as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, self.RATE, "NONE"):
                raise ValueError("原WAV格式不符合24kHz单声道s16le")
            if wav.readframes(self.RATE * 31) != self.pcm:
                raise ValueError("原WAV与PCM内容不一致")
        if (sent.get("send_complete") is not True or sent.get("actions_sent_complete") is not True
                or sent.get("reading_text") is not None or sent.get("reading_audio_seconds") != 0
                or sent.get("pcm_transport_sent_bytes") != len(self.pcm)
                or sent.get("timeline") != source_part["timeline"]):
            raise ValueError("原发送回执未完整成功或不匹配计划")
        events = sent.get("motion_events")
        if (not isinstance(events, list) or len(events) != len(source_part["timeline"])
                or any(event.get("comment_id") != ident or event.get("group_id") != sent.get("group_id")
                       or event.get("node_index") != node["index"]
                       or event.get("instruction") != node["instruction"]
                       or event.get("role") != node["role"]
                       for event, node in zip(events, source_part["timeline"]))):
            raise ValueError("原动作发送记录与指定评论不一致")
        if receipt.get("state") != "ended" or receipt.get("cloud_end_confirmed") is not True:
            raise ValueError("原云端会话尚未确认结束")
        self.avatar_id = receipt.get("creation_avatar_id")
        if (receipt.get("creation_input_mode") != "avatar_id" or not isinstance(self.avatar_id, str)
                or not re.fullmatch(r"[0-9]{1,64}", self.avatar_id)
                or variant.get("avatar_id") != self.avatar_id):
            raise ValueError("原形象创建基线无法核验")
        image_name = Path(variant.get("used_image_path", "")).name
        if not re.fullmatch(r"input-image(?:-[0-9a-f]{16})?\.png", image_name):
            raise ValueError("原形象文件不在精确白名单")
        self.image_sha256 = _sha(self._read(image_name, 20_000_000))
        if variant.get("image_sha256") != self.image_sha256:
            raise ValueError("原形象图片哈希不一致")

        replay = copy.deepcopy(raw_plan)
        self.original_actions = [a["motions"][0]["description"] for a in source_part["actions"]]
        if action_kind == "L":
            if len(self.original_actions) != 4:
                raise ValueError("本次L对照只接受原四动作样本")
            if long_action_group_count == 1:
                # 保留此前单L对照，四动作仍作为一条Type9发送。
                connectors = ("先", "然后", "随后", "最后")
                descriptions = ["，".join(prefix + action for prefix, action in zip(connectors, self.original_actions))]
                first_indexes, delays, holds = [0], [0], [self.audio_seconds]
            else:
                # 两个原S合成一个可组合的L；Q与下一GE各留0.5秒，
                # 不让文字里的“然后”承担组间时序，也不改动原PCM。
                midpoint = self.audio_seconds / 2
                descriptions = ["，同时".join(self.original_actions[start:start + 2]) for start in (0, 2)]
                first_indexes, delays = [0, 2], [0, midpoint]
                holds = [midpoint - 1.0, self.audio_seconds - midpoint]
                if first_q_after_seconds is not None:
                    # 只提前第一Q；L2原起点及其窗口不动，空出的时间留给正常坐姿。
                    holds[0] = float(first_q_after_seconds)
                if second_l_at_seconds is not None:
                    # 将本样本的第二L移到“百废待兴”，末Q仍留在原音频结尾。
                    # 由编译器推导GE前导；不改变PCM，也不靠提前末Q缩短第二组。
                    delays[1] = float(second_l_at_seconds)
                    holds[1] = round(self.audio_seconds - delays[1], 6)
            replay["segments"][0]["actions"] = [
                {"kind": "L", "delay_seconds": delay, "hold_seconds": hold,
                 "motions": [{"part": "whole_body", "description": description}],
                 "gaze": source_part["actions"][first_index]["gaze"],
                 "expression": source_part["actions"][first_index]["expression"]}
                for first_index, description, delay, hold in zip(first_indexes, descriptions, delays, holds)
            ]
            self._plan = parse_long_action_replay_plan(replay, group_count=long_action_group_count)
        else:
            for action in replay["segments"][0]["actions"][1:]:
                action["delay_seconds"] = self.group_interval
            self._plan = parse_long_speech_plan(replay, group_interval=self.group_interval)
            needed = long_speech_required_audio_seconds(self._plan["segments"][0]["actions"],
                                                       group_interval=self.group_interval)
            if self.audio_seconds < needed:
                raise ValueError(f"原音频只有{self.audio_seconds:g}秒，5秒间距需要至少{needed:g}秒；不删动作不补静音")
        self._replay_plan_bytes = _json_bytes(self._plan)
        self.fingerprint = _sha(_json_bytes({"source_run": self.source_run.name, "comment_id": ident,
                                            "source_files": self._source_hashes,
                                            "plan_sha256": _sha(self._replay_plan_bytes),
                                            "avatar_id": self.avatar_id, "image_sha256": self.image_sha256,
                                            "group_interval": self.group_interval, "action_kind": self.action_kind,
                                            "long_action_group_count": self.long_action_group_count}))

    def _read(self, relative, limit):
        path = self.source_run / relative
        # 只读调用方指定的精确文件，禁止符号链接把白名单引向凭据或其他run。
        current = self.source_run
        for piece in Path(relative).parts:
            current = current / piece
            if current.is_symlink():
                raise ValueError("固定素材不接受符号链接")
        if not path.is_file() or path.stat().st_size > limit:
            raise ValueError("固定素材文件缺失或超过大小限制：" + relative)
        raw = path.read_bytes()
        if len(raw) > limit:
            raise ValueError("固定素材文件超过大小限制")
        self._source_hashes[relative] = _sha(raw)
        return raw

    @property
    def plan(self):
        return copy.deepcopy(self._plan)

    def public(self):
        part = self._plan["segments"][0]
        return {"source_run": self.source_run.name, "source_comment_id": self.comment_id,
                "text": part["text"], "audio_seconds": self.audio_seconds,
                "actions": [a["motions"][0]["description"] for a in part["actions"]],
                "group_interval": self.group_interval if self.action_kind == "S" else None,
                "pcm_sha256": self.pcm_sha256, "fingerprint": self.fingerprint,
                "action_kind": self.action_kind, "original_actions": list(self.original_actions),
                "long_action_group_count": self.long_action_group_count if self.action_kind == "L" else None,
                "first_q_after_seconds": self.first_q_after_seconds,
                "second_l_anchor": "百废待兴" if self.second_l_at_seconds is not None else None,
                "hold_seconds": self.audio_seconds if self.action_kind == "L" and self.long_action_group_count == 1 else None,
                "group_holds_seconds": [a["hold_seconds"] for a in part["actions"]] if self.action_kind == "L" else [],
                "second_l_audio_seconds": part["actions"][1]["delay_seconds"] if self.action_kind == "L" and self.long_action_group_count == 2 else None,
                "last_q_audio_seconds": round(part["actions"][1]["delay_seconds"] + part["actions"][1]["hold_seconds"], 6)
                if self.action_kind == "L" and self.long_action_group_count == 2 else None,
                "q_to_next_l_seconds": (part["actions"][1]["delay_seconds"] - part["actions"][0]["hold_seconds"])
                if self.action_kind == "L" and self.long_action_group_count == 2 else None,
                "type9_count": len(part["timeline"])}

    def _destination(self, out):
        out = Path(out).resolve()
        if out == self.source_run or out.is_relative_to(self.source_run):
            raise ValueError("固定回放不能写入原运行目录")
        return out

    @staticmethod
    def _write_new(path, raw):
        with path.open("xb") as handle:
            handle.write(raw)
        if path.read_bytes() != raw:
            raise ValueError("固定素材复制字节不一致")

    def snapshot(self, out):
        target = self._destination(out) / "replay-source"
        target.mkdir(parents=True, exist_ok=False)
        files = {"original-plan.json": self._original_plan, "replay-plan.json": self._replay_plan_bytes,
                 "sample.pcm": self.pcm, "sample.wav": self.wav}
        manifest = {**self.public(), "image_sha256": self.image_sha256, "avatar_id": self.avatar_id,
                    "source_files": dict(self._source_hashes),
                    "files": {name: {"sha256": _sha(raw), "bytes": len(raw)} for name, raw in files.items()}}
        for name, raw in {**files, "manifest.json": _json_bytes(manifest)}.items():
            self._write_new(target / name, raw)
            (target / name).chmod(0o444)
        return copy.deepcopy(manifest)

    def prepare(self, out, comment, part, label):
        if (comment.get("replay_fixture_fingerprint") != self.fingerprint
                or part != self._plan["segments"][0] or type(label) is not int or label != 1):
            raise ValueError("固定回放身份或台词动作计划不匹配")
        ident = _identifier(comment.get("id"))
        segment_out = self._destination(out) / "tts" / f"{ident}-{label}"
        segment_out.mkdir(parents=True, exist_ok=False)
        self._write_new(segment_out / "sample.pcm", self.pcm)
        self._write_new(segment_out / "sample.wav", self.wav)
        result = {"status": "reused", "text": part["text"], "text_codepoints": len(part["text"]),
                  "pcm_path": str(segment_out / "sample.pcm"), "wav_path": str(segment_out / "sample.wav"),
                  "pcm_sha256": self.pcm_sha256, "wav_sha256": self.wav_sha256,
                  "audio_seconds": self.audio_seconds, "usage_verified": True,
                  "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_cny": 0,
                  "source_comment_id": self.comment_id, "replay_fixture_fingerprint": self.fingerprint}
        self._write_new(segment_out / "result.json", _json_bytes(result))
        return self.pcm, segment_out
