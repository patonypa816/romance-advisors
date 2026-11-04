"""Local avatar catalogue; remote mutations occur only on an explicit create call.

Signed image URLs and API credentials never enter the persisted/public catalogue.
A write-ahead upload record prevents a browser retry or process restart from
silently creating a second billable/cloud asset after an ambiguous response.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import time
import warnings
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from PIL import Image, UnidentifiedImageError

try:
    from . import credentials
except ImportError:
    import credentials

MAX_IMAGE = 20 * 1024 * 1024
MAX_PIXELS = 40_000_000
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
STATUS = {1: "created", 6: "processing", 11: "success", 12: "failed"}


class AvatarError(ValueError):
    """A safe, user-facing error that contains no upstream response body."""


class CreateRejected(AvatarError):
    """A definitive HTTP rejection: the server did not accept creation."""


def _identifier(value):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise AvatarError("资产或请求 ID 格式不正确")
    return value


def _settings(value):
    if not isinstance(value, dict):
        raise AvatarError("角色设定必须是对象")
    result = {}
    for key, limit in (("name", 80), ("persona", 12000), ("reply_style", 4000)):
        item = value.get(key, "")
        if not isinstance(item, str) or len(item) > limit or "\x00" in item:
            raise AvatarError("角色设定格式不正确或过长")
        result[key] = item.strip()
    return result


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 80 or "\x00" in value:
        raise AvatarError("请填写 1–80 字的资产名称")
    return value.strip()


def inspect_image(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) < MAX_IMAGE:
        raise AvatarError("图片不能为空且必须小于 20 MB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                fmt, size = image.format, image.size
                if fmt not in {"PNG", "JPEG", "WEBP"} or size[0] * size[1] > MAX_PIXELS:
                    raise AvatarError("只支持 PNG、JPEG、WebP，且图片不能超过 4000 万像素")
                image.verify()
            # verify checks the container; load also verifies compressed pixel data.
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning, ValueError) as error:
        if isinstance(error, AvatarError):
            raise
        raise AvatarError("图片内容损坏或格式不受支持") from None
    extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}[fmt]
    return extension, size[0], size[1], hashlib.sha256(raw).hexdigest()


def validate_image_url(url):
    """Only Vidu/CDN HTTPS URLs are eligible; redirects are never followed."""
    if not isinstance(url, str) or len(url) > 8192:
        raise AvatarError("云端图片地址不符合安全要求")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        allowed = host.endswith(".vidu.cn") or host.endswith(".vidu.com") or host.endswith(".aliyuncs.com")
        if (parts.scheme != "https" or not allowed or parts.username or parts.password
                or parts.port not in (None, 443) or parts.fragment):
            raise ValueError()
    except ValueError:
        raise AvatarError("云端图片地址不符合安全要求") from None
    return url


class _PublicResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        rows = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM, family=family)
        result = []
        for af, _, proto, _, address in rows:
            address_ip = ipaddress.ip_address(address[0])
            # 本机 TUN 将已核验的 Vidu OSS 域名解析到合成地址；这不是内网素材。
            # 例外只覆盖该精确域名与 TUN 地址段，HTTPS 证书/主机名校验仍照常执行。
            tun_cdn = (host.lower() == "prod-ss-vidu.s3.oss-cn-beijing.aliyuncs.com"
                       and address_ip in ipaddress.ip_network("198.18.0.0/15"))
            if not address_ip.is_global and not tun_cdn:
                raise AvatarError("云端图片地址解析到非公网地址")
            result.append({"hostname": host, "host": address[0], "port": port,
                           "family": af, "proto": proto, "flags": socket.AI_NUMERICHOST})
        if not result:
            raise AvatarError("云端图片地址无法解析")
        return result

    async def close(self):
        pass


class ViduAvatarClient:
    """Lazy credential loading and two isolated HTTP sessions avoid CDN auth leaks."""
    def __init__(self):
        self._api = None
        self._images = None

    async def _request(self, method, path, *, body=None, params=None):
        if self._api is None:
            self._api = aiohttp.ClientSession()
        try:
            key = credentials.load_vidu()
            async with self._api.request(method, "https://api.vidu.cn" + path, json=body, params=params,
                    headers={"Authorization": "Token " + key}, allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=60)) as response:
                if response.status >= 300:
                    if method == "POST" and 400 <= response.status < 500:
                        raise CreateRejected(f"Vidu 拒绝创建（HTTP {response.status}）")
                    raise AvatarError(f"Vidu 请求失败（HTTP {response.status}）")
                chunks, length = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    length += len(chunk)
                    if length > 4 * 1024 * 1024:
                        raise AvatarError("Vidu 返回数据过大")
                    chunks.append(chunk)
                value = json.loads(b"".join(chunks))
                if not isinstance(value, dict):
                    raise AvatarError("Vidu 返回格式无法识别")
                return value
        except (AvatarError, asyncio.CancelledError):
            raise
        except Exception:
            raise AvatarError("Vidu 请求未完成，请检查网络或已有凭据文件") from None

    async def list_page(self, page, page_size, page_token=None):
        params = {"pager.page": page, "pager.pagesz": page_size}
        if page_token:
            params["page_token"] = page_token
        return await self._request("GET", "/live/v1/avatars", params=params)

    async def get_avatar(self, avatar_id):
        return await self._request("GET", "/live/v1/avatars/" + _identifier(avatar_id))

    async def create_avatar(self, image_uri, name):
        # Deliberately one attempt: timeout/5xx can mean the creation succeeded.
        return await self._request("POST", "/live/v1/avatars", body={"image_uri": image_uri, "name": name})

    async def download_image(self, url):
        validate_image_url(url)
        if self._images is None:
            self._images = aiohttp.ClientSession(connector=aiohttp.TCPConnector(
                resolver=_PublicResolver(), family=socket.AF_INET))
        try:
            async with self._images.get(url, allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    raise AvatarError("云端底图暂时无法读取")
                if response.content_length and response.content_length >= MAX_IMAGE:
                    raise AvatarError("云端底图超过大小限制")
                chunks, length = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    length += len(chunk)
                    if length >= MAX_IMAGE:
                        raise AvatarError("云端底图超过大小限制")
                    chunks.append(chunk)
                return b"".join(chunks)
        except (AvatarError, asyncio.CancelledError):
            raise
        except Exception:
            raise AvatarError("云端底图下载未完成") from None

    async def close(self):
        for session in (self._api, self._images):
            if session is not None:
                await session.close()


class AvatarLibrary:
    def __init__(self, data: Path, *, client=None):
        self.data = Path(data)
        self.directory = self.data / "avatar-images"
        self.path = self.data / "avatar-library.json"
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.mkdir(exist_ok=True, mode=0o700)
        os.chmod(self.data, 0o700)
        os.chmod(self.directory, 0o700)
        self.client = client if client is not None else ViduAvatarClient()
        self._lock = asyncio.Lock()
        self.syncing = False
        self.state = {"version": 1, "assets": [], "uploads": [], "selected_id": None,
                      "last_sync": None, "error": None}
        if self.path.exists():
            try:
                if self.path.stat().st_size > 16 * 1024 * 1024:
                    raise ValueError()
                value = json.loads(self.path.read_text())
                if (not isinstance(value, dict) or not isinstance(value.get("assets"), list)
                        or not isinstance(value.get("uploads"), list)):
                    raise ValueError()
                self.state.update({k: value[k] for k in self.state if k in value})
                for asset in self.state["assets"]:
                    _identifier(asset["id"])
                    asset["settings"] = _settings(asset.get("settings", {}))
                os.chmod(self.path, 0o600)
            except (OSError, ValueError, TypeError, KeyError):
                raise AvatarError("本地资产目录损坏；请保留文件并检查，系统未覆盖原目录") from None

    def _save(self):
        temporary = self.path.with_suffix(".json.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(self.state, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def get(self, asset_id):
        _identifier(asset_id)
        for asset in self.state["assets"]:
            if asset["id"] == asset_id:
                return copy.deepcopy(asset)
        return None

    def image_path(self, asset_id):
        asset = self.get(asset_id)
        if not asset or not asset.get("image_file"):
            return None
        filename = asset["image_file"]
        if not isinstance(filename, str) or not re.fullmatch(r"[a-f0-9]{64}\.(png|jpg|webp)", filename):
            return None
        path = self.directory / filename
        if path.is_symlink() or not path.is_file() or path.resolve().parent != self.directory.resolve():
            return None
        return path

    def _public_asset(self, asset):
        return {"id": asset["id"], "name": asset.get("name", ""), "status": asset.get("status", "unknown"),
                "image_available": self.image_path(asset["id"]) is not None,
                "image_version": asset.get("sha256", ""), "width": asset.get("width"), "height": asset.get("height"),
                "settings": copy.deepcopy(asset["settings"]),
                "image_error": asset.get("image_error")}

    def public(self):
        return {"assets": [self._public_asset(a) for a in self.state["assets"]],
                "selected_id": self.state["selected_id"], "syncing": self.syncing,
                "last_sync": self.state["last_sync"], "error": self.state["error"],
                "uploads": [self._public_upload(u) for u in self.state["uploads"]]}

    @staticmethod
    def _public_upload(upload):
        keys = ("request_id", "status", "asset_id", "name", "error", "created_at", "sha256")
        return {k: upload.get(k) for k in keys}

    def _cache(self, raw):
        extension, width, height, digest = inspect_image(raw)
        filename = digest + "." + extension
        path = self.directory / filename
        if path.is_symlink():
            raise AvatarError("本地图片缓存路径不安全")
        # Hash filenames make every write immutable from the application's view.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        os.chmod(path, 0o600)
        return {"image_file": filename, "sha256": digest, "width": width, "height": height}

    def _remote_record(self, value):
        if not isinstance(value, dict):
            raise AvatarError("Vidu 资产格式无法识别")
        asset_id = _identifier(value.get("id"))
        name = value.get("name", "")
        if not isinstance(name, str) or len(name) > 500:
            raise AvatarError("Vidu 资产格式无法识别")
        status = value.get("status")
        status = STATUS.get(int(status), status) if isinstance(status, str) and status.isdecimal() else STATUS.get(status, status)
        if status not in {"created", "processing", "success", "failed"}:
            raise AvatarError("Vidu 资产状态无法识别")
        return {"id": asset_id, "name": name, "status": status}

    async def _merge(self, value, *, image=None, settings=None):
        record = self._remote_record(value)
        existing = self.get(record["id"])
        merged = existing or {"id": record["id"], "settings": _settings(settings or {})}
        merged.update(record)
        merged.pop("image_error", None)
        if image:
            merged.update(image)
        elif value.get("image_uri"):
            try:
                # Validate even injected clients so tests cannot bypass this boundary.
                url = validate_image_url(value["image_uri"])
                merged.update(self._cache(await self.client.download_image(url)))
            except asyncio.CancelledError:
                raise
            except Exception:
                merged["image_error"] = "底图缓存失败，可稍后重新同步或刷新"
        latest = self.get(record["id"])
        if latest:
            # Settings can be saved synchronously while the image download yields.
            # Read them again so a slow sync cannot restore an older prompt.
            merged["settings"] = latest["settings"]
            self.state["assets"] = [merged if a["id"] == merged["id"] else a for a in self.state["assets"]]
        else:
            self.state["assets"].append(merged)
        for upload in self.state["uploads"]:
            if upload.get("asset_id") == merged["id"]:
                upload.update(status=merged["status"], error=None)
        return merged

    async def sync(self):
        async with self._lock:
            self.syncing = True
            self.state["error"] = None
            try:
                gathered, seen, tokens = [], set(), set()
                page, token, total = 0, None, None
                for _ in range(100):
                    result = await self.client.list_page(page, 100, token)
                    if (not isinstance(result, dict) or not isinstance(result.get("items"), list)
                            or not isinstance(result.get("total"), int) or isinstance(result.get("total"), bool)
                            or result["total"] < 0 or len(result["items"]) > 100):
                        raise AvatarError("Vidu 资产列表格式无法识别")
                    if total is None:
                        total = result["total"]
                    elif result["total"] != total:
                        raise AvatarError("同步期间资产列表发生变化，请重新同步")
                    for value in result["items"]:
                        record = self._remote_record(value)
                        if record["id"] in seen:
                            raise AvatarError("Vidu 分页重复，已停止同步以防遗漏")
                        seen.add(record["id"])
                        gathered.append(value)
                    next_token = result.get("next_page_token", "")
                    if not isinstance(next_token, str) or len(next_token) > 4096:
                        raise AvatarError("Vidu 分页格式无法识别")
                    if len(gathered) == total and not next_token:
                        break
                    if len(gathered) > total or not result["items"]:
                        raise AvatarError("Vidu 分页数量不一致")
                    if next_token:
                        if next_token in tokens:
                            raise AvatarError("Vidu 分页游标重复")
                        tokens.add(next_token)
                    token, page = next_token or None, page + 1
                else:
                    raise AvatarError("资产分页超过安全上限")
                # Only merge after all pages validate; existing local settings survive.
                for value in gathered:
                    await self._merge(value)
                self.state["last_sync"] = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.state["error"] = str(error) if isinstance(error, AvatarError) else "资产同步未完成，请稍后重试"
            finally:
                self.syncing = False
                self._save()
            return self.public()

    async def refresh(self, asset_id):
        _identifier(asset_id)
        async with self._lock:
            if not self.get(asset_id):
                raise AvatarError("未找到资产")
            try:
                value = await self.client.get_avatar(asset_id)
                if self._remote_record(value)["id"] != asset_id:
                    raise AvatarError("Vidu 返回的资产 ID 不匹配")
                asset = await self._merge(value)
                for upload in self.state["uploads"]:
                    if upload.get("asset_id") == asset_id:
                        upload["status"] = asset["status"]
                self.state["error"] = None
                self._save()
                return self._public_asset(asset)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.state["error"] = str(error) if isinstance(error, AvatarError) else "资产刷新未完成"
                self._save()
                raise AvatarError(self.state["error"]) from None

    async def create(self, raw: bytes, filename: str, name: str, settings: dict, request_id: str):
        _identifier(request_id)
        name, settings = _name(name), _settings(settings)
        # Filename is display-only input, never used to construct paths.
        if not isinstance(filename, str) or len(filename) > 255 or "\x00" in filename:
            raise AvatarError("文件名格式不正确")
        extension, _, _, digest = inspect_image(raw)
        async with self._lock:
            for upload in self.state["uploads"]:
                if upload["request_id"] == request_id:
                    if upload.get("sha256") != digest:
                        raise AvatarError("同一上传请求不能更换图片，请重新选择图片")
                    return self._public_upload(upload)
            for upload in self.state["uploads"]:
                if upload.get("sha256") == digest and upload.get("status") == "creation_unknown":
                    # A reload creates a new browser request ID. The image hash
                    # still identifies its unresolved create, so do not POST twice.
                    return self._public_upload(upload)
            image = self._cache(raw)
            upload = {"request_id": request_id, "status": "creation_unknown", "asset_id": None,
                      "name": name, "sha256": digest, "created_at": time.time(),
                      "error": "创建结果尚未确认；此请求不会再次上传，请同步资产核对"}
            self.state["uploads"].append(upload)
            # Persist before network I/O: a crash cannot erase the no-retry decision.
            self._save()
            uri = "data:image/" + ("jpeg" if extension == "jpg" else extension) + ";base64," + base64.b64encode(raw).decode("ascii")
            try:
                value = await self.client.create_avatar(uri, name)
                # Preserve an acknowledged ID even if a newly introduced status
                # cannot be interpreted. Follow-up uses GET, never another POST.
                known_id = _identifier(value.get("id")) if isinstance(value, dict) else None
                if known_id:
                    upload["asset_id"] = known_id
                    if not self.get(known_id):
                        self.state["assets"].append({"id": known_id, "name": name,
                            "status": "unknown", "settings": settings, **image})
                    self._save()
                asset = await self._merge(value, image=image, settings=settings)
                upload.update(status=asset["status"], asset_id=asset["id"], error=None)
            except CreateRejected as error:
                upload.update(status="creation_failed", error=str(error))
            except asyncio.CancelledError:
                self._save()
                raise
            except Exception:
                # No automatic retry, including malformed success responses and 5xx.
                pass
            self._save()
            return self._public_upload(upload)

    def update_settings(self, asset_id, settings):
        _identifier(asset_id)
        settings = _settings(settings)
        for asset in self.state["assets"]:
            if asset["id"] == asset_id:
                asset["settings"] = settings
                self._save()
                return self._public_asset(asset)
        raise AvatarError("未找到资产")

    def set_selected(self, asset_id):
        if asset_id is not None and not self.get(asset_id):
            raise AvatarError("未找到资产")
        self.state["selected_id"] = asset_id
        self._save()
        return self.public()

    def import_local(self, asset_id, path, *, name="", settings=None, status="success"):
        """Explicit local seed for an already known cloud ID; never uploads it."""
        record = self._remote_record({"id": asset_id, "name": name, "status": status})
        image = self._cache(Path(path).read_bytes())
        existing = self.get(asset_id)
        if existing:
            existing.update(image)
            self.state["assets"] = [existing if a["id"] == asset_id else a for a in self.state["assets"]]
        else:
            self.state["assets"].append({**record, **image, "settings": _settings(settings or {})})
        self._save()
        return self._public_asset(self.get(asset_id))

    async def close(self):
        await self.client.close()
