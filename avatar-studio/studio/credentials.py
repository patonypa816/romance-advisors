"""Use existing operator-owned credential files; never copy secrets into this app."""
import csv
import json
import os
import re
from pathlib import Path

LEGACY_SETTINGS = Path(__file__).resolve().parents[2] / "artifacts/zhuyuanzhang-qwen31-libai-test-20261029/local-settings.json"

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
    """Only an external CSV reference is stored; never put credentials in JSON."""
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
    # Any environment override must be complete: never silently mix sources.
    if "BAILIAN_API_KEY" in os.environ or "BAILIAN_WORKSPACE_ID" in os.environ:
        key = os.environ.get("BAILIAN_API_KEY", "").strip()
        workspace = os.environ.get("BAILIAN_WORKSPACE_ID", "").strip()
    else:
        source = Path(config()["bailian_credentials_file"])
        records = dict(csv.reader(source.read_text(encoding="utf-8-sig").splitlines()))
        key, workspace = records.get("apiKey", "").strip(), records.get("workspaceId", "").strip()
        if records.get("apiHost", "").removeprefix("https://").rstrip("/") != workspace + ".cn-beijing.maas.aliyuncs.com":
            raise ValueError("百炼凭据地域不匹配")
    if not key.startswith("sk-") or not re.fullmatch(r"[A-Za-z0-9-]+", workspace):
        raise ValueError("百炼凭据格式不正确")
    return key, workspace


def load_vidu():
    if "VIDU_API_KEY" in os.environ:
        key = os.environ["VIDU_API_KEY"].strip()
    else:
        key = Path(config()["vidu_credentials_file"]).read_text(encoding="utf-8-sig").strip()
    if not key.startswith("vda_") or len(key) < 20 or any(c.isspace() for c in key):
        raise ValueError("请填写完整Vidu密钥")
    return key


def source_available(names, legacy_settings=LEGACY_SETTINGS):
    """Check source presence only; local validation still occurs before use."""
    if any(name in os.environ for name in names):
        return all(bool(os.environ.get(name, "").strip()) for name in names)
    return legacy_settings.exists()
