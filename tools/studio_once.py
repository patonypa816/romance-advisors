#!/usr/bin/env python3
"""Preview real game facts as a Studio once arrangement; submit only with explicit authorization."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from hell_inc.studio_once import InputError, build_once, control_endpoint, digest, submit_once


def save(handle, value):
    handle.seek(0)
    json.dump(value, handle, ensure_ascii=False, indent=2)
    handle.truncate()
    handle.flush()
    os.fsync(handle.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, required=True, help='Two comment + one result freeze responses, in that order')
    parser.add_argument('--submit', action='store_true', help='Explicit authorization: existing Studio may immediately invoke paid models/TTS/media')
    parser.add_argument('--receipt', type=Path, help='New private receipt file; an existing file refuses repeat submission')
    args = parser.parse_args()
    try:
        prepared = build_once(json.loads(args.sources.read_text(encoding='utf-8')))
        if not args.submit:
            print(json.dumps({'state': 'preview_only', **prepared}, ensure_ascii=False, indent=2))
            return 0
        if args.receipt is None:
            parser.error('--submit requires --receipt; unknown outcomes must not be blindly resubmitted')
        control_url = os.environ.get('STUDIO_CONTROL_URL', '')
        control_endpoint(control_url)  # validate before creating an attempt record
        # O_EXCL blocks reuse. Record unknown durably BEFORE the single network call.
        fd = os.open(args.receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            record = {'state': 'unknown', 'attempted_at': datetime.now(timezone.utc).isoformat(),
                      **prepared, 'body_hash': digest(prepared['body']),
                      'next_action': '在原工作台核对安排与计划；提交不等于播放完成，未知不得盲目重发。'}
            save(handle, record)
            result = asyncio.run(submit_once(prepared, control_url))
            record.update(result)
            save(handle, record)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['state'] == 'accepted' else 2
    except InputError as error:
        print(json.dumps({"state": "not_submitted", "error": str(error)}, ensure_ascii=False))
        return 2
    except (Exception, KeyboardInterrupt) as error:
        # No raw exception/path/URL output: the configured URL contains control credentials.
        print(json.dumps({'state': 'not_submitted_or_unknown',
            'error_type': 'interrupted' if isinstance(error, KeyboardInterrupt) else ('receipt_exists' if isinstance(error, FileExistsError) else 'local_error'),
            'next_action': '检查输入、既有控制入口和私有回执；如已有尝试记录请先在原工作台核对，不要重发。'}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
