"""Optional, narrowly scoped OBS WebSocket v5 helper.

inspect reads OBS state; setup only owns its named scene/source for one player URL.
Neither command starts OBS, streaming, recording or a virtual camera. Credentials
are read only when an authenticated connection is requested and never printed.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

SCENE = "御书房工作台"
SOURCE = "御书房工作台 · 数字人"
WIDTH, HEIGHT = 1080, 1920
SAFE_SETTINGS = ("url", "width", "height", "reroute_audio", "shutdown", "restart_when_active")


class OBSFailure(RuntimeError):
    pass


class OBS:
    def __init__(self, ws):
        self.ws = ws

    @classmethod
    async def connect(cls, http, url: str, password_file: Path | None):
        try:
            ws = await http.ws_connect(url)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as error:
            raise OBSFailure("OBS WebSocket 不可用；请先打开 OBS 并检查已启用的本机 WebSocket。不会自动修改 OBS 配置。") from error
        try:
            hello = await asyncio.wait_for(ws.receive_json(), 8)
            if hello.get("op") != 0:
                raise OBSFailure("OBS 未返回兼容的 WebSocket v5 握手。")
            identify = {"rpcVersion": 1, "eventSubscriptions": 0}
            authentication = hello.get("d", {}).get("authentication")
            if authentication:
                if password_file is None:
                    raise OBSFailure("OBS 要求认证，请通过 --password-file 指定已有密码文件；不要把密码写在命令行中。")
                try:
                    password = password_file.expanduser().read_text().strip()
                except (OSError, UnicodeError) as error:
                    raise OBSFailure("无法读取指定的 OBS 密码文件。") from error
                if not password:
                    raise OBSFailure("指定的 OBS 密码文件为空。")
                secret = base64.b64encode(hashlib.sha256((password + authentication["salt"]).encode()).digest()).decode()
                identify["authentication"] = base64.b64encode(hashlib.sha256((secret + authentication["challenge"]).encode()).digest()).decode()
            await ws.send_json({"op": 1, "d": identify})
            reply = await asyncio.wait_for(ws.receive_json(), 8)
            if reply.get("op") != 2:
                raise OBSFailure("OBS 认证未通过，请在 OBS 中核对连接设置。")
            return cls(ws)
        except BaseException:
            await ws.close()
            raise

    async def call(self, request: str, data: dict | None = None):
        request_id = uuid.uuid4().hex
        await self.ws.send_json({"op": 6, "d": {"requestType": request, "requestId": request_id, "requestData": data or {}}})
        async with asyncio.timeout(8):
            while True:
                message = await self.ws.receive_json()
                result = message.get("d", {})
                if message.get("op") == 7 and result.get("requestId") == request_id:
                    status = result.get("requestStatus", {})
                    if not status.get("result"):
                        # OBS comments can echo user data; bounded codes are enough for diagnosis.
                        raise OBSFailure(f"OBS 的 {request} 请求失败（代码 {status.get('code', '未知')}）。")
                    return result.get("responseData") or {}


async def read_owned(obs: OBS):
    scenes = await obs.call("GetSceneList")
    inputs = await obs.call("GetInputList")
    scene_exists = any(row.get("sceneName") == SCENE for row in scenes.get("scenes", []))
    source = next((row for row in inputs.get("inputs", []) if row.get("inputName") == SOURCE), None)
    settings = None
    if source:
        settings = await obs.call("GetInputSettings", {"inputName": SOURCE})
    items = await obs.call("GetSceneItemList", {"sceneName": SCENE}) if scene_exists else {"sceneItems": []}
    return scene_exists, source, settings, items.get("sceneItems", [])


async def inspect(obs: OBS):
    exists, source, settings, items = await read_owned(obs)
    stream = await obs.call("GetStreamStatus")
    record = await obs.call("GetRecordStatus")
    return {"scene": SCENE, "scene_exists": exists, "source": SOURCE,
            "source_exists": source is not None,
            "source_kind": source.get("inputKind") if source else None,
            "settings": {key: (settings or {}).get("inputSettings", {}).get(key) for key in SAFE_SETTINGS} if settings else None,
            "scene_items": [{"id": item.get("sceneItemId"), "source": item.get("sourceName"),
                             "enabled": item.get("sceneItemEnabled"), "transform": item.get("sceneItemTransform")} for item in items],
            "streaming": bool(stream.get("outputActive")), "recording": bool(record.get("outputActive"))}


async def setup(obs: OBS, url: str):
    exists, source, current, items = await read_owned(obs)
    # 同名不等于属于本次工作台：先完整判断归属，之后才允许第一次写入。
    if source and (source.get("inputKind") != "browser_source" or current.get("inputSettings", {}).get("url") != url):
        raise OBSFailure("同名来源不属于当前播放器 URL；未修改，请先在 OBS 中自行检查命名冲突。")
    if exists and (not source or not items or any(item.get("sourceName") != SOURCE for item in items) or len(items) != 1):
        raise OBSFailure("同名场景无法确认为本工作台独占；未修改，请先在 OBS 中检查。")
    # 即使来源属于自己，改设置也可能重载正在录像的 RTC 页面；输出活跃时只允许 inspect。
    for request, label in (("GetStreamStatus", "推流"), ("GetRecordStatus", "录像"), ("GetVirtualCamStatus", "虚拟摄像头")):
        if (await obs.call(request)).get("outputActive"):
            raise OBSFailure(f"OBS 正在{label}，未修改任何场景或来源；请先结束该输出，再运行 setup。")
    canvas = await obs.call("GetVideoSettings")
    width, height = int(canvas["baseWidth"]), int(canvas["baseHeight"])
    settings = {"url": url, "width": WIDTH, "height": HEIGHT, "reroute_audio": True,
                "shutdown": False, "restart_when_active": False}
    if not exists:
        await obs.call("CreateScene", {"sceneName": SCENE})
    if source:
        if any(current.get("inputSettings", {}).get(key) != value for key, value in settings.items()):
            await obs.call("SetInputSettings", {"inputName": SOURCE, "inputSettings": settings, "overlay": True})
        item_id = items[0]["sceneItemId"] if items else (await obs.call("CreateSceneItem", {"sceneName": SCENE, "sourceName": SOURCE, "sceneItemEnabled": True}))["sceneItemId"]
    else:
        item_id = (await obs.call("CreateInput", {"sceneName": SCENE, "inputName": SOURCE,
                   "inputKind": "browser_source", "inputSettings": settings, "sceneItemEnabled": True}))["sceneItemId"]
    # 场景内等比容纳竖屏底图，保留用户原有的全局画布和其他场景。
    scale = min(width / WIDTH, height / HEIGHT)
    await obs.call("SetSceneItemTransform", {"sceneName": SCENE, "sceneItemId": item_id, "sceneItemTransform": {
        "positionX": (width - WIDTH * scale) / 2, "positionY": (height - HEIGHT * scale) / 2,
        "scaleX": scale, "scaleY": scale, "alignment": 5, "rotation": 0,
        "cropLeft": 0, "cropRight": 0, "cropTop": 0, "cropBottom": 0, "boundsType": "OBS_BOUNDS_NONE"}})
    await obs.call("SetInputAudioMonitorType", {"inputName": SOURCE, "monitorType": "OBS_MONITORING_TYPE_NONE"})
    result = await inspect(obs)
    if result["settings"] != settings or len(result["scene_items"]) != 1:
        raise OBSFailure("已提交配置，但 OBS 回读不一致；请保留现场检查，未自动重试或删除。")
    result["configured"] = True
    result["note"] = "仅配置专用来源与场景；未切换场景、推流、录像或启动虚拟摄像头。请在 OBS 检查实际画面与音频电平。"
    return result


def local_url(value: str, *, websocket: bool = False):
    parsed = urlparse(value)
    if parsed.scheme != ("ws" if websocket else "http") or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password or parsed.fragment:
        raise argparse.ArgumentTypeError("只接受无内嵌凭据的 127.0.0.1 本机地址。")
    if not websocket and (not parsed.path.startswith("/player/") or not parsed.path.endswith("/") or parsed.query):
        raise argparse.ArgumentTypeError("播放器地址应为 http://127.0.0.1:端口/player/标识/。")
    return value


async def run(args):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
        obs = await OBS.connect(http, args.ws_url, args.password_file)
        try:
            return await (setup(obs, args.url) if args.command == "setup" else inspect(obs))
        finally:
            await obs.ws.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ws-url", type=lambda value: local_url(value, websocket=True), default="ws://127.0.0.1:4455")
    parser.add_argument("--password-file", type=Path, help="已有 OBS WebSocket 密码文件；仅连接认证时读取，不回显")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect", help="只读检查专用场景、来源和输出状态")
    setup_parser = commands.add_parser("setup", help="创建或核对专用场景和浏览器源")
    setup_parser.add_argument("--url", type=local_url, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))
        return 0
    except OBSFailure as error:
        print(str(error), file=sys.stderr)
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError, KeyError, TypeError):
        print("OBS 连接或响应未能确认；请检查 OBS 状态。未自动重试或更改连接配置。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
