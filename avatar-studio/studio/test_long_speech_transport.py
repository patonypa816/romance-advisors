"""长文多组口播的传输验收；假socket、真实PCM节拍，无云调用。"""
import asyncio
from itertools import combinations
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from motion_component import MotionComponent
from test_motion_component import FakeSocket, NoNetwork, GE, short, long, eventually


def actions(count=4):
    return [short(0 if index == 0 else 3.0) for index in range(count)]


class LongSpeechTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="long-speech-transport-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.count = 0

    def component(self, **flags):
        self.count += 1
        out = Path(self.tmp.name) / str(self.count)
        c = MotionComponent(NoNetwork(), out, out / "unused.png", **({"long_speech_test": True} | flags))
        c._ws = FakeSocket(write_delay=.01)
        c.live_id = "offline-only"
        c._ready.set(); c._joined.set(); c.state = "ready"
        c.DRAIN_SECONDS = 0
        return c

    async def send(self, c, *, text="本" * 100, pcm=None, action_nodes=None, **kwargs):
        return await c.send_expression(pcm if pcm is not None else b"\x01\x00" * (c.RATE*12),
                                       text, actions() if action_nodes is None else action_nodes,
                                       1, recovery=GE, **kwargs)

    async def test_twelve_seconds_one_hundred_characters_four_groups_one_stream(self):
        c = self.component()
        pcm, text = b"\x01\x00" * (c.RATE*12), "本" * 100
        drains = []
        real_wait = c._wait
        c.DRAIN_SECONDS = 4.0

        async def capture_drain(seconds):
            # 保留真实12秒PCM，只截获整段结束后的固定缓冲等待。
            if seconds == 4.0:
                drains.append(time.monotonic())
                return
            await real_wait(seconds)

        c._wait = capture_drain
        receipt = await self.send(c, text=text, pcm=pcm)
        motions = c._ws.motions()
        texts = [row for row in c._ws.sent if row["frame"]["type"] == 10]
        self.assertEqual(len(texts), 1)
        self.assertEqual(texts[0]["frame"]["payload"]["text_msg"]["content"], text)
        self.assertEqual(len(motions), 12)
        self.assertEqual(len(c._ws.binary), 120)
        self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary), pcm)
        self.assertGreater(c._ws.binary[-1]["time"] - c._ws.binary[0]["time"], 11.8)
        self.assertLess(max(b["time"]-a["time"] for a, b in zip(c._ws.binary, c._ws.binary[1:])), .25)
        self.assertEqual(len(drains), 1)
        self.assertGreater(drains[0], c._ws.binary[-1]["time"])
        audio_events = [row["kind"] for row in c.diagnostics.records if row["kind"].startswith("audio_")]
        self.assertEqual(audio_events, ["audio_start", "audio_end"])
        for group_index in range(4):
            ge, s, q = motions[group_index*3:group_index*3+3]
            self.assertGreaterEqual(s["started"] - ge["completed"], .49)
            self.assertGreaterEqual(q["started"] - s["completed"], 1.49)
            self.assertIn("正常坐姿", q["frame"]["payload"]["text_msg"]["content"])
            self.assertNotIn("双手向外轻摊", q["frame"]["payload"]["text_msg"]["content"])
            if group_index:
                offset = group_index*3-.5
                self.assertGreaterEqual(ge["started"] - c._ws.binary[0]["time"], offset-.01)
                successful_bytes = sum(len(row["chunk"]) for row in c._ws.binary if row["time"] <= ge["started"])
                self.assertGreaterEqual(successful_bytes, offset*c.RATE*2)
                self.assertGreaterEqual(ge["started"], motions[group_index*3-1]["completed"])
            self.assertTrue(any(s["completed"] < row["time"] < q["started"] for row in c._ws.binary))
        self.assertEqual(receipt["required_audio_seconds"], 11)
        self.assertEqual(receipt["mode"], "long_speech_test")
        self.assertEqual([row["role"] for row in receipt["motion_events"]], ["GE", "S", "Q"]*4)
        self.assertEqual([group["group_index"] for group in receipt["groups"]], [1, 2, 3, 4])
        self.assertTrue(all(group["actions_sent"] == 3 and group["actions_sent_complete"] for group in receipt["groups"]))
        self.assertEqual(c._motion_epoch, 1)
        self.assertTrue(receipt["actions_sent_complete"])
        self.assertFalse(receipt["visual_effect_confirmed"])
        self.assertFalse(receipt["playback_complete_confirmed"])
        self.assertTrue(c.public()["long_speech_test"])

    async def test_invalid_text_structure_and_audio_have_zero_writes(self):
        invalids = [{"pcm": b"\0\0"*(24000*11-1)}, {"text": "长"*121},
                    {"action_nodes": actions(7)}, {"action_nodes": []},
                    {"action_nodes": [short(), short(2.9)]},
                    {"action_nodes": [short(), long()]},
                    {"action_nodes": [short(pair=True)]},
                    {"action_nodes": [{**short(), "delay_seconds": .1}]},
                    {"reading": {"text": "短读", "pcm": b"\0\0"}},
                    {"action_nodes": actions(6), "pcm": b"\0\0"*(24000*17-1)}]
        for invalid in invalids:
            c = self.component()
            with self.subTest(invalid=tuple(invalid)), self.assertRaises(TypeError if "reading" in invalid else ValueError):
                await self.send(c, **invalid)
            self.assertFalse(c._ws.attempts)
            self.assertFalse(c._ws.binary)
            self.assertEqual(c.utterances_started, 0)

    async def test_legacy_audio_and_motion_entries_cannot_bypass_plan_validation(self):
        c = self.component()
        with self.assertRaises(ValueError):
            await c.send_audio(b"\0\0"*(c.RATE*12), "本"*100)
        with self.assertRaises(ValueError):
            await c._send_audio(b"\0\0"*(c.RATE*12), "本"*100)
        with self.assertRaises(ValueError):
            await c.send_motion_expression(actions(), recovery=GE)
        self.assertFalse(c._ws.attempts)
        self.assertFalse(c._ws.binary)

    async def test_stop_during_first_and_later_groups_cancels_all_remaining_groups(self):
        for sent_count in (2, 4):
            c = self.component()
            task = asyncio.create_task(self.send(c))
            await eventually(lambda: len(c._ws.motions()) == sent_count, timeout=8)
            c.latch_stop()
            with self.subTest(sent_count=sent_count), self.assertRaises(RuntimeError):
                await task
            before = len(c._ws.sent), len(c._ws.binary)
            await asyncio.sleep(.03)
            self.assertEqual((len(c._ws.sent), len(c._ws.binary)), before)
            self.assertEqual(len(c._ws.motions()), sent_count)
            self.assertFalse(c.last_utterance["actions_sent_complete"])
            self.assertTrue(all(group["actions_sent"] == 0 for group in c.last_utterance["groups"][2:]))

    def test_all_three_flags_are_mutually_exclusive(self):
        flags = ("long_speech_test", "continuous_speech_test", "short_performance_test")
        for pair in combinations(flags, 2):
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                MotionComponent(NoNetwork(), Path(self.tmp.name), Path(self.tmp.name)/"unused.png",
                                **{name: True for name in pair})

    async def test_short_and_normal_still_accept_one_byte_pair_audio_with_thirty_char_limit(self):
        for short_mode in (False, True):
            c = self.component(long_speech_test=False, short_performance_test=short_mode)
            c._audio = AsyncMock()
            receipt = await c.send_expression(b"\0\0", "一句话", [short()], 1, recovery=GE)
            c._audio.assert_awaited_once_with(b"\0\0", count_audio=True)
            self.assertFalse(receipt["long_speech_test"])
            self.assertEqual([row["role"] for row in receipt["timeline"]],
                             ["GE", "S", "Q"] if short_mode else ["S", "Q"])
            with self.assertRaises(ValueError):
                await c.send_expression(b"\0\0", "本"*31, [short()], 2, recovery=GE)


if __name__ == "__main__":
    unittest.main()
