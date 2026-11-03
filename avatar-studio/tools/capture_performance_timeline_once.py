"""One paid upstream pass; original sender writes to local capture, never Vidu."""
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'avatar-studio' / 'studio'))
from bailian_ai import BailianAI
from credentials import load_bailian
from connection import client_session
from performance_tasks import chunk_plan, PerformanceTasks
from types import SimpleNamespace
from tts_live import synthesize_segment
from motion_component import MotionComponent

OUT = Path(os.environ.get('RUNNER_TEMP', '/tmp')) / 'performance-timeline-private'
PUBLIC = Path('performance-timeline-result.json')
REQUEST = '先别急着猜答案，咱们一起看看屏幕上的线索。同一个细节，换个角度，也许就有不同的解释。你可以在评论里说说，哪个地方让你觉得不对劲，为什么会这样想。一句话也能提出好问题。接下来，我们把大家的猜测和已知事实放在一起比较。发现矛盾就停下来想一想，找到证据再往前走。准备好了，就从你最在意的细节开始吧。'

CONTEXT = {'character': {'identity': '虚构的推理互动主持人', 'temperament': '亲切、沉稳，带一点好奇',
                        'speech': '自然中文口语，正常语速，不故意拉长', 'scene': '坐在桌前，正面镜头'}}
REPORT = {'status': 'starting', 'scope': 'Real Bailian brain and TTS; original Vidu sender captured locally only',
          'vidu_session_created': False, 'vidu_network_writes': 0,
          'time_semantics': 'captured times are local socket writes, NOT Vidu received/rendered times',
          'request': REQUEST, 'kind': 'script', 'auto_actions': True, 'context': CONTEXT, 'blocks': [], 'model_calls': {}, 'base_commit': '477e73bf642557d2ef52370afccfaf601858235a'}

def safe(value):
    if isinstance(value, dict):
        return {k: safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [safe(v) for v in value]
    if isinstance(value, str):
        for name in ('DASHSCOPE_API_KEY', 'BAILIAN_API_KEY'):
            key = os.environ.get(name, '')
            if key: value = value.replace(key, '[redacted]')
        import re
        return re.sub(r'https?://[^\s\"<>]+', '[redacted-url]', value)
    return value

def save():
    PUBLIC.write_text(json.dumps(safe(REPORT), ensure_ascii=False, indent=2))

class NoViduNetwork:
    def __getattr__(self, name):
        raise RuntimeError('vidu_network_disabled')

class CaptureSocket:
    closed = False
    def __init__(self):
        self.frames = []
        self.binary = []
    async def send_json(self, frame):
        self.frames.append({'monotonic': time.monotonic(), 'frame': copy.deepcopy(frame)})
    async def send_bytes(self, chunk):
        self.binary.append({'monotonic': time.monotonic(), 'bytes': len(chunk),
                            'sha256': hashlib.sha256(chunk).hexdigest()})

async def run():
    OUT.mkdir(parents=True, exist_ok=False)
    key, workspace = load_bailian()
    brain = BailianAI({'model': 'qwen3.8-flash', 'region': 'cn-beijing', 'enable_thinking': False})
    try:
        REPORT['phase'] = 'fixed_script_partition'
        studio = SimpleNamespace(current=SimpleNamespace(out=OUT, comments=[], diagnostics=SimpleNamespace(clean=safe)))
        tasks = PerformanceTasks(studio)
        tasks.submit({'request_id': 'fixed-script-30s-once', 'kind': 'script', 'text': REQUEST,
                      'auto_actions': True, 'block_chars': 80,
                      'instruction': '自然清晰，正常语速，按语义停顿，不刻意拖长。',
                      'context': {'performance': '每块配一个简洁自然的点头或手势，动作随语义出现；镜头正面，无已知评论区域位置。'}})
        task = tasks.tasks['fixed-script-30s-once']
        count = len(task['plans'])
        if not 1 <= count <= 4:
            raise RuntimeError('sample_exceeds_four_blocks_no_more_calls')
        blocks = []
        REPORT['brain_output'] = []
        for index in range(count):
            REPORT['phase'] = f'fixed_script_expression_{index+1}'
            c = {'id': f'block-{index+1}', 'performance_request_id': task['request_id'],
                 'performance_block': index, 'status': 'pending'}
            studio.current.comments.append(c)
            task['item_id'] = c['id']
            plan = await tasks.prepare_plan(c, brain, CONTEXT)
            blocks.append(plan)
            REPORT['brain_output'].append(plan)
            save()
        if ''.join(b['segments'][0]['text'] for b in blocks) != REQUEST:
            raise RuntimeError('fixed_script_changed')
        REPORT['block_count'] = len(blocks)
        prepared = []
        async with client_session(trust_env=False) as http:
            for i, block in enumerate(blocks, 1):
                part = block['segments'][0]
                REPORT['phase'] = f'tts_{i}'
                result = await synthesize_segment(http, key, workspace, OUT / f'tts-{i}', part['text'],
                                                   part['instruction'], word_timestamps=True)
                summary = {k: result.get(k) for k in ('status', 'model', 'voice', 'audio_seconds', 'pcm_sha256',
                           'usage_verified', 'input_tokens', 'output_tokens', 'total_tokens', 'alignment')}
                row = {'index': i, 'input_part': part, 'tts': summary}
                REPORT['blocks'].append(row)
                save()
                if result.get('status') not in ('ok', 'usage_unverified') or not result.get('pcm_path'):
                    raise RuntimeError('tts_unusable_no_retry')
                pcm = Path(result['pcm_path']).read_bytes()
                REPORT['phase'] = f'action_planner_{i}'
                planned = await brain.plan_timed_actions(part, result['alignment'], len(pcm) / 48000,
                                                       CONTEXT, source_text=REQUEST)
                row['planned'] = planned
                save()
                prepared.append((part, pcm, result['alignment'], planned, row))
        # Real upstream preparation is complete before this bounded delivery capture.
        # No network constructor/start/join method is called, and no Vidu key is supplied.
        component = MotionComponent(NoViduNetwork(), OUT / 'capture', OUT / 'unused.png')
        sock = CaptureSocket()
        component._ws = sock
        component.live_id = 'local-capture-not-a-vidu-session'
        component._ready.set(); component._joined.set(); component.state = 'ready'
        start = time.monotonic()
        for i, (part, pcm, alignment, planned, row) in enumerate(prepared, 1):
            REPORT['phase'] = f'local_sender_capture_{i}'
            json_start, audio_start = len(sock.frames), len(sock.binary)
            receipt = await component.send_aligned_expression(pcm, part['text'], planned['actions'], i,
                recovery=part['recovery'], alignment=alignment, action_kind='S', ge_lead_seconds=1.1,
                allow_tail_q_after_audio=True, local_action_anchors=True)
            first_pcm = receipt['first_pcm_sent_at_monotonic']
            row['capture_start_seconds'] = round(first_pcm - start, 6)
            row['sender_return_seconds'] = round(time.monotonic() - start, 6)
            row['type_frames'] = [{'at_seconds': round(f['monotonic'] - start, 6),
                                   'relative_to_block_pcm_seconds': round(f['monotonic'] - first_pcm, 6),
                                   'frame': f['frame']} for f in sock.frames[json_start:]]
            row['pcm_chunks'] = [{'at_seconds': round(f['monotonic'] - start, 6),
                                  'relative_to_block_pcm_seconds': round(f['monotonic'] - first_pcm, 6),
                                  'bytes': f['bytes']} for f in sock.binary[audio_start:]]
            row['motion_events'] = [{k: event.get(k) for k in ('role', 'instruction', 'planned_at_seconds',
                                      'actual_at_seconds', 'timebase', 'anchor', 'group_index')}
                                    for event in receipt.get('motion_events', [])]
            row['sender_receipt'] = {k: receipt.get(k) for k in ('status', 'audio_seconds', 'send_complete',
                                    'actions_sent_complete', 'pcm_transport_sent_bytes',
                                    'visual_effect_confirmed', 'playback_complete_confirmed')}
            save()
        REPORT.update(status='completed', phase='done', pcm_format={'rate':24000,'channels':1,'encoding':'signed16le',
            'frame_bytes':4800,'frame_ms':100}, total_audio_seconds=sum(len(x[1])/48000 for x in prepared),
            capture_elapsed_seconds=round(time.monotonic()-start, 6), original_drain_seconds=component.DRAIN_SECONDS,
            preparation_note='All real audio prepared before local capture; this does not measure live next-block prefetch latency.')
    finally:
        REPORT['model_calls'] = dict(brain.requests)
        REPORT['model_usage'] = copy.deepcopy(brain.usage)
        await brain.close()
        save()

if __name__ == '__main__':
    try:
        asyncio.run(run())
    except Exception as exc:
        REPORT['status'] = 'failed'
        REPORT['error_type'] = type(exc).__name__
        # Only bounded model error codes or our fixed local codes; never exception repr.
        code = getattr(exc, 'code', None)
        if isinstance(code, str) and len(code) < 100: REPORT['error_code'] = code
        save()
        print('PERFORMANCE_RESULT_BEGIN')
        print(PUBLIC.read_text())
        print('PERFORMANCE_RESULT_END')
        sys.exit(1)
    print('PERFORMANCE_RESULT_BEGIN')
    print(PUBLIC.read_text())
    print('PERFORMANCE_RESULT_END')
