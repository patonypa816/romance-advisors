"""Real adapter, original TTS converter and scheduler against offline providers.

Generated WAV is a synthetic binary fixture; no credentials, network, cloud,
RTC or audible playback is used or inferred. ffmpeg/ffprobe are really executed.
"""
import asyncio
import base64
import copy
import hashlib
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from hell_inc.avatar_transport import tts_live, motion_component, component_live, idle_motion
import test_m4_scheduler as scheduler
from test_m4_policy import action, RECOVERY


class StageOnePromptPolicyTests(unittest.TestCase):
    def test_original_generic_voice_policy_preserved_without_old_character_or_text_rewrite(self):
        from hell_inc.real_performance import STAGE1_PROMPT
        # Exact generic source sentence, not the old character/persona fields.
        self.assertIn('回答声音自然清晰、松弛、正常语速，按语义生成重音与停顿。', STAGE1_PROMPT)
        self.assertIn('不刻意拖长或压嗓。', STAGE1_PROMPT)
        self.assertIn('逐段按冻结台词与表达意图生成声音指令和动作', STAGE1_PROMPT)
        self.assertIn('不得修改、增删、复述或输出台词', STAGE1_PROMPT)
        self.assertIn('只能包含instruction、actions、recovery', STAGE1_PROMPT)
        for historical_identity in ('朱元璋', '洪武', '陛下', '朕'):
            self.assertNotIn(historical_identity, STAGE1_PROMPT)


def wave_fixture(seconds=6):
    pcm = b'\0\0' * (24000 * seconds)
    out = io.BytesIO()
    with wave.open(out, 'wb') as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(24000); f.writeframes(pcm)
    return out.getvalue(), pcm


class Response:
    status = 200
    def __init__(self, raw, content_type, on_exit=None):
        self.raw = raw; self.content_type = content_type; self.content_length = len(raw)
        self.content = self; self.on_exit = on_exit
    async def read(self, size=-1): return self.raw if size < 0 else self.raw[:size]
    async def __aenter__(self): return self
    async def __aexit__(self, *args):
        if self.on_exit: self.on_exit()
    async def iter_chunked(self, size):
        # Deliberately fragmented SSE and download boundaries.
        for offset in range(0, len(self.raw), min(size, 8001)):
            yield self.raw[offset:offset + min(size, 8001)]
            await asyncio.sleep(0)


class OfflineTTSHTTP:
    def __init__(self, wav, on_tts_exit=None, wrong_word=False):
        self.wav = wav; self.on_tts_exit = on_tts_exit; self.calls = []; self.wrong_word = wrong_word
    def post(self, url, **kwargs):
        self.calls.append(('POST', url, copy.deepcopy(kwargs)))
        words = [{'text': '错字' if self.wrong_word else kwargs['json']['input']['text'].strip(), 'begin_index': 0, 'end_index': 2,
                  'begin_time': 2000, 'end_time': 2500}]
        event = {'request_id': 'offline-tts-request', 'usage': {'input_tokens': 5, 'output_tokens': 8, 'total_tokens': 13},
                 'output': {'type': 'sentence-end', 'sentence': {'index': 0, 'words': words},
                            'audio': {'data': base64.b64encode(self.wav).decode(),
                                      'url': 'https://approved-audio.example/test.wav?signature=must-not-persist'},
                            'finish_reason': 'stop'}}
        raw = ('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode()
        return Response(raw, 'text/event-stream', self.on_tts_exit)
    def get(self, url, **kwargs):
        self.calls.append(('GET', url, copy.deepcopy(kwargs)))
        if kwargs.get('headers', {}).get('Authorization'): raise AssertionError('credential on audio download')
        return Response(self.wav, 'audio/wav')


class OfflineModel:
    def __init__(self, *, actions=True): self.calls = []; self.actions = actions
    async def complete_json(self, prompt, data):
        self.calls.append((prompt, copy.deepcopy(data)))
        if 'input' in data and 'schedules' in data['input']:
            output = {'schedule_id': data['input']['schedules'][0]['schedule_id']}
        else:
            output = {'instruction': '自然平静说话', 'actions': [action()] if self.actions else [],
                      'recovery': RECOVERY if self.actions else None}
        return {'output': output, 'request_id': 'offline-model-' + str(len(self.calls)), 'usage': {'total_tokens': 3}}


class OriginalTTSRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_sse_ffmpeg_probe_preserves_exact_text_and_safe_evidence(self):
        wav, pcm = wave_fixture(); http = OfflineTTSHTTP(wav)
        with tempfile.TemporaryDirectory() as tmp, patch('aiohttp.ClientSession', side_effect=AssertionError('network forbidden')):
            result = await tts_live.synthesize_segment(http, 'opaque-offline-credential', 'test-workspace', Path(tmp) / 'tts',
                                                      ' 你好\n', '自然平静说话', word_timestamps=True, voice='approved-test-voice')
            self.assertEqual(result['status'], 'ok', result)
            self.assertEqual(result['text'], ' 你好\n')
            self.assertEqual(Path(result['pcm_path']).read_bytes(), pcm)
            self.assertEqual(result['pcm_sha256'], hashlib.sha256(pcm).hexdigest())
            self.assertEqual(result['alignment']['status'], 'ok')
            self.assertTrue(result['alignment']['text_matches_script'])
            self.assertEqual([c[0] for c in http.calls], ['POST', 'GET'])
            request = http.calls[0][2]
            self.assertEqual(request['json']['input']['text'], ' 你好\n')
            self.assertEqual(request['json']['input']['voice'], 'approved-test-voice')
            self.assertEqual(request['headers']['Authorization'], 'Bearer opaque-offline-credential')
            self.assertEqual(request['headers']['X-DashScope-SSE'], 'enable')
            for path in (Path(tmp) / 'tts').rglob('*'):
                if path.is_file():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600, path.name)
                    if path.suffix in ('.json', '.jsonl'):
                        text = path.read_text(); self.assertNotIn('must-not-persist', text); self.assertNotIn('opaque-offline-credential', text)


class OwnedRuntime:
    """Ownership/gate fixture only; adapter and MotionComponent are production classes."""
    def __init__(self, component, session_id):
        self.component = component; self.session_id = session_id; self.epoch = 'offline-runtime-epoch'; self.allowed = True
    def validate(self, context):
        if not self.allowed or context['session_id'] != self.session_id: raise ValueError('runtime_gate_closed')
    def ready(self, session_id): return self.allowed and session_id == self.session_id


class RealAdapterChainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from hell_inc.real_performance import RealPerformanceAdapter
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.clock = scheduler.VirtualClock()
        for module in (motion_component, component_live, idle_motion):
            patched = patch.object(module, 'time', self.clock); patched.start(); self.addCleanup(patched.stop)
        self.component = scheduler.ready_component(Path(self.tmp.name) / 'component', self.clock)
        self.context = {'performance_id': str(uuid4()), 'session_id': str(uuid4()), 'segment_index': 0, 'payload_hash': 'a' * 64}
        self.runtime = OwnedRuntime(self.component, self.context['session_id'])
        self.wav, self.pcm = wave_fixture(); self.http = OfflineTTSHTTP(self.wav); self.model = OfflineModel()
        self.evidence = []
        async def persist(context, value):
            self.evidence.append((copy.deepcopy(context), copy.deepcopy(value), len(self.model.calls), len(self.http.calls)))
        self.adapter = RealPerformanceAdapter(self.runtime, self.http, self.model, 'opaque-offline-credential', 'test-workspace',
            Path(self.tmp.name) / 'private-artifacts', persist, voice='approved-test-voice', persona='经批准的运行时人设')
        self.segment = {'text': ' 你好\n', 'intent': 'result'}

    async def prepare(self, cancellation=lambda: False):
        return await self.adapter.prepare(self.segment, cancellation, context=self.context)

    async def test_genuine_real_adapter_full_offline_chain_stage1_sse_tools_stage2_original_scheduler(self):
        with patch('aiohttp.ClientSession', side_effect=AssertionError('external network forbidden')) as network:
            prepared = await self.prepare()
            receipt = await self.adapter.send(prepared, {'mode': 'real', 'session_id': self.context['session_id']}, lambda: True)
            network.assert_not_called()
        self.assertEqual(len(self.model.calls), 2)
        self.assertEqual([c[0] for c in self.http.calls], ['POST', 'GET'])
        self.assertIn('回答声音自然清晰、松弛、正常语速，按语义生成重音与停顿。', self.model.calls[0][0])
        self.assertEqual(self.model.calls[0][1]['persona'], '经批准的运行时人设')
        self.assertEqual(prepared.semantic.content['instruction'], '自然平静说话')
        self.assertEqual(self.http.calls[0][2]['json']['input']['instruction'], prepared.semantic.content['instruction'])
        self.assertEqual(self.model.calls[0][1]['text'], self.segment['text'])
        self.assertEqual(self.model.calls[1][1]['input']['text'], self.segment['text'])
        self.assertTrue(self.model.calls[1][1]['input']['schedules'])
        self.assertEqual(prepared.semantic.content['text'], self.segment['text'])
        self.assertEqual(prepared.semantic.content['actions'][0]['motions'], action()['motions'])
        self.assertEqual(prepared.semantic.content['recovery'], RECOVERY)
        self.assertEqual(prepared.semantic.pcm, self.pcm)
        self.assertEqual(b''.join(c for _, c in self.component._ws.bytes), self.pcm)
        self.assertEqual([e['role'] for e in self.component.motion_events], ['GE', 'S', 'Q'])
        self.assertEqual(receipt, {'execution_kind': 'real', 'transport_sent': True,
            'pcm_sha256': hashlib.sha256(self.pcm).hexdigest(), 'provider_request_id': 'offline-tts-request',
            'media_verified': False, 'audience_heard': False})
        phases = [e[1]['phase'] for e in self.evidence]
        self.assertEqual(phases, ['stage1', 'stage1_returned', 'stage1_complete', 'tts', 'tts_returned', 'stage2', 'stage2_returned', 'ready'])
        self.assertEqual(self.evidence[0][2:], (0, 0))  # durable callback before first charge
        self.assertEqual(next(e for e in self.evidence if e[1]['phase'] == 'tts')[3], 0)
        self.assertEqual(next(e for e in self.evidence if e[1]['phase'] == 'stage2')[2], 1)
        with self.assertRaises(FileExistsError): await self.prepare()
        self.assertEqual(len(self.model.calls), 2); self.assertEqual(len(self.http.calls), 2)

    async def test_cancel_during_tts_retains_request_usage_hash_and_never_stage2_or_send(self):
        cancelled = False
        def cancel():
            nonlocal cancelled
            cancelled = True
        self.http.on_tts_exit = cancel
        with self.assertRaisesRegex(ValueError, 'preparation_cancelled'):
            await self.prepare(lambda: cancelled)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual([c[0] for c in self.http.calls], ['POST', 'GET'])
        evidence = self.evidence[-1][1]
        self.assertTrue(evidence['attempted']); self.assertEqual(evidence['request_id'], 'offline-tts-request')
        self.assertTrue(evidence['usage']['usage_verified']); self.assertEqual(evidence['usage']['total_tokens'], 13)
        self.assertEqual(evidence['pcm_sha256'], hashlib.sha256(self.pcm).hexdigest())
        self.assertEqual(self.component._ws.frames, []); self.assertEqual(self.component._ws.bytes, [])
        with self.assertRaises(FileExistsError): await self.prepare()
        self.assertEqual(len(self.http.calls), 2)

    async def test_no_action_skips_second_model_but_tts_still_requests_word_timestamp_limit(self):
        self.model.actions = False
        self.segment['text'] = '你好' * 50  # above legacy30, within current120
        prepared = await self.prepare()
        self.assertEqual(len(self.model.calls), 1)
        self.assertTrue(self.http.calls[0][2]['json']['input']['word_timestamp_enabled'])
        receipt = await self.adapter.send(prepared, {'mode': 'real', 'session_id': self.context['session_id']}, lambda: True)
        self.assertTrue(receipt['transport_sent'])
        self.assertEqual([f['type'] for _, f in self.component._ws.frames], [10])
        self.assertEqual(prepared.semantic.content['actions'], [])

    async def test_wrong_provider_word_mapping_fails_before_stage2_or_any_send(self):
        self.http.wrong_word = True
        with self.assertRaises(ValueError): await self.prepare()
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.component._ws.frames, [])
        self.assertEqual(self.component._ws.bytes, [])
        self.assertTrue(self.evidence[-1][1]['attempted'])

    async def test_model_cannot_replace_text_and_no_tts_fallback(self):
        async def invalid(prompt, data):
            self.model.calls.append((prompt, copy.deepcopy(data)))
            return {'output': {'text': '替换台词', 'instruction': '平静', 'actions': [], 'recovery': None}}
        self.model.complete_json = invalid
        with self.assertRaisesRegex(ValueError, 'invalid_stage1_output'): await self.prepare()
        self.assertEqual(len(self.model.calls), 1); self.assertEqual(self.http.calls, [])

    async def test_changed_runtime_epoch_gate_or_prepared_evidence_cannot_send(self):
        from dataclasses import replace
        prepared = await self.prepare()
        session = {'mode': 'real', 'session_id': self.context['session_id']}
        self.runtime.epoch = 'new-epoch'
        with self.assertRaisesRegex(ValueError, 'real_send_gate_closed'):
            await self.adapter.send(prepared, session, lambda: True)
        self.runtime.epoch = prepared.runtime_epoch; self.runtime.allowed = False
        with self.assertRaisesRegex(ValueError, 'runtime_gate_closed'):
            await self.adapter.send(prepared, session, lambda: True)
        self.runtime.allowed = True
        corrupt = replace(prepared, semantic=replace(prepared.semantic, pcm_sha256='0' * 64))
        with self.assertRaisesRegex(ValueError, 'prepared_evidence_changed'):
            await self.adapter.send(corrupt, session, lambda: True)
        self.assertEqual(self.component._ws.frames, []); self.assertEqual(self.component._ws.bytes, [])

    async def test_owner_task_cancellation_waits_for_charged_tts_result_and_preserves_evidence(self):
        started, release = asyncio.Event(), asyncio.Event()
        original_post = self.http.post
        def gated_post(url, **kwargs):
            response = original_post(url, **kwargs)
            original_content = response.content
            class GateContent:
                async def iter_chunked(self, size):
                    started.set(); await release.wait()
                    async for chunk in original_content.iter_chunked(size): yield chunk
            response.content = GateContent()
            return response
        self.http.post = gated_post
        task = asyncio.create_task(self.prepare())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel(); await asyncio.sleep(0)
        self.assertFalse(task.done())  # cannot throw away a potentially charged response
        release.set()
        with self.assertRaises(asyncio.CancelledError): await task
        evidence = self.evidence[-1][1]
        self.assertEqual(evidence['phase'], 'interrupted')
        self.assertEqual(evidence['request_id'], 'offline-tts-request')
        self.assertTrue(evidence['usage']['usage_verified'])
        self.assertEqual(evidence['pcm_sha256'], hashlib.sha256(self.pcm).hexdigest())
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual([c[0] for c in self.http.calls], ['POST', 'GET'])
        self.assertEqual(self.component._ws.frames, [])

    async def test_full_chain_uses_real_bailian_client_and_proxy_http_wrapper_not_mocked_planner(self):
        from hell_inc.media_http import MediaHTTP, BailianPlanner
        owner = self
        class ProviderSession:
            headers = {}
            def __init__(self): self.calls = []
            def request(self, method, url, **kwargs):
                self.calls.append((method, url, copy.deepcopy(kwargs)))
                if url.endswith('/chat/completions'):
                    data = json.loads(kwargs['json']['messages'][1]['content'])
                    output = ({'schedule_id': data['input']['schedules'][0]['schedule_id']} if 'input' in data else
                              {'instruction': '自然平静说话', 'actions': [action()], 'recovery': RECOVERY})
                    response = {'id': 'offline-planner-request', 'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6},
                                'choices': [{'message': {'content': json.dumps(output, ensure_ascii=False)}}]}
                    return Response(json.dumps(response).encode(), 'application/json')
                return owner.http.post(url, **kwargs) if method == 'POST' else owner.http.get(url, **kwargs)
        provider = ProviderSession()
        transport = MediaHTTP(provider, workspace='test-workspace', proxy='http://approved-proxy.example:8080',
                              audio_hosts=('approved-audio.example',))
        self.adapter.http = transport
        self.adapter.model = BailianPlanner(transport, 'opaque-offline-credential', 'test-workspace', 'approved-model')
        with patch('socket.create_connection', side_effect=AssertionError('network forbidden')) as network:
            prepared = await self.prepare()
            receipt = await self.adapter.send(prepared, {'mode': 'real', 'session_id': self.context['session_id']}, lambda: True)
            network.assert_not_called()
        self.assertEqual([c[0] for c in provider.calls], ['POST', 'POST', 'GET', 'POST'])
        for method, url, kwargs in provider.calls:
            self.assertEqual(kwargs['proxy'], 'http://approved-proxy.example:8080')
            self.assertTrue(kwargs['ssl']); self.assertFalse(kwargs['allow_redirects'])
            if method == 'GET': self.assertNotIn('Authorization', kwargs['headers'])
            else: self.assertEqual(kwargs['headers']['Authorization'], 'Bearer opaque-offline-credential')
        self.assertTrue(receipt['transport_sent']); self.assertFalse(receipt['audience_heard'])
        self.assertEqual(prepared.semantic.content['text'], ' 你好\n')
        self.assertEqual([e['role'] for e in self.component.motion_events], ['GE', 'S', 'Q'])
        self.assertEqual(self.evidence[-1][1]['stage1_usage']['total_tokens'], 6)
        self.assertEqual(self.evidence[-1][1]['stage2_usage']['total_tokens'], 6)

    async def test_stage2_cannot_invent_schedule_or_change_actions_no_third_fallback_call(self):
        original = self.model.complete_json
        async def invalid_second(prompt, data):
            reply = await original(prompt, data)
            if 'input' in data:
                reply['output'] = {'schedule_id': 'invented-seconds', 'actions': []}
            return reply
        self.model.complete_json = invalid_second
        with self.assertRaisesRegex(ValueError, 'invalid_timed_action_result'): await self.prepare()
        self.assertEqual(len(self.model.calls), 2)
        self.assertEqual([c[0] for c in self.http.calls], ['POST', 'GET'])
        self.assertEqual(self.component._ws.frames, []); self.assertEqual(self.component._ws.bytes, [])
        self.assertEqual(self.evidence[-1][1]['phase'], 'failed')
        self.assertTrue(self.evidence[-1][1]['usage']['usage_verified'])

    async def test_closed_runtime_and_untrusted_artifact_context_never_call_provider(self):
        self.runtime.allowed = False
        with self.assertRaisesRegex(ValueError, 'runtime_gate_closed'): await self.prepare()
        self.runtime.allowed = True
        for change in ({'performance_id': '../escape'}, {'segment_index': True}, {'payload_hash': 'bad'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                await self.adapter.prepare(self.segment, lambda: False, context={**self.context, **change})
        self.assertEqual(self.model.calls, []); self.assertEqual(self.http.calls, [])
        self.assertEqual(self.evidence, [])


class OriginalComponentCreationTests(unittest.IsolatedAsyncioTestCase):
    """Actual imported start/_run/_connect/_finish, with fake HTTP/WSS only."""
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.events = []; self.clock = scheduler.VirtualClock()
        owner = self
        class LiveSocket(scheduler.FakeSocket):
            def __init__(self):
                super().__init__(owner.clock); self.closed = False; self.closed_event = asyncio.Event()
            def __aiter__(self): return self
            async def __anext__(self):
                await self.closed_event.wait(); raise StopAsyncIteration
            async def close(self): self.closed = True; self.closed_event.set()
            async def ping(self): pass
        class JsonResponse(Response):
            async def json(self, **kwargs): return json.loads(self.raw)
        class Provider:
            def __init__(self): self.calls = []; self.ws_calls = []; self.ended = True
            def request(self, method, url, **kwargs):
                self.calls.append((method, url, copy.deepcopy(kwargs))); owner.events.append(method)
                value = ({'live': {'id': 'offline-owned-live', 'status': 'creating'},
                          'managed_rtc': {'token': 'offline-rtc-token-only', 'user_id': 'receiver-test'}} if method == 'POST' else
                         {'live': {'id': 'offline-owned-live', 'status': 'ended' if self.ended else 'active'}})
                return JsonResponse(json.dumps(value).encode(), 'application/json')
            async def ws_connect(self, url, **kwargs):
                self.ws_calls.append((url, copy.deepcopy(kwargs))); owner.events.append('WSS')
                owner.component._ready.set(); owner.component._joined.set()
                return LiveSocket()
        self.http = Provider()
        self.component = component_live.PersistentComponent(self.http, Path(self.tmp.name) / 'component',
            Path(self.tmp.name) / 'image-does-not-exist.png')
        self.component.END_TIMEOUT = .02; self.component.POLL_INTERVAL = .001
        self.addAsyncCleanup(self.component.close)

    async def test_avatar_id_skips_missing_image_and_awaits_durable_callback_before_wss(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def on_created(live_id, safe):
            self.events.append('callback-enter'); self.assertEqual(live_id, 'offline-owned-live')
            self.assertNotIn('token', json.dumps(safe)); entered.set(); await release.wait()
            self.events.append('callback-committed')
        self.component.on_created = on_created
        await self.component.start('opaque-test-vidu-key', avatar_id='123456789')
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual(self.http.ws_calls, [])
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(self.http.calls[0][2]['json']['avatar_id'], '123456789')
        self.assertNotIn('image_uri', self.http.calls[0][2]['json'])
        self.assertFalse(self.http.calls[0][2]['json']['managed_rtc']['enable_recording'])
        owned = json.loads((self.component.out / 'component-owned-id.json').read_text())
        self.assertEqual(owned, {'live_id': 'offline-owned-live'})
        release.set()
        for _ in range(100):
            if self.component.state == 'ready': break
            await asyncio.sleep(.001)
        self.assertEqual(self.component.state, 'ready')
        self.assertLess(self.events.index('callback-committed'), self.events.index('WSS'))
        await self.component.stop(); await self.component._task
        self.assertTrue(self.component.cloud_end_confirmed)
        with self.assertRaises(RuntimeError): await self.component.start('opaque-test-vidu-key', avatar_id='123456789')
        self.assertEqual(sum(method == 'POST' for method, _, _ in self.http.calls), 1)
        self.assertTrue(all(kwargs['headers']['Authorization'] == 'Token opaque-test-vidu-key' for _, _, kwargs in self.http.calls))

    async def test_callback_commit_failure_known_id_controlled_stop_unknown_no_second_create(self):
        async def failed_commit(live_id, safe):
            self.events.append('commit-failed'); raise RuntimeError('injected durable ownership commit failure')
        self.component.on_created = failed_commit; self.http.ended = False
        await self.component.start('opaque-test-vidu-key', avatar_id='123456789')
        await asyncio.wait_for(self.component._task, 2)
        self.assertEqual(self.component.live_id, 'offline-owned-live')
        self.assertTrue(self.component.creation_unknown)
        self.assertFalse(self.component.cloud_end_confirmed)
        self.assertEqual(self.component.state, 'stop_unconfirmed')
        self.assertEqual(self.component._rtc, {})
        # Cleanup may connect ONLY to hang up the already-returned owned live.
        self.assertLess(self.events.index('commit-failed'), self.events.index('WSS'))
        self.assertTrue(any(method == 'GET' and url.endswith('/offline-owned-live') for method, url, _ in self.http.calls))
        self.assertEqual([frame['type'] for _, frame in self.component._ws.frames], [1, 5])
        self.assertEqual(self.component._ws.bytes, [])
        with self.assertRaises(RuntimeError): await self.component.start('opaque-test-vidu-key', avatar_id='123456789')
        self.assertEqual(sum(method == 'POST' for method, _, _ in self.http.calls), 1)
        receipt = (self.component.out / 'component-owned-id.json').read_text()
        self.assertNotIn('opaque-test-vidu-key', receipt); self.assertNotIn('offline-rtc-token-only', receipt)
