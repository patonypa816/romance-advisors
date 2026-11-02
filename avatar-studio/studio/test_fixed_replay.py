"""固定素材回放离线测试；仅使用临时合成文件，不读取凭据或调用网络。"""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import wave

from action_schema import parse_long_speech_plan
from cloudbase_ai import LONG_SPEECH_EXAMPLE
from fixed_replay import FixedReplay


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))


class FixedReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fixed-replay-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def source(self, label="source", seconds=17.36):
        root = self.root / label
        root.mkdir()
        plan = parse_long_speech_plan(copy.deepcopy(LONG_SPEECH_EXAMPLE))
        part = plan["segments"][0]
        save(root / "plans/c0001.json", {"comment_id": "c0001", "comment": "固定原始描述", **plan})
        pcm = b"\x01\x00" * round(24000 * seconds)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            wav.writeframes(pcm)
        folder = root / "tts/c0001-1"
        folder.mkdir(parents=True)
        (folder / "sample.pcm").write_bytes(pcm)
        (folder / "sample.wav").write_bytes(buffer.getvalue())
        save(folder / "result.json", {"status": "ok", "text": part["text"], "audio_seconds": seconds,
                                      "pcm_sha256": digest(pcm), "wav_sha256": digest(buffer.getvalue())})
        events = [{"comment_id": "c0001", "group_id": "source-group", "node_index": n["index"],
                   "instruction": n["instruction"], "role": n["role"]} for n in part["timeline"]]
        save(folder / "component-send.json", {"group_id": "source-group", "send_complete": True,
             "actions_sent_complete": True, "reading_text": None, "reading_audio_seconds": 0,
             "pcm_transport_sent_bytes": len(pcm), "audio_seconds": seconds,
             "timeline": part["timeline"], "motion_events": events})
        image = b"offline-fixed-image-identity"
        image_name = f"input-image-{digest(image)[:16]}.png"
        (root / image_name).write_bytes(image)
        save(root / "variant.json", {"image_sha256": digest(image), "avatar_id": "123456789",
             "used_image_path": f"/not-read/original-machine/{image_name}"})
        save(root / "component-receipt.json", {"state": "ended", "cloud_end_confirmed": True,
             "creation_input_mode": "avatar_id", "creation_avatar_id": "123456789"})
        return root

    def mutate(self, root, relative, change):
        path = root / relative
        data = json.loads(path.read_text())
        change(data)
        save(path, data)

    def test_historical_receipt_requires_explicit_zero_reading(self):
        root = self.source("missing-text")
        self.mutate(root, "tts/c0001-1/component-send.json", lambda d: d.pop("reading_text"))
        FixedReplay(root)
        for i, change in enumerate((lambda d: d.pop("reading_audio_seconds"),
                                    lambda d: d.update(reading_text="短读"),
                                    lambda d: d.update(reading_audio_seconds=1))):
            root = self.source(f"bad-reading-{i}")
            self.mutate(root, "tts/c0001-1/component-send.json", change)
            with self.subTest(case=i), self.assertRaises(ValueError):
                FixedReplay(root)

    def test_only_group_interval_changes_and_public_has_no_paths(self):
        root = self.source()
        before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        replay = FixedReplay(root)
        source = json.loads((root / "plans/c0001.json").read_text())
        original_part = source["segments"][0]
        actual = replay.plan["segments"][0]
        self.assertEqual([n["delay_seconds"] for n in actual["actions"]], [0, 5, 5, 5])
        for old, new in zip(original_part["actions"], actual["actions"]):
            self.assertEqual({k: v for k, v in old.items() if k != "delay_seconds"},
                             {k: v for k, v in new.items() if k != "delay_seconds"})
        for key in ("text", "instruction", "recovery"):
            self.assertEqual(actual[key], original_part[key])
        for old, new in zip(original_part["timeline"], actual["timeline"]):
            self.assertEqual(old["instruction"], new["instruction"])
            if new["role"] in ("S", "Q"):
                self.assertEqual(new["delay_seconds"], .5 if new["role"] == "S" else 1.5)
        self.assertEqual(replay.audio_seconds, 17.36)
        self.assertEqual(replay.avatar_id, "123456789")
        public = replay.public()
        self.assertEqual(public["source_run"], root.name)
        self.assertEqual(public["pcm_sha256"], digest(replay.pcm))
        self.assertNotIn(str(self.root), json.dumps(public))
        self.assertEqual(len(public["actions"]), 4)
        self.assertEqual(before, {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()})
        changed = replay.plan
        changed["segments"][0]["text"] = "不得修改内部固定副本"
        self.assertEqual(replay.plan["segments"][0]["text"], original_part["text"])

    def test_snapshot_caches_verified_source_and_never_overwrites(self):
        root = self.source()
        replay = FixedReplay(root)
        original = (root / "plans/c0001.json").read_bytes()
        # 源文件后续变化不会悄悄更换已验证素材，快照写入的是构造时缓存字节。
        (root / "plans/c0001.json").write_bytes(b"changed-after-validation")
        target = self.root / "new-run"
        manifest = replay.snapshot(target)
        snapshot = target / "replay-source"
        self.assertEqual((snapshot / "original-plan.json").read_bytes(), original)
        for name, raw in (("sample.pcm", replay.pcm), ("sample.wav", replay.wav)):
            self.assertEqual((snapshot / name).read_bytes(), raw)
            self.assertEqual(manifest["files"][name], {"sha256": digest(raw), "bytes": len(raw)})
        self.assertEqual(json.loads((snapshot / "replay-plan.json").read_text()), replay.plan)
        self.assertEqual(json.loads((snapshot / "manifest.json").read_text()), manifest)
        self.assertEqual(manifest["fingerprint"], replay.fingerprint)
        with self.assertRaises(FileExistsError):
            replay.snapshot(target)
        with self.assertRaises(ValueError):
            replay.snapshot(root / "must-not-write")
        self.assertFalse((root / "must-not-write").exists())

    def test_prepare_reuses_exact_audio_and_records_zero_new_usage(self):
        replay = FixedReplay(self.source())
        comment = {"id": "c0042", "replay_fixture_fingerprint": replay.fingerprint}
        pcm, folder = replay.prepare(self.root / "new-run", comment, replay.plan["segments"][0], 1)
        self.assertEqual(folder, (self.root / "new-run/tts/c0042-1").resolve())
        self.assertEqual(pcm, replay.pcm)
        self.assertEqual((folder / "sample.pcm").read_bytes(), replay.pcm)
        self.assertEqual((folder / "sample.wav").read_bytes(), replay.wav)
        result = json.loads((folder / "result.json").read_text())
        self.assertEqual(result["status"], "reused")
        self.assertEqual(result["pcm_sha256"], digest(pcm))
        self.assertEqual(result["replay_fixture_fingerprint"], replay.fingerprint)
        self.assertTrue(result["usage_verified"])
        for key in ("input_tokens", "output_tokens", "total_tokens", "cost_cny"):
            self.assertEqual(result[key], 0)
        with self.assertRaises(FileExistsError):
            replay.prepare(self.root / "new-run", comment, replay.plan["segments"][0], 1)

    def test_prepare_rejects_changed_identity_plan_label_or_destination_before_write(self):
        replay = FixedReplay(self.source())
        valid = {"id": "c0002", "replay_fixture_fingerprint": replay.fingerprint}
        changed = replay.plan["segments"][0]
        changed["actions"][1]["gaze"] = "不同眼神"
        for index, (comment, part, label) in enumerate((
                ({**valid, "replay_fixture_fingerprint": "other"}, replay.plan["segments"][0], 1),
                (valid, changed, 1), (valid, replay.plan["segments"][0], "1"),
                ({**valid, "id": "../credentials"}, replay.plan["segments"][0], 1))):
            target = self.root / f"rejected-{index}"
            with self.subTest(index=index), self.assertRaises(ValueError):
                replay.prepare(target, comment, part, label)
            self.assertFalse(target.exists())
        with self.assertRaises(ValueError):
            replay.prepare(replay.source_run, valid, replay.plan["segments"][0], 1)

    def test_source_corruption_and_incomplete_or_wrong_comment_receipts_fail_closed(self):
        cases = (
            ("tts/c0001-1/result.json", lambda d: d.update(status="error")),
            ("tts/c0001-1/result.json", lambda d: d.update(text="不同台词")),
            ("tts/c0001-1/result.json", lambda d: d.update(pcm_sha256="0" * 64)),
            ("tts/c0001-1/result.json", lambda d: d.update(wav_sha256="0" * 64)),
            ("tts/c0001-1/component-send.json", lambda d: d.update(pcm_transport_sent_bytes=2)),
            ("tts/c0001-1/component-send.json", lambda d: d.update(actions_sent_complete=False)),
            ("tts/c0001-1/component-send.json", lambda d: d["motion_events"][0].update(comment_id="c0002")),
            ("component-receipt.json", lambda d: d.update(creation_avatar_id="999")),
            ("component-receipt.json", lambda d: d.update(cloud_end_confirmed=False)),
            ("variant.json", lambda d: d.update(image_sha256="0" * 64)),
        )
        for index, (relative, change) in enumerate(cases):
            root = self.source(f"bad-{index}")
            self.mutate(root, relative, change)
            with self.subTest(file=relative, index=index), self.assertRaises(ValueError):
                FixedReplay(root)

    def test_path_traversal_symlinks_invalid_spacing_and_insufficient_audio_are_rejected(self):
        root = self.source()
        for value in ("../c0001", "c0001/other", "/c0001", None):
            with self.subTest(comment=value), self.assertRaises(ValueError):
                FixedReplay(root, comment_id=value)
        for spacing in (3, 4, True, "5", float("nan")):
            with self.subTest(spacing=spacing), self.assertRaises(ValueError):
                FixedReplay(root, group_interval=spacing)
        pcm = root / "tts/c0001-1/sample.pcm"
        pcm.unlink()
        forbidden = self.root / "do-not-read.json"
        forbidden.write_text("synthetic-only-forbidden-target")
        pcm.symlink_to(forbidden)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            FixedReplay(root)
        with self.assertRaisesRegex(ValueError, "需要至少17秒"):
            FixedReplay(self.source("insufficient", seconds=11))


if __name__ == "__main__":
    unittest.main()
