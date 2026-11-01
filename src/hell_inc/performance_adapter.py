"""Offline protocol adapter. Nothing here creates or plays media."""
from dataclasses import dataclass
import hashlib
import inspect
import json
from typing import Protocol


@dataclass(frozen=True)
class FixturePrepared:
    text: str
    intent: str
    content_hash: str


class PerformanceAdapter(Protocol):
    async def prepare(self, frozen_segment: dict, cancellation): ...
    async def send(self, prepared, session: dict, still_valid): ...


async def _hook(hook, *args):
    if hook is not None:
        result = hook(*args)
        if inspect.isawaitable(result):
            await result


class FixtureAdapter:
    """Deterministic no-audio exercise; barriers are injected by offline tests.

    Cancellation callable is True when preparation must stop. still_valid is
    True when sending is allowed. Hooks receive prepared/segment and can block
    for concurrency tests without claiming simulated time as media evidence.
    """
    execution_kind = 'offline_fixture'

    def __init__(self, *, prepare_hook=None, send_hook=None):
        self.prepare_hook = prepare_hook
        self.send_hook = send_hook

    async def prepare(self, frozen_segment: dict, cancellation) -> FixturePrepared:
        if cancellation():
            raise ValueError('preparation_cancelled')
        # Take value ownership before the first await: hooks cannot rewrite words.
        text, intent = frozen_segment['text'], frozen_segment['intent']
        if not isinstance(text, str) or not text.strip() or len(text) > 120:
            raise ValueError('invalid_fixture_text')
        if intent not in {'question', 'listen', 'comment', 'result', 'neutral'}:
            raise ValueError('invalid_fixture_intent')
        payload = json.dumps({'text': text, 'intent': intent}, ensure_ascii=False,
                             sort_keys=True, separators=(',', ':'), allow_nan=False)
        prepared = FixturePrepared(text, intent, hashlib.sha256(payload.encode()).hexdigest())
        await _hook(self.prepare_hook, prepared)
        if cancellation():
            raise ValueError('preparation_cancelled')
        return prepared

    async def send(self, prepared: FixturePrepared, session: dict, still_valid) -> dict:
        if session.get('mode') != 'offline_fixture' or not still_valid():
            raise ValueError('fixture_send_gate_closed')
        # Invocation marks the protocol's in-flight boundary. A cancel during
        # this hook does not pretend the current attempt was remotely aborted.
        await _hook(self.send_hook, prepared)
        return {'fixture_receipt': prepared.content_hash,
                'execution_kind': 'offline_fixture', 'media_verified': False,
                'audience_heard': False, 'audio_generated': False,
                'provider_called': False}
