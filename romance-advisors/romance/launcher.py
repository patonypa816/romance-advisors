"""Start or stop only this project's local workbench, preserving its game data."""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "data/runtime.json"


def running():
    try:
        value = json.loads(RUNTIME.read_text())
        parsed = urlsplit(value["url"])
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.path:
            return None
        request = urllib.request.Request(value["url"] + "/api/health")
        with urllib.request.urlopen(request, timeout=1) as response:
            actual = json.load(response)
        if actual.get("app") != "romance-advisors" or actual.get("pid") != value.get("pid"):
            return None
        # A copied project may contain another instance's runtime file. Match
        # the database as well as the PID before opening or stopping it.
        expected = str((ROOT / "data/game.sqlite").resolve())
        if actual.get("database") != expected or value.get("database") != expected:
            return None
        return value
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    current = running()
    if args.stop:
        if not current:
            print("本项目工作台未运行。")
            return
        os.kill(current["pid"], signal.SIGTERM)
        print("已停止本项目工作台，聊天与榜单数据保留。")
        return
    if not current:
        directory = ROOT / "data"
        directory.mkdir(exist_ok=True)
        with (directory / "server.log").open("ab") as output:
            process = subprocess.Popen([sys.executable, "-m", "romance.server"], cwd=ROOT,
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                       start_new_session=True)
        for _ in range(40):
            time.sleep(.2)
            current = running()
            if current:
                break
            if process.poll() is not None:
                break
        if not current:
            raise SystemExit("启动失败，请查看本项目data/server.log。")
    print("游戏工作台：" + current["url"])
    print("透明叠层：" + current["url"] + "/overlay")
    if not args.no_open:
        webbrowser.open(current["url"])


if __name__ == "__main__":
    main()
