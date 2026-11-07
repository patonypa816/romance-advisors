"""Start or stop only this project's local workbench, preserving its game data."""
import argparse
import json
import os
import signal
import socket
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


def ensure_running(*, obs_output=False, port=0):
    """Reuse only the game's own database and, for a broadcast, the saved OBS address."""
    current = running()
    if not current:
        if port:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    raise SystemExit(f"节目源端口 {port} 正被其他服务使用，请核对后重新准备。")
        directory = ROOT / "data"
        directory.mkdir(exist_ok=True)
        command = [sys.executable, "-B", "-m", "romance.server"]
        if obs_output:
            command.append("--obs-output")
        if port:
            command.extend(["--port", str(port)])
        with (directory / "server.log").open("ab") as output:
            process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=output, start_new_session=True)
        for _ in range(50):
            time.sleep(.2)
            current = running()
            if current or process.poll() is not None:
                break
        if not current:
            raise SystemExit("游戏工作台未就绪，请查看 romance-advisors/data/server.log。")
    if obs_output:
        if urlsplit(current["url"]).port != port:
            raise SystemExit("已运行的游戏工作台端口与 OBS 节目源不同。请在空闲时关闭原工作台，再点开播准备。")
        with urllib.request.urlopen(current["url"] + "/api/obs-output", timeout=2) as response:
            if json.load(response).get("obs_output") is not True:
                raise SystemExit("已有游戏工作台处于预览模式。请在空闲时关闭它，再点开播准备。")
    return current


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
    current = ensure_running()
    print("游戏工作台：" + current["url"])
    print("透明叠层：" + current["url"] + "/overlay")
    if not args.no_open:
        webbrowser.open(current["url"])


if __name__ == "__main__":
    main()
