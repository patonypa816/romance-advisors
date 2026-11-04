"""Local semantic transport checks: no network, credentials or services."""
import asyncio
import copy
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from motion_component import MotionComponent
from semantic_speech import SemanticSpeechError
from test_motion_component import FakeSocket, NoNetwork, GE, eventually


def action(quote="甲", pair=False):
    motions = [{"part": "both_arms", "description": "双手向外轻摊"}]
    if pair:
        motions.append({"part": "head", "description": "头部轻轻右倾"})
    return {"anchor": {"quote": quote, "occurrence": 1}, "motions": motions, **GE}


def fixture(seconds=3.05, starts=(0,), *, word_repeat=1):
    pcm = b"\0\0" * round(seconds * 24000)
    words = [{"text": text * word_repeat, "begin_time": round(start * 1000),
              "end_time": round((start + .05) * 1000)}
             for text, start in zip("甲乙丙丁戊己", starts)]
    return pcm, {"status": "ok", "timebase": "audio_start", "words": words,
                 "pcm_sha256": hashlib.sha256(pcm).hexdigest()}


class ControlledSocket(FakeSocket):
    def __init__(self, delays=None, stop_motion=None):
        super().__init__()
        self.delays = delays or {}
        self.stop_motion = stop_motion
        self.component = None

    async def send_json(self, frame):
        if frame["type"] == 9:
            await asyncio.sleep(self.delays.get(self.motion_attempts + 1, 0))
        await super().send_json(frame)
        if frame["type"] == 9 and self.motion_attempts == self.stop_motion:
            self.component.latch_stop()


class SemanticTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="semantic-transport-offline-")
        self.addCleanup(self.temp.cleanup)
        self.count = 0

    def component(self, socket=None):
        self.count += 1
        out = Path(self.temp.name) / str(self.count)
        c = MotionComponent(NoNetwork(), out, out / "unused.png")
        c._ws = socket or FakeSocket()
        if isinstance(c._ws, ControlledSocket):
            c._ws.component = c
        c.live_id = "offline-only"
        c._ready.set(); c._joined.set(); c.state = "ready"
        c.DRAIN_SECONDS = 0
        return c

    async def send(self, c, starts=(0,), seconds=3.05, pair=False, reading=None, action_kind="L", ge_lead_seconds=.5):
        pcm, alignment = fixture(seconds, starts)
        actions = [action(char, pair) for char in "甲乙"[:len(starts)]]
        receipt = await c.send_aligned_expression(pcm, "甲乙"[:len(starts)], actions, 1,
                                                   recovery=GE, alignment=alignment,
                                                   reading=reading, action_kind=action_kind,
                                                   ge_lead_seconds=ge_lead_seconds)
        return pcm, receipt

    async def test_zero_anchor_preroll_then_l_text_and_exact_pcm(self):
        c = self.component()
        pcm, receipt = await self.send(c, pair=True)
        ge, long, quiet = c._ws.motions()
        text = next(r for r in c._ws.sent if r["frame"]["type"] == 10)
        zero = c._ws.binary[0]["time"]
        self.assertGreaterEqual(long["started"] - ge["completed"], .5)
        self.assertLess(long["completed"], text["started"])
        self.assertLess(text["completed"], zero)
        self.assertGreaterEqual(quiet["started"] - zero, 3.05)
        self.assertEqual(b"".join(r["chunk"] for r in c._ws.binary), pcm)
        self.assertEqual(receipt["motion_events"][1]["simultaneous_count"], 2)
        self.assertLess(receipt["motion_events"][0]["actual_at_seconds"], -.49)
        self.assertLess(abs(receipt["motion_events"][1]["actual_at_seconds"]), .1)
        self.assertTrue(receipt["actions_sent_complete"])
        self.assertNotIn("visual_effect_confirmed", receipt)
        self.assertEqual({r["frame"]["conn_id"] for r in c._ws.sent}, {c._conn_id})
        self.assertEqual(len(c._motion_claims), 3)

    async def test_local_anchor_mode_sends_original_pcm_with_unrelated_text_difference(self):
        pcm, alignment = fixture(.2, (0, .1))
        alignment["words"][1]["text"] = "错"
        before = copy.deepcopy(alignment)
        c = self.component()
        receipt = await c.send_aligned_expression(pcm, "甲乙", [action()], 1,
            recovery=GE, alignment=alignment, action_kind="S", ge_lead_seconds=1.1,
            allow_tail_q_after_audio=True, local_action_anchors=True)
        self.assertEqual(alignment, before)
        self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary), pcm)
        self.assertEqual([row["role"] for row in receipt["motion_events"]], ["GE", "S", "Q"])
        self.assertEqual(receipt["alignment"]["words"][1]["text"], "错")

    async def test_local_anchor_or_pcm_failure_blocks_even_reading_prelude(self):
        pcm, alignment = fixture(.2, (0, .1))
        for changed in ({**alignment, "pcm_sha256": "wrong"},
                        {**alignment, "words": [{**word, "text": "错"} for word in alignment["words"]]},
                        {**alignment, "words": [{**word, "text": "甲"} for word in alignment["words"]]}):
            c = self.component()
            with self.assertRaises(SemanticSpeechError):
                await c.send_aligned_expression(pcm, "甲乙", [action()], 1,
                    recovery=GE, alignment=changed, reading={"text": "读", "pcm": pcm},
                    action_kind="S", ge_lead_seconds=1.1, allow_tail_q_after_audio=True,
                    local_action_anchors=True)
            self.assertEqual(c._ws.sent, [])
            self.assertEqual(c._ws.binary, [])

    async def test_historical_default_still_rejects_unrelated_text_difference(self):
        pcm, alignment = fixture(.2, (0, .1))
        alignment["words"][1]["text"] = "错"
        c = self.component()
        with self.assertRaises(SemanticSpeechError):
            await c.send_aligned_expression(pcm, "甲乙", [action()], 1,
                recovery=GE, alignment=alignment, action_kind="S", ge_lead_seconds=1.1,
                allow_tail_q_after_audio=True)
        self.assertEqual(c._ws.sent, [])
        self.assertEqual(c._ws.binary, [])

    async def test_s_tail_q_sends_while_real_pcm_scheduler_keeps_streaming(self):
        c = self.component()
        pcm, receipt = await self.send(c, seconds=2, pair=True, action_kind="S")
        ge, short, quiet = c._ws.motions()
        self.assertEqual([event["role"] for event in receipt["motion_events"]], ["GE", "S", "Q"])
        self.assertEqual(receipt["action_kind"], "S")
        self.assertGreaterEqual(short["started"] - ge["completed"], .5)
        self.assertGreaterEqual(quiet["started"] - short["completed"], 1.5)
        self.assertLess(quiet["completed"], c._ws.binary[-1]["time"])
        self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary), pcm)
        self.assertEqual(receipt["pcm_transport_sent_bytes"], len(pcm))
        self.assertTrue(receipt["send_complete"])
        self.assertNotIn("playback_complete_confirmed", receipt)

    async def test_ab_zero_anchor_real_transport_keeps_gaze_expression_and_full_pcm(self):
        for lead in (1.1, None):
            with self.subTest(lead=lead):
                c = self.component()
                pcm, receipt = await self.send(c, seconds=2, action_kind="S", ge_lead_seconds=lead)
                expected = ["GE", "S", "Q"] if lead is not None else ["S", "Q"]
                self.assertEqual([event["role"] for event in receipt["motion_events"]], expected)
                short = next(event for event in receipt["motion_events"] if event["role"] == "S")
                self.assertIn("眼神：", short["instruction"])
                self.assertIn("表情：", short["instruction"])
                self.assertLessEqual(c._ws.motions()[-2]["completed"], c._ws.binary[0]["time"])
                self.assertLess(c._ws.motions()[-1]["completed"], c._ws.binary[-1]["time"])
                self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary), pcm)
                self.assertEqual(receipt["independent_ge"], lead is not None)
                self.assertEqual(receipt["ge_lead_seconds"], lead)

    async def test_minute_scope_and_pcm_timestamp_binding_fail_closed_before_frames(self):
        starts = (.08, 9, 18, 28, 40, 53)
        pcm, alignment = fixture(57, starts, word_repeat=30)
        text = "".join(char * 30 for char in "甲乙丙丁戊己")
        actions = [action(char) for char in "甲乙丙丁戊己"]
        minute = {"action_kind": "S", "ge_lead_seconds": 1.1,
                  "max_audio_seconds": 60, "max_text_codepoints": 240}
        variants = [({}, pcm, text, alignment),
                    ({}, pcm[:48000], text, alignment),
                    ({**minute, "max_audio_seconds": 30}, pcm, text, alignment),
                    ({**minute, "max_text_codepoints": 120}, pcm, text, alignment),
                    ({**minute, "max_audio_seconds": True}, pcm, text, alignment),
                    ({**minute, "ge_lead_seconds": None}, pcm, text, alignment),
                    ({**minute, "ge_lead_seconds": .5}, pcm, text, alignment),
                    ({**minute, "action_kind": "L"}, pcm, text, alignment),
                    ({**minute, "reading": {"text": "读", "pcm": b"\0\0"}}, pcm, text, alignment),
                    (minute, pcm + b"\0\0" * 24000 * 4, text, alignment),
                    (minute, pcm, "字" * 241, alignment),
                    (minute, pcm, text, {**alignment, "pcm_sha256": "wrong"}),
                    (minute, pcm, text, {**alignment, "words": alignment["words"][:-1]})]
        bad_times = {**alignment, "words": [dict(word) for word in alignment["words"]]}
        bad_times["words"][-1]["end_time"] = 58000
        variants.append((minute, pcm, text, bad_times))
        for options, audio, speech, timestamps in variants:
            with self.subTest(options=options, bytes=len(audio)):
                c = self.component()
                with self.assertRaises(ValueError):
                    await c.send_aligned_expression(audio, speech, actions, 1,
                                                    recovery=GE, alignment=timestamps, **options)
                self.assertFalse(c._ws.sent)
                self.assertFalse(c._ws.binary)
                self.assertEqual(c.MAX_AUDIO_SECONDS, 30)

    async def test_s_invalid_kind_short_tail_or_dense_groups_send_nothing(self):
        for kind, starts, seconds in (("Q", (0,), 2), ("S", (.7,), 2), ("S", (0, 2.999), 5)):
            c = self.component()
            pcm, alignment = fixture(seconds, starts)
            text = "甲乙"[:len(starts)]
            with self.assertRaises(ValueError):
                await c.send_aligned_expression(pcm, text, [action(char) for char in text], 1,
                                                recovery=GE, alignment=alignment, action_kind=kind)
            self.assertFalse(c._ws.sent)
            self.assertFalse(c._ws.binary)
            self.assertEqual(c.utterances_started, 0)

    async def test_nonzero_first_anchor_keeps_pcm_origin_and_word_time(self):
        c = self.component()
        _, receipt = await self.send(c, starts=(.65,), seconds=3.7)
        ge, long, quiet = c._ws.motions()
        zero = c._ws.binary[0]["time"]
        self.assertGreaterEqual(ge["started"] - zero, .15)
        self.assertGreaterEqual(long["started"] - zero, .65)
        self.assertGreaterEqual(long["started"] - ge["completed"], .5)
        self.assertGreaterEqual(quiet["started"] - zero, 3.7)
        self.assertEqual(receipt["timeline"][1]["audio_at_seconds"], .65)

    async def test_delayed_writes_cannot_compress_any_group_window(self):
        c = self.component(ControlledSocket({1: .06, 3: .22, 4: .2, 5: .12}))
        _, receipt = await self.send(c, starts=(0, 4.6), seconds=7.65)
        rows = c._ws.motions()
        for ge, long, quiet in (rows[:3], rows[3:]):
            self.assertGreaterEqual(long["started"] - ge["completed"], .5)
            self.assertGreaterEqual(quiet["started"] - long["completed"], 3)
        self.assertGreaterEqual(rows[3]["started"] - rows[2]["completed"], 1.1 - 1e-6)
        self.assertEqual([n["role"] for n in receipt["motion_events"]], ["GE", "L", "Q"] * 2)
        self.assertGreater(receipt["motion_events"][-1]["actual_at_seconds"], 7.65)


    async def test_early_nonzero_anchor_only_prerolls_ge_and_preserves_l_word_time(self):
        c = self.component()
        task = asyncio.create_task(self.send(c, starts=(.24,), seconds=3.3))
        await eventually(lambda: len(c._ws.motions()) >= 2, timeout=2)
        ge, long = c._ws.motions()
        zero = c._ws.binary[0]["time"]
        self.assertLess(ge["completed"], zero)
        self.assertGreaterEqual(long["started"]-ge["completed"], .5)
        self.assertGreaterEqual(long["started"]-zero, .24)
        self.assertLess(long["completed"]-zero, .4)
        c.latch_stop()
        with self.assertRaisesRegex(RuntimeError, "停止"):
            await task
        self.assertEqual(c.last_motion_receipt["timeline"][1]["audio_at_seconds"], .24)
        self.assertEqual(c.last_motion_receipt["motion_events"][0]["timebase"], "answer_first_pcm")

    async def test_pcm_progress_gate_blocks_elapsed_clock_node(self):
        c = self.component()
        pcm, alignment = fixture(3.7, (.65,))
        allow = asyncio.Event()
        async def held_audio(pcm, count_audio, *, on_frame_sent):
            await c._ws.send_bytes(pcm[:4800])
            on_frame_sent(4800, time.monotonic())
            await allow.wait()
            c._check()
        c._audio = held_audio
        task = asyncio.create_task(c.send_aligned_expression(
            pcm, "甲", [action()], 1, recovery=GE, alignment=alignment))
        await eventually(lambda: c._ws.binary)
        await asyncio.sleep(.22)
        self.assertEqual(c._ws.motions(), [])
        c.latch_stop(); allow.set()
        with self.assertRaisesRegex(RuntimeError, "停止"):
            await task
        self.assertEqual(c._ws.motions(), [])

    async def test_stops_before_ge_during_preroll_pcm_and_q_wait(self):
        for stage in ("before", "preroll", "nonzero_wait", "after_l", "after_q1"):
            with self.subTest(stage=stage):
                c = self.component()
                if stage == "before":
                    c.latch_stop()
                starts = (.65,) if stage == "nonzero_wait" else ((0, 4.6) if stage == "after_q1" else (0,))
                seconds = 7.65 if len(starts) == 2 else (3.7 if starts[0] else 3.05)
                task = asyncio.create_task(self.send(c, starts=starts, seconds=seconds))
                if stage != "before":
                    if stage == "nonzero_wait":
                        await eventually(lambda: c._ws.binary)
                    else:
                        wanted = {"preroll": 1, "after_l": 2, "after_q1": 3}[stage]
                        await eventually(lambda: len(c._ws.motions()) >= wanted, timeout=4)
                    c.latch_stop()
                with self.assertRaisesRegex(RuntimeError, "停止"):
                    await task
                counts = (len(c._ws.sent), len(c._ws.binary))
                await asyncio.sleep(.03)
                self.assertEqual((len(c._ws.sent), len(c._ws.binary)), counts)
                self.assertFalse(c.active_motion_plan.get("is_active", False))

    async def test_reading_preserves_r_pcm_s0_and_answer_relative_times(self):
        c = self.component()
        reading = {"text": "读弹幕", "pcm": b"\0\0" * 1200}
        with patch("motion_component.READING_LEAD_SECONDS", .02):
            _, receipt = await self.send(c, reading=reading)
        motions = c._ws.motions()
        self.assertGreaterEqual(motions[2]["started"]-motions[1]["completed"], 1.0)
        self.assertEqual([e["role"] for e in receipt["motion_events"]], ["R", "S0", "GE", "L", "Q"])
        texts = [r["frame"]["payload"]["text_msg"]["content"] for r in c._ws.sent if r["frame"]["type"] == 10]
        self.assertEqual(texts, ["读弹幕", "甲"])
        self.assertEqual(receipt["timeline"][1]["audio_at_seconds"], 0)
        self.assertEqual(len({e["node_index"] for e in receipt["motion_events"]}), 5)
        self.assertTrue(receipt["reading_sent_complete"])
        self.assertEqual(c._ws.binary[0]["chunk"], reading["pcm"])

    async def test_stop_reading_r_audio_or_s0_cancels_answer(self):
        for stage in ("r", "audio", "s0"):
            with self.subTest(stage=stage):
                socket = ControlledSocket(stop_motion=1 if stage == "r" else 2 if stage == "s0" else None)
                c = self.component(socket)
                reading = {"text": "读弹幕", "pcm": b"\0\0" * 4800}
                with patch("motion_component.READING_LEAD_SECONDS", .02):
                    task = asyncio.create_task(self.send(c, reading=reading))
                    if stage == "audio":
                        await eventually(lambda: socket.binary)
                        c.latch_stop()
                    with self.assertRaisesRegex(RuntimeError, "停止"):
                        await task
                self.assertLessEqual(len(socket.motions()), 2)
                self.assertFalse(any(r["frame"]["type"] == 10 and r["frame"]["payload"]["text_msg"]["content"] == "甲" for r in socket.sent))

    async def test_bad_alignment_reading_and_conflict_send_nothing(self):
        pcm, alignment = fixture(4, (0, .5))
        cases = [({**alignment, "pcm_sha256": "wrong"}, [action()], None),
                 (alignment, [action(), action("乙")], None),
                 (alignment, [action()], {"text": "读", "pcm": b"x"}),
                 ({**alignment, "status": "unavailable"}, [action()], None)]
        for aligned, actions, reading in cases:
            c = self.component()
            with self.assertRaises(ValueError):
                await c.send_aligned_expression(pcm, "甲乙", actions, 1, recovery=GE,
                                                alignment=aligned, reading=reading)
            self.assertEqual(c._ws.sent, [])
            self.assertEqual(c._ws.binary, [])
            self.assertEqual(c.utterances_started, 0)


    async def test_reading_empty_actions_short_answer_retains_single_s0_timed_q(self):
        c = self.component()
        pcm, _ = fixture(.1)
        reading = {"text": "读弹幕", "pcm": b"\0\0" * 1200}
        alignment = {"status": "unavailable", "reason": "timestamps_missing"}
        with patch("motion_component.READING_LEAD_SECONDS", .02):
            receipt = await c.send_aligned_expression(
                pcm, "甲", [], 1, recovery=GE, alignment=alignment, reading=reading)
        self.assertEqual([e["role"] for e in receipt["motion_events"]], ["R", "S0", "Q"])
        self.assertEqual(receipt["timeline"], [])
        self.assertEqual([n["role"] for n in receipt["reading_timeline"]], ["R", "S0", "Q"])
        r, s0, q = c._ws.motions()
        answer = [row for row in c._ws.sent if row["frame"]["type"] == 10][1]
        self.assertGreaterEqual(answer["started"]-s0["completed"], 1)
        self.assertLess(answer["completed"]-s0["completed"], 1.2)
        self.assertGreaterEqual(q["started"]-s0["completed"], 1.5)
        self.assertLess(q["completed"]-s0["completed"], 1.7)
        self.assertGreater(q["started"], c._ws.binary[-1]["time"]+.3)
        self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary), reading["pcm"]+pcm)
        self.assertTrue(receipt["actions_sent_complete"])
        self.assertNotIn("visual_effect_confirmed", receipt)

    async def test_empty_actions_still_send_exact_pcm_and_alignment(self):
        c = self.component()
        pcm, alignment = fixture(.1)
        alignment = {"status": "unavailable", "reason": "timestamps_missing"}
        receipt = await c.send_aligned_expression(pcm, "甲", [], 1, alignment=alignment)
        self.assertEqual(c._ws.motions(), [])
        self.assertEqual(len(c._ws.sent), 1)
        self.assertEqual(b"".join(r["chunk"] for r in c._ws.binary), pcm)
        self.assertEqual(receipt["alignment"], alignment)
        self.assertTrue(receipt["actions_sent_complete"])


if __name__ == "__main__":
    unittest.main()
