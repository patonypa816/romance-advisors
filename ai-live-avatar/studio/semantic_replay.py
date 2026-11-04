"""Verified semantic source audio replayed with two independent short gestures.

The source stays immutable. Only the local GE/S/Q window changes; this fixture
never asks a model to rewrite the audio, gestures or their final word anchors.
"""
from __future__ import annotations

import copy
import io
import json
import math
from pathlib import Path
import re
import wave

from fixed_replay import FixedReplay, _identifier, _json_bytes, _sha
from semantic_speech import (_actions, _normalized, _occurrences, compile_semantic_timeline,
                             parse_historical_semantic_plan, validate_alignment)


def _same_timeline(actual, expected):
    # These are sender provenance, not part of the compiler's motion contract.
    transport = {"timebase", "pcm_required_bytes"}
    return (isinstance(actual, list) and len(actual) == len(expected)
            and all(isinstance(node, dict)
                    and {k: v for k, v in node.items() if k not in transport} == reference
                    for node, reference in zip(actual, expected)))


def _ge_lead(value):
    if value is not None and (type(value) not in (int, float) or value not in (.5, 1.1)):
        raise ValueError("固定语义对照的GE前导只允许0.5秒、1.1秒或取消独立GE")
    return value


class SemanticFixedReplay(FixedReplay):
    semantic_speech = True
    action_kind = "S"
    long_action_group_count = 1
    group_interval = 3.0  # Compatibility only; final source word anchors are retained.
    first_q_after_seconds = None
    second_l_at_seconds = None

    def __init__(self, source_run: Path, comment_id="c0002", *, ge_lead_seconds=.5):
        self._ge_lead_seconds = _ge_lead(ge_lead_seconds)
        self.source_run = Path(source_run).resolve(strict=True)
        self.comment_id = _identifier(comment_id)
        self._source_hashes = {}
        folder = f"tts/{self.comment_id}-1"
        self._original_plan = self._read(f"plans/{self.comment_id}.json", 100_000)
        original = json.loads(self._original_plan)
        if (not isinstance(original, dict) or original.get("comment_id") != self.comment_id
                or not {"emotion", "observation", "reading", "segments"} <= set(original)
                or set(original) - {"comment_id", "comment", "emotion", "observation", "reading", "segments"}):
            raise ValueError("原计划评论身份或字段不匹配")
        fields = ("emotion", "observation", "reading", "segments")
        canonical = parse_historical_semantic_plan({key: original[key] for key in fields})
        if canonical != {key: original[key] for key in fields}:
            raise ValueError("原计划必须已经通过规范化，不在回放中改写原文")
        if canonical["reading"] is not None or len(canonical["segments"]) != 1:
            raise ValueError("语义短动作对照只接受一段且无reading的原样本")
        source_part = canonical["segments"][0]
        if len(source_part["actions"]) != 2:
            raise ValueError("语义短动作对照必须保留原来的两个动作组")

        self._original_timed_plan = self._read(f"{folder}/timed-action-plan.json", 200_000)
        timed = json.loads(self._original_timed_plan)
        if (not isinstance(timed, dict)
                or set(timed) != {"text", "draft_actions", "actions", "timeline", "planning"}
                or timed["text"] != source_part["text"]
                or timed["draft_actions"] != source_part["actions"]):
            raise ValueError("TTS后计划与原台词或动作草案不一致")
        final_actions = _actions(timed["actions"], source_part["text"])
        if final_actions != timed["actions"] or len(final_actions) != 2 or any(
                {k: v for k, v in old.items() if k != "anchor"}
                != {k: v for k, v in new.items() if k != "anchor"}
                for old, new in zip(source_part["actions"], final_actions)):
            raise ValueError("TTS后计划只能更改原动作锚点，不可更改动作或眼神表情")

        result = json.loads(self._read(f"{folder}/result.json", 200_000))
        sent = json.loads(self._read(f"{folder}/component-send.json", 300_000))
        receipt = json.loads(self._read("component-receipt.json", 1_000_000))
        variant = json.loads(self._read("variant.json", 100_000))
        if any(not isinstance(value, dict) for value in (result, sent, receipt, variant)):
            raise ValueError("原音频、发送或形象记录必须为对象")
        self.pcm = self._read(f"{folder}/sample.pcm", self.RATE * 2 * 30)
        self.wav = self._read(f"{folder}/sample.wav", self.RATE * 2 * 30 + 100_000)
        self.pcm_sha256, self.wav_sha256 = _sha(self.pcm), _sha(self.wav)
        self.audio_seconds = len(self.pcm) / (self.RATE * 2)
        if (not self.pcm or len(self.pcm) % 2 or result.get("status") != "ok"
                or result.get("text") != source_part["text"]
                or result.get("pcm_sha256") != self.pcm_sha256
                or result.get("wav_sha256") != self.wav_sha256):
            raise ValueError("原音频状态、台词或哈希核验失败")
        for value in (result.get("audio_seconds"), sent.get("audio_seconds")):
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or abs(value - self.audio_seconds) > .001):
                raise ValueError("原音频时长与PCM字节不一致")
        with wave.open(io.BytesIO(self.wav)) as wav:
            if ((wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype())
                    != (1, 2, self.RATE, "NONE") or wav.readframes(self.RATE * 31) != self.pcm):
                raise ValueError("原WAV格式或内容与PCM不一致")
        self._alignment = copy.deepcopy(result.get("alignment"))
        validate_alignment(self._alignment, source_part["text"], self.audio_seconds,
                           pcm_sha256=self.pcm_sha256)
        if sent.get("alignment") != self._alignment:
            raise ValueError("原发送时间戳与本次PCM的时间戳不一致")
        planning = timed.get("planning")
        if (not isinstance(planning, dict) or planning.get("status") != "ready"
                or planning.get("phase") != "tts_aligned" or planning.get("group_count") != 2
                or planning.get("pcm_sha256") != self.pcm_sha256
                or planning.get("audio_seconds") != self.audio_seconds):
            raise ValueError("原最终排程未绑定同次PCM或未完成")

        original_timeline = compile_semantic_timeline(final_actions, source_part["recovery"],
                                                       self._alignment, self.audio_seconds)
        if (not _same_timeline(timed.get("timeline"), original_timeline)
                or not _same_timeline(sent.get("timeline"), original_timeline)):
            raise ValueError("原最终锚点、L时间线与发送计划不一致")
        if (sent.get("comment_id") != self.comment_id or sent.get("segment_index") != 1
                or sent.get("send_complete") is not True or sent.get("actions_sent_complete") is not True
                or sent.get("reading_text") is not None or sent.get("reading_audio_seconds") != 0
                or sent.get("pcm_transport_sent_bytes") != len(self.pcm)):
            raise ValueError("原样本未完整发送或评论身份不匹配")
        events = sent.get("motion_events")
        if (not isinstance(events, list) or len(events) != len(original_timeline)
                or any(not isinstance(event, dict)
                       or event.get("comment_id") != self.comment_id
                       or event.get("group_id") != sent.get("group_id")
                       or event.get("node_index") != node["index"]
                       or event.get("instruction") != node["instruction"]
                       or event.get("role") != node["role"]
                       for event, node in zip(events, original_timeline))):
            raise ValueError("原动作发送记录与最终计划不一致")
        if receipt.get("state") != "ended" or receipt.get("cloud_end_confirmed") is not True:
            raise ValueError("原云端会话尚未确认结束")
        self.avatar_id = receipt.get("creation_avatar_id")
        if (receipt.get("creation_input_mode") != "avatar_id"
                or not isinstance(self.avatar_id, str) or not re.fullmatch(r"[0-9]{1,64}", self.avatar_id)
                or variant.get("avatar_id") != self.avatar_id):
            raise ValueError("原形象创建基线无法核验")
        if not isinstance(variant.get("used_image_path"), str):
            raise ValueError("原形象文件路径无效")
        image_name = Path(variant["used_image_path"]).name
        if not re.fullmatch(r"input-image(?:-[0-9a-f]{16})?\.png", image_name):
            raise ValueError("原形象文件不在精确白名单")
        self.image_sha256 = _sha(self._read(image_name, 20_000_000))
        if variant.get("image_sha256") != self.image_sha256:
            raise ValueError("原形象图片哈希不一致")
        self.image_path = self.source_run / image_name

        self._plan = copy.deepcopy(canonical)
        part = self._plan["segments"][0]
        part.update(actions=final_actions, alignment=copy.deepcopy(self._alignment),
                    timeline=compile_semantic_timeline(final_actions, part["recovery"], self._alignment,
                                                        self.audio_seconds, action_kind="S",
                                                        ge_lead_seconds=self.ge_lead_seconds))
        self.original_actions = [motion["description"] for action in final_actions for motion in action["motions"]]
        self._replay_plan_bytes = _json_bytes(self._plan)
        self.fingerprint = _sha(_json_bytes({"source_run": self.source_run.name,
            "comment_id": self.comment_id, "source_files": self._source_hashes,
            "plan_sha256": _sha(self._replay_plan_bytes), "action_kind": self.action_kind,
            "ge_lead_seconds": self.ge_lead_seconds,
            "semantic_speech": True, "avatar_id": self.avatar_id, "image_sha256": self.image_sha256}))

    @property
    def ge_lead_seconds(self):
        return self._ge_lead_seconds

    @property
    def alignment(self):
        return copy.deepcopy(self._alignment)

    def public(self):
        part = self._plan["segments"][0]
        starts = [node["at_seconds"] for node in part["timeline"] if node["role"] == "S"]
        ends = [node["at_seconds"] for node in part["timeline"] if node["role"] == "Q"]
        ge_times = [round(node["at_seconds"], 6) for node in part["timeline"] if node["role"] == "GE"]
        label = ("取消独立GE" if self.ge_lead_seconds is None else
                 "1.1秒延长GE" if self.ge_lead_seconds == 1.1 else "0.5秒GE基线")
        return {"source_run": self.source_run.name, "source_comment_id": self.comment_id,
                "text": part["text"], "audio_seconds": self.audio_seconds,
                "actions": ["，同时".join(motion["description"] for motion in action["motions"])
                            for action in part["actions"]],
                "original_actions": list(self.original_actions),
                "anchors": [copy.deepcopy(action["anchor"]) for action in part["actions"]],
                "anchor_seconds": starts, "s_seconds": starts, "q_seconds": ends,
                "ge_lead_seconds": self.ge_lead_seconds, "independent_ge": self.ge_lead_seconds is not None,
                "ge_seconds": ge_times, "comparison_label": label,
                "semantic_speech": True, "action_kind": "S", "group_interval": None,
                "long_action_group_count": None, "first_q_after_seconds": None,
                "second_l_anchor": None, "group_holds_seconds": [], "second_l_audio_seconds": None,
                "last_q_audio_seconds": ends[-1], "q_to_next_l_seconds": None,
                "pcm_sha256": self.pcm_sha256, "fingerprint": self.fingerprint,
                "type9_count": len(part["timeline"])}

    def _snapshot_files(self):
        return {"original-plan.json": self._original_plan,
                "original-timed-action-plan.json": self._original_timed_plan,
                "replay-plan.json": self._replay_plan_bytes, "alignment.json": _json_bytes(self._alignment),
                "sample.pcm": self.pcm, "sample.wav": self.wav}

    def snapshot(self, out):
        target = self._destination(out) / "replay-source"
        target.mkdir(parents=True, exist_ok=False)
        files = self._snapshot_files()
        manifest = {**self.public(), "image_sha256": self.image_sha256, "avatar_id": self.avatar_id,
                    "source_files": dict(self._source_hashes),
                    "files": {name: {"sha256": _sha(raw), "bytes": len(raw)} for name, raw in files.items()}}
        for name, raw in {**files, "manifest.json": _json_bytes(manifest)}.items():
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            self._write_new(target / name, raw)
            (target / name).chmod(0o444)
        return copy.deepcopy(manifest)

    def prepare(self, out, comment, part, label):
        pcm, folder = super().prepare(out, comment, part, label)
        self._write_new(folder / "alignment.json", _json_bytes(self._alignment))
        self._write_new(folder / "timed-action-plan.json", _json_bytes({
            "text": part["text"], "actions": part["actions"], "timeline": part["timeline"],
            "alignment": self._alignment, "planning": {"phase": "fixed_semantic_replay",
            "model_called": False, "source_comment_id": self.comment_id,
            "source_kind": getattr(self, "source_kind", "recorded"),
            "ge_lead_seconds": self.ge_lead_seconds, "independent_ge": self.ge_lead_seconds is not None,
            "comparison_label": self.public()["comparison_label"],
            "pcm_sha256": self.pcm_sha256, "replay_fixture_fingerprint": self.fingerprint}}))
        return pcm, folder


class PreparedSemanticReplay(SemanticFixedReplay):
    """Replay pre-generated TTS material without claiming a previous live send.

    Every read uses a fixed filename inside the preparation directory. Paths
    retained in the genuine TTS result are evidence only and are never opened.
    """
    source_kind = "prepared"
    SCHEMA = "prepared-semantic-replay-v1"
    FILE_LIMITS = {"prepared-plan.json": 100_000, "sample.pcm": 24000 * 2 * 30,
                   "sample.wav": 24000 * 2 * 30 + 100_000, "result.json": 300_000,
                   "image.png": 20_000_000}

    def _load_preparation(self, preparation, ge_lead_seconds):
        self._ge_lead_seconds = _ge_lead(ge_lead_seconds)
        self.source_run = Path(preparation).resolve(strict=True)
        self.comment_id = "prepared"
        self._source_hashes = {}
        self._preparation_manifest = self._read("manifest.json", 16384)
        manifest = json.loads(self._preparation_manifest)
        if (not isinstance(manifest, dict)
                or set(manifest) != {"schema", "source_kind", "avatar_id", "image_sha256", "files"}
                or manifest.get("schema") != self.SCHEMA or manifest.get("source_kind") != self.source_kind
                or not isinstance(manifest.get("files"), dict)
                or set(manifest["files"]) != set(self.FILE_LIMITS)):
            raise ValueError("准备素材manifest的版本、来源或文件白名单不匹配")
        verified = {}
        for name, limit in self.FILE_LIMITS.items():
            expected = manifest["files"][name]
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError("准备素材文件必须有完整SHA256")
            raw = self._read(name, limit)
            if _sha(raw) != expected:
                raise ValueError("准备素材文件与manifest哈希不一致：" + name)
            verified[name] = raw
        self._prepared_files = verified
        return manifest, verified

    def __init__(self, preparation: Path, *, ge_lead_seconds=1.1):
        manifest, verified = self._load_preparation(preparation, ge_lead_seconds)
        original = json.loads(verified["prepared-plan.json"])
        canonical = parse_historical_semantic_plan(original)
        if (canonical != original or canonical["reading"] is not None
                or len(canonical["segments"]) != 1
                or not 3 <= len(canonical["segments"][0]["actions"]) <= 6):
            raise ValueError("准备素材必须为规范单段无reading计划，包含3至6个动作组")
        part = canonical["segments"][0]
        result = json.loads(verified["result.json"])
        self.pcm, self.wav = verified["sample.pcm"], verified["sample.wav"]
        self.pcm_sha256, self.wav_sha256 = _sha(self.pcm), _sha(self.wav)
        self.audio_seconds = len(self.pcm) / (self.RATE * 2)
        if (not isinstance(result, dict) or not self.pcm or len(self.pcm) % 2
                or not 27 <= self.audio_seconds <= 30 or result.get("status") != "ok"
                or result.get("text") != part["text"]
                or result.get("pcm_sha256") != self.pcm_sha256
                or result.get("wav_sha256") != self.wav_sha256):
            raise ValueError("准备音频必须真实完成且为27至30秒（约30秒的10%容差），台词和哈希一致")
        seconds = result.get("audio_seconds")
        if (type(seconds) not in (int, float) or not math.isfinite(seconds)
                or abs(seconds - self.audio_seconds) > .001):
            raise ValueError("准备音频报告时长与PCM字节不一致")
        with wave.open(io.BytesIO(self.wav)) as wav:
            if ((wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype())
                    != (1, 2, self.RATE, "NONE") or wav.readframes(self.RATE * 31) != self.pcm):
                raise ValueError("准备WAV格式或内容与PCM不一致")
        self._alignment = copy.deepcopy(result.get("alignment"))
        validate_alignment(self._alignment, part["text"], self.audio_seconds,
                           pcm_sha256=self.pcm_sha256)
        self.avatar_id = manifest["avatar_id"]
        self.image_sha256 = _sha(verified["image.png"])
        if (not isinstance(self.avatar_id, str) or not re.fullmatch(r"[0-9]{1,64}", self.avatar_id)
                or manifest["image_sha256"] != self.image_sha256):
            raise ValueError("准备素材的数字人ID或底图哈希无效")
        self.image_path = self.source_run / "image.png"
        self._original_plan = verified["prepared-plan.json"]
        self._plan = copy.deepcopy(canonical)
        replay_part = self._plan["segments"][0]
        replay_part.update(alignment=copy.deepcopy(self._alignment),
            timeline=compile_semantic_timeline(part["actions"], part["recovery"], self._alignment,
                self.audio_seconds, action_kind="S", ge_lead_seconds=self.ge_lead_seconds))
        self.original_actions = [motion["description"] for action in part["actions"] for motion in action["motions"]]
        self._replay_plan_bytes = _json_bytes(self._plan)
        self.fingerprint = _sha(_json_bytes({"source_kind": self.source_kind,
            "source_files": self._source_hashes, "plan_sha256": _sha(self._replay_plan_bytes),
            "ge_lead_seconds": self.ge_lead_seconds, "avatar_id": self.avatar_id,
            "image_sha256": self.image_sha256}))

    def public(self):
        return {**super().public(), "source_kind": self.source_kind,
                "action_group_count": len(self._plan["segments"][0]["actions"]),
                "preparation_schema": self.SCHEMA,
                "preparation_manifest_sha256": _sha(self._preparation_manifest)}

    def _snapshot_files(self):
        return {**self._prepared_files, "preparation-manifest.json": self._preparation_manifest,
                "replay-plan.json": self._replay_plan_bytes, "alignment.json": _json_bytes(self._alignment)}


class PreparedMinuteSemanticReplay(PreparedSemanticReplay):
    """One continuous playback assembled from two independently verified TTS calls."""
    source_kind = "prepared_minute"
    SCHEMA = "prepared-minute-semantic-replay-v1"
    max_audio_seconds = 60
    max_text_codepoints = 240
    FILE_LIMITS = {"prepared-plan.json": 100_000, "image.png": 20_000_000,
        **{f"segment-{number}/{name}": limit for number in (1, 2) for name, limit in
           (("result.json", 300_000), ("sample.pcm", 24000 * 2 * 30),
            ("sample.wav", 24000 * 2 * 30 + 100_000))}}

    def __init__(self, preparation: Path):
        from tts_live import MODEL, VOICE
        manifest, verified = self._load_preparation(preparation, 1.1)
        original = json.loads(verified["prepared-plan.json"])
        canonical = parse_historical_semantic_plan(original)  # Each source segment retains the ordinary 120-character contract.
        parts = canonical["segments"]
        if (canonical != original or canonical["reading"] is not None or len(parts) != 2
                or any(len(part["actions"]) != 3 for part in parts)
                or parts[0]["instruction"] != parts[1]["instruction"]
                or parts[0]["recovery"] != parts[1]["recovery"]):
            raise ValueError("一分钟素材必须为规范双段、每段三个动作，且语音指令和恢复目标一致")
        pcms, words, provenance, offset_ms = [], [], [], 0
        for number, part in enumerate(parts, 1):
            prefix = f"segment-{number}/"
            result = json.loads(verified[prefix + "result.json"])
            pcm, wav_bytes = verified[prefix + "sample.pcm"], verified[prefix + "sample.wav"]
            seconds = len(pcm) / (self.RATE * 2)
            if (not isinstance(result, dict) or not pcm or len(pcm) % 48 or not 0 < seconds <= 30
                    or result.get("status") != "ok" or result.get("text") != part["text"]
                    or result.get("pcm_sha256") != _sha(pcm) or result.get("wav_sha256") != _sha(wav_bytes)
                    or result.get("model") != MODEL or result.get("voice") != VOICE
                    or type(result.get("audio_seconds")) not in (int, float)
                    or not math.isfinite(result["audio_seconds"])
                    or abs(result["audio_seconds"] - seconds) > .001):
                raise ValueError("分段TTS的台词、模型音色、整毫秒边界、时长或哈希不一致")
            with wave.open(io.BytesIO(wav_bytes)) as wav:
                if ((wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype())
                        != (1, 2, self.RATE, "NONE") or wav.readframes(self.RATE * 31) != pcm):
                    raise ValueError("分段WAV与原PCM不一致")
            alignment = result.get("alignment")
            validate_alignment(alignment, part["text"], seconds, pcm_sha256=_sha(pcm))
            # Offset only measured provider timestamps. Never interpolate within
            # a word or pretend that the concatenation came from one TTS call.
            words.extend({"text": word["text"], "begin_time": word["begin_time"] + offset_ms,
                          "end_time": word["end_time"] + offset_ms,
                          "source_segment": number, "source_word_index": index}
                         for index, word in enumerate(alignment["words"]))
            provenance.append({"segment_index": number, "offset_seconds": offset_ms / 1000,
                "audio_seconds": seconds, "pcm_sha256": _sha(pcm), "wav_sha256": _sha(wav_bytes),
                "result_sha256": _sha(verified[prefix + "result.json"]),
                "model": MODEL, "voice": VOICE, "word_count": len(alignment["words"])})
            pcms.append(pcm)
            offset_ms += len(pcm) // 48
        self.pcm = b"".join(pcms)
        self.audio_seconds = len(self.pcm) / (self.RATE * 2)
        if not 54 <= self.audio_seconds <= 58.5:
            raise ValueError("一分钟素材的原始合并语音必须为54至58.5秒，不变速或补静音")
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setparams((1, 2, self.RATE, 0, "NONE", "not compressed"))
            wav.writeframes(self.pcm)
        self.wav = buffer.getvalue()
        self.pcm_sha256, self.wav_sha256 = _sha(self.pcm), _sha(self.wav)
        text = "".join(part["text"] for part in parts)
        if len(text) > self.max_text_codepoints:
            raise ValueError("一分钟合并台词不得超过240字")
        merged_actions, normalized_prefix = [], 0
        for part in parts:
            for action in part["actions"]:
                action = copy.deepcopy(action)
                quote = action["anchor"]["quote"]
                local_offset = _occurrences(_normalized(part["text"]), quote)[action["anchor"]["occurrence"] - 1]
                action["anchor"]["occurrence"] = _occurrences(_normalized(text), quote).index(normalized_prefix + local_offset) + 1
                merged_actions.append(action)
            normalized_prefix += len(_normalized(part["text"]))
        self._alignment = {"status": "ok", "timebase": "audio_start", "pcm_sha256": self.pcm_sha256,
            "words": words, "source_kind": "concatenated_tts", "source_segments": provenance}
        self._transport_limits = {"max_audio_seconds": self.max_audio_seconds,
                                  "max_text_codepoints": self.max_text_codepoints}
        validate_alignment(self._alignment, text, self.audio_seconds, pcm_sha256=self.pcm_sha256,
                           **self._transport_limits)
        timeline = compile_semantic_timeline(merged_actions, parts[0]["recovery"], self._alignment,
            self.audio_seconds, action_kind="S", ge_lead_seconds=1.1, **self._transport_limits)
        self.avatar_id = manifest["avatar_id"]
        self.image_sha256 = _sha(verified["image.png"])
        if (not isinstance(self.avatar_id, str) or not re.fullmatch(r"[0-9]{1,64}", self.avatar_id)
                or manifest["image_sha256"] != self.image_sha256):
            raise ValueError("一分钟素材的数字人ID或底图哈希无效")
        self.image_path = self.source_run / "image.png"
        self.comment_id = "prepared-minute"
        self._original_plan = verified["prepared-plan.json"]
        self._plan = {**copy.deepcopy(canonical), "segments": [{"text": text,
            "instruction": parts[0]["instruction"], "actions": merged_actions,
            "recovery": copy.deepcopy(parts[0]["recovery"]), "alignment": copy.deepcopy(self._alignment),
            "timeline": timeline}]}
        self.original_actions = [motion["description"] for action in merged_actions for motion in action["motions"]]
        self._replay_plan_bytes = _json_bytes(self._plan)
        self._concatenation = {"source_kind": self.source_kind, "tts_calls": 2,
            "model": MODEL, "voice": VOICE, "source_segments": provenance,
            "combined_pcm_sha256": self.pcm_sha256, "combined_wav_sha256": self.wav_sha256,
            "audio_seconds": self.audio_seconds, "speed_changed": False, "silence_added": False}
        self.fingerprint = _sha(_json_bytes({"source_kind": self.source_kind,
            "source_files": self._source_hashes, "plan_sha256": _sha(self._replay_plan_bytes),
            "ge_lead_seconds": 1.1, **self._transport_limits,
            "avatar_id": self.avatar_id, "image_sha256": self.image_sha256}))

    def public(self):
        return {**super().public(), "comparison_label": "A · 1分钟六动作", "minute_replay": True,
            **self._transport_limits, "source_tts_segments": 2,
            "source_segment_seconds": [row["audio_seconds"] for row in self._concatenation["source_segments"]]}

    def _snapshot_files(self):
        return {**super()._snapshot_files(), "sample.pcm": self.pcm, "sample.wav": self.wav,
                "concatenation.json": _json_bytes(self._concatenation)}

    def prepare(self, out, comment, part, label):
        pcm, folder = super().prepare(out, comment, part, label)
        self._write_new(folder / "concatenation.json", _json_bytes(self._concatenation))
        return pcm, folder
