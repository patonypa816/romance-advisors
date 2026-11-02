"""Local CLI-only lock and independent stop-only watchdog. Never a scene creator."""
import asyncio
import fcntl
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time
from .record_only import _write

LOCK_PATH=Path(f'/tmp/hell-inc-record-only-{os.getuid()}.lock')


def _read(path):
    try:
        with open(path,encoding='utf-8') as f:return json.load(f)
    except FileNotFoundError:return {}


def _owned(out,manifest):
    emergency=_read(out/'component'/'component-owned-id.json').get('live_id')
    owned=manifest.get('owned_live_id')
    if owned and emergency and owned!=emergency:raise ValueError('owned_id_mismatch')
    value=owned or emergency
    if value is not None and (not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value)):
        raise ValueError('invalid_owned_id')
    return value


def _prepare_stop(out):
    # Construct before watchdog readiness, hence before any paid preparation.
    # A private sibling avoids making the runner's exclusive attempt dir exist.
    from .avatar_transport.component_live import PersistentComponent
    from .media_http import MediaHTTP, validate_secret, validate_proxy
    key=os.environ['HELL_INC_VIDU_KEY'] if 'HELL_INC_VIDU_KEY' in os.environ else os.getenv('VIDU_API_KEY')
    validate_secret(key)
    http=MediaHTTP(workspace='cleanup-only',proxy=validate_proxy(os.getenv('HTTPS_PROXY') or None))
    cleanup=out.parent/(out.name+'.watchdog-cleanup')
    # Stop-only recovery must not depend on a writable evidence directory.
    component=PersistentComponent(http,cleanup,Path('UNUSED'),recording=False,stop_only=True)
    return component,http,key


async def stop_owned(out,owned_id,*,prepared=None):
    # No runtime.start, model, TTS, collection, or arbitrary CLI ID. Production
    # watchdog supplies its preconstructed object; no disk allocation at hangup.
    component,http,key=prepared if prepared is not None else _prepare_stop(out)
    component.bind_owned_for_stop(owned_id,key)
    try:
        result=await asyncio.wait_for(component.stop('independent diagnostic watchdog'),110)
        return result.get('cloud_end_confirmed') is True
    finally:
        await component._close_ws()
        await http.close()


async def watch(out,parent_alive,*,stop=stop_owned,clock=time.monotonic,sleep=asyncio.sleep):
    """Only this attempt's files can supply ownership. Injected seams are tests only."""
    triggered=None;stop_attempted=False
    while True:
        try:
            manifest=_read(out/'attempt.json')
            owned=_owned(out,manifest)
            if manifest.get('cloud_end_confirmed') is True:
                result={'state':'ended','cloud_end_confirmed':True,'stop_attempted':False}
                break
            alive=parent_alive()
            created=manifest.get('create_attempted') is True
            started=manifest.get('create_started_monotonic')
            expired=created and (not isinstance(started,(int,float)) or clock()-started>=180)
            if not alive or expired:
                triggered=triggered or clock()
                # Parent checks this latch before creation/each send; CLI monitor
                # cancels its active task, preserving original cleanup semantics.
                if out.exists():
                    try:_write(out/'watchdog-stop.json',{'reason':'parent_exited' if not alive else 'scene_deadline'})
                    except OSError:pass  # Evidence failure must never block hangup.
                if not created and not owned:
                    result={'state':'no_cloud_attempt','cloud_end_confirmed':False,'stop_attempted':False};break
                if owned:
                    stop_attempted=True
                    ended=await stop(out,owned)
                    result={'state':'ended' if ended else 'unknown','cloud_end_confirmed':ended,
                            'owned_live_id':owned,'stop_attempted':True};break
                # A live parent can still receive an in-flight create response.
                # A dead parent cannot recover its lost response: do not guess IDs.
                if not alive or clock()-triggered>=125:
                    result={'state':'unknown','cloud_end_confirmed':False,'stop_attempted':False,'reason':'created_id_unavailable'};break
            await sleep(.1)
        except BaseException as exc:
            if isinstance(exc,asyncio.CancelledError):raise
            result={'state':'unknown','cloud_end_confirmed':False,'stop_attempted':stop_attempted,'reason':'watchdog_failed'};break
    if out.exists():
        try:_write(out/'watchdog.json',result)
        except OSError:pass  # Return actual stop evidence even when persistence fails.
    return result


class DiagnosticGuard:
    """The watchdog inherits the locked open description; death cannot release it early."""
    def __init__(self,out,*,lock_path=LOCK_PATH):
        self.out=Path(out).resolve();self.lock_path=Path(lock_path)
        self.fd=None;self.pipe=None;self.child=None

    def acquire(self):
        self.fd=os.open(self.lock_path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            os.fchmod(self.fd,0o600)
            raw=os.read(self.fd,8192)
            if raw:
                previous=Path(json.loads(raw)['attempt_dir'])
                body=_read(previous/'attempt.json');guard=_read(previous/'watchdog.json')
                if (body.get('create_attempted') or _owned(previous,body)) and not (body.get('cloud_end_confirmed') is True or guard.get('cloud_end_confirmed') is True):
                    raise RuntimeError('previous_diagnostic_unknown_requires_reconciliation')
            if not self.out.is_absolute() or self.out.exists():raise ValueError('new_absolute_attempt_required')
            os.lseek(self.fd,0,os.SEEK_SET);os.ftruncate(self.fd,0)
            os.write(self.fd,json.dumps({'attempt_dir':str(self.out)}).encode());os.fsync(self.fd)
        except BaseException:
            os.close(self.fd);self.fd=None;raise
        return self

    def start(self):
        read_fd,self.pipe=os.pipe();ready_read,ready_write=os.pipe()
        # Watchdog deliberately receives no Bailian credentials/configuration.
        names=('PATH','PYTHONPATH','PYTHONHOME','LD_LIBRARY_PATH','LANG','HTTPS_PROXY','HELL_INC_VIDU_KEY','VIDU_API_KEY')
        env={key:os.environ[key] for key in names if key in os.environ}
        try:
            self.child=subprocess.Popen([sys.executable,'-m','hell_inc.record_only_guard',str(self.out),str(read_fd),str(ready_write),str(self.fd)],
                pass_fds=(read_fd,ready_write,self.fd),env=env,start_new_session=True,
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            if not select.select([ready_read],[],[],5)[0] or os.read(ready_read,1)!=b'R':
                raise RuntimeError('watchdog_not_ready')
        finally:
            os.close(read_fd);os.close(ready_write);os.close(ready_read)

    async def supervise(self,awaitable):
        task=asyncio.create_task(awaitable)
        try:
            while not task.done():
                if (self.out/'watchdog-stop.json').exists() or (self.child.poll() is not None and not _read(self.out/'watchdog.json').get('cloud_end_confirmed')):
                    task.cancel()
                    try:await task
                    except asyncio.CancelledError:pass
                    raise RuntimeError('independent_watchdog_stopped_attempt')
                await asyncio.sleep(.1)
            return await task
        except BaseException:
            if not task.done():
                task.cancel()
                try:await task
                except BaseException:pass
            raise

    def close(self):
        if self.pipe is not None:os.close(self.pipe);self.pipe=None
        # Close, never LOCK_UN: the child's inherited descriptor keeps ownership.
        if self.fd is not None:os.close(self.fd);self.fd=None


def main():
    out=Path(sys.argv[1]);parent_fd=int(sys.argv[2]);ready_fd=int(sys.argv[3]);lock_fd=int(sys.argv[4])
    from .media_http import validate_secret,validate_proxy
    validate_secret(os.environ['HELL_INC_VIDU_KEY'] if 'HELL_INC_VIDU_KEY' in os.environ else os.getenv('VIDU_API_KEY'))
    validate_proxy(os.getenv('HTTPS_PROXY') or None)
    os.fstat(lock_fd);os.set_blocking(parent_fd,False)
    def alive():
        try:return os.read(parent_fd,1)!=b''
        except BlockingIOError:return True
    prepared=_prepare_stop(out)
    async def stop(_out,owned):return await stop_owned(_out,owned,prepared=prepared)
    async def run():
        try:return await watch(out,alive,stop=stop)
        finally:await prepared[1].close()
    os.write(ready_fd,b'R');os.close(ready_fd)
    try:asyncio.run(run())
    finally:os.close(parent_fd);os.close(lock_fd)


if __name__=='__main__':main()
