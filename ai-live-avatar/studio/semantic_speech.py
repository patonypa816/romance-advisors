"""Local speech anchors and GE/L/Q plans; no cloud calls or playback claims.

Speech times use the answer PCM's first frame as zero. A negative first GE
is a pre-roll instruction, not a negative provider timestamp. Silent A motion
uses a separate relative timeline and never invents audio timestamps.
"""
from __future__ import annotations

import copy
import difflib
import math
import unicodedata

try:
    from .speech_markup import parse_speech_markup
except ImportError:
    from speech_markup import parse_speech_markup

try:
    from .action_schema import ACTION_POST_SEND_SECONDS, PARTS, QUIET_DELAY_SECONDS, _ge, _instruction, _text, _validate_reading, _validate_recovery, compile_timeline
except ImportError:
    from action_schema import ACTION_POST_SEND_SECONDS, PARTS, QUIET_DELAY_SECONDS, _ge, _instruction, _text, _validate_reading, _validate_recovery, compile_timeline


MAX_TEXT = 120
MAX_GROUPS = 6
MIN_L_SECONDS = 3.0  # Local scheduling policy, not a universal visual completion time.
GE_LEAD_SECONDS = 0.5
# The formal dialogue policy is explicit; historical compilers and fixtures
# retain their original L/.5 defaults for reproducible source verification.
DIALOGUE_ACTION_KIND = "S"
DIALOGUE_GE_LEAD_SECONDS = 1.1
DIALOGUE_S_TO_Q_SECONDS = QUIET_DELAY_SECONDS


class SemanticSpeechError(ValueError):
    def __init__(self, code, details=None):
        self.code = code
        self.details = dict(details or {})
        super().__init__({"alignment_unavailable": "字时间或原文锚点不足，未发送本条音画",
                          "anchor_window_conflict": "语义锚点间距或末尾音频不足以容纳完整动作组"}.get(code, code))


TTS_ALIGNMENT_REASON_MESSAGES = {
    "audio_unavailable": "音频未通过核验",
    "multiple_sentences_unverified": "返回了多个尚未确认统一起点的时间轴",
    "stream_audio_invalid": "语音流数据不完整",
    "invalid_sentence": "语音分段信息不符合要求",
    "invalid_sentence_sequence": "语音分段缺失、重复结束或顺序不一致",
    "invalid_timestamps": "词时间或索引不连续、重叠或超出音频范围",
    "inconsistent_snapshots": "前后返回的词时间记录互相矛盾",
    "timestamps_missing": "未返回完整词时间",
    "stream_download_mismatch": "语音流与最终音频不一致",
    "text_mismatch": "时间戳文字与台词内容不一致",
}


def alignment_reason_message(reason):
    """UI text is local and fixed; provider strings never become display text."""
    local = {"anchor_not_covered": "时间戳未覆盖所选短语",
             "anchor_not_contiguous": "所选短语跨过了标点，无法连续匹配",
             "anchor_inside_provider_word": "所选短语位于词内部，没有独立起始时间",
             "anchor_ambiguous": "所选短语重复出现，无法唯一核验动作落点",
             "anchor_correspondence_unverified": "所选短语与原台词的局部顺序无法对应",
             "no_verified_anchor": "没有可独立核验的原文动作落点",
             "pcm_mismatch": "时间戳与本次音频的校验值不一致",
             "pcm_sha256_mismatch": "时间戳与本次音频的校验值不一致",
             "word_text_coverage_mismatch": "时间戳文字未完整覆盖台词"}
    if not isinstance(reason, str):
        return "未取得可核验的完整词时间"
    return TTS_ALIGNMENT_REASON_MESSAGES.get(reason, local.get(reason, "未取得可核验的完整词时间"))


def text_without_whitespace(text):
    # Providers may spell an acronym as A + " I". Ignore only presentation
    # whitespace, preserving punctuation, case, every letter/digit and order.
    return "".join(char for char in text if not char.isspace())


def _normalized(text):
    # Keep letters/digits/symbols unchanged: only punctuation and spacing have
    # a verified correspondence between the script and provider word strings.
    return "".join(c for c in text if not c.isspace() and unicodedata.category(c)[0] not in {"P", "Z"})


def _occurrences(text, quote):
    positions, start = [], 0
    while (position := text.find(quote, start)) >= 0:
        positions.append(position)
        start = position + 1
    return positions


def _continuous_quote(text, normalized_offset, quote, *, provider_whitespace=False):
    positions = [index for index, char in enumerate(text) if _normalized(char)]
    start = positions[normalized_offset]
    end = positions[normalized_offset + len(quote) - 1] + 1
    span = text[start:end]
    return (text_without_whitespace(span) if provider_whitespace else span) == quote


def validate_ssml_action_anchors(text, ssml, actions):
    """发音改写区暂不作为动作锚点；其他原文短语仍可照常使用。"""
    markup = parse_speech_markup(text, ssml)
    ranges = markup["rewritten_ranges"]
    if not ranges or not actions:
        return markup
    positions = [index for index, char in enumerate(text) if _normalized(char)]
    normalized = _normalized(text)
    for action in actions:
        anchor = action["anchor"]
        matches = _occurrences(normalized, anchor["quote"])
        occurrence = anchor["occurrence"]
        if not 1 <= occurrence <= len(matches):
            raise ValueError("SSML动作锚点不在原文中")
        start = matches[occurrence - 1]
        left, right = positions[start], positions[start + len(anchor["quote"]) - 1] + 1
        if any(left < span["end"] and right > span["start"] for span in ranges):
            raise ValueError("SSML动作锚点需落在未改写读法的原文短语上")
    return markup


def speech_transcription(text, ssml, alignment, audio_seconds, pcm_sha256):
    """字幕保留text；数字人转写使用明确读法或本次音频的实测词文字。"""
    markup = parse_speech_markup(text, ssml)
    spoken = markup["spoken_text"]
    if spoken is None:
        # say-as的展开由供应商决定，不在本地猜日期/数字/名字的读法。
        validate_alignment(alignment, None, audio_seconds, pcm_sha256=pcm_sha256)
        spoken = "".join(word["text"] for word in alignment["words"])
    if (not isinstance(spoken, str) or not spoken.strip() or len(text_without_whitespace(spoken)) > MAX_TEXT
            or any(char in spoken for char in "<>[]［］\ufffd")):
        raise ValueError("SSML实际朗读文字不可用")
    return spoken


def _actions(value, text=None):
    if not isinstance(value, list) or len(value) > MAX_GROUPS:
        raise ValueError("语义actions必须为0至6组的数组")
    clean = []
    for node in value:
        if not isinstance(node, dict) or set(node) != {"anchor", "motions", "gaze", "expression"}:
            raise ValueError("语义动作组只能包含anchor、motions、gaze、expression，不接受秒数或kind")
        anchor = node["anchor"]
        if not isinstance(anchor, dict) or set(anchor) != {"quote", "occurrence"}:
            raise ValueError("anchor必须且只能包含quote与occurrence")
        quote = _text(anchor["quote"], MAX_TEXT, "anchor.quote")
        if quote != _normalized(quote):
            raise ValueError("anchor.quote必须是不含标点或空白的原文连续短语")
        occurrence = anchor["occurrence"]
        if type(occurrence) is not int or occurrence < 1:
            raise ValueError("anchor.occurrence必须为从1开始的整数")
        if text is not None:
            if quote not in text:
                raise ValueError("anchor.quote必须是本段台词的原文连续子串")
            matches = _occurrences(_normalized(text), quote)
            if len(matches) < occurrence:
                raise ValueError("anchor.occurrence超过本段台词中的规范化匹配次数")
            if not _continuous_quote(text, matches[occurrence - 1], quote):
                raise ValueError("anchor.occurrence选中的短语必须在原文连续存在，不得跨标点拼接")
        motions = node["motions"]
        if not isinstance(motions, list) or not 1 <= len(motions) <= 2:
            raise ValueError("每个语义L组必须保留1至2个动作单元")
        checked, resources = [], set()
        for motion in motions:
            if not isinstance(motion, dict) or set(motion) != {"part", "description"}:
                raise ValueError("每个motion必须且只能包含part和description")
            if not isinstance(motion["part"], str) or motion["part"] not in PARTS:
                raise ValueError("motion.part不在允许的身体部位中")
            part = motion["part"]
            occupied = {"left_arm", "right_arm"} if part == "both_arms" else {part}
            if resources & occupied or (resources and (part == "whole_body" or "whole_body" in resources)):
                raise ValueError("同一语义L组的同时动作存在身体部位冲突")
            resources.update(occupied)
            description = _text(motion["description"], 70, "动作描述")
            checked.append({"part": motion["part"], "description": description})
        group = {"anchor": {"quote": quote, "occurrence": occurrence}, "motions": checked,
                 "gaze": _ge(node["gaze"], "gaze"), "expression": _ge(node["expression"], "expression")}
        # Check the eventual transport limit before paying for any TTS.
        _instruction("，同时".join(m["description"] for m in checked), group["gaze"], group["expression"])
        clean.append(group)
    return clean


def _normalize_input_actions(actions):
    """Accept only the observed, unambiguous flat anchor layout at input.

    The provider sometimes places occurrence beside a string anchor. Moving
    those same values into an object changes no meaning; all value validation
    still happens below. Unknown or mixed layouts remain errors.
    """
    if not isinstance(actions, list):
        return actions
    normalized = []
    for node in actions:
        if (isinstance(node, dict) and
                set(node) == {"anchor", "occurrence", "motions", "gaze", "expression"} and
                isinstance(node["anchor"], str)):
            normalized.append({"anchor": {"quote": node["anchor"], "occurrence": node["occurrence"]},
                               **{key: node[key] for key in ("motions", "gaze", "expression")}})
        else:
            normalized.append(node)
    return normalized


def parse_semantic_plan(body, source_text=None, mode=None):
    """Validate the normal speech contract without accepting model timing.

    The trusted caller chooses mode. Archived semantic fixtures use the
    explicit historical adapter below; pure motion has its own parser.
    """
    if mode not in (None, "once", "topic"):
        raise ValueError("语义台词只接受观众、一次安排或主讲话题")
    fields = {"emotion", "reading", "segments"}
    if not isinstance(body, dict) or set(body) != fields:
        raise ValueError("语义计划必须且只能包含emotion、reading、segments")
    reading = _validate_reading(body["reading"], source_text)
    if mode is not None and reading is not None:
        raise ValueError("主持指令不得使用reading，必须为null")
    segments = body["segments"]
    if not isinstance(segments, list) or not 1 <= len(segments) <= 2:
        raise ValueError("每轮需要1至2段完整台词")
    clean = []
    for index, segment in enumerate(segments):
        if (not isinstance(segment, dict) or not {"text", "instruction", "actions", "recovery"} <= set(segment)
                or set(segment) - {"text", "instruction", "actions", "recovery", "ssml"}):
            raise ValueError("每段必须且只能包含text、instruction、actions、recovery")
        text = _text(segment["text"], MAX_TEXT, "text")
        actions = _actions(_normalize_input_actions(segment["actions"]), text)
        validate_ssml_action_anchors(text, segment.get("ssml"), actions)
        recovery = _validate_recovery(segment["recovery"], actions, required=index == 0 and reading is not None)
        clean.append({"text": text, "instruction": _text(segment["instruction"], 160, "语音instruction"),
                      "actions": actions, "recovery": recovery})
        if segment.get("ssml") is not None:
            clean[-1]["ssml"] = segment["ssml"]
    return {"emotion": _text(body["emotion"], 24, "emotion"),
            "reading": reading, "segments": clean}


def parse_historical_semantic_plan(body, source_text=None, mode=None):
    """Read archived experiments without changing their original plan or hash.

    Observation belongs to that stored format only; current generation never
    accepts or invents it. Validate the old value before adapting at this edge.
    """
    fields = {"emotion", "observation", "reading", "segments"}
    if not isinstance(body, dict) or set(body) != fields:
        raise ValueError("历史语义计划必须且只能包含emotion、observation、reading、segments")
    observation = _text(body["observation"], 300, "observation")
    parsed = parse_semantic_plan({key: value for key, value in body.items() if key != "observation"},
                                 source_text=source_text, mode=mode)
    return {"emotion": parsed["emotion"], "observation": observation,
            "reading": parsed["reading"], "segments": parsed["segments"]}


def _dialogue_parse_error(error):
    reasons = {
        "每个语义L组必须保留1至2个动作单元": "每个语义S组必须保留1至2个动作单元",
        "同一语义L组的同时动作存在身体部位冲突":
            "同一语义S组的同时动作存在身体部位冲突",
    }
    return ValueError(reasons.get(str(error), str(error)))


def validate_dialogue_actions(actions, text=None):
    """Reject unsupported S resources before any TTS or second model request."""
    try:
        clean = _actions(actions, text)
    except ValueError as error:
        raise _dialogue_parse_error(error) from None
    if any(motion["part"] == "whole_body" for group in clean for motion in group["motions"]):
        raise ValueError("A方案S短动作不支持whole_body；不得把全身动作伪装为局部动作")
    return clean


def parse_dialogue_semantic_plan(body, source_text=None, mode=None):
    """Formal dialogue uses A short gestures; historical parsing is unchanged."""
    try:
        parsed = parse_semantic_plan(body, source_text=source_text, mode=mode)
    except ValueError as error:
        raise _dialogue_parse_error(error) from None
    for part in parsed["segments"]:
        validate_dialogue_actions(part["actions"], part["text"])
    return parsed


def compile_dialogue_semantic_timeline(actions, recovery, alignment, audio_seconds):
    """The ordinary 30s/120-character A path, without minute-mode overrides."""
    clean = validate_dialogue_actions(actions)
    return compile_semantic_timeline(clean, recovery, alignment, audio_seconds,
        action_kind=DIALOGUE_ACTION_KIND, ge_lead_seconds=DIALOGUE_GE_LEAD_SECONDS,
        allow_tail_q_after_audio=True)


def compile_dialogue_motion_timeline(actions, recovery):
    """无声A表演沿用GE/S/Q职责与间隔，不伪造音频、锚点或字时间。"""
    timeline = compile_timeline(actions, recovery)
    if (len(actions) != 1 or actions[0]["kind"] != DIALOGUE_ACTION_KIND
            or actions[0]["delay_seconds"] != 0):
        raise ValueError("无声A表演只接受一个立即执行的短S动作及收束")
    if any(motion["part"] == "whole_body" for motion in actions[0]["motions"]):
        raise ValueError("A方案S短动作不支持whole_body")
    node = timeline[0]
    lead = {"role": "GE", "kind": "GE", "index": 1, "at_seconds": 0.0,
            "delay_seconds": 0.0, "motions": [], "hold_seconds": 0.0,
            "instruction": _instruction(None, node["gaze"], node["expression"]),
            "gaze": node["gaze"], "expression": node["expression"]}
    node.update(index=2, at_seconds=DIALOGUE_GE_LEAD_SECONDS, delay_seconds=DIALOGUE_GE_LEAD_SECONDS)
    timeline[1].update(index=3, at_seconds=DIALOGUE_GE_LEAD_SECONDS + DIALOGUE_S_TO_Q_SECONDS,
                       delay_seconds=DIALOGUE_S_TO_Q_SECONDS)
    return [lead, *timeline]


def _semantic_limits(max_audio_seconds=30, max_text_codepoints=MAX_TEXT):
    # Only the explicitly verified minute fixture gets the paired larger scope.
    # Callers cannot enlarge normal speech a little at a time or by one field.
    if (type(max_audio_seconds) is not int or type(max_text_codepoints) is not int
            or (max_audio_seconds, max_text_codepoints) not in ((30, MAX_TEXT), (60, 240))):
        raise ValueError("语义范围只能为30秒/120字或显式分钟素材60秒/240字")
    return max_audio_seconds, max_text_codepoints


def _seconds(value, *, max_audio_seconds=30):
    if type(max_audio_seconds) is not int or max_audio_seconds not in (30, 60):
        raise ValueError("语义音频范围只能为30秒或60秒")
    try:
        seconds = float(value) if not isinstance(value, bool) and isinstance(value, (int, float)) else float("nan")
    except (OverflowError, ValueError):
        seconds = float("nan")
    if not math.isfinite(seconds) or seconds <= 0 or seconds > max_audio_seconds:
        raise SemanticSpeechError("alignment_unavailable", {"reason": "invalid_audio_seconds"})
    return seconds


def validate_alignment(alignment, text, audio_seconds, *, pcm_sha256=None,
                       max_audio_seconds=30, max_text_codepoints=MAX_TEXT):
    """Require measured, audio-start word times and optionally bind them to PCM.

    Provider words stay intact. In particular, we never interpolate a start
    inside a multi-character word or infer sentence-relative time origins.
    """
    _semantic_limits(max_audio_seconds, max_text_codepoints)
    seconds = _seconds(audio_seconds, max_audio_seconds=max_audio_seconds)
    def unavailable(reason):
        raise SemanticSpeechError("alignment_unavailable", {"reason": reason})
    if isinstance(alignment, dict) and alignment.get("status") == "unavailable":
        reason = alignment.get("reason")
        if isinstance(reason, str) and reason in TTS_ALIGNMENT_REASON_MESSAGES:
            unavailable(reason)
    if not isinstance(alignment, dict) or alignment.get("status") != "ok" or alignment.get("timebase") != "audio_start":
        unavailable("verified_audio_start_alignment_required")
    if pcm_sha256 is not None and alignment.get("pcm_sha256") != pcm_sha256:
        unavailable("pcm_sha256_mismatch")
    words = alignment.get("words")
    if not isinstance(words, list) or not words:
        unavailable("word_timestamps_missing")
    last_begin = last_end = -1
    for word in words:
        if not isinstance(word, dict) or not isinstance(word.get("text"), str) or not word["text"]:
            unavailable("invalid_word_text")
        begin, end = word.get("begin_time"), word.get("end_time")
        if (type(begin) is not int or type(end) is not int or begin < 0 or end < begin
                or begin < last_begin or end < last_end or end > seconds * 1000 + 1):
            unavailable("invalid_word_timestamps")
        last_begin, last_end = begin, end
    joined = "".join(w["text"] for w in words)
    if len(text_without_whitespace(joined)) > max_text_codepoints:
        unavailable("word_text_coverage_mismatch")
    if text is not None and (not isinstance(text, str) or not 1 <= len(text.strip()) <= max_text_codepoints
                             or _normalized(joined) != _normalized(text)):
        unavailable("word_text_coverage_mismatch")
    return alignment


class LocalActionAlignment:
    """Verify only actual anchors while retaining the provider's words/times.

    Matching blocks locate monotonic exact spans; they do not prove which
    occurrence a repeated phrase means. Both sides must independently be unique.
    Historical replay validation above deliberately keeps its full-text check.
    """

    def __init__(self, alignment, text, audio_seconds, *, pcm_sha256=None):
        validate_alignment(alignment, None, audio_seconds, pcm_sha256=pcm_sha256)
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_TEXT:
            raise SemanticSpeechError("alignment_unavailable", {"reason": "word_text_coverage_mismatch"})
        self.text, self.words = text, alignment["words"]
        self.source = _normalized(text)
        self.raw_provider = "".join(word["text"] for word in self.words)
        self.provider = _normalized(self.raw_provider)
        self.boundaries = {}
        offset = prior_end = cursor = 0
        indexed = any("begin_index" in word or "end_index" in word for word in self.words)
        for index, word in enumerate(self.words):
            if (not word["text"].strip() or word["begin_time"] < prior_end
                    or word["end_time"] <= word["begin_time"]):
                raise SemanticSpeechError("alignment_unavailable", {"reason": "invalid_timestamps"})
            if indexed:
                begin, end = word.get("begin_index"), word.get("end_index")
                if type(begin) is not int or type(end) is not int or begin != cursor or end <= begin:
                    raise SemanticSpeechError("alignment_unavailable", {"reason": "invalid_timestamps"})
                cursor = end  # Provider index units need not equal Chinese characters.
            normalized = _normalized(word["text"])
            if normalized:
                self.boundaries[offset] = index
            offset += len(normalized)
            prior_end = word["end_time"]
        self.blocks = difflib.SequenceMatcher(a=self.source, b=self.provider, autojunk=False).get_matching_blocks()

    def verify_anchor(self, anchor):
        quote, occurrence = anchor["quote"], anchor["occurrence"]
        sources, providers = _occurrences(self.source, quote), _occurrences(self.provider, quote)
        def unavailable(reason):
            raise SemanticSpeechError("alignment_unavailable", {"reason": reason})
        if not sources or not providers:
            unavailable("anchor_not_covered")
        if len(sources) != 1 or len(providers) != 1 or occurrence != 1:
            unavailable("anchor_ambiguous")
        source, provider = sources[0], providers[0]
        if not any(block.a <= source and source + len(quote) <= block.a + block.size
                   and provider == block.b + source - block.a for block in self.blocks):
            unavailable("anchor_correspondence_unverified")
        if (not _continuous_quote(self.text, source, quote) or
                not _continuous_quote(self.raw_provider, provider, quote, provider_whitespace=True)):
            unavailable("anchor_not_contiguous")
        if provider not in self.boundaries:
            unavailable("anchor_inside_provider_word")
        index = self.boundaries[provider]
        return {"source_offset": source, "provider_offset": provider, "word_index": index,
                "s_at_seconds": self.words[index]["begin_time"] / 1000}

    def verify_actions(self, actions):
        return [self.verify_anchor(action["anchor"]) for action in validate_dialogue_actions(actions, self.text)]


def compile_semantic_timeline(actions, recovery, alignment, audio_seconds, *, action_kind="L",
                              ge_lead_seconds=GE_LEAD_SECONDS, max_audio_seconds=30,
                              max_text_codepoints=MAX_TEXT, allow_tail_q_after_audio=False):
    """Compile all requested groups or reject before any transport frame.

    L keeps its full window and final audio-tail Q. Explicit S replay instead
    returns to Q after 1.5 seconds while the original audio keeps playing.
    Ordinary A dialogue explicitly allows its final Q after audio, while all
    active S word starts remain inside speech. Fixed replays keep the default.
    Neither mode moves anchors or shortens Q's gap before the next GE.
    """
    _semantic_limits(max_audio_seconds, max_text_codepoints)
    if max_audio_seconds == 60 and (action_kind != "S" or ge_lead_seconds != 1.1):
        raise ValueError("分钟素材只允许显式S与1.1秒GE前导")
    if not isinstance(action_kind, str) or action_kind not in {"L", "S"}:
        raise ValueError("语义动作种类只能为L或S")
    if ge_lead_seconds is not None and (type(ge_lead_seconds) not in (int, float)
                                        or ge_lead_seconds not in (.5, 1.1)):
        raise ValueError("语义GE前导只能为0.5秒、1.1秒或None")
    if action_kind != "S" and ge_lead_seconds != GE_LEAD_SECONDS:
        raise ValueError("非默认GE前导只允许S固定对照")
    if (type(allow_tail_q_after_audio) is not bool or
            (allow_tail_q_after_audio and (action_kind != DIALOGUE_ACTION_KIND or
             ge_lead_seconds != DIALOGUE_GE_LEAD_SECONDS or max_audio_seconds != 30))):
        raise ValueError("音频后末尾Q只允许正常A方案")
    clean = _actions(actions)
    if action_kind == "S" and any(m["part"] == "whole_body" for g in clean for m in g["motions"]):
        raise ValueError("S不得使用whole_body")
    # A reading-only prelude still needs its recovery although the answer has
    # no active groups. The plan parser owns whether reading is present.
    recovery = _validate_recovery(recovery, clean, required=recovery is not None)
    if not clean:
        return []
    seconds = _seconds(audio_seconds, max_audio_seconds=max_audio_seconds)
    validate_alignment(alignment, None, seconds, max_audio_seconds=max_audio_seconds,
                       max_text_codepoints=max_text_codepoints)
    full_text, boundaries = "", {}
    original_text = "".join(word["text"] for word in alignment["words"])
    for word in alignment["words"]:
        normalized = _normalized(word["text"])
        if normalized:
            boundaries[len(full_text)] = word["begin_time"] / 1000
            full_text += normalized
    starts = []
    for index, group in enumerate(clean, 1):
        anchor = group["anchor"]
        matches = _occurrences(full_text, anchor["quote"])
        if len(matches) < anchor["occurrence"]:
            raise SemanticSpeechError("alignment_unavailable", {"reason": "anchor_not_covered", "group_index": index})
        offset = matches[anchor["occurrence"] - 1]
        if not _continuous_quote(original_text, offset, anchor["quote"], provider_whitespace=True):
            raise SemanticSpeechError("alignment_unavailable", {"reason": "anchor_not_contiguous", "group_index": index})
        if offset not in boundaries:
            raise SemanticSpeechError("alignment_unavailable", {"reason": "anchor_inside_provider_word", "group_index": index})
        starts.append(boundaries[offset])
    timeline = []
    for index, (group, start) in enumerate(zip(clean, starts), 1):
        minimum = MIN_L_SECONDS if action_kind == "L" else QUIET_DELAY_SECONDS
        q_at = seconds if action_kind == "L" and index == len(clean) else start + minimum
        next_entry = (starts[index] - (ge_lead_seconds or 0.0)) if index < len(clean) else None
        # Only the final recovery may outlast speech. Active S still needs an
        # actual word start before the audio ends, including zero-duration words.
        tail_q_allowed = allow_tail_q_after_audio and index == len(clean)
        # Q也是Type9；下一组最早的节点（GE，或无GE时的S）须在Q后留足1秒。
        if ((allow_tail_q_after_audio and start >= seconds) or
                (q_at > seconds + 1e-9 and not tail_q_allowed) or q_at - start < minimum - 1e-9
                or (next_entry is not None and next_entry - q_at < ACTION_POST_SEND_SECONDS - 1e-9)):
            raise SemanticSpeechError("anchor_window_conflict", {
                "group_index": index, action_kind.lower() + "_at_seconds": start, "q_at_seconds": q_at,
                "next_ge_at_seconds": next_entry if ge_lead_seconds is not None else None,
                **({"next_s_at_seconds": next_entry} if ge_lead_seconds is None else {}),
                "minimum_" + action_kind.lower() + "_seconds": minimum,
                "minimum_post_send_seconds": ACTION_POST_SEND_SECONDS,
                "audio_seconds": seconds})
        ge = _instruction(None, group["gaze"], group["expression"])
        active = "，同时".join(m["description"] for m in group["motions"])
        entries = [(action_kind, start, group["motions"], _instruction(active, group["gaze"], group["expression"]),
                    group, q_at - start if action_kind == "L" else 0.0),
                   ("Q", q_at, [], _instruction(None, recovery["gaze"], recovery["expression"]), recovery, 0.0)]
        if ge_lead_seconds is not None:
            entries.insert(0, ("GE", start - ge_lead_seconds, [], ge, group, 0.0))
        for role, at, motions, instruction, gaze_expression, hold in entries:
            timeline.append({"role": role, "kind": role, "index": len(timeline) + 1,
                             "group_index": index, "at_seconds": at, "audio_at_seconds": at,
                             "delay_seconds": 0.0 if not timeline else at - timeline[-1]["at_seconds"],
                             "motions": copy.deepcopy(motions), "instruction": instruction,
                             "hold_seconds": hold, "gaze": gaze_expression["gaze"],
                             "expression": gaze_expression["expression"], "anchor": dict(group["anchor"])})
    return timeline
