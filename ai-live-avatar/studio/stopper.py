"""Operator action: exit only an idle or fully finalized local studio."""
import json
from pathlib import Path
import urllib.error
import urllib.request

try:
    runtime = json.loads((Path(__file__).with_name("data") / "runtime.json").read_text())
    if not runtime["url"].startswith("http://127.0.0.1:"):
        raise ValueError("地址不是本机工作台")
    request = urllib.request.Request(runtime["url"] + "quit", data=b"{}",
                                     headers={"Origin": runtime["origin"], "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        result = json.load(response)
    print("已请求退出本机工作台，OBS 来源将显示等待连接。")
except urllib.error.HTTPError as exc:
    print(json.load(exc).get("error", "退出未确认"))
except (OSError, ValueError, KeyError):
    print("未连接到本机工作台；没有停止其他进程。")
