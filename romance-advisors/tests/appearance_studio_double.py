"""离线 Studio 发送替身：真实组包和锁，内存 WS，合成 PCM，无外部请求。

这里的 confirmed 只表示模拟接收者已确认，永远不是 Vidu/RTC 画面证据。
临时目录仅承接真实组件自身的诊断输出，不接触项目 data/ 或凭据。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import time

STUDIO = Path(__file__).resolve().parents[2] / "ai-live-avatar" / "studio"
# Do not leave Studio ahead of the game's tests in unittest discovery: both
# projects have a test_token_plan.py. Only the sender import needs this path.
sys.path.insert(0, str(STUDIO))
try:
    from motion_component import MotionComponent
finally:
    sys.path.remove(str(STUDIO))

SWITCH_VIDEOS = ["https://example.com/offline-only-transition.mp4"]
ASSET_IDS = {"normal": "100050", "refined": "100062"}


class NoNetwork:
    def __getattr__(self, name):
        raise AssertionError("离线替身禁止访问 HTTP：" + name)


class MemorySocket:
    closed = False

    def __init__(self, avatar):
        self.avatar = avatar
        self.binary = []
        self.sent = []
        self.received_assets = {}

    async def send_json(self, frame):
        row = self.avatar._record("frame", type=frame["type"], seq=frame["seq_id"],
                                  seq_id=frame["seq_id"], frame=copy.deepcopy(frame),
                                  role=self.avatar.component.writing_role)
        self.sent.append(row)
        if frame["type"] == 16:
            self.received_assets[frame["seq_id"]] = frame["payload"]["switch_avatar"]["avatar_id"]

    async def send_bytes(self, chunk):
        data = bytes(chunk)
        row = self.avatar._record("pcm", type="PCM", byte_count=len(data),
                                  sha256=hashlib.sha256(data).hexdigest())
        self.binary.append(data)
        job_id = self.avatar.component.job_id
        self.avatar.node_event(job_id, "pcm").set()
        # 故意停在真实写锁里面，测试此时 Type16 是否能绕过整段表演锁。
        await self.avatar._gate(job_id, "pcm")


class ObservedMotionComponent(MotionComponent):
    def __init__(self, avatar, *args, **kwargs):
        self.avatar = avatar
        self.writing_role = None
        super().__init__(*args, **kwargs)

    async def _write_motion(self, node, *args, **kwargs):
        if node["role"] == "Q":
            self.avatar.node_event(self.job_id, "before_Q").set()
            await self.avatar._gate(self.job_id, "Q")
        self.writing_role = node["role"]
        try:
            sent = await super()._write_motion(node, *args, **kwargs)
        finally:
            self.writing_role = None
        # 真正发送成功且 motion_events 已追加后才释放 S 节点。
        self.avatar.node_event(self.job_id, node["role"]).set()
        return sent


class OfflineStudioAvatar:
    """实现 Program 使用的 AvatarClient 接口及显式模拟换形接口。

    hold(job_id, 'pcm'/'Q') / release(...) 可定位并发注入；wait_node 可等
    pcm、GE、S、before_Q、Q、complete。默认保留真实 4 秒语音交接等待；
    若调用方显式缩短 drain_seconds，报告必须披露，不冒充真实 4 秒验证。
    """
    supports_simulated_appearance = True

    def __init__(self, *, drain_seconds=4.0, pcm_seconds=.2):
        self.origin = "http://127.0.0.1:1"
        self.player_url = self.origin + "/offline-simulated-player/"
        self.generation = None
        self.owner_id = None
        self.state = "idle"
        self.calls, self.sent, self.prepared = [], [], []
        self.wire_events, self.boundaries = [], []
        self.jobs, self.switches = {}, {}
        self._nodes, self._gates = {}, {}
        self._tasks = set()
        self.closed = False
        self.can_start = True
        self.blocking_code = self.blocking_reason = ""
        self.confirmed_appearance_key = "normal"
        self.drain_seconds = drain_seconds
        self.pcm = b"\x01\x00" * round(24000 * pcm_seconds)
        self._temp = tempfile.TemporaryDirectory(prefix="appearance-studio-offline-")
        out = Path(self._temp.name)
        self.component = ObservedMotionComponent(self, NoNetwork(), out, out / "unused.png")
        self.socket = MemorySocket(self)
        self.component._ws = self.socket
        self.component.live_id = "offline-no-cloud-session"
        self.component._ready.set()
        self.component._joined.set()
        self.component.state = "ready"
        self.component.DRAIN_SECONDS = drain_seconds

    def _record(self, kind, **values):
        row = {"order": len(self.wire_events) + 1, "at": time.monotonic(),
               "kind": kind, "job_id": self.component.job_id,
               "appearance_key": self.confirmed_appearance_key, **values}
        self.wire_events.append(row)
        return row

    def node_event(self, job_id, node):
        return self._nodes.setdefault((job_id, node), asyncio.Event())

    async def wait_node(self, job_id, node, timeout=15):
        await asyncio.wait_for(self.node_event(job_id, node).wait(), timeout)

    def hold(self, job_id, node):
        gate = self._gates.setdefault((job_id, node), asyncio.Event())
        gate.clear()

    def release(self, job_id, node):
        self._gates.setdefault((job_id, node), asyncio.Event()).set()

    async def _gate(self, job_id, node):
        gate = self._gates.get((job_id, node))
        if gate is not None:
            await gate.wait()

    async def prepare_session(self, owner_id):
        self.owner_id, self.generation = owner_id, "offline-generation-1"
        self.calls.append("claim")
        return await self.snapshot()

    async def snapshot(self):
        return {"session": {"state": self.state}, "studio": {"can_start": self.can_start,
                "blocking_code": self.blocking_code, "blocking_reason": self.blocking_reason}}

    async def start(self):
        self.calls.append("start")
        self.state = "ready"

    async def heartbeat(self):
        self.calls.append("heartbeat")

    async def external_state(self):
        return {"owner_id": self.owner_id, "generation": self.generation,
                "state": self.state if self.state == "ended" else self.component.state}

    def _receipt(self, job):
        record = job["record"]
        events = [e for e in self.component.motion_events if e.get("job_id") == record["job_id"]]
        receipt = job.get("component_receipt", {})
        return {"owner_id": self.owner_id, "generation": self.generation,
                "job_id": record["job_id"], "status": job["status"],
                "worker_pending": job["status"] == "sending", "preparation_pending": False,
                "delivery_started": job.get("started", False),
                "short_action_sent": record.get("mode") == "motion" and any(e["role"] == "S" for e in events),
                "send_complete": receipt.get("send_complete", False),
                "handoff_wait_elapsed": receipt.get("handoff_wait_elapsed", False),
                "post_audio_wait_seconds": receipt.get("post_audio_wait_seconds"),
                "playback_confirmed": False, "evidence": "memory_websocket_write",
                "cancel_result": job.get("cancel_result", ""), "failure_code": job.get("failure_code", "")}

    async def prepare(self, record, text):
        previous = self.jobs.get(record["job_id"])
        if previous is not None:
            if previous["record"] != record or previous["text"] != text:
                raise ValueError("same job ID has conflicting preparation")
            return self._receipt(previous)
        job = {"record": copy.deepcopy(record), "text": text, "status": "prepared"}
        self.jobs[record["job_id"]] = job
        self.prepared.append(copy.deepcopy(record))
        return self._receipt(job)

    async def dispatch(self, record):
        job = self.jobs[record["job_id"]]
        if job["status"] != "prepared":
            return self._receipt(job)
        job["status"] = "sending"
        task = asyncio.create_task(self._run_job(job), name="offline-studio-" + record["job_id"])
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return self._receipt(job)

    async def submit(self, record, text):
        if record["job_id"] not in self.jobs:
            await self.prepare(record, text)
        return await self.dispatch(record)

    async def job(self, record):
        return self._receipt(self.jobs[record["job_id"]])

    async def cancel_job(self, record):
        job = self.jobs[record["job_id"]]
        if not job.get("started") and job["status"] == "prepared":
            job.update(status="cancelled", cancel_result="cancelled_before_delivery")
        else:
            job["cancel_result"] = "already_started" if job.get("started") else "already_terminal"
        return self._receipt(job)

    def _speech_alignment(self, text):
        # 明确为合成字时间；仅验证本地编译/发送，从不声称 TTS 自然度。
        duration = len(self.pcm) / 48000
        step_ms = duration * 1000 / max(1, len(text))
        return {"status": "ok", "timebase": "audio_start",
                "pcm_sha256": hashlib.sha256(self.pcm).hexdigest(), "words": [
                    {"text": char, "begin_index": i, "end_index": i + 1,
                     "begin_time": round(i * step_ms), "end_time": round((i + 1) * step_ms)}
                    for i, char in enumerate(text)]}

    async def _run_job(self, job):
        record, text = job["record"], job["text"]
        job_id = record["job_id"]
        self.component.comment_id = job_id
        self.component.job_id = job_id
        job["started"] = True
        boundary = {"job_id": job_id, "mode": record.get("mode", "speech"),
                    "start_order": self._record("protected_start")["order"], "end_order": None}
        self.boundaries.append(boundary)
        self.sent.append((copy.deepcopy(record), text))
        try:
            if record.get("mode") == "motion":
                plan = record["plan"]
                receipt = await self.component.send_motion_expression(
                    plan["actions"], recovery=plan["recovery"], dialogue_motion=True)
            else:
                parts = (record.get("plan") or {}).get("segments") or [{}]
                if len(parts) != 1:
                    raise ValueError("offline fixture supports a single speech segment")
                part = parts[0]
                receipt = await self.component.send_aligned_expression(
                    self.pcm, text, part["actions"], 1, recovery=part["recovery"],
                    alignment=self._speech_alignment(text), action_kind="S", ge_lead_seconds=1.1,
                    allow_tail_q_after_audio=True, local_action_anchors=True)
            job["component_receipt"] = receipt
            job["status"] = "sent"
        except asyncio.CancelledError:
            job["status"] = "cancelled"
            raise
        except Exception as error:
            job.update(status="failed", failure_code=type(error).__name__)
            job["error"] = error
        finally:
            boundary["end_order"] = self._record("protected_end")["order"]
            self.node_event(job_id, "complete").set()
            self.component.job_id = self.component.comment_id = None

    async def switch_appearance(self, record):
        request_id = record["request_id"]
        previous = self.switches.get(request_id)
        if previous is not None:
            if previous["record"] != record:
                raise ValueError("same request ID has conflicting appearance binding")
            return copy.deepcopy(previous["receipt"])
        if record["owner_id"] != self.owner_id or record["generation"] != self.generation:
            raise ValueError("appearance owner mismatch")
        target = record["target_key"]
        if target not in ASSET_IDS:
            raise ValueError("unsupported offline appearance")
        # 同一发送器的 Q 后保护仍按真实墙钟等待，不能用 ready 代替。
        await self.component._wait(max(0, self.component._motion_not_before - time.monotonic()))
        sent = await self.component.switch_avatar(ASSET_IDS[target], SWITCH_VIDEOS, request_id, lambda: True)
        receipt = {**copy.deepcopy(record), "status": "written" if sent.get("sent") else "failed",
                   "simulated": True, "transport_sent": sent.get("sent", False),
                   "seq_id": sent.get("seq_id"), "evidence": "memory_websocket_write",
                   "playback_confirmed": False}
        self.switches[request_id] = {"record": copy.deepcopy(record), "receipt": receipt,
                                     "component_receipt": sent}
        if sent.get("sent"):
            # Resolve the appearance from the frame captured on the receiving side.
            received_id = self.socket.received_assets.get(sent["seq_id"])
            received_key = next((k for k, v in ASSET_IDS.items() if v == received_id), None)
            if received_key != target:
                receipt.update(status="failed", evidence="mock_receiver_asset_mismatch")
                return copy.deepcopy(receipt)
            self._record("mock_receiver_confirmation", request_id=request_id, target_key=received_key,
                         avatar_id=received_id,
                         seq_id=sent["seq_id"], evidence="mock_receiver")
            self.confirmed_appearance_key = received_key
            receipt.update(status="confirmed", evidence="mock_receiver")
        return copy.deepcopy(receipt)

    async def appearance(self, record):
        operation = self.switches[record["request_id"]]
        if operation["record"] != record:
            raise ValueError("appearance query binding mismatch")
        return copy.deepcopy(operation["receipt"])

    async def stop(self):
        self.calls.append("stop")
        self.component.latch_stop()
        for gate in self._gates.values():
            gate.set()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self.state = "ended"

    async def close(self):
        if self._tasks:
            await self.stop()
        self.closed = True
        self._temp.cleanup()

