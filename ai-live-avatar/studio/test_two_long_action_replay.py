"""双L连续原PCM的离线合同与时序验收；不启动服务或访问云端。"""
import asyncio
import copy
from pathlib import Path
import tempfile
import unittest

from action_schema import (compile_long_action_replay_timeline, compile_long_speech_timeline,
                           parse_long_action_replay_plan, parse_long_speech_plan,
                           validate_long_action_replay_plan)
from motion_component import MotionComponent
from test_continuous_speech import DelayedSocket
from test_motion_component import FakeSocket, NoNetwork, GE, short, long, eventually


def actions(first_hold=7.68, second_delay=8.68, second_hold=8.68):
    first, second = long(first_hold), long(second_hold)
    first["motions"][0]["description"] = "轻轻点头一次，随后右手抬起向前一压"
    second["motions"][0]["description"] = "左手缓缓摊开，随后上身微微前倾"
    second["delay_seconds"] = second_delay
    return [first, second]


def plan():
    return {"emotion": "温和", "observation": "当前姿态未知", "reading": None,
            "segments": [{"text": "本"*100, "instruction": "自然清楚",
                          "actions": actions(), "recovery": GE}]}


class TwoLongActionReplayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="two-long-action-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.count = 0

    def component(self, *, socket=None, **options):
        self.count += 1
        out = Path(self.tmp.name)/str(self.count)
        c = MotionComponent(NoNetwork(), out, out/"unused.png",
                            **({"long_speech_test": True, "long_speech_action_kind": "L",
                                "long_action_group_count": 2} | options))
        c._ws = socket or FakeSocket()
        c._ready.set(); c._joined.set(); c.state = "ready"
        c.live_id = "offline-only"; c.DRAIN_SECONDS = 0
        return c

    async def send(self, c, *, nodes=None, pcm=None):
        pcm = b"\x01\0"*(24000*17+8640) if pcm is None else pcm
        return await c.send_expression(pcm, "本"*100, actions() if nodes is None else nodes, 1, recovery=GE)

    def test_double_l_is_explicit_and_plan_compiles_complete_q_between_groups(self):
        body = plan()
        parsed = parse_long_action_replay_plan(body, group_count=2)
        timeline = parsed["segments"][0]["timeline"]
        self.assertEqual([n["role"] for n in timeline], ["GE", "L", "Q"]*2)
        self.assertEqual([n["at_seconds"] for n in timeline], [0, .5, 8.18, 8.68, 9.18, 17.86])
        self.assertEqual([n["delay_seconds"] for n in timeline], [0, .5, 7.68, .5, .5, 8.68])
        self.assertEqual(timeline[3]["audio_at_seconds"], 8.18)
        self.assertEqual([n["group_index"] for n in timeline], [1, 1, 1, 2, 2, 2])
        for index in (2, 5):
            self.assertEqual(timeline[index]["instruction"], "眼神：自然看向镜头。\n表情：神情温和。")
        parsed["segments"][0]["timeline"] = []
        validate_long_action_replay_plan(parsed, group_count=2)
        self.assertEqual(len(parsed["segments"][0]["timeline"]), 6)
        with self.assertRaises(ValueError):
            parse_long_action_replay_plan(body)
        with self.assertRaises(ValueError):
            parse_long_speech_plan(body)
        single = copy.deepcopy(body)
        single["segments"][0]["actions"] = [long(17.36)]
        self.assertEqual([n["role"] for n in parse_long_action_replay_plan(single)["segments"][0]["timeline"]],
                         ["GE", "L", "Q"])
        for interval in (3, 5):
            self.assertEqual([n["at_seconds"] for n in compile_long_speech_timeline(
                [short(), short(interval)], GE, group_interval=interval)],
                [0, .5, 2, interval, interval+.5, interval+2])

    async def test_real_seventeen_point_three_six_second_pcm_once_with_six_type9(self):
        c = self.component()
        pcm = b"\x01\0"*(24000*17+8640)
        receipt = await self.send(c, pcm=pcm)
        rows = c._ws.motions()
        self.assertEqual(len(rows), 6)
        self.assertEqual(sum(r["frame"]["type"] == 10 for r in c._ws.sent), 1)
        self.assertEqual(b"".join(r["chunk"] for r in c._ws.binary), pcm)
        # 同一音频流还记录首末帧成功写入；精确顺序同时防止漏记和重复流。
        self.assertEqual([r["kind"] for r in c.diagnostics.records if r["kind"].startswith("audio_")],
                         ["audio_start", "audio_first_frame", "audio_last_frame", "audio_end"])
        self.assertEqual(receipt["required_audio_seconds"], 17.36)
        self.assertEqual(receipt["long_action_group_count"], 2)
        self.assertEqual(receipt["group_hold_seconds"], [7.68, 8.68])
        self.assertEqual([group["hold_seconds"] for group in receipt["groups"]], [7.68, 8.68])
        self.assertNotIn("hold_seconds", receipt)
        self.assertEqual(c.public()["motion_rules"]["sequential_node_count_limit"], 2)
        self.assertEqual(c.active_motion_plan["long_action_group_count"], 2)
        for group, hold in ((0, 7.68), (3, 8.68)):
            self.assertGreaterEqual(rows[group+1]["started"]-rows[group]["completed"], .49)
            self.assertGreaterEqual(rows[group+2]["started"]-rows[group+1]["completed"], hold-.01)
        self.assertGreaterEqual(rows[3]["started"]-rows[2]["completed"], .49)
        self.assertGreaterEqual(rows[3]["started"]-c._ws.binary[0]["time"], 8.17)
        self.assertGreaterEqual(sum(len(r["chunk"]) for r in c._ws.binary if r["time"] <= rows[3]["started"]),
                                8.18*48000)
        self.assertTrue(any(rows[2]["completed"] < r["time"] < rows[4]["started"] for r in c._ws.binary))
        self.assertLess(max(b["time"]-a["time"] for a, b in zip(c._ws.binary, c._ws.binary[1:])), .25)
        self.assertTrue(receipt["actions_sent_complete"])
        self.assertEqual(c._motion_epoch, 1)

    async def test_delayed_q_preserves_q_ge_l_and_second_hold_without_catchup(self):
        c = self.component(socket=DelayedSocket(delayed_node=3, node_delay=.9))
        receipt = await self.send(c, nodes=actions(2, 3, 2.2), pcm=b"\0\0"*(24000*7))
        ge1, l1, q1, ge2, l2, q2 = c._ws.motions()
        self.assertGreaterEqual(ge2["started"]-q1["completed"], .49)
        self.assertGreaterEqual(l2["started"]-ge2["completed"], .49)
        self.assertGreaterEqual(q2["started"]-l2["completed"], 2.19)
        self.assertGreater(ge2["completed"]-ge1["completed"], 3.8)
        self.assertEqual(receipt["required_audio_seconds"], 5.2)

    async def test_stop_after_q1_or_before_l2_cancels_all_unsent_nodes(self):
        for sent_count in (3, 4):
            c = self.component()
            task = asyncio.create_task(self.send(c, nodes=actions(2, 3, 2.2), pcm=b"\0\0"*(24000*6)))
            await eventually(lambda: len(c._ws.motions()) == sent_count, timeout=5)
            c.latch_stop()
            with self.subTest(sent_count=sent_count), self.assertRaises(RuntimeError):
                await task
            counts = len(c._ws.sent), len(c._ws.binary)
            await asyncio.sleep(.03)
            self.assertEqual((len(c._ws.sent), len(c._ws.binary)), counts)
            self.assertEqual(len(c._ws.motions()), sent_count)
            self.assertFalse(c.last_utterance["actions_sent_complete"])

    async def test_invalid_or_short_audio_rejects_without_writes(self):
        bad_part = actions()
        bad_part[1]["motions"][0]["part"] = "torso"
        for options in ({"pcm": b"\0\0"*(24000*17+8639)}, {"nodes": [long(17.36)]},
                        {"nodes": actions(7.68, 8.67, 8.68)}, {"nodes": bad_part},
                        {"nodes": [short(), short(3)]}):
            c = self.component()
            with self.subTest(options=tuple(options)), self.assertRaises(ValueError):
                await self.send(c, **options)
            self.assertFalse(c._ws.attempts)
            self.assertFalse(c._ws.binary)
            self.assertEqual(c.utterances_started, 0)
        for value in (0, 3, True, 2.0, "2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.component(long_action_group_count=value)
            with self.assertRaises(ValueError):
                compile_long_action_replay_timeline(actions(), GE, group_count=value)
        for options in ({"long_speech_test": False, "long_speech_action_kind": "S"},
                        {"long_speech_action_kind": "S"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.component(**options)


if __name__ == "__main__":
    unittest.main()
