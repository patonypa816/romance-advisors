"""M4 wire contract: exact text, bounded codepoints and canonical semantic hashes."""
import hashlib
import json
import re
from datetime import datetime, timezone
from .contracts import ValidationError
from .interviews import uuid, fields, InterviewError

class PerformanceError(InterviewError):
    pass

STATES = ('accepted','preparing','ready','sending','send_completed','failed','cancelled','expired','unknown')
TERMINAL = ('send_completed','failed','cancelled','expired','unknown')
INTENTS = ('question','listen','comment','result','neutral')
FIXED_SCRIPT = [
    {'text':'欢迎来到地狱集团。请介绍你的创意。','intent':'question'},
    {'text':'我在听，请继续。','intent':'listen'},
    {'text':'这是离线协议演练，不生成或播放音画。','intent':'comment'},
    {'text':'本次固定剧本演练结束，不产生游戏评分或奖励。','intent':'result'},
]

def canonical_hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()

def timestamp(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)',value):
        raise ValidationError('expires_at 必须为 UTC RFC3339。')
    try: dt=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError: raise ValidationError('expires_at 无效。') from None
    if dt.microsecond % 1000: raise ValidationError('expires_at 最多毫秒精度。')
    return dt.isoformat(timespec='milliseconds').replace('+00:00','Z')

def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')

def split_text(value):
    if not isinstance(value,str) or not value.strip() or len(value)>960 or '\x00' in value:
        raise ValidationError('台词须为 1–960 字符。')
    try: value.encode('utf-8')
    except UnicodeError: raise ValidationError('台词 Unicode 无效。') from None
    original=value
    chunks=[]
    while value:
        end=min(len(value),120)
        # Preserve exact concatenation; punctuation is never stripped.
        if len(value)>120:
            candidates=[i+1 for i,c in enumerate(value[:120]) if c in '。！？；，.!?;\n' and i>=60]
            if candidates: end=candidates[-1]
        chunks.append(value[:end]);value=value[end:]
    if len(chunks)>8: chunks=[original[i:i+120] for i in range(0,len(original),120)]
    return chunks

def source_ref(value, frozen=True):
    if not isinstance(value,dict): raise ValidationError('source_ref 无效。')
    kind=value.get('kind'); names={'kind','id'}
    if kind not in ('fixed_script','interview_result','competition_comment','competition_result'): raise ValidationError('来源类型无效。')
    if kind=='competition_comment': names.add('submission_id')
    if frozen and kind!='fixed_script': names.update(('source_version','source_hash'))
    if set(value)!=names: raise ValidationError('来源字段无效。')
    if kind=='fixed_script':
        if value['id'] not in ('m4-demo-v1','m4-live-smoke-v1'): raise ValidationError('固定剧本不存在。')
    else:
        uuid(value['id'])
        if 'submission_id' in value: uuid(value['submission_id'])
        if frozen:
            if type(value['source_version']) is not int or value['source_version']<1: raise ValidationError('来源版本无效。')
            validate_hash(value['source_hash'])
    return value

def validate_hash(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value): raise ValidationError('哈希必须为小写 SHA256。')

def validate_envelope(data):
    """Validate hash envelope before semantic checks, permitting terminal ID replay.

    A well-formed changed payload reserves no new work but conflicts with a known
    ID even when its new source/segments would be semantically invalid.
    """
    fields(data,'performance_id session_id source_ref segments expires_at payload_hash')
    uuid(data['performance_id'])
    result={**data,'expires_at':timestamp(data['expires_at'])}
    validate_hash(data['payload_hash'])
    payload={k:result[k] for k in ('session_id','source_ref','segments','expires_at')}
    try: digest=canonical_hash(payload)
    except (ValueError,TypeError,UnicodeError): raise ValidationError('载荷 JSON 无效。') from None
    if digest!=data['payload_hash']: raise ValidationError('payload_hash 不匹配。')
    return result

def validate_submission(data):
    fields(data,'performance_id session_id source_ref segments expires_at payload_hash')
    uuid(data['performance_id']);uuid(data['session_id']);source_ref(data['source_ref'])
    segments=data['segments']
    if not isinstance(segments,list) or not 1<=len(segments)<=8: raise ValidationError('需要 1–8 段台词。')
    for s in segments:
        fields(s,'text intent')
        if not isinstance(s['text'],str) or not 1<=len(s['text'])<=120 or not s['text'].strip(): raise ValidationError('每段台词须为 1–120 字符。')
        split_text(s['text'])
        if s['intent'] not in INTENTS: raise ValidationError('intent 无效。')
    result={**data,'expires_at':timestamp(data['expires_at'])}
    validate_hash(data['payload_hash'])
    payload={k:result[k] for k in ('session_id','source_ref','segments','expires_at')}
    if canonical_hash(payload)!=data['payload_hash']: raise ValidationError('payload_hash 不匹配。')
    return result
