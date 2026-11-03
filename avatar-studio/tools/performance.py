#!/usr/bin/env python3
"""Local CLI for the universal performance API; never creates a Vidu session."""
import argparse
import json
from pathlib import Path
import sys
from urllib import error, parse, request

DEFAULT_RUNTIME = Path(__file__).resolve().parents[1] / "studio/data/runtime.json"
PERSONA_FIELDS = {"name", "persona", "reply_style"}


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("本地控制请求不接受重定向")


def endpoint(runtime):
    info = json.loads(Path(runtime).read_text())
    url = parse.urlsplit(info["url"])
    if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port
            or url.username or url.password or url.query or url.fragment
            or not url.path.startswith("/studio/") or not url.path.endswith("/")):
        raise ValueError("runtime必须指向本机Studio控制地址")
    origin = "http://" + url.netloc
    if info.get("origin") != origin:
        raise ValueError("runtime来源不一致")
    return origin + url.path, origin


def call(runtime, path, body=None):
    base, origin = endpoint(runtime)
    encoded = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = request.Request(base + path, data=encoded, headers={"Origin": origin, "Content-Type": "application/json"})
    # Local control tokens must not be sent through an environment HTTP proxy.
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=15) as response:
        return json.load(response)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    sub = parser.add_subparsers(dest="command", required=True)
    submit = sub.add_parser("submit", help="提交有限任务；已有云场就绪后会实际调用模型/语音并表演")
    submit.add_argument("file", type=Path)
    sub.add_parser("status", help="读取任务计划与发送进度，不代表视觉验收")
    for command in ("pause", "resume", "cancel"):
        sub.add_parser(command).add_argument("request_id")
    sub.add_parser("export-persona").add_argument("file", type=Path)
    sub.add_parser("import-persona").add_argument("file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            result = call(args.runtime, "performance")
        elif args.command == "submit":
            result = call(args.runtime, "performance/submit", json.loads(args.file.read_text()))
        elif args.command in {"pause", "resume", "cancel"}:
            result = call(args.runtime, "performance/" + args.command, {"request_id": args.request_id})
        elif args.command == "export-persona":
            state = call(args.runtime, "state")
            profile = {key: state["studio"]["settings"][key] for key in PERSONA_FIELDS}
            # Exclusive create: never overwrite a tuned profile without review.
            with args.file.open("x") as handle:
                json.dump(profile, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            result = {"exported": True, "fields": sorted(PERSONA_FIELDS)}
        else:
            profile = json.loads(args.file.read_text())
            if not isinstance(profile, dict) or set(profile) != PERSONA_FIELDS:
                raise ValueError("角色文件只接受name/persona/reply_style，不接受密钥或运行状态")
            result = call(args.runtime, "settings", profile)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except error.HTTPError as exc:
        # HTTPError.__str__ can include the secret control URL.
        print("本机工作台拒绝请求，HTTP " + str(exc.code), file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError):
        print("本机连接、输入文件或runtime无效；请检查工作台和文件，不会自动重试。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
