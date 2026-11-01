"""Pure game-facing semantic preparation; no model, TTS, session or asset client.

Rules are exact local copies of avatar-studio at 2e076b66. This module ports only
LiveSession.prepare_timed_actions' immutable-content boundary. It is NOT a
configured real-media adapter. See docs/m4-avatar-compatibility.md.
"""
from dataclasses import dataclass
import hashlib
import json
from typing import Callable

from .avatar_rules.action_schema import (
    ACTION_POST_SEND_SECONDS, QUIET_DELAY_SECONDS, QUIET_INSTRUCTION,
    _text, _validate_recovery,
)
from .avatar_rules.semantic_speech import (
    DIALOGUE_ACTION_KIND, DIALOGUE_GE_LEAD_SECONDS, DIALOGUE_S_TO_Q_SECONDS,
    LocalActionAlignment, SemanticSpeechError, compile_dialogue_semantic_timeline,
    parse_dialogue_semantic_plan, validate_dialogue_actions,
)

PCM_RATE = 24000
PCM_CHANNELS = 1
PCM_SAMPLE_BYTES = 2


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
                      allow_nan=False)


def _safe_text(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 120 or '\x00' in text:
        raise ValueError('invalid_frozen_text')
    text.encode('utf-8')


@dataclass(frozen=True)
class FrozenStageOne:
    """Serialized value ownership prevents mutation through nested dict aliases."""
    content_json: str

    @property
    def content(self):
        return json.loads(self.content_json)


def freeze_stage1(frozen_text: str, plan: dict) -> FrozenStageOne:
    """Validate model expression suggestions but never rewrite game speech.

    Legacy _text normalizes whitespace for Studio callers. We use its checks,
    retain original exact strings, and never adopt its stripped return value.
    """
    _safe_text(frozen_text)
    if not isinstance(plan, dict) or set(plan) != {'text', 'instruction', 'actions', 'recovery'}:
        raise ValueError('invalid_stage1_fields')
    if plan['text'] != frozen_text:
        raise ValueError('frozen_text_changed')
    _text(plan['instruction'], 160, 'instruction')
    actions = validate_dialogue_actions(plan['actions'], frozen_text)
    _validate_recovery(plan['recovery'], actions)
    return FrozenStageOne(_json(plan))


@dataclass(frozen=True)
class PreparedSemantic:
    content_json: str
    alignment_json: str
    timeline_json: str
    pcm: bytes
    pcm_sha256: str
    audio_seconds: float

    @property
    def content(self):
        return json.loads(self.content_json)

    @property
    def alignment(self):
        return json.loads(self.alignment_json)

    @property
    def timeline(self):
        return json.loads(self.timeline_json)


def _pcm(pcm, *, sample_rate=PCM_RATE, channels=PCM_CHANNELS, sample_format='s16le'):
    # Bytes alone cannot establish endianness; the injected synthesizer must
    # explicitly attest the unmodified decoded format alongside the bytes.
    if (type(pcm) is not bytes or not pcm or len(pcm) % PCM_SAMPLE_BYTES or
            type(sample_rate) is not int or sample_rate != PCM_RATE or
            type(channels) is not int or channels != PCM_CHANNELS or sample_format != 's16le'):
        raise ValueError('invalid_pcm_format')
    seconds = len(pcm) / (PCM_RATE * PCM_SAMPLE_BYTES)
    if seconds > 30:
        raise ValueError('audio_too_long')
    return hashlib.sha256(pcm).hexdigest(), seconds


def bind_audio(stage1: FrozenStageOne, pcm: bytes, alignment, stage2=None, *,
               sample_rate=PCM_RATE, channels=PCM_CHANNELS, sample_format='s16le') -> PreparedSemantic:
    """Bind exact PCM to verified original anchors and compile before sending.

    stage2, when needed, is the complete four-field plan. Only anchors may
    differ. No synthesis, padding, cropping or invented timestamps occurs here.
    """
    original = stage1.content
    freeze_stage1(original['text'], original)
    digest, seconds = _pcm(pcm, sample_rate=sample_rate, channels=channels,
                           sample_format=sample_format)
    if not original['actions']:
        if stage2 is not None:
            raise ValueError('unnecessary_stage2')
        return PreparedSemantic(_json(original), _json(None), _json([]), pcm, digest, seconds)
    local = LocalActionAlignment(alignment, original['text'], seconds, pcm_sha256=digest)
    if not isinstance(stage2, dict) or set(stage2) != set(original):
        raise ValueError('stage2_required')
    if any(stage2[k] != original[k] for k in ('text', 'instruction', 'recovery')):
        raise ValueError('stage2_content_changed')
    actions = stage2['actions']
    if (not isinstance(actions, list) or len(actions) != len(original['actions']) or
            any(not isinstance(new, dict) or
                {k: v for k, v in new.items() if k != 'anchor'} !=
                {k: v for k, v in old.items() if k != 'anchor'}
                for new, old in zip(actions, original['actions']))):
        raise ValueError('stage2_actions_changed')
    local.verify_actions(actions)
    timeline = compile_dialogue_semantic_timeline(actions, original['recovery'], alignment, seconds)
    return PreparedSemantic(_json(stage2), _json(alignment), _json(timeline), pcm, digest, seconds)


async def prepare_semantic(stage1: FrozenStageOne, synthesize: Callable, plan_anchors: Callable | None,
                           cancelled: Callable = lambda: False) -> PreparedSemantic:
    """Dependency-injected two-stage seam; never installed by the HTTP service.

    synthesize(content) -> {pcm, alignment, sample_rate, channels, sample_format}.
    plan_anchors(content, alignment, seconds) -> complete four-field plan.
    The caller owns authorization and cancellation of its injected providers.
    """
    if cancelled():
        raise ValueError('preparation_cancelled')
    content = stage1.content
    freeze_stage1(content['text'], content)
    audio = await synthesize(stage1.content)
    if cancelled():
        raise ValueError('preparation_cancelled')
    if not isinstance(audio, dict) or set(audio) != {'pcm', 'alignment', 'sample_rate', 'channels', 'sample_format'}:
        raise ValueError('invalid_synthesizer_contract')
    fmt = {k: audio[k] for k in ('sample_rate', 'channels', 'sample_format')}
    digest, seconds = _pcm(audio['pcm'], **fmt)
    stage2 = None
    if content['actions']:
        # Reject wrong-PCM/unusable alignment BEFORE requesting the second model.
        LocalActionAlignment(audio['alignment'], content['text'], seconds, pcm_sha256=digest)
        if not callable(plan_anchors):
            raise ValueError('anchor_planner_not_configured')
        stage2 = await plan_anchors(stage1.content, json.loads(_json(audio['alignment'])), seconds)
        if cancelled():
            raise ValueError('preparation_cancelled')
    return bind_audio(stage1, audio['pcm'], audio['alignment'], stage2, **fmt)


async def send_prepared(prepared: PreparedSemantic, owned_send_aligned_expression: Callable,
                        still_valid: Callable, *, number=1):
    """Delegate to an ALREADY-owned MotionComponent; never implement Type9 here.

    This is an injection seam, not a real adapter readiness claim. The owner must
    verify session epoch, receiver readiness, sole-sender ownership and DB gate.
    Existing MotionComponent keeps Type9 waits outside the PCM socket lock.
    """
    if not callable(owned_send_aligned_expression) or not still_valid():
        raise ValueError('real_media_not_configured_or_gate_closed')
    validate_prepared(prepared)
    content = prepared.content
    return await owned_send_aligned_expression(
        prepared.pcm, content['text'], content['actions'], number,
        recovery=content['recovery'], reading=None, alignment=prepared.alignment,
        action_kind=DIALOGUE_ACTION_KIND, ge_lead_seconds=DIALOGUE_GE_LEAD_SECONDS,
        allow_tail_q_after_audio=True, local_action_anchors=True)


def validate_prepared(prepared: PreparedSemantic):
    """Pure revalidation before paid creation as well as before sending."""
    content = prepared.content
    # Detect even a manually constructed/altered prepared record before effects.
    stage1 = freeze_stage1(content['text'], content)
    checked = bind_audio(stage1, prepared.pcm, prepared.alignment,
                         content if content['actions'] else None)
    if checked != prepared:
        raise ValueError('prepared_evidence_changed')
