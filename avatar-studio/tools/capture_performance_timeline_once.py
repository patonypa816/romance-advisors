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
from performance_tasks import chunk_plan
from tts_live import synthesize_segment
from motion_component import MotionComponent

OUT = Path(os.environ.get('RUNNER_TEMP', '/tmp')) / 'performance-timeline-private'
PUBLIC = Path('performance-timeline-result.json')
REQUEST = ('请用约30秒的自然中文口语介绍一场观察与推理互动，约110至140个汉字。'
           '完整表达三个意思：先观察屏幕上的线索，不急着下结论；可以在评论中说出怀疑的地方和理由；'
           '最后邀请大家一起验证。角色坐在桌前，语气亲切、有一点悬念，正常语速。'
           '配两到三个简洁自然的手势或点头，动作随语义出现，不表演任何真实游戏结果。')
CONTEXT = {'character': {'identity': '虚构的推理互动主持人', 'temperament': '亲切、沉稳，带一点好奇',
                        'speech': '自然中文口语，正常语速，不故意拉长', 'scene': '坐在桌前，正面镜头'}}
REPORT = {'status': 'starting', 'scope': 'Real Bailian brain and TTS; original Vidu sender captured locally only',
          'vidu_session_created': False, 'vidu_network_writes': 0,
          'time_semantics': 'captured times are local socket writes, NOT Vidu received/rendered times',
          'request': REQUEST, 'context': CONTEXT, 'blocks': [], 'model_calls': {}, 'base_commit': '477e73bf642557d2ef52370afccfaf601858235a'}

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
        REPORT['phase'] = 'expression_brain'
        raw = await brain.generate_performance('intent', REQUEST, CONTEXT, {}, '自然清晰，正常语速，按语义停顿。')
        REPORT['brain_output'] = raw
        save()
        blocks = chunk_plan(raw, 80)
        if not 1 <= len(blocks) <= 4:
            raise RuntimeError('sample_exceeds_four_blocks_no_more_calls')
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
