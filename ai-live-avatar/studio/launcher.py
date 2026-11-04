"""Operator-invoked launcher; reuse a healthy local studio, never a paid session."""
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

HERE = Path(__file__).resolve().parent


def existing():
    try:
        runtime = json.loads((HERE / "data/runtime.json").read_text())
        if not runtime["url"].startswith("http://127.0.0.1:"):
            return None
        with urllib.request.urlopen(runtime["url"] + "state", timeout=1) as response:
            state = json.load(response)
        if state.get("studio", {}).get("obs_url") == runtime.get("player_url"):
            return runtime["url"]
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        pass
    return None


def main():
    url = existing()
    if not url:
        (HERE / "data").mkdir(exist_ok=True, mode=0o700)
        log = (HERE / "data/service.log").open("a")
        process = subprocess.Popen([sys.executable, "-B", str(HERE / "server.py")],
                                   cwd=HERE.parent, stdout=log, stderr=log, start_new_session=True)
        log.close()
        for _ in range(40):
            url = existing()
            if url:
                break
            if process.poll() is not None:
                raise SystemExit("工作台未能启动，请查看 studio/data/service.log；未自动重试。")
            time.sleep(.25)
    if not url:
        raise SystemExit("工作台尚未就绪，请查看 studio/data/service.log。")
    print("工作台已打开；尚未启动收费数字人会话。\n" + url)
    webbrowser.open(url)


if __name__ == "__main__":
    main()
