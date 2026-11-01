"""Narrow local bridge used by the active ChatGPT turn, with no LLM API call."""
import argparse
import json
from pathlib import Path
import sys
import urllib.request
import urllib.error


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["next", "reply", "status", "stop"])
    parser.add_argument("--wait", type=int, default=25)
    args = parser.parse_args()
    runtime = json.loads((Path(__file__).with_name("data") / "runtime.json").read_text())
    url = runtime["url"]
    origin = runtime["origin"]
    headers = {"X-Brain-Token": runtime["brain_token"], "Origin": origin}
    payload = None
    if args.command == "next":
        path = f"brain/next?wait={min(max(args.wait, 0), 25)}"
    elif args.command == "reply":
        path = "brain/reply"
        payload = json.dumps(json.load(sys.stdin), ensure_ascii=False).encode()
    elif args.command == "stop":
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
        # Only this local app's fixed validation response is printed, never headers.
        if error.code == 400 and args.command == "reply":
            result = json.load(error)
        else:
            result = {"error": "local_bridge_request_failed", "status": error.code}
    if args.command == "next" and result.get("pending") is None and "session" in result:
        result = {"pending": None, "state": result["session"].get("state"),
                  "stopping": result.get("stopping", False)}
    # The local bearer token stays in memory; only the pending comment is exposed.
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
