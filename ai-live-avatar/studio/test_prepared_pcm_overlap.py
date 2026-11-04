"""Real-clock PCM pacing with concurrent local preparation; no services/providers.

Only provider TTS and action-model outputs are synthetic. WAV conversion/probing,
LiveSession preparation/cache/checkpoints, and MotionComponent writes stay real.
"""
import asyncio
import copy
import hashlib
import json
import math
from pathlib import Path
import shutil
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import wave

import live_session
import tts_live
from external_game import ExternalGame
from motion_component import MotionComponent
from semantic_speech import parse_dialogue_semantic_plan
from test_motion_component import FakeSocket, NoNetwork

HERE = Path(__file__).resolve().parent
REPORT = HERE.parent.parent / "romance-advisors/artifacts/character-continuity-20261104/pcm-overlap-check.json"
A_SECONDS = 5.0
B_SECONDS = 2.0
HISTORY_COUNT = 60
# A packet represents 100 ms. Allow ordinary OS jitter, while a whole additional
# packet-period stall or sustained >40 ms drift fails this local-load regression.
MAX_INTERVAL_SECONDS = .200
P95_INTERVAL_SECONDS = .140


class RecordingSocket(FakeSocket):
    def __init__(self, component):
        super().__init__()
        self.component = component
        self.first_pcm = asyncio.Event()
        self.owner_writes = []

    async def send_json(self, frame):
        self.owner_writes.append(("json", self.component.comment_id, time.monotonic()))
        await super().send_json(frame)

    async def send_bytes(self, chunk):
        self.owner_writes.append(("pcm", self.component.comment_id, time.monotonic()))
        await super().send_bytes(chunk)
        self.first_pcm.set()


def frozen_plan(text, *, action=False):
    actions = [{"anchor": {"quote": "别", "occurrence": 1},
                "motions": [{"part": "head", "description": "轻轻点头"}],
                "gaze": "自然看向镜头", "expression": "保持认真"}] if action else []
    return parse_dialogue_semantic_plan({"emotion": "认真", "reading": None,
        "segments": [{"text": text, "instruction": "自然清晰地说。", "actions": actions,
                      "recovery": {"gaze": "继续看向镜头", "expression": "保留认真神情"} if action else None}]}, mode="once")


class PreparedPCMOverlapTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_pcm_pacing_baseline_and_overlapped_preparation(self):
        self.assertIsNotNone(shutil.which("ffmpeg"), "This local audio regression requires ffmpeg")
        self.assertIsNotNone(shutil.which("ffprobe"), "This local audio regression requires ffprobe")
        with tempfile.TemporaryDirectory(prefix="prepared-pcm-overlap-offline-") as temporary:
            self.root = Path(temporary)
            image = self.root / "fixture.png"
            image.write_bytes(b"offline-fixture-image-no-cloud")
            self.image = image
            baseline = await self.measure("baseline", overlap=False)
            overlap = await self.measure("overlap", overlap=True)
        # Compare against the same machine's baseline, in addition to absolute
        # limits. This detects a new preparation-induced pause without treating
        # sub-millisecond scheduling variation as a transport failure.
        self.assertLessEqual(overlap["max_interval_seconds"] - baseline["max_interval_seconds"], .100)
        self.assertLessEqual(overlap["p95_interval_seconds"] - baseline["p95_interval_seconds"], .040)
        report = {"status": "passed", "recorded_at_unix": time.time(),
            "scope": "本机模拟负载；未调用真实TTS/动作模型/Vidu，未创建HTTP服务或云会话，不能证明真实播放或云渲染",
            "clock": "unmodified time.monotonic and real asyncio waits",
            "real_paths": ["MotionComponent.send_aligned_expression/_audio", "LiveSession.prepare_comment",
                           "prepare_audio PCM read/hash", "prepare_timed_actions alignment binding",
                           "ffprobe WAV/PCM metadata", "ffmpeg WAV to 24kHz mono s16le",
                           "conversation.json and external-game.json checkpoints"],
            "synthetic": {"external_tts_delay_seconds": .200, "external_action_model_delay_seconds": .150,
                          "pcm_fixture": "deterministic nonzero 16-bit sample bytes", "history_jobs": HISTORY_COUNT},
            "limits": {"nominal_interval_seconds": .100, "max_interval_seconds": MAX_INTERVAL_SECONDS,
                       "p95_interval_seconds": P95_INTERVAL_SECONDS, "max_delta_over_baseline_seconds": .100,
                       "p95_delta_over_baseline_seconds": .040, "packet_span_tolerance_seconds": .150,
                       "drain_seconds": 4.0},
            "baseline": baseline, "overlap": overlap,
            "source_sha256": {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest() for name in
                ("live_session.py", "motion_component.py", "component_live.py", "tts_live.py",
                 "external_game.py", Path(__file__).name)}}
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    async def measure(self, label, *, overlap):
        session = live_session.LiveSession(self.root / label, "A", image=self.image)
        session.http = NoNetwork()
        component = MotionComponent(session.http, session.out, session.image, session.diagnostics)
        socket = RecordingSocket(component)
        component._ws, component.live_id, component.state = socket, "offline-only-never-started", "ready"
        component._ready.set()
        component._joined.set()
        session.component, session.started = component, True
        audio_ends = []
        original_record = session.diagnostics.record
        def observe_record(direction, kind, **fields):
            # Observe the actual audio-loop exit; preserve the original logger
            # and every wait. Diagnostics' stored elapsed_ms is rounded to ms.
            if kind == "audio_end":
                audio_ends.append(time.monotonic())
            return original_record(direction, kind, **fields)
        session.diagnostics.record = observe_record
        self.assertEqual(component.DRAIN_SECONDS, 4.0)  # Never shorten the handoff for this test.
        host = SimpleNamespace(current=session, generation="offline-generation", closing=False,
                               automation=SimpleNamespace(last_error=""))
        external = ExternalGame(host)
        external.owner_id, external.generation = "offline-owner", host.generation
        session.external_checkpoint = external.checkpoint
        session.external_preparation_gate = external.preparation_allowed
        session.delivery_gate = external.delivery_allowed
        active, maximum, finished = set(), 0, {}
        def started(item):
            nonlocal maximum
            active.add(item["id"])
            maximum = max(maximum, len(active))
        def ended(item):
            active.discard(item["id"])
            finished[item["id"]] = time.monotonic()
        session.on_delivery_started, session.on_delivery_finished = started, ended

        def add_job(name, text, *, action=False, historical=False):
            plan = frozen_plan(text, action=action)
            item = {"id": name, "status": "sent" if historical else "processing", "text": text,
                    "answer": text, "plan": plan, "input_kind": "external_game", "segments_sent": int(historical),
                    "reading_status": "skipped", "motion_receipts": [],
                    "usage": {"input_tokens": 0, "output_tokens": 0, "cost_cny": 0.0},
                    "requires_prepared": True, "external_job_id": name,
                    "external_owner_id": external.owner_id, "external_generation": external.generation,
                    "external_mode": "speech", "external_submission_mode": "prepared",
                    "external_request_fingerprint": hashlib.sha256(text.encode()).hexdigest()}
            session.comments.append(item)
            external.jobs[name] = {"job_id": name, "mode": "speech", "text": text,
                "instruction": plan["segments"][0]["instruction"], "submitted_plan": copy.deepcopy(plan),
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "submission_mode": "prepared", "item": item}
            return item

        # Modest, equal archive load in both runs includes real synchronous JSON
        # checkpoints; a tiny two-comment fixture would miss this common work.
        for index in range(HISTORY_COUNT):
            add_job(f"history-{index}", "这是已结束的离线归档样本。" * 6, historical=True)
        a = add_job("A", "我先认真听听大家的想法。")
        expected = {}
        local_stages = []
        async def provider_fixture(http, key, workspace, out, text, instruction, **options):
            name = "B" if text == "别急。" else "A"
            seconds = B_SECONDS if name == "B" else A_SECONDS
            if name == "B":
                await asyncio.sleep(.200)  # Provider wait is asynchronous, like the real request.
            out.mkdir(parents=True, exist_ok=True)
            wav_path, pcm_path = out / "sample.wav", out / "sample.pcm"
            samples = round(seconds * component.RATE)
            pcm = (bytes(range(256)) * math.ceil(samples * 2 / 256))[:samples * 2]
            with wave.open(str(wav_path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(component.RATE)
                wav.writeframes(pcm)
            # Use the production local audio helpers; only the remote response
            # and its word timestamps are fake. PCM read/hash below is real too.
            begin = time.monotonic()
            wav_probe = await tts_live._probe(wav_path)
            tts_live._audio_metadata(wav_probe)
            await tts_live._process("ffmpeg", "-v", "error", "-nostdin", "-n", "-i", str(wav_path),
                "-vn", "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "s16le", str(pcm_path))
            pcm_probe = await tts_live._probe(pcm_path, pcm=True)
            _, duration = tts_live._audio_metadata(pcm_probe)
            converted = pcm_path.read_bytes()
            self.assertEqual(converted, pcm)
            digest = hashlib.sha256(converted).hexdigest()
            expected[name] = converted
            alignment = {"status": "ok", "timebase": "audio_start", "pcm_sha256": digest,
                "words": [{"text": char, "begin_time": round(i * duration * 1000 / len(text)),
                           "end_time": round((i + 1) * duration * 1000 / len(text))}
                          for i, char in enumerate(text)]}
            local_stages.append({"job_id": name, "seconds": time.monotonic() - begin,
                                 "pcm_sha256": digest, "pcm_bytes": len(converted)})
            return {"status": "ok", "usage_verified": False, "audio_seconds": duration,
                    "pcm_path": str(pcm_path), "pcm_sha256": digest, "alignment": alignment}

        async def action_model(content, alignment, seconds, context, **kwargs):
            await asyncio.sleep(.150)
            return {"actions": copy.deepcopy(content["actions"]), "planning": {"phase": "tts_aligned"}}
        session.timed_action_planner = AsyncMock(side_effect=action_model)
        tts = AsyncMock(side_effect=provider_fixture)
        worker = None
        with patch.object(live_session, "load_bailian", return_value=("offline-key", "offline-workspace")), \
             patch.object(live_session, "load_vidu", side_effect=AssertionError("Vidu is forbidden")), \
             patch.object(live_session, "synthesize_segment", tts):
            try:
                self.assertTrue(await session.prepare_comment(a))
                a.update(external_dispatched=True, external_worker_pending=True)
                worker = asyncio.create_task(session._process_answers())
                session.work.put_nowait(a)
                await asyncio.wait_for(socket.first_pcm.wait(), 3)
                prepared_at = None
                b = None
                if overlap:
                    b = add_job("B", "别急。", action=True)
                    self.assertTrue(await asyncio.wait_for(session.prepare_comment(b), 3))
                    prepared_at = time.monotonic()
                    self.assertEqual(b["status"], "prepared")
                    self.assertFalse(b.get("delivery_started"))
                    self.assertEqual(a["status"], "sending")
                    self.assertFalse(component.audio_sent_complete)
                    self.assertTrue(component._speech_lock.locked())
                    self.assertEqual(component.comment_id, "A")
                    cached = session._cached_preparation(b)
                    self.assertEqual(cached["segments"][0][0], expected["B"])
                    self.assertEqual(cached["media_fingerprint"], session._preparation_media_fingerprint(b, cached))
                    self.assertEqual(b["plan"]["segments"][0]["alignment"]["pcm_sha256"],
                                     hashlib.sha256(expected["B"]).hexdigest())
                    session.timed_action_planner.assert_awaited_once()
                    self.assertTrue(session.work.empty())  # Prepared B is never auto-enqueued.
                await asyncio.wait_for(session.work.join(), 11)
                self.assertEqual(a["status"], "sent", a.get("error"))
                self.assertEqual(tts.await_count, 2 if overlap else 1)
                self.assertEqual(component.utterances_started, 1)
                self.assertEqual(maximum, 1)
                self.assertEqual(active, set())
                self.assertEqual({owner for _, owner, _ in socket.owner_writes}, {"A"})
                self.assertEqual(b"".join(row["chunk"] for row in socket.binary), expected["A"])
                self.assertEqual(component.sent_audio_bytes, len(expected["A"]))
                self.assertEqual(len(socket.binary), 50)
                times = [row["time"] for row in socket.binary]
                intervals = [after - before for before, after in zip(times, times[1:])]
                p95 = sorted(intervals)[math.ceil(len(intervals) * .95) - 1]
                maximum_interval = max(intervals)
                span = times[-1] - times[0]
                self.assertEqual(len(audio_ends), 1)
                audio_end = audio_ends[0]
                # The completion hook occurs after the unmodified audio loop's
                # final 100 ms wait and its original four-second drain.
                drain = finished["A"] - audio_end
                self.assertGreaterEqual(drain, 4.0)
                self.assertLess(abs(span - 4.9), .150)
                self.assertLessEqual(maximum_interval, MAX_INTERVAL_SECONDS)
                self.assertLessEqual(p95, P95_INTERVAL_SECONDS)
                if overlap:
                    self.assertLess(prepared_at, times[-1])
                    self.assertEqual(b["status"], "prepared")
                archived = json.loads((session.out / "conversation.json").read_text())
                jobs = json.loads((session.out / "external-game.json").read_text())
                self.assertEqual(len(archived["comments"]), HISTORY_COUNT + (2 if overlap else 1))
                self.assertEqual(len(jobs["jobs"]), HISTORY_COUNT + (2 if overlap else 1))
                return {"a_audio_seconds": A_SECONDS, "pcm_bytes": len(expected["A"]),
                    "pcm_sha256": hashlib.sha256(expected["A"]).hexdigest(), "bytes_exact": True,
                    "packet_count": len(times), "packet_span_seconds": span, "expected_span_seconds": 4.9,
                    "max_interval_seconds": maximum_interval, "p95_interval_seconds": p95,
                    "all_intervals_seconds": intervals, "drain_seconds_observed": drain,
                    "maximum_active_senders": maximum, "wire_owners": ["A"], "b_sent": False,
                    "b_prepared_before_a_last_pcm": prepared_at < times[-1] if overlap else None,
                    "b_prepared_after_a_first_pcm_seconds": prepared_at - times[0] if overlap else None,
                    "real_local_audio_stages": local_stages,
                    "conversation_archive_bytes": (session.out / "conversation.json").stat().st_size,
                    "jobs_archive_bytes": (session.out / "external-game.json").stat().st_size}
            finally:
                if worker is not None:
                    session.work.put_nowait(None)
                    await asyncio.wait_for(worker, 12)


if __name__ == "__main__":
    unittest.main()
