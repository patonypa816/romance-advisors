"""Operator-invoked launcher; reuse a healthy local studio, never a paid session."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent


def existing(*, require_obs=False):
    try:
        runtime = json.loads((HERE / "data/runtime.json").read_text())
        parsed = urlsplit(runtime["url"])
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or not parsed.path.startswith("/studio/") or not parsed.path.endswith("/")):
            return None
        if require_obs and Path(runtime["run_dir"]).resolve().parent != (HERE / "data/runs").resolve():
            raise SystemExit("Studio 运行记录来自另一份目录，请先核对工作台位置。")
        with urllib.request.urlopen(runtime["url"] + "state", timeout=1) as response:
            state = json.load(response)
        if state.get("studio", {}).get("obs_url") == runtime.get("player_url"):
            if require_obs and state["studio"].get("receiver_mode") != "obs":
                raise SystemExit("已有 Studio 处于预览模式。请在空闲时正常退出后，再点开播准备。")
            return runtime["url"]
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        pass
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-obs", action="store_true",
                        help="准备已有 OBS 节目工作台，页面由游戏统一打开")
    args = parser.parse_args()
    url = existing(require_obs=args.prepare_obs)
    if not url:
        (HERE / "data").mkdir(exist_ok=True, mode=0o700)
        log = (HERE / "data/service.log").open("a")
        process = subprocess.Popen([sys.executable, "-B", str(HERE / "server.py")],
                                   cwd=HERE.parent, stdout=log, stderr=log, start_new_session=True)
        log.close()
        for _ in range(40):
            url = existing(require_obs=args.prepare_obs)
            if url:
                break
            if process.poll() is not None:
                raise SystemExit("工作台未能启动，请查看 studio/data/service.log；未自动重试。")
            time.sleep(.25)
    if not url:
        raise SystemExit("工作台尚未就绪，请查看 studio/data/service.log。")
    if args.prepare_obs:
        print("数字人 Studio 已就绪（OBS 接收）。")
    else:
        print("工作台已打开；尚未启动收费数字人会话。\n" + url)
        webbrowser.open(url)


if __name__ == "__main__":
    main()
