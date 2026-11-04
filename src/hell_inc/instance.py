"""Same-host, same database/world singleton. No cross-host takeover promise."""
import fcntl
import hashlib
import os
from pathlib import Path


class InstanceAlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, database_url: str, world_id: str, lock_dir: Path):
        # Conservative per-host world lock: aliases or changed DB credentials cannot
        # create a second owner. Distinct isolated environments use distinct world IDs.
        key = world_id
        self.path = Path(lock_dir) / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
        self.fd = None

    def acquire(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise InstanceAlreadyRunning("同一世界已有实例运行；请先停止旧实例。") from None
        self.fd = fd

    def release(self):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
        # Never unlink: unlinking a locked inode allows another lock owner.
