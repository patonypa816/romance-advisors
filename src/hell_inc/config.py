from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
import os


@dataclass(frozen=True)
class Settings:
    database_url: str
    world_id: str = "hell-inc-dev"
    host: str = "127.0.0.1"
    port: int = 8080
    origin: str | None = None
    session_seconds: int = 3600
    frontend_dir: Path = field(default_factory=lambda: Path("frontend/dist"))
    lock_dir: Path = field(default_factory=lambda: Path("/tmp/hell-inc-locks"))

    def __post_init__(self):
        if not self.world_id or len(self.world_id) > 128 or self.world_id != self.world_id.strip():
            raise ValueError("world_id 必须是 1–128 字符且无首尾空白。")
        if not 1 <= self.session_seconds <= 86400:
            raise ValueError("会话有效期必须在 1–86400 秒内。")
        if not 1 <= self.port <= 65535:
            raise ValueError("端口不合法。")
        value = self.public_origin
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.path or parts.query or parts.fragment:
            raise ValueError("origin 必须为不含路径的 http(s) 源。")
        if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("非回环访问必须通过 HTTPS。")

    @property
    def public_origin(self):
        return self.origin or f"http://127.0.0.1:{self.port}"

    @classmethod
    def from_env(cls):
        url = os.environ.get("HELL_INC_DATABASE_URL")
        if not url:
            raise ValueError("请设置 HELL_INC_DATABASE_URL（仅隔离开发数据库）。")
        return cls(database_url=url, world_id=os.getenv("HELL_INC_WORLD_ID", "hell-inc-dev"),
            host=os.getenv("HELL_INC_HOST", "127.0.0.1"), port=int(os.getenv("HELL_INC_PORT", "8080")),
            origin=os.getenv("HELL_INC_ORIGIN"), session_seconds=int(os.getenv("HELL_INC_SESSION_SECONDS", "3600")),
            frontend_dir=Path(os.getenv("HELL_INC_FRONTEND_DIR", "frontend/dist")),
            lock_dir=Path(os.getenv("HELL_INC_LOCK_DIR", "/tmp/hell-inc-locks")))
