"""Prepare the local programme stack using the port stored in its OBS browser source."""
import asyncio
import fcntl
import json
from pathlib import Path
import socket
import subprocess
import time
import urllib.request
import webbrowser

from . import launcher

GAME = launcher.ROOT
PROJECT = GAME.parent
STUDIO = PROJECT / "ai-live-avatar"
DYHUB = PROJECT / "dyhub"
HUB_PORT = 8757


def local_json(url):
    with urllib.request.urlopen(url, timeout=1.5) as response:
        return json.load(response)


def dyhub_ready():
    """An unrelated listener or an older build cannot provide room lifecycle evidence."""
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", HUB_PORT)) != 0:
            return False
    try:
        health = local_json(f"http://127.0.0.1:{HUB_PORT}/api/health")
        pid = health["pid"]
        if type(pid) is not int or pid <= 0 or health["app"] != "dyhub" or not health["features"]["live_status"]:
            raise ValueError()
        # Check the OS owner as well as the application's self-reported identity.
        listeners = subprocess.check_output(
            ["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), f"-iTCP:{HUB_PORT}", "-sTCP:LISTEN", "-t"], text=True)
        cwd = subprocess.check_output(["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], text=True)
        if str(pid) not in listeners.splitlines() or "n" + str(DYHUB.resolve()) not in cwd.splitlines():
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError):
        raise SystemExit("8757 端口上的服务不是本项目新版 DyHub。请先处理端口冲突或更新采集器，再重新准备。") from None
    return True


def ensure_dyhub():
    if dyhub_ready():
        return
    if not (DYHUB / "dist/index.js").is_file():
        raise SystemExit("DyHub 尚未构建。请先在 dyhub 目录完成安装并执行 npm run build。")
    (DYHUB / "data").mkdir(mode=0o700, exist_ok=True)
    logfile = DYHUB / "data/prepare-service.log"
    with logfile.open("ab") as log:
        process = subprocess.Popen([str(STUDIO / "studio/启动评论采集.command")], cwd=DYHUB,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(80):
        if process.poll() is not None:
            break
        if dyhub_ready():
            return
        time.sleep(.25)
    raise SystemExit("本机采集器尚未就绪，请检查 dyhub/data/prepare-service.log。")


def prepare():
    from .broadcast_apps import inspect_setup, prepare_apps
    setup = inspect_setup()
    if not setup["ready"]:
        raise SystemExit("\n".join(setup["issues"]) or "请先配置 OBS 专用竖屏节目场景。")
    studio_python = STUDIO / ".venv/bin/python"
    if not studio_python.is_file():
        raise SystemExit("缺少 Studio 的 Python 环境。请先按根 README 配置本机依赖。")
    # Local components only. Creating a cloud session and public streaming are
    # separate operator actions even if the room is already live.
    subprocess.run([str(studio_python), "-B", str(STUDIO / "studio/launcher.py"), "--prepare-obs"],
                   cwd=STUDIO, check=True)
    ensure_dyhub()
    print("本机评论采集器已就绪。", flush=True)
    game = launcher.ensure_running(obs_output=True, port=setup["port"])
    source = local_json(game["url"] + "/api/source")
    if "auto" not in source:
        raise SystemExit("现有游戏工作台需要更新。请在空闲时正常退出，再点开播准备。")
    print("游戏工作台（OBS 输出）：" + game["url"], flush=True)
    result = asyncio.run(prepare_apps(setup))
    webbrowser.open(game["url"])
    for message in result.get("issues", []) + result.get("manual_steps", []):
        print(message)
    if not result["obs_ready"] or not result["companion_open"]:
        raise SystemExit("本机服务已准备。请按上面的提示完成音画准备后再开播。")
    print("OBS 虚拟摄像机已开启，直播伴侣已打开。画面和声音核对后可按「开始直播」。")


def main():
    directory = GAME / "data"
    directory.mkdir(exist_ok=True)
    with (directory / "prepare.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("另一窗口正在准备开播，请留意原窗口。") from None
        try:
            prepare()
        except (OSError, ValueError, subprocess.CalledProcessError):
            raise SystemExit("开播准备未完成，请检查上一步提示和本机工作台日志。") from None


if __name__ == "__main__":
    main()
