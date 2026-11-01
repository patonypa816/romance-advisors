"""5秒固定素材对照：离线schema与真实17.36秒PCM传输，不连接云端。"""
import asyncio
import copy
import tempfile
from pathlib import Path
import unittest

from action_schema import (compile_long_speech_timeline, long_speech_required_audio_seconds,
                           parse_long_speech_plan, validate_long_speech_actions,
                           validate_long_speech_plan)
from motion_component import MotionComponent
from test_motion_component import FakeSocket, NoNetwork, GE, short, eventually


def actions(interval=5.0, count=4):
    return [short(0 if index == 0 else interval) for index in range(count)]


def plan(interval=5.0):
    return {"emotion": "温和", "observation": "当前姿态未知", "reading": None,
            "segments": [{"text": "本"*100, "instruction": "自然清楚",
                          "actions": actions(interval), "recovery": GE}]}


class ReplayIntervalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="replay-interval-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.count = 0

    def component(self, **options):
        self.count += 1
        out = Path(self.tmp.name)/str(self.count)
        c = MotionComponent(NoNetwork(), out, out/"unused.png",
                            **({"long_speech_test": True, "long_speech_group_interval": 5.0} | options))
        c._ws = FakeSocket()
        c._ready.set(); c._joined.set(); c.state = "ready"
        c.live_id = "offline-only"
        c.DRAIN_SECONDS = 0
        return c

    async def send(self, c, *, pcm=None, action_nodes=None):
        # 与原素材相同的字节数/时长；确定性合成fixture避免测试依赖真实运行目录。
        pcm = b"\x01\0"*(24000*17+8640) if pcm is None else pcm
        return await c.send_expression(pcm, "本"*100, actions() if action_nodes is None else action_nodes,
                                       1, recovery=GE)

    def test_schema_parameter_changes_only_group_offsets_and_audio_requirement(self):
        original = plan(3.0)
        baseline = parse_long_speech_plan(original)
        changed = copy.deepcopy(original)
        for action in changed["segments"][0]["actions"][1:]:
            action["delay_seconds"] = 5.0
        result = parse_long_speech_plan(changed, group_interval=5)
        validate_long_speech_plan(result, group_interval=5.0)
        self.assertEqual(result["segments"][0]["text"], baseline["segments"][0]["text"])
        self.assertEqual(result["segments"][0]["recovery"], baseline["segments"][0]["recovery"])
        timeline = result["segments"][0]["timeline"]
        self.assertEqual([node["at_seconds"] for node in timeline], [0, .5, 2, 5, 5.5, 7, 10, 10.5, 12, 15, 15.5, 17])
        self.assertEqual([node["audio_at_seconds"] for node in timeline if "audio_at_seconds" in node], [4.5, 9.5, 14.5])
        self.assertEqual(long_speech_required_audio_seconds(actions(), group_interval=5), 17)
        self.assertEqual(long_speech_required_audio_seconds(actions(3)), 11)
        self.assertEqual(long_speech_required_audio_seconds(actions(5, 6), group_interval=5), 27)
        self.assertEqual([node["at_seconds"] for node in baseline["segments"][0]["timeline"]],
                         [0, .5, 2, 3, 3.5, 5, 6, 6.5, 8, 9, 9.5, 11])
        self.assertEqual([node["instruction"] for node in timeline],
                         [node["instruction"] for node in baseline["segments"][0]["timeline"]])

    def test_invalid_interval_or_action_mismatch_never_silently_rewrites_plan(self):
        for interval in (4, 0, -1, True, "5", float("nan"), float("inf")):
            with self.subTest(interval=interval):
                for operation in (
                    lambda: validate_long_speech_actions(actions(), group_interval=interval),
                    lambda: compile_long_speech_timeline(actions(), GE, group_interval=interval),
                    lambda: long_speech_required_audio_seconds(actions(), group_interval=interval),
                    lambda: parse_long_speech_plan(plan(), group_interval=interval),
                    lambda: validate_long_speech_plan(plan(), group_interval=interval),
                    lambda: self.component(long_speech_group_interval=interval),
                ):
                    with self.assertRaises(ValueError):
                        operation()
        for expected, actual in ((3, 5), (5, 3)):
            body = plan(actual)
            untouched = copy.deepcopy(body)
            with self.assertRaises(ValueError):
                parse_long_speech_plan(body, group_interval=expected)
            with self.assertRaises(ValueError):
                validate_long_speech_plan(body, group_interval=expected)
            self.assertEqual(body, untouched)
        for flags in ({}, {"short_performance_test": True}, {"continuous_speech_test": True}):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                self.component(long_speech_test=False, **flags)

    async def test_four_groups_fixed_seventeen_point_three_six_second_pcm(self):
        c = self.component()
        pcm = b"\x01\0"*(24000*17+8640)
        receipt = await self.send(c, pcm=pcm)
        motions = c._ws.motions()
        self.assertEqual(len(motions), 12)
        self.assertEqual(sum(row["frame"]["type"] == 10 for row in c._ws.sent), 1)
        self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary), pcm)
        self.assertEqual(receipt["audio_seconds"], 17.36)
        self.assertEqual(receipt["required_audio_seconds"], 17)
        self.assertEqual(receipt["long_speech_group_interval"], 5.0)
        self.assertEqual(c.active_motion_plan["long_speech_group_interval"], 5.0)
        self.assertEqual(c.public()["motion_rules"]["group_interval_seconds"], 5.0)
        self.assertEqual([row["kind"] for row in c.diagnostics.records if row["kind"].startswith("audio_")],
                         ["audio_start", "audio_end"])
        self.assertGreater(c._ws.binary[-1]["time"]-c._ws.binary[0]["time"], 17.2)
        for group in range(4):
            ge, s, q = motions[group*3:group*3+3]
            self.assertAlmostEqual(s["completed"]-motions[1]["completed"], group*5, delta=.08)
            self.assertGreaterEqual(s["started"]-ge["completed"], .49)
            self.assertGreaterEqual(q["started"]-s["completed"], 1.49)
            if group:
                offset = group*5-.5
                self.assertGreaterEqual(ge["started"]-c._ws.binary[0]["time"], offset-.01)
                self.assertGreaterEqual(sum(len(row["chunk"]) for row in c._ws.binary
                                            if row["time"] <= ge["started"]), offset*48000)
                self.assertGreaterEqual(ge["started"], motions[group*3-1]["completed"])
        self.assertEqual(c._motion_epoch, 1)
        self.assertTrue(receipt["actions_sent_complete"])

    async def test_stop_while_waiting_for_second_ge_cancels_remaining_three_groups(self):
        c = self.component()
        task = asyncio.create_task(self.send(c))
        await eventually(lambda: len(c._ws.motions()) == 3, timeout=4)
        c.latch_stop()
        with self.assertRaises(RuntimeError):
            await task
        counts = len(c._ws.sent), len(c._ws.binary)
        await asyncio.sleep(.03)
        self.assertEqual((len(c._ws.sent), len(c._ws.binary)), counts)
        self.assertEqual(len(c._ws.motions()), 3)
        self.assertFalse(c.last_utterance["actions_sent_complete"])
        self.assertTrue(all(group["actions_sent"] == 0 for group in c.last_utterance["groups"][1:]))

    async def test_short_pcm_and_mismatched_interval_reject_before_any_write(self):
        for options in ({"pcm": b"\0\0"*(24000*17-1)}, {"action_nodes": actions(3)}):
            c = self.component()
            with self.subTest(options=tuple(options)), self.assertRaises(ValueError):
                await self.send(c, **options)
            self.assertFalse(c._ws.attempts)
            self.assertFalse(c._ws.binary)
            self.assertEqual(c.utterances_started, 0)


if __name__ == "__main__":
    unittest.main()
