#!/usr/bin/env python3
"""Manual M3 diagnostic only: no calls without an explicitly authorized CLI flag."""
import argparse
import asyncio
import json
from pathlib import Path
from hell_inc.media_runtime import MediaConfig
from hell_inc.record_only import run_record_only


async def run(args):
    return await run_record_only(MediaConfig.from_env(),args.out,
        sources=json.loads(args.sources.read_text(encoding='utf-8')),
        confirm_paid=args.confirm_paid,max_total_seconds=args.max_total_seconds)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources',type=Path,required=True,help='Exactly two competition_comment and one competition_result freeze API responses; no rewritten text')
    parser.add_argument('--out',type=Path,required=True,help='New absolute private attempt directory; existing directories refuse replay')
    parser.add_argument('--max-total-seconds',type=float,default=90,help='Approved PCM upper bound, at most 90; no lower target or retries')
    parser.add_argument('--confirm-paid',action='store_true',help='Explicitly authorized preparation for original split segments (up to 8), then one Vidu create')
    args=parser.parse_args()
    if not args.confirm_paid:parser.error('No calls made: --confirm-paid requires separate operator authorization')
    try:result=asyncio.run(run(args))
    except (Exception,KeyboardInterrupt):
        print(json.dumps({'state':'failed_or_unknown','action':'Inspect private attempt evidence; never auto-retry'}));return 1
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['cloud_end_confirmed'] else 2


if __name__=='__main__':raise SystemExit(main())
