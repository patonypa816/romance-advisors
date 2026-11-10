"""Use existing operator-owned credential files; never copy secrets into this app."""
import csv
import json
import re
from pathlib import Path

LEGACY_SETTINGS = Path(__file__).resolve().parents[2] / "artifacts/zhuyuanzhang-qwen31-libai-test-20261029/local-settings.json"
QWEN_ROUTE_FILE = Path(__file__).resolve().parent / "data/qwen-route.json"

def bailian_config(data):
    """Only model options live here; the API key stays in the existing TTS CSV.

    A missing options file uses the explicitly selected default. A damaged or
    unexpected file fails configuration instead of silently changing routing.
    """
    defaults = {"model": "qwen3.8-flash", "region": "cn-beijing", "enable_thinking": False}
    path = Path(data) / "bailian-brain.json"
    try:
        with path.open("rb") as handle:
            raw = handle.read(16385)
    except FileNotFoundError:
        return defaults
    except OSError:
        return {}
    try:
        value = json.loads(raw) if len(raw) <= 16384 else None
        if not isinstance(value, dict) or set(value) - set(defaults):
            return {}
        return {**defaults, **value}
    except (ValueError, UnicodeError):
        return {}

def cloudbase_config(data):
    """The retained adapter requires an operator-selected environment and CSV.

    Missing configuration leaves only CloudBase unavailable; current Bailian
    routing does not depend on this file. Never put credentials in JSON.
    """
    defaults = {"env_id": "", "region": "ap-shanghai",
                "model": "deepseek-v4.1-flash-1017", "credentials_file": ""}
    path = Path(data) / "cloudbase.json"
    try:
        raw = path.read_text()
        value = json.loads(raw) if len(raw) <= 16384 else {}
    except (OSError, ValueError):
        value = {}
    if isinstance(value, dict):
        defaults.update({k: value[k] for k in defaults if k in value})
    return defaults

def config():
    return json.loads(LEGACY_SETTINGS.read_text())

def load_bailian():
    source = Path(config()["bailian_credentials_file"])
    records = dict(csv.reader(source.read_text(encoding="utf-8-sig").splitlines()))
    key, workspace = records.get("apiKey", "").strip(), records.get("workspaceId", "").strip()
    if not key.startswith("sk-") or not re.fullmatch(r"[A-Za-z0-9-]+", workspace):
        raise ValueError("百炼凭据文件格式不正确")
    if records.get("apiHost", "").removeprefix("https://").rstrip("/") != workspace + ".cn-beijing.maas.aliyuncs.com":
        raise ValueError("百炼凭据地域不匹配")
    return key, workspace

def load_bailian_qwen():
    """Route only text generation to the plan; TTS retains load_bailian()."""
    # Presence is an explicit choice. Invalid/missing plan credentials must not
    # silently charge the original ordinary-API account instead.
    try:
        raw = QWEN_ROUTE_FILE.read_bytes()
    except FileNotFoundError:
        return load_bailian()
    if len(raw) > 8192:
        raise ValueError("invalid Qwen route")
    route = json.loads(raw)
    if not isinstance(route, dict) or set(route) != {"billing", "api_key_file"} or route["billing"] != "token_plan":
        raise ValueError("invalid Qwen route")
    source = Path(route["api_key_file"])
    if not source.is_absolute():
        raise ValueError("absolute credential reference required")
    with source.open(encoding="utf-8-sig") as handle:
        key = handle.read(4097).strip()
    if not re.fullmatch(r"sk-sp-[!-~]{8,253}", key) or any(c.isspace() for c in key):
        raise ValueError("invalid plan credential")
    # The existing transport builds its host from this DNS label. Token Plan
    # has a fixed shared label rather than the ordinary workspace label.
    return key, "token-plan"

def load_vidu():
    key = Path(config()["vidu_credentials_file"]).read_text(encoding="utf-8-sig").strip()
    if not key.startswith("vda_") or len(key) < 20 or any(c.isspace() for c in key):
        raise ValueError("请在已有Vidu凭据文件中填写完整密钥")
    return key
