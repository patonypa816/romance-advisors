"""Offline round through the game, gift intake and Studio's sender. Use --json for the receipt."""
import asyncio
import copy
import json
import sys
import unittest

from appearance_studio_double import OfflineStudioAvatar
from romance.appearance import AppearanceLedger
from romance.dyhub_intake import DyhubIntake
from romance.server import Workbench
from test_server import SyntheticModel


class PortraitReplyModel(SyntheticModel):
    """Only the reply payload can distinguish these two synthetic portraits."""
    async def generate(self, task, payload):
        if task == "draft":
            draft = await super().generate(task, payload)
            draft["reaction_expression"] = {
                "emotion": "认真", "actions": [{
                    "anchor": {"quote": "我", "occurrence": 1}, "gaze": "看向镜头",
                    "expression": "神情认真", "motions": [{"part": "head", "description": "轻点一下头"}],
                }], "recovery": {"gaze": "看向镜头", "expression": "神情温和"},
            }
            return draft
        if task != "reply":
            return await super().generate(task, payload)
        self.calls.append((task, copy.deepcopy(payload)))
        self.inspect_at_reply()
        description = payload["reply_to_message"]["portrait"]["description"]
        if "梳理整齐" in description:
            text = "周六见。你头像里把刘海梳起来了，见面我应该一眼就能认出你。"
        elif "日常刘海" in description:
            text = "周六见。你头像里那撮自然垂着的刘海很好认。"
        else:
            raise AssertionError("模拟女生收到了没有验收预期的头像描述")
        return {"message": text, "delta": 1, "ending": "continue"}


class AppearanceRoundTests(unittest.IsolatedAsyncioTestCase):
    receipts = {}

    async def asyncSetUp(self):
        self.studio = OfflineStudioAvatar()
        self.model = PortraitReplyModel()
        self.wb = Workbench(":memory:", model=self.model, avatar_client=self.studio)
        self.addAsyncCleanup(self.wb.close)
        self.game = self.wb.game
        self.program = self.wb.program
        self.program.start()
        await self.program.task
        await self.wb.job
        self.assertIsNone(self.game.data["error"])
        self.ledger = AppearanceLedger({"face-wash": 1, "late-gaming": -1},
                                      episode_id=self.game.episode["id"])
        # Inject at the same event handler as the collector without opening a source.
        self.intake = DyhubIntake(self.wb, on_simulated_gift=self.accept_gift)
        self.intake.episode_id = self.ledger.episode_id
        self.intake.room_id = self.ledger.room_id
        self.intake.stop_event = asyncio.Event()
        self.marks = []
        self.model.inspect_at_reply = lambda: self.mark("女生收到回复输入")
        self.mark("普通出场")
        self.game.submit_proposal("advisor", "军师", "约她周六见面")
        await self.game.close_collection()
        self.game.vote("viewer", "观众", "1")
        self.speech = self.game.current_round["id"] + ":reaction"
        self.typing = self.speech + ":typing"

    def mark(self, name, **details):
        self.marks.append({"event": name, "score": self.ledger.score,
                           "confirmed": self.program.confirmed_appearance_key,
                           "after_trace_rows": len(self.studio.wire_events), **details})

    def accept_gift(self, event):
        receipt = self.ledger.apply(event)
        if receipt["accepted"] and receipt["target"] is not None:
            self.program.request_appearance(receipt["target"])
        return receipt

    async def gift(self, gift_id, price, count, msg_id):
        event = {"type": "gift", "simulated": True, "platform": "douyin", "roomId": self.ledger.room_id,
                 "episodeId": self.ledger.episode_id, "id": msg_id, "user": {"id": "viewer"},
                 "data": {"giftId": gift_id, "diamondCount": price, "repeatCount": count,
                          "groupId": "combo-" + gift_id}}
        receipt = await self.intake.on_event(event)
        self.assertTrue(receipt and receipt["accepted"], receipt)
        self.mark("模拟礼物入账", gift=gift_id, receipt=receipt)

    def start_formal_pair(self, gate_job, gate_node):
        self.studio.hold(gate_job, gate_node)
        self.round_task = asyncio.create_task(self.game.close_voting())

        async def finish_if_failed():
            self.studio.release(gate_job, gate_node)
            if not self.round_task.done():
                self.round_task.cancel()
            await asyncio.gather(self.round_task, return_exceptions=True)
        self.addAsyncCleanup(finish_if_failed)

    async def public_bubble(self):
        # The component's S write precedes the game's receipt poll and DB commit.
        async with asyncio.timeout(12):
            while True:
                rows = [row for row in self.game.public_state()["messages"] if row["role"] == "man"]
                if rows:
                    return rows[0]
                if self.round_task.done():
                    self.fail(f"气泡未公开，回合已结束 {self.game.data['error']}")
                await asyncio.sleep(.01)

    async def finish_round(self, case, expected, final, expected_switches):
        state = await asyncio.wait_for(self.round_task, timeout=25)
        self.assertIsNone(state["error"], self.program.last_failure)
        self.assertEqual(state["round"]["phase"], "DONE")
        self.assertEqual(self.program.confirmed_appearance_key, final)
        wire = self.studio.wire_events
        switches = [row["order"] for row in wire if row.get("type") == 16]
        self.assertEqual(len(switches), expected_switches)
        speech, typing = self.studio.boundaries
        self.assertEqual([speech["job_id"], typing["job_id"]], [self.speech, self.typing])
        self.assertLess(speech["end_order"], typing["start_order"])
        # Include the gap between jobs and all of the audio handoff delay.
        self.assertFalse(any(speech["start_order"] <= i <= typing["end_order"] for i in switches))
        self.assertGreater(switches[-1], typing["end_order"])
        if len(switches) == 2:
            self.assertLess(switches[0], speech["start_order"])
        # Validate the actual asset in the captured packet, not just a target in the game ledger.
        observed_assets = [r["frame"]["payload"]["switch_avatar"]["avatar_id"] for r in wire
                           if r.get("type") == 16]
        self.assertEqual(observed_assets, ["100062", "100050"] if expected_switches == 2 else ["100062"])
        self.assertEqual(self.studio.confirmed_appearance_key, final)
        speech_frames = [r for r in wire if r["job_id"] == self.speech]
        self.assertIn(10, [r.get("type") for r in speech_frames])
        pcm = [r for r in speech_frames if r["kind"] == "pcm"]
        self.assertGreaterEqual(len(pcm), 2)
        for job in [self.speech, self.typing]:
            self.assertEqual([r["role"] for r in wire if r["job_id"] == job and r.get("type") == 9],
                             ["GE", "S", "Q"])
        speech_receipt = self.game.current_round["performance"]
        self.assertTrue(speech_receipt["handoff_wait_elapsed"])
        self.assertEqual(self.studio.drain_seconds, 4.0)
        self.assertGreaterEqual(speech_receipt["post_audio_wait_seconds"], self.studio.drain_seconds)
        messages = state["messages"]
        bubble = next(row for row in messages if row["role"] == "man")
        replies = [payload["reply_to_message"] for task, payload in self.model.calls if task == "reply"]
        self.assertEqual(len(replies), 1)
        target = replies[0]
        self.assertEqual(target, {k: bubble[k] for k in ("id", "text", "portrait")})
        saved = self.game.storage.load()["rounds"][-1]["man_appearance"]
        self.assertEqual(saved, {"message_id": target["id"], "portrait": target["portrait"]})
        self.assertEqual(target["portrait"]["key"], expected)
        self.assertEqual(self.marks[-1]["event"], "女生收到回复输入")
        self.assertEqual(self.marks[-1]["confirmed"], final)
        woman = messages[-1]
        self.assertEqual(woman["role"], "woman")
        self.assertIn("梳起来" if expected == "refined" else "自然垂着", woman["text"])
        self.mark("女生回复公开")
        self.receipts[case] = {"result": "pass", "run_id": self.program.run_id,
                               "round_id": self.game.current_round["id"], "marks": self.marks,
                               "studio_trace": wire, "protected_bounds": self.studio.boundaries,
                               "type16_orders": switches, "type16_inside_protected_pair": 0,
                               "audio_handoff_seconds": speech_receipt["post_audio_wait_seconds"],
                               "message_snapshot_readback": saved, "reply_to_message": target,
                               "reply": woman["text"]}

    async def test_gift_after_message_during_typing(self):
        await self.gift("face-wash", 6, 1, "wash-1")
        self.assertEqual(self.ledger.score, 56)
        await self.gift("face-wash", 6, 2, "wash-2")
        await self.program.wait_for_appearance()
        self.assertEqual((self.ledger.score, self.program.confirmed_appearance_key), (62, "refined"))
        self.mark("精致换形已模拟确认")
        self.start_formal_pair(self.typing, "Q")
        bubble = await self.public_bubble()
        self.mark("低头 S 后气泡已公开", message_id=bubble["id"], portrait=bubble["portrait"])
        self.assertEqual(bubble["portrait"]["key"], "refined")
        await self.gift("late-gaming", 12, 1, "down-1")
        self.assertEqual(self.ledger.score, 50)
        self.assertEqual(self.program.confirmed_appearance_key, "refined")
        self.assertFalse(self.studio.node_event(self.typing, "Q").is_set())
        self.studio.release(self.typing, "Q")
        await self.finish_round("gift_after_public_bubble", "refined", "normal", 2)

    async def test_gift_during_speech_cannot_replace_portrait_at_send(self):
        self.start_formal_pair(self.speech, "pcm")
        await self.studio.wait_node(self.speech, "pcm")
        self.assertFalse(any(m["role"] == "man" for m in self.game.public_state()["messages"]))
        await self.gift("face-wash", 12, 1, "wash-1")
        self.assertEqual((self.ledger.score, self.program.confirmed_appearance_key), (62, "normal"))
        self.studio.release(self.speech, "pcm")
        await self.finish_round("gift_during_pcm", "normal", "refined", 1)


if __name__ == "__main__":
    if "--json" in sys.argv:
        sys.argv.remove("--json")
        run = unittest.main(exit=False)
        if not run.result.wasSuccessful():
            sys.exit(1)
        print(json.dumps(AppearanceRoundTests.receipts, ensure_ascii=False, indent=2))
    else:
        unittest.main()
