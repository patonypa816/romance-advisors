"""Local status/stop helper; content generation is owned by the workspace."""
import argparse
import json
from pathlib import Path
import urllib.request
import urllib.error


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["status", "stop"])
    args = parser.parse_args()
    runtime = json.loads((Path(__file__).with_name("data") / "runtime.json").read_text())
    url = runtime["url"]
    origin = runtime["origin"]
    headers = {"X-Brain-Token": runtime["brain_token"], "Origin": origin}
    payload = None
    if args.command == "stop":
        path, payload = "stop", b"{}"
    else:
        path = "state"
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url + path, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        result = {"error": "local_bridge_request_failed", "status": error.code}
    # The local bearer token stays in memory.
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
