"""Prepare only the existing, dedicated macOS OBS output; never start a stream.

OBS launch flags: https://obsproject.com/kb/launch-parameters/
VCam type2=3: obsproject/obs-studio frontend/utility/VCamConfig.hpp.
No passwords, streaming-service configuration or companion account data are read.
"""
from __future__ import annotations

import asyncio
import configparser
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from urllib.parse import urlsplit

import aiohttp

SOURCE = "恋爱军师团专用节目页"
OBS_APP = "/Applications/OBS.app"
COMPANION_APP = "/Applications/Douyin Webcast Mate.app"
COMPANION_PROCESS = "Douyin Webcast Mate"
MANUAL = ["在直播伴侣确认视频来源为 OBS Virtual Camera、画面为竖屏节目页。",
          "在直播伴侣确认音频输入为 BlackHole 2ch，并检查真实口播电平与试听声音。",
          "确认直播间、画面和声音后，由你在直播伴侣点击开播。"]


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def inspect_setup(obs_root=None):
    """Read a uniquely identifiable existing collection/profile, without writes.

    `ready` means saved OBS configuration passed checks. It does not mean OBS,
    the browser receiver, the companion input or audible playback is running.
    """
    root = Path(obs_root) if obs_root else Path.home() / "Library/Application Support/obs-studio"
    result = {"ready": False, "port": None, "obs_root": str(root), "source": SOURCE, "issues": []}
    candidates = []
    try:
        for path in (root / "basic/scenes").glob("*.json"):
            collection = _read_json(path)
            sources = [s for s in collection.get("sources", []) if s.get("name") == SOURCE]
            if len(sources) != 1:
                continue
            source = sources[0]
            scenes = [s for s in collection.get("sources", []) if s.get("id") == "scene"
                      and any(i.get("name") == SOURCE for i in s.get("settings", {}).get("items", []))]
            for scene in scenes:
                candidates.append((path, collection, source, scene))
        if len(candidates) != 1:
            result["issues"].append("未找到唯一的恋爱军师团专用 OBS 场景，请在 OBS 核对专用来源和场景集合。")
            return result
        path, collection, source, scene = candidates[0]
        result.update(collection=collection.get("name"), scene=scene.get("name"), collection_file=str(path))
        if any(not isinstance(result[key], str) or not result[key].strip() for key in ("collection", "scene")):
            result["issues"].append("专用场景或集合缺少名称，请在 OBS 保存有效配置。")
            return result
        settings = source.get("settings", {})
        url = urlsplit(settings.get("url", ""))
        if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port
                or url.username or url.password or url.query or url.fragment
                or url.path != "/static/obs-output.html"):
            result["issues"].append("专用节目页必须使用无凭据的 127.0.0.1 本机 OBS 输出地址。")
        else:
            result.update(url=url.geturl(), port=url.port)
        items = scene.get("settings", {}).get("items", [])
        visible = [item for item in items if item.get("name") == SOURCE and item.get("visible") is True]
        if len(visible) != 1 or source.get("id") != "browser_source":
            result["issues"].append("场景中需要一个可见的恋爱军师团节目页，请在 OBS 核对。")
        if (settings.get("width"), settings.get("height")) != (1080, 1920):
            result["issues"].append("专用浏览器来源尺寸不是 1080×1920。")
        if settings.get("reroute_audio") is not True or source.get("monitoring_type") != 2:
            result["issues"].append("专用浏览器音频须由 OBS 控制并设为监听和输出。")
        if collection.get("virtual-camera", {}).get("type2") != 3:
            result["issues"].append("OBS 虚拟摄像机须输出 Program（节目），请在 OBS 核对。")
        profiles = []
        for profile_path in (root / "basic/profiles").glob("*/basic.ini"):
            profile = configparser.ConfigParser(interpolation=None)
            profile.read(profile_path, encoding="utf-8")
            if profile.get("General", "Name", fallback="") == result["collection"] or profile_path.parent.name == path.stem:
                profiles.append((profile_path, profile))
        if len(profiles) != 1:
            result["issues"].append("无法唯一确定此专用场景配套的 OBS 配置，请在 OBS 核对。")
        else:
            profile_path, profile = profiles[0]
            result["profile"] = profile.get("General", "Name", fallback=profile_path.parent.name)
            result["profile_file"] = str(profile_path)
            if any(profile.getint("Video", key, fallback=0) != size
                   for key, size in (("BaseCX", 1080), ("BaseCY", 1920), ("OutputCX", 1080), ("OutputCY", 1920))):
                result["issues"].append("配套 OBS 画布和输出尺寸须均为 1080×1920。")
            monitor = profile.get("Audio", "MonitoringDeviceName", fallback="")
            result["audio_monitor"] = monitor
            if "blackhole" not in monitor.lower():
                result["issues"].append("配套 OBS 监听设备未确认包含 BlackHole，请在 OBS 核对音频路由。")
        result["ready"] = not result["issues"]
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, configparser.Error):
        result["issues"].append("无法完整读取专用 OBS 配置；请先在 OBS 检查并保存配置。")
    return result


def _process_started(name):
    """Use exact executable names; do not inspect unrelated command arguments."""
    found = subprocess.run(["/usr/bin/pgrep", "-x", name], capture_output=True, text=True, timeout=3)
    pids = found.stdout.split()
    if found.returncode == 1:
        return None
    if found.returncode or len(pids) != 1 or not pids[0].isdigit():
        raise RuntimeError("无法唯一确认正在运行的软件实例。")
    started = subprocess.run(["/bin/ps", "-p", pids[0], "-o", "lstart="],
                             capture_output=True, text=True, timeout=3, env={"LC_ALL": "C"})
    if started.returncode:
        raise RuntimeError("无法确认软件实例的启动时间。")
    return datetime.strptime(started.stdout.strip(), "%a %b %d %H:%M:%S %Y").timestamp()


def _open(args):
    completed = subprocess.run(["/usr/bin/open", *args], capture_output=True, timeout=8)
    if completed.returncode:
        raise RuntimeError("macOS 未能打开软件，请检查应用是否安装及系统权限。")


def _log_status(setup, started):
    """Only accept this exact process's fresh startup log, never yesterday's."""
    result = {"confirmed": False, "evidence": "unconfirmed"}
    for path in sorted((Path(setup["obs_root"]) / "logs").glob("*.txt"), reverse=True):
        try:
            log_start = datetime.strptime(path.stem, "%Y-%m-%d %H-%M-%S").timestamp()
            if abs(log_start - started) > 5:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "==== Shutting down" in text:
                continue
            command = re.search(r"Command Line Arguments: (.*)", text)
            args = shlex.split(command[1]) if command else []
            correct_args = all(flag in args and args.index(flag) + 1 < len(args)
                               and args[args.index(flag) + 1] == setup[key]
                               for flag, key in (("--profile", "profile"), ("--collection", "collection"), ("--scene", "scene")))
            scenes = re.findall(r"Switched to scene '([^']*)'", text)
            virtual = re.findall(r"Starting Virtual Camera output to ([^\r\n]+)|==== Virtual Camera (Stop)", text)
            active_program = bool(virtual and virtual[-1][0] == "Program")
            if (correct_args and scenes and scenes[-1] == setup["scene"] and active_program
                    and re.search(r"base resolution:\s+1080x1920", text)
                    and re.search(r"output resolution:\s+1080x1920", text)
                    and setup["audio_monitor"] in text and SOURCE in text):
                result.update(confirmed=True, evidence="current_process_log", log_file=path.name)
            return result
        except (OSError, ValueError):
            continue
    return result


async def _websocket_status(setup):
    # Reuse the existing protocol implementation, explicitly without a password
    # file. An authenticated server becomes a manual action, never a secret read.
    path = Path(__file__).resolve().parents[2] / "ai-live-avatar/studio/obs_setup.py"
    spec = importlib.util.spec_from_file_location("romance_obs_inspection", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as http:
        obs = await module.OBS.connect(http, "ws://127.0.0.1:4455", None)
        try:
            scene = await obs.call("GetCurrentProgramScene")
            profile = await obs.call("GetProfileList")
            collection = await obs.call("GetSceneCollectionList")
            video = await obs.call("GetVideoSettings")
            settings = (await obs.call("GetInputSettings", {"inputName": SOURCE})).get("inputSettings", {})
            items = (await obs.call("GetSceneItemList", {"sceneName": setup["scene"]})).get("sceneItems", [])
            monitor = await obs.call("GetInputAudioMonitorType", {"inputName": SOURCE})
            if (scene.get("currentProgramSceneName", scene.get("sceneName")) != setup["scene"]
                    or profile.get("currentProfileName") != setup["profile"]
                    or collection.get("currentSceneCollectionName") != setup["collection"]
                    or any(video.get(key) != size for key, size in (("baseWidth", 1080), ("baseHeight", 1920), ("outputWidth", 1080), ("outputHeight", 1920)))
                    or settings.get("url") != setup["url"] or settings.get("reroute_audio") is not True
                    or (settings.get("width"), settings.get("height")) != (1080, 1920)
                    or sum(1 for item in items if item.get("sourceName") == SOURCE and item.get("sceneItemEnabled")) != 1
                    or monitor.get("monitorType") != "OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT"):
                return {"confirmed": False, "evidence": "websocket", "issue": "运行中的 OBS 与专用配置不一致；未切场或修改，请在 OBS 核对。"}
            virtual = await obs.call("GetVirtualCamStatus")
            if not virtual.get("outputActive"):
                # Only the already-correct output may be started; an unrelated
                # stream is never stopped or reconfigured to make checks pass.
                if (await obs.call("GetStreamStatus")).get("outputActive") or (await obs.call("GetRecordStatus")).get("outputActive"):
                    return {"confirmed": False, "evidence": "websocket", "issue": "OBS 正在推流或录像，未额外启动虚拟摄像机，请先核对现有输出。"}
                await obs.call("StartVirtualCam")
                virtual = await obs.call("GetVirtualCamStatus")
            # WebSocket does not expose the virtual camera's Program/Preview
            # selector or global monitor device; corroborate them with this
            # process's log before claiming the full route is ready.
            return {"confirmed": bool(virtual.get("outputActive")), "evidence": "websocket"}
        finally:
            await obs.ws.close()


async def prepare_apps(setup, *, wait_seconds=8):
    """Open the existing dedicated apps and report observed preparation state.

    Safe to call from the preparation service. Does not start game/model/avatar,
    record, stream, select companion sources, or edit any OBS configuration.
    """
    result = {"ready": False, "obs_ready": False, "companion_open": False,
              "issues": [], "manual_steps": list(MANUAL), "obs_evidence": "unconfirmed"}
    if sys.platform != "darwin":
        result["issues"].append("自动准备 OBS 和直播伴侣仅支持本机 macOS。")
        return result
    checked = inspect_setup(setup.get("obs_root"))
    if not checked["ready"] or any(checked.get(key) != setup.get(key) for key in ("url", "port", "profile", "collection", "scene")):
        result["issues"] = checked["issues"] or ["OBS 配置在检查后已变化，请重新准备。"]
        return result
    try:
        started = await asyncio.to_thread(_process_started, "OBS")
        if started is None:
            if not Path(OBS_APP).is_dir():
                result["issues"].append("未找到 OBS.app，请先在本机安装或打开 OBS。")
                return result
            await asyncio.to_thread(_open, ["-a", OBS_APP, "--args", "--profile", setup["profile"],
                                           "--collection", setup["collection"], "--scene", setup["scene"], "--startvirtualcam"])
        deadline = time.monotonic() + max(0, min(wait_seconds, 20))
        observation = None
        while True:
            started = await asyncio.to_thread(_process_started, "OBS")
            if started is not None:
                log = await asyncio.to_thread(_log_status, setup, started)
                try:
                    observation = await asyncio.wait_for(_websocket_status(setup), timeout=5)
                    if observation.get("issue"):
                        result["issues"].append(observation["issue"])
                        break
                except Exception:
                    observation = None
                if log["confirmed"] and (observation is None or observation["confirmed"]):
                    result.update(obs_ready=True, obs_evidence="websocket_and_current_log" if observation else "current_process_log")
                    break
            if time.monotonic() >= deadline:
                result["issues"].append("尚未确认 OBS 专用节目画面、Program 虚拟摄像机和 BlackHole 路由；请在 OBS 核对，必要时手动启动虚拟摄像机。")
                break
            await asyncio.sleep(0.5)
        if not Path(COMPANION_APP).is_dir():
            result["issues"].append("未找到抖音直播伴侣，请先在本机安装或打开。")
        else:
            if await asyncio.to_thread(_process_started, COMPANION_PROCESS) is None:
                await asyncio.to_thread(_open, ["-a", COMPANION_APP])
            result["companion_open"] = await asyncio.to_thread(_process_started, COMPANION_PROCESS) is not None
            if not result["companion_open"]:
                result["issues"].append("直播伴侣尚未确认打开，请检查系统提示或手动打开。")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        result["issues"].append("软件启动或状态检查未完成，请检查 macOS 权限和正在运行的软件。")
    return result
