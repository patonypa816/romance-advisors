import tempfile
import unittest
from pathlib import Path
from hell_inc.instance import InstanceAlreadyRunning, InstanceLock


class InstanceTests(unittest.TestCase):
    def test_same_world_refuses_duplicate_even_with_different_credentials_and_host_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            first = InstanceLock("postgresql+asyncpg://one:pw@localhost/test", "world", Path(directory))
            second = InstanceLock("postgresql+asyncpg://two:pw@127.0.0.1/test", "world", Path(directory))
            first.acquire()
            try:
                with self.assertRaises(InstanceAlreadyRunning):
                    second.acquire()
            finally:
                first.release()
            second.acquire()
            second.release()

    def test_different_worlds_can_use_independent_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            first = InstanceLock("postgresql+asyncpg://one:pw@localhost/test", "one", Path(directory))
            second = InstanceLock("postgresql+asyncpg://one:pw@localhost/test", "two", Path(directory))
            try:
                first.acquire(); second.acquire()
            finally:
                first.release(); second.release()
