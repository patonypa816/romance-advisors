"""单L长音频回放离线验收；真实PCM节拍，无云调用、凭据或服务。"""
import asyncio
from pathlib import Path
import tempfile
import unittest

from action_schema import (compile_long_action_replay_timeline, compile_long_speech_timeline,
                           long_speech_required_audio_seconds, parse_long_action_replay_plan,
                           parse_long_speech_plan, validate_long_action_replay_plan)
from motion_component import MotionComponent
from test_motion_component import FakeSocket, NoNetwork, GE, short, long, eventually


def long_node(hold=17.36):
    node = long(hold)
    node["motions"][0]["description"] = "自然地抬起右手，随后轻轻点头，最后双手摊开"
    return node


def plan():
    return {"emotion": "温和", "observation": "当前姿态未知", "reading": None,
            "segments": [{"text": "本"*100, "instruction": "自然清楚",
                          "actions": [long_node()], "recovery": GE}]}


class LongActionReplayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="long-action-replay-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.count = 0

    def component(self, **options):
        self.count += 1
        out = Path(self.tmp.name)/str(self.count)
        c = MotionComponent(NoNetwork(), out, out/"unused.png",
                            **({"long_speech_test": True, "long_speech_action_kind": "L"} | options))
        c._ws = FakeSocket(write_delay=.01)
        c._ready.set(); c._joined.set(); c.state = "ready"
        c.live_id = "offline-only"; c.DRAIN_SECONDS = 0
        return c

    async def send(self, c, *, action_nodes=None, pcm=None, text="本"*100, **kwargs):
        pcm = b"\x01\0"*(24000*17+8640) if pcm is None else pcm
        return await c.send_expression(pcm, text, [long_node()] if action_nodes is None else action_nodes,
                                       1, recovery=GE, **kwargs)

    def test_independent_single_l_contract_preserves_description_and_full_hold(self):
        original = plan()
        result = parse_long_action_replay_plan(original)
        timeline = result["segments"][0]["timeline"]
        self.assertEqual([n["role"] for n in timeline], ["GE", "L", "Q"])
        self.assertEqual([n["at_seconds"] for n in timeline], [0, .5, 17.86])
        self.assertEqual([n["delay_seconds"] for n in timeline], [0, .5, 17.36])
        self.assertEqual([n["group_index"] for n in timeline], [1, 1, 1])
        self.assertFalse(any("audio_at_seconds" in n for n in timeline))
        self.assertIn(original["segments"][0]["actions"][0]["motions"][0]["description"], timeline[1]["instruction"])
        self.assertIn("正常坐姿", timeline[2]["instruction"])
        self.assertEqual(result["segments"][0]["text"], original["segments"][0]["text"])
        result["segments"][0]["timeline"] = []
        validate_long_action_replay_plan(result)
        self.assertEqual(len(result["segments"][0]["timeline"]), 3)
        with self.assertRaises(ValueError):
            parse_long_speech_plan(original)
        with self.assertRaises(ValueError):
            parse_long_action_replay_plan({**original, "timeline": []})

    async def test_seventeen_point_three_six_seconds_one_stream_and_only_tail_q(self):
        c = self.component()
        pcm = b"\x01\0"*(24000*17+8640)
        receipt = await self.send(c, pcm=pcm)
        ge, node, q = c._ws.motions()
        self.assertGreaterEqual(node["started"]-ge["completed"], .49)
        self.assertGreaterEqual(q["started"]-node["completed"], 17.35)
        self.assertLess(q["started"]-node["completed"], 17.55)
        text_rows = [r for r in c._ws.sent if r["frame"]["type"] == 10]
        self.assertEqual(len(text_rows), 1)
        self.assertGreaterEqual(text_rows[0]["started"], node["completed"])
        self.assertGreaterEqual(c._ws.binary[0]["time"], node["completed"])
        self.assertEqual(b"".join(r["chunk"] for r in c._ws.binary), pcm)
        self.assertEqual(receipt["pcm_transport_sent_bytes"], len(pcm))
        self.assertEqual([r["kind"] for r in c.diagnostics.records if r["kind"].startswith("audio_")],
                         ["audio_start", "audio_end"])
        self.assertEqual([n["role"] for n in receipt["motion_events"]], ["GE", "L", "Q"])
        self.assertEqual(receipt["mode"], "long_action_replay")
        self.assertEqual(receipt["long_speech_action_kind"], "L")
        self.assertEqual(receipt["required_audio_seconds"], 17.36)
        self.assertEqual(receipt["hold_seconds"], 17.36)
        self.assertEqual(len(receipt["groups"]), 1)
        self.assertTrue(receipt["actions_sent_complete"])
        self.assertEqual(c._motion_epoch, 1)
        self.assertFalse(receipt["visual_effect_confirmed"])
        self.assertFalse(receipt["playback_complete_confirmed"])
        self.assertEqual(c.public()["mode"], "long_action_replay")
        self.assertNotIn("group_interval_seconds", c.public()["motion_rules"])
        self.assertNotIn("minimum_tail_seconds", c.public()["motion_rules"])
        self.assertNotIn("long_speech_group_interval", receipt)

    async def test_stop_before_l_and_during_hold_cancels_unsent_nodes(self):
        for sent_count in (1, 2):
            c = self.component()
            task = asyncio.create_task(self.send(c))
            await eventually(lambda: len(c._ws.motions()) == sent_count, timeout=2)
            c.latch_stop()
            with self.subTest(sent_count=sent_count), self.assertRaises(RuntimeError):
                await task
            counts = len(c._ws.sent), len(c._ws.binary)
            await asyncio.sleep(.03)
            self.assertEqual((len(c._ws.sent), len(c._ws.binary)), counts)
            self.assertEqual(len(c._ws.motions()), sent_count)
            self.assertFalse(c.last_utterance["actions_sent_complete"])

    async def test_invalid_actions_short_audio_and_text_have_zero_writes(self):
        first, second = long_node(2), long_node(2)
        second["delay_seconds"] = 2
        invalids = [{"action_nodes": [first, second]}, {"action_nodes": []},
                    {"action_nodes": [short()]}, {"action_nodes": [long_node(1.5)]},
                    {"action_nodes": [long_node(30.1)]},
                    {"action_nodes": [{**first, "delay_seconds": .1}]},
                    {"action_nodes": [{**first, "motions": [{"part": "head", "description": "轻轻点头"}]}]},
                    {"action_nodes": [{**first, "motions": first["motions"]*2}]},
                    {"pcm": b"\0\0"*(24000*17+8639)}, {"text": "本"*121},
                    {"reading": {"text": "短读", "pcm": b"\0\0"}}]
        for invalid in invalids:
            c = self.component()
            with self.subTest(invalid=tuple(invalid)), self.assertRaises(ValueError):
                await self.send(c, **invalid)
            self.assertFalse(c._ws.attempts)
            self.assertFalse(c._ws.binary)
            self.assertEqual(c.utterances_started, 0)
        c = self.component()
        with self.assertRaises(ValueError):
            await c.send_audio(b"\0\0", "一句话")
        with self.assertRaises(ValueError):
            await c.send_motion_expression([long_node()], recovery=GE)
        self.assertFalse(c._ws.attempts)

    def test_kind_l_is_explicit_and_existing_three_five_second_s_contracts_unchanged(self):
        for kind in ("invalid", "l", None, True):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.component(long_speech_action_kind=kind)
        with self.assertRaises(ValueError):
            self.component(long_speech_test=False)
        for interval in (3, 5):
            nodes = [short(), short(interval), short(interval), short(interval)]
            timeline = compile_long_speech_timeline(nodes, GE, group_interval=interval)
            self.assertEqual([n["role"] for n in timeline], ["GE", "S", "Q"]*4)
            self.assertEqual(long_speech_required_audio_seconds(nodes, group_interval=interval), 3*interval+2)
            c = self.component(long_speech_action_kind="S", long_speech_group_interval=interval)
            self.assertEqual(c.public()["mode"], "long_speech_test")
            self.assertEqual(c.public()["motion_rules"]["group_interval_seconds"], interval)
            with self.assertRaises(ValueError):
                compile_long_action_replay_timeline(nodes, GE)
        for part in ("torso", "whole_body"):
            node = long_node(2)
            node["motions"][0]["part"] = part
            self.assertEqual(compile_long_action_replay_timeline([node], GE)[1]["kind"], "L")


if __name__ == "__main__":
    unittest.main()
