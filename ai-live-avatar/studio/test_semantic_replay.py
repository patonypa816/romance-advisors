"""Synthetic local source verification; no credentials, TTS or cloud calls."""
import base64
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
import wave

from fixed_replay import _sha
from semantic_replay import PreparedMinuteSemanticReplay, PreparedSemanticReplay, SemanticFixedReplay
from semantic_speech import compile_semantic_timeline, parse_historical_semantic_plan


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def make_source(root):
    """Build one verified two-anchor L source; root is the source run itself."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    text = "甲乙丙丁"
    actions = [{"anchor": {"quote": char, "occurrence": 1},
                "motions": [{"part": part, "description": description}],
                "gaze": "注视镜头", "expression": "神情平静"}
               for char, part, description in (("甲", "head", "微微抬起下巴示意"),
                                                ("丁", "right_arm", "右手轻摆一下表示肯定"))]
    part = {"text": text, "instruction": "自然清晰。", "actions": actions,
            "recovery": {"gaze": "注视镜头", "expression": "神情平静"}}
    plan = {"emotion": "平静", "observation": "观众向角色问候。", "reading": None, "segments": [part]}
    save(root / "plans/c0002.json", {"comment_id": "c0002", "comment": "你好", **plan})
    pcm = b"\x01\x00" * (24000 * 104 // 10)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(pcm)
    alignment = {"status": "ok", "timebase": "audio_start", "pcm_sha256": _sha(pcm),
                 "words": [{"text": char, "begin_time": at, "end_time": at + 240}
                           for char, at in zip(text, (80, 3000, 6640, 9440))]}
    final_actions = copy.deepcopy(actions)
    final_actions[1]["anchor"]["quote"] = "丙"
    timeline = compile_semantic_timeline(final_actions, part["recovery"], alignment, 10.4)
    folder = root / "tts/c0002-1"
    folder.mkdir(parents=True)
    (folder / "sample.pcm").write_bytes(pcm)
    (folder / "sample.wav").write_bytes(buffer.getvalue())
    save(folder / "result.json", {"status": "ok", "text": text, "audio_seconds": 10.4,
         "pcm_sha256": _sha(pcm), "wav_sha256": _sha(buffer.getvalue()), "alignment": alignment})
    save(folder / "timed-action-plan.json", {"text": text, "draft_actions": actions,
         "actions": final_actions, "timeline": timeline,
         "planning": {"phase": "tts_aligned", "group_count": 2, "status": "ready",
                      "pcm_sha256": _sha(pcm), "audio_seconds": 10.4}})
    events = [{"comment_id": "c0002", "group_id": "synthetic-group", "node_index": n["index"],
               "instruction": n["instruction"], "role": n["role"]} for n in timeline]
    sent_timeline = [{**node, "timebase": "answer_first_pcm",
                      "pcm_required_bytes": round(max(0, node["at_seconds"]) * 48000)} for node in timeline]
    save(folder / "component-send.json", {"group_id": "synthetic-group", "comment_id": "c0002",
         "segment_index": 1, "send_complete": True, "actions_sent_complete": True,
         "reading_text": None, "reading_audio_seconds": 0, "pcm_transport_sent_bytes": len(pcm),
         "audio_seconds": 10.4, "alignment": alignment, "timeline": sent_timeline, "motion_events": events})
    image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=")
    name = f"input-image-{_sha(image)[:16]}.png"
    (root / name).write_bytes(image)
    save(root / "variant.json", {"avatar_id": "123456789", "used_image_path": f"/not-read/{name}",
                                "image_sha256": _sha(image)})
    save(root / "component-receipt.json", {"state": "ended", "cloud_end_confirmed": True,
         "creation_input_mode": "avatar_id", "creation_avatar_id": "123456789"})
    return root


def seal_preparation(root):
    """Refresh file hashes after a deliberate synthetic fixture mutation."""
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["files"] = {name: _sha((root / name).read_bytes()) for name in PreparedSemanticReplay.FILE_LIMITS}
    save(root / "manifest.json", manifest)


def make_preparation(root, *, groups=3, seconds=29.2):
    """Synthetic TTS preparation without any invented prior cloud-send records."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    text = "甲乙丙丁戊己"
    actions = [{"anchor": {"quote": char, "occurrence": 1},
                "motions": [{"part": "head", "description": "轻轻点头一次"}],
                "gaze": "注视镜头", "expression": "神情平静"} for char in text[:groups]]
    part = {"text": text, "instruction": "自然清晰。", "actions": actions,
            "recovery": {"gaze": "注视镜头", "expression": "神情平静"}}
    save(root / "prepared-plan.json", {"emotion": "平静", "observation": "原创的对话素材。",
                                      "reading": None, "segments": [part]})
    pcm = b"\x01\x00" * round(24000 * seconds)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(pcm)
    alignment = {"status": "ok", "timebase": "audio_start", "pcm_sha256": _sha(pcm),
                 "words": [{"text": char, "begin_time": at, "end_time": at + 240}
                           for char, at in zip(text, (80, 5000, 10000, 15000, 20000, 25000))]}
    (root / "sample.pcm").write_bytes(pcm)
    (root / "sample.wav").write_bytes(buffer.getvalue())
    save(root / "result.json", {"status": "ok", "text": text, "audio_seconds": seconds,
         "pcm_sha256": _sha(pcm), "wav_sha256": _sha(buffer.getvalue()), "alignment": alignment,
         "pcm_path": "/never-read/provider-result/sample.pcm", "wav_path": "/never-read/provider-result/sample.wav"})
    image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=")
    (root / "image.png").write_bytes(image)
    save(root / "manifest.json", {"schema": PreparedSemanticReplay.SCHEMA, "source_kind": "prepared",
         "avatar_id": "123456789", "image_sha256": _sha(image), "files": {}})
    seal_preparation(root)
    return root


def make_minute_preparation(root, *, first_seconds=28.0, second_seconds=29.0):
    """Two synthetic ordinary TTS results, including repeated cross-segment anchors."""
    from tts_live import MODEL, VOICE
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    text = "甲乙丙丁戊己" + "民" * 84
    part = {"text": text, "instruction": "自然清晰。", "actions": [
        {"anchor": {"quote": char, "occurrence": 1},
         "motions": [{"part": "head", "description": "轻轻点头一次"}],
         "gaze": "注视镜头", "expression": "神情平静"} for char in "甲丙戊"],
        "recovery": {"gaze": "注视镜头", "expression": "神情平静"}}
    save(root / "prepared-plan.json", {"emotion": "平静", "observation": "两段原创互动。",
        "reading": None, "segments": [copy.deepcopy(part), copy.deepcopy(part)]})
    for number, seconds in enumerate((first_seconds, second_seconds), 1):
        folder = root / f"segment-{number}"
        folder.mkdir()
        pcm = bytes((number, 0)) * round(seconds * 24000)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            wav.writeframes(pcm)
        (folder / "sample.pcm").write_bytes(pcm)
        (folder / "sample.wav").write_bytes(buffer.getvalue())
        words = [{"text": char, "begin_time": at, "end_time": at + 240}
                 for char, at in zip(text[:5], (1000, 5000, 10000, 15000, 20000))]
        words.append({"text": text[5:], "begin_time": 24000, "end_time": int(seconds * 1000) - 100})
        save(folder / "result.json", {"status": "ok", "text": text, "model": MODEL, "voice": VOICE,
            "request_id": f"synthetic-tts-{number}", "audio_seconds": seconds,
            "pcm_sha256": _sha(pcm), "wav_sha256": _sha(buffer.getvalue()),
            "alignment": {"status": "ok", "timebase": "audio_start", "pcm_sha256": _sha(pcm), "words": words}})
    image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=")
    (root / "image.png").write_bytes(image)
    save(root / "manifest.json", {"schema": PreparedMinuteSemanticReplay.SCHEMA,
        "source_kind": "prepared_minute", "avatar_id": "123456789", "image_sha256": _sha(image),
        "files": {name: _sha((root / name).read_bytes()) for name in PreparedMinuteSemanticReplay.FILE_LIMITS}})
    return root


class SemanticReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="semantic-fixed-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def source(self, name="source"):
        return make_source(self.root / name)

    def mutate(self, source, relative, change):
        path = source / relative
        data = json.loads(path.read_text())
        change(data)
        save(path, data)

    def test_final_anchors_audio_and_all_content_stay_while_short_q_windows_change(self):
        source = self.source()
        before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
        replay = SemanticFixedReplay(source)
        original = json.loads((source / "plans/c0002.json").read_text())
        timed = json.loads((source / "tts/c0002-1/timed-action-plan.json").read_text())
        part = replay.plan["segments"][0]
        self.assertEqual(part["actions"], timed["actions"])
        for key in ("text", "instruction", "recovery"):
            self.assertEqual(part[key], original["segments"][0][key])
        self.assertEqual([n["role"] for n in part["timeline"]], ["GE", "S", "Q"] * 2)
        self.assertEqual([round(n["at_seconds"], 2) for n in part["timeline"]],
                         [-.42, .08, 1.58, 6.14, 6.64, 8.14])
        self.assertEqual([n["instruction"] for n in part["timeline"]],
                         [n["instruction"] for n in timed["timeline"]])
        self.assertEqual(replay.pcm, (source / "tts/c0002-1/sample.pcm").read_bytes())
        self.assertEqual(replay.wav, (source / "tts/c0002-1/sample.wav").read_bytes())
        self.assertEqual(replay.audio_seconds, 10.4)
        public = replay.public()
        self.assertTrue(public["semantic_speech"])
        self.assertEqual(public["action_kind"], "S")
        self.assertEqual(public["anchor_seconds"], [.08, 6.64])
        self.assertEqual([round(v, 2) for v in public["q_seconds"]], [1.58, 8.14])
        self.assertEqual(public["type9_count"], 6)
        self.assertNotIn(str(self.root), json.dumps(public))
        self.assertEqual(before, {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()})
        part["actions"][0]["gaze"] = "changed"
        self.assertNotEqual(part, replay.plan["segments"][0])

    def test_snapshot_and_prepare_preserve_alignment_plan_audio_and_zero_usage(self):
        replay = SemanticFixedReplay(self.source())
        out = self.root / "new-run"
        manifest = replay.snapshot(out)
        snapshot = out / "replay-source"
        self.assertEqual(json.loads((snapshot / "replay-plan.json").read_text()), replay.plan)
        self.assertEqual(json.loads((snapshot / "alignment.json").read_text()), replay.alignment)
        self.assertEqual(manifest["files"]["sample.pcm"]["sha256"], replay.pcm_sha256)
        part = replay.plan["segments"][0]
        before = copy.deepcopy(part)
        pcm, folder = replay.prepare(out, {"id": "c0042", "replay_fixture_fingerprint": replay.fingerprint}, part, 1)
        self.assertEqual(part, before)
        self.assertEqual(pcm, replay.pcm)
        self.assertEqual(json.loads((folder / "alignment.json").read_text()), replay.alignment)
        prepared = json.loads((folder / "timed-action-plan.json").read_text())
        self.assertEqual(prepared["timeline"], part["timeline"])
        self.assertFalse(prepared["planning"]["model_called"])
        result = json.loads((folder / "result.json").read_text())
        self.assertEqual(result["status"], "reused")
        self.assertEqual(result["cost_cny"], 0)
        with self.assertRaises(FileExistsError):
            replay.snapshot(out)
        with self.assertRaises(FileExistsError):
            replay.prepare(out, {"id": "c0042", "replay_fixture_fingerprint": replay.fingerprint}, part, 1)

    def test_public_describes_one_entry_per_group_with_simultaneous_motions(self):
        source = self.source()
        motion = {"part": "left_arm", "description": "轻抬左手"}
        original = json.loads((source / "plans/c0002.json").read_text())
        original["segments"][0]["actions"][0]["motions"].append(motion)
        save(source / "plans/c0002.json", original)
        folder = source / "tts/c0002-1"
        timed = json.loads((folder / "timed-action-plan.json").read_text())
        timed["draft_actions"][0]["motions"].append(motion)
        timed["actions"][0]["motions"].append(motion)
        result = json.loads((folder / "result.json").read_text())
        timeline = compile_semantic_timeline(timed["actions"], original["segments"][0]["recovery"],
                                             result["alignment"], 10.4)
        timed["timeline"] = timeline
        save(folder / "timed-action-plan.json", timed)
        sent = json.loads((folder / "component-send.json").read_text())
        sent["timeline"] = timeline
        for event, node in zip(sent["motion_events"], timeline):
            event["instruction"] = node["instruction"]
        save(folder / "component-send.json", sent)
        public = SemanticFixedReplay(source).public()
        self.assertEqual(public["actions"], ["微微抬起下巴示意，同时轻抬左手", "右手轻摆一下表示肯定"])
        self.assertEqual(public["original_actions"], ["微微抬起下巴示意", "轻抬左手", "右手轻摆一下表示肯定"])
        self.assertEqual(len(public["actions"]), len(public["s_seconds"]))

    def test_ge_comparisons_only_change_independent_ge_and_derived_timing_fields(self):
        source = self.source()
        baseline = SemanticFixedReplay(source)
        extended = SemanticFixedReplay(source, ge_lead_seconds=1.1)
        direct = SemanticFixedReplay(source, ge_lead_seconds=None)
        self.assertEqual(len({f.fingerprint for f in (baseline, extended, direct)}), 3)
        for fixture, ge_times, count, label in (
                (baseline, [-.42, 6.14], 6, "0.5秒GE基线"),
                (extended, [-1.02, 5.54], 6, "1.1秒延长GE"),
                (direct, [], 4, "取消独立GE")):
            with self.subTest(lead=fixture.ge_lead_seconds):
                part = fixture.plan["segments"][0]
                base_part = baseline.plan["segments"][0]
                self.assertEqual({k: v for k, v in part.items() if k != "timeline"},
                                 {k: v for k, v in base_part.items() if k != "timeline"})
                # S/Q内容与绝对起点不变；删除GE会合理改变节点编号和相邻间隔。
                keep = lambda n: {k: v for k, v in n.items() if k not in {"index", "delay_seconds"}}
                self.assertEqual([keep(n) for n in part["timeline"] if n["role"] != "GE"],
                                 [keep(n) for n in base_part["timeline"] if n["role"] != "GE"])
                public = fixture.public()
                self.assertEqual([round(v, 2) for v in public["ge_seconds"]], ge_times)
                self.assertEqual(public["s_seconds"], [.08, 6.64])
                self.assertEqual([round(v, 2) for v in public["q_seconds"]], [1.58, 8.14])
                self.assertEqual(public["type9_count"], count)
                self.assertEqual(public["comparison_label"], label)
                self.assertIs(public["independent_ge"], fixture.ge_lead_seconds is not None)
                self.assertEqual(fixture.pcm, baseline.pcm)
                self.assertEqual(fixture.wav, baseline.wav)
                with self.assertRaises(AttributeError):
                    fixture.ge_lead_seconds = .5

    def test_ge_configuration_and_cross_comparison_identity_fail_before_output(self):
        source = self.source()
        for value in (True, False, 0, 1, .6, "1.1", float("nan"), float("inf"), [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SemanticFixedReplay(source, ge_lead_seconds=value)
        extended = SemanticFixedReplay(source, ge_lead_seconds=1.1)
        direct = SemanticFixedReplay(source, ge_lead_seconds=None)
        extended_id = {"id": "c0042", "replay_fixture_fingerprint": extended.fingerprint}
        changed = extended.plan["segments"][0]
        changed["timeline"][0]["at_seconds"] = -.42
        for index, (comment, part) in enumerate((
                (extended_id, direct.plan["segments"][0]), (extended_id, changed),
                ({**extended_id, "replay_fixture_fingerprint": direct.fingerprint}, extended.plan["segments"][0]))):
            target = self.root / f"bad-identity-{index}"
            with self.assertRaises(ValueError):
                extended.prepare(target, comment, part, 1)
            self.assertFalse(target.exists())
        for index, fixture in enumerate((extended, direct)):
            target = self.root / f"comparison-{index}"
            manifest = fixture.snapshot(target)
            self.assertEqual(manifest["ge_lead_seconds"], fixture.ge_lead_seconds)
            self.assertEqual(manifest["independent_ge"], fixture.ge_lead_seconds is not None)
            _, folder = fixture.prepare(target, {"id": "c0042", "replay_fixture_fingerprint": fixture.fingerprint},
                                        fixture.plan["segments"][0], 1)
            prepared = json.loads((folder / "timed-action-plan.json").read_text())
            self.assertEqual(prepared["planning"]["ge_lead_seconds"], fixture.ge_lead_seconds)
            self.assertEqual(prepared["planning"]["comparison_label"], manifest["comparison_label"])

    def test_changed_original_or_final_content_anchor_timeline_pcm_binding_and_end_are_rejected(self):
        cases = [
            ("plans/c0002.json", lambda d: d["segments"][0].update(text="different")),
            ("plans/c0002.json", lambda d: d.update(comment_id="c0001")),
            ("plans/c0002.json", lambda d: d["segments"][0]["actions"].pop()),
            ("tts/c0002-1/timed-action-plan.json", lambda d: d["actions"][1]["motions"][0].update(description="另一动作")),
            ("tts/c0002-1/timed-action-plan.json", lambda d: d["actions"][1].update(gaze="另一眼神")),
            ("tts/c0002-1/timed-action-plan.json", lambda d: d["actions"][1]["anchor"].update(quote="丁")),
            ("tts/c0002-1/timed-action-plan.json", lambda d: d["timeline"][4].update(at_seconds=6.8)),
            ("tts/c0002-1/component-send.json", lambda d: d["timeline"][4].update(instruction="另一指令")),
            ("tts/c0002-1/component-send.json", lambda d: d["motion_events"][4].update(comment_id="c0001")),
            ("tts/c0002-1/component-send.json", lambda d: d.update(actions_sent_complete=False)),
            ("tts/c0002-1/component-send.json", lambda d: d.update(pcm_transport_sent_bytes=2)),
            ("tts/c0002-1/result.json", lambda d: d["alignment"].update(pcm_sha256="0" * 64)),
            ("tts/c0002-1/result.json", lambda d: d.update(wav_sha256="0" * 64)),
            ("tts/c0002-1/timed-action-plan.json", lambda d: d["planning"].update(pcm_sha256="0" * 64)),
            ("component-receipt.json", lambda d: d.update(state="ready")),
            ("component-receipt.json", lambda d: d.update(cloud_end_confirmed=False)),
            ("variant.json", lambda d: d.update(avatar_id="different")),
            ("variant.json", lambda d: d.update(image_sha256="0" * 64)),
            ("variant.json", lambda d: d.update(used_image_path="../../credential.csv")),
        ]
        for index, (relative, change) in enumerate(cases):
            source = self.source(f"bad-{index}")
            self.mutate(source, relative, change)
            with self.subTest(file=relative, case=index), self.assertRaises(ValueError):
                SemanticFixedReplay(source)
        source = self.source("bad-pcm")
        (source / "tts/c0002-1/sample.pcm").write_bytes(b"\x00\x00")
        with self.assertRaises(ValueError):
            SemanticFixedReplay(source)

    def test_paths_fingerprint_and_source_destination_are_rejected_before_writes(self):
        source = self.source()
        for comment in ("../c0002", "/c0002", "c0002/other", None):
            with self.subTest(comment=comment), self.assertRaises(ValueError):
                SemanticFixedReplay(source, comment)
        replay = SemanticFixedReplay(source)
        valid = {"id": "c0042", "replay_fixture_fingerprint": replay.fingerprint}
        changed = replay.plan["segments"][0]
        changed["alignment"]["words"][0]["begin_time"] += 1
        for index, (comment, part, label) in enumerate((
                ({**valid, "id": "../credential"}, replay.plan["segments"][0], 1),
                ({**valid, "replay_fixture_fingerprint": "other"}, replay.plan["segments"][0], 1),
                (valid, changed, 1), (valid, replay.plan["segments"][0], True))):
            destination = self.root / f"rejected-{index}"
            with self.assertRaises(ValueError):
                replay.prepare(destination, comment, part, label)
            self.assertFalse(destination.exists())
        with self.assertRaises(ValueError):
            replay.snapshot(source / "forbidden")
        self.assertFalse((source / "forbidden").exists())
        pcm = source / "tts/c0002-1/sample.pcm"
        cached = pcm.read_bytes()
        pcm.unlink()
        forbidden = self.root / "forbidden.pcm"
        forbidden.write_bytes(cached)
        pcm.symlink_to(forbidden)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            SemanticFixedReplay(source)


class PreparedSemanticReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="prepared-semantic-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_three_to_six_groups_reuse_one_preparation_without_prior_send_evidence(self):
        for groups in (3, 6):
            source = make_preparation(self.root / f"prepared-{groups}", groups=groups)
            before = {p.name: p.read_bytes() for p in source.iterdir()}
            a = PreparedSemanticReplay(source, ge_lead_seconds=1.1)
            b = PreparedSemanticReplay(source, ge_lead_seconds=None)
            self.assertEqual(a.pcm, b.pcm)
            self.assertEqual(a.wav, b.wav)
            self.assertEqual(a.alignment, b.alignment)
            self.assertEqual(a.audio_seconds, 29.2)
            self.assertNotEqual(a.fingerprint, b.fingerprint)
            self.assertEqual(a.public()["s_seconds"], b.public()["s_seconds"])
            self.assertEqual(a.public()["q_seconds"], b.public()["q_seconds"])
            self.assertEqual(a.public()["actions"], b.public()["actions"])
            self.assertEqual(a.public()["source_kind"], "prepared")
            self.assertEqual(a.public()["action_group_count"], groups)
            self.assertEqual(a.public()["type9_count"], groups * 3)
            self.assertEqual(b.public()["type9_count"], groups * 2)
            self.assertEqual(a.image_path, (source / "image.png").resolve())
            self.assertNotIn(str(source), json.dumps(a.public()))
            self.assertEqual(before, {p.name: p.read_bytes() for p in source.iterdir()})
            self.assertFalse((source / "component-send.json").exists())
            self.assertFalse((source / "component-receipt.json").exists())
            # A prepared source is a distinct provenance path, not an escape
            # hatch through the strict two-action recorded-source verifier.
            with self.assertRaises(ValueError):
                SemanticFixedReplay(source)

    def test_preparation_provenance_and_original_result_are_cached_in_snapshot(self):
        source = make_preparation(self.root / "prepared")
        fixture = PreparedSemanticReplay(source)
        genuine_result = (source / "result.json").read_bytes()
        manifest = fixture.snapshot(self.root / "new-run")
        snapshot = self.root / "new-run/replay-source"
        self.assertEqual((snapshot / "result.json").read_bytes(), genuine_result)
        self.assertEqual(manifest["source_kind"], "prepared")
        self.assertEqual(manifest["preparation_manifest_sha256"], _sha((snapshot / "preparation-manifest.json").read_bytes()))
        self.assertEqual((snapshot / "image.png").read_bytes(), (source / "image.png").read_bytes())
        part = fixture.plan["segments"][0]
        pcm, folder = fixture.prepare(self.root / "new-run", {
            "id": "c0042", "replay_fixture_fingerprint": fixture.fingerprint}, part, 1)
        self.assertEqual(pcm, fixture.pcm)
        prepared = json.loads((folder / "timed-action-plan.json").read_text())
        self.assertEqual(prepared["planning"]["source_kind"], "prepared")
        self.assertFalse(prepared["planning"]["model_called"])
        self.assertEqual(json.loads((folder / "result.json").read_text())["cost_cny"], 0)
        self.assertNotIn("original-timed-action-plan.json", manifest["files"])
        self.assertFalse((snapshot / "component-send.json").exists())
        self.assertFalse((snapshot / "component-receipt.json").exists())

    def test_manifest_whitelist_hashes_and_asset_identity_fail_closed(self):
        mutations = (
            lambda d: d.update(schema="other-version"),
            lambda d: d.update(source_kind="recorded"),
            lambda d: d.update(avatar_id="user@host"),
            lambda d: d.update(image_sha256="0" * 64),
            lambda d: d["files"].update({"../outside.json": "0" * 64}),
            lambda d: d["files"].update({"result.json": "0" * 64}),
            lambda d: d["files"].update({"result.json": {"sha256": "0" * 64}}),
            lambda d: d.update(credentials_file="/never-read/secret"),
        )
        for index, change in enumerate(mutations):
            source = make_preparation(self.root / f"bad-manifest-{index}")
            path = source / "manifest.json"
            manifest = json.loads(path.read_text())
            change(manifest)
            save(path, manifest)
            with self.subTest(case=index), self.assertRaises(ValueError):
                PreparedSemanticReplay(source)
        source = make_preparation(self.root / "changed-bytes")
        (source / "sample.pcm").write_bytes(b"\0\0")
        with self.assertRaises(ValueError):
            PreparedSemanticReplay(source)

    def test_resealed_invalid_plan_audio_alignment_and_group_count_are_rejected(self):
        cases = (
            ("prepared-plan.json", lambda d: d["segments"][0].update(text="different")),
            ("prepared-plan.json", lambda d: d["segments"][0]["actions"].pop()),
            ("prepared-plan.json", lambda d: d["segments"][0].update(instruction=" noncanonical ")),
            ("prepared-plan.json", lambda d: d.update(reading={"text": "甲", "instruction": "自然。"})),
            ("result.json", lambda d: d.update(status="error")),
            ("result.json", lambda d: d.update(audio_seconds=29.1)),
            ("result.json", lambda d: d.update(pcm_sha256="0" * 64)),
            ("result.json", lambda d: d.update(wav_sha256="0" * 64)),
            ("result.json", lambda d: d["alignment"].update(pcm_sha256="0" * 64)),
            ("result.json", lambda d: d["alignment"]["words"][0].update(text="错")),
        )
        for index, (relative, change) in enumerate(cases):
            source = make_preparation(self.root / f"resealed-{index}")
            path = source / relative
            value = json.loads(path.read_text())
            change(value)
            save(path, value)
            seal_preparation(source)
            with self.subTest(case=index), self.assertRaises(ValueError):
                PreparedSemanticReplay(source)
        for seconds in (26.9, 30.1):
            source = make_preparation(self.root / f"duration-{seconds}", seconds=seconds)
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                PreparedSemanticReplay(source)

    def test_natural_twenty_seven_point_six_seconds_and_lower_tolerance_boundary_are_accepted(self):
        for seconds in (27, 27.6):
            source = make_preparation(self.root / f"accepted-{seconds}", seconds=seconds)
            a = PreparedSemanticReplay(source, ge_lead_seconds=1.1)
            b = PreparedSemanticReplay(source, ge_lead_seconds=None)
            self.assertEqual(a.audio_seconds, seconds)
            self.assertEqual(a.pcm, b.pcm)
            self.assertEqual(len(a.pcm), round(seconds * 48000))
        source = make_preparation(self.root / "below-lower-bound", seconds=26.999)
        with self.assertRaisesRegex(ValueError, "27至30秒"):
            PreparedSemanticReplay(source)

    def test_prepared_paths_identity_and_output_cannot_escape_or_mix_modes(self):
        source = make_preparation(self.root / "prepared")
        a = PreparedSemanticReplay(source, ge_lead_seconds=1.1)
        b = PreparedSemanticReplay(source, ge_lead_seconds=None)
        part = a.plan["segments"][0]
        with self.assertRaises(ValueError):
            a.prepare(self.root / "bad-id", {"id": "c0042", "replay_fixture_fingerprint": b.fingerprint}, part, 1)
        self.assertFalse((self.root / "bad-id").exists())
        with self.assertRaises(ValueError):
            a.snapshot(source / "forbidden")
        self.assertFalse((source / "forbidden").exists())
        pcm = source / "sample.pcm"
        other = self.root / "outside.pcm"
        other.write_bytes(pcm.read_bytes())
        pcm.unlink()
        pcm.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            PreparedSemanticReplay(source)


class PreparedMinuteReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="minute-semantic-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_two_genuine_source_shapes_merge_once_without_retiming_or_global_limit_changes(self):
        source = make_minute_preparation(self.root / "prepared")
        f = PreparedMinuteSemanticReplay(source)
        original = json.loads((source / "prepared-plan.json").read_text())
        part = f.plan["segments"][0]
        self.assertEqual(f.audio_seconds, 57)
        self.assertEqual(len(part["text"]), 180)
        self.assertEqual(part["text"], "".join(p["text"] for p in original["segments"]))
        self.assertEqual(f.pcm, (source / "segment-1/sample.pcm").read_bytes() + (source / "segment-2/sample.pcm").read_bytes())
        self.assertEqual(f.public()["s_seconds"], [1, 10, 20, 29, 38, 48])
        self.assertEqual([a["anchor"]["occurrence"] for a in part["actions"]], [1, 1, 1, 2, 2, 2])
        self.assertEqual(f.alignment["words"][6]["begin_time"], 29000)
        self.assertEqual(f.alignment["pcm_sha256"], _sha(f.pcm))
        self.assertEqual(f.public()["type9_count"], 18)
        self.assertEqual(f.public()["source_kind"], "prepared_minute")
        self.assertEqual(f.public()["comparison_label"], "A · 1分钟六动作")
        self.assertEqual(f.public()["source_tts_segments"], 2)
        self.assertEqual((f.max_audio_seconds, f.max_text_codepoints), (60, 240))
        self.assertEqual(parse_historical_semantic_plan(original), original)
        with self.assertRaises(ValueError):
            parse_historical_semantic_plan({**original, "segments": [{k: v for k, v in part.items() if k not in {"alignment", "timeline"}}]})
        with self.assertRaises(ValueError):
            PreparedSemanticReplay(source)

    def test_snapshot_and_runtime_prepare_keep_both_real_tts_results_and_concatenation_provenance(self):
        source = make_minute_preparation(self.root / "prepared")
        f = PreparedMinuteSemanticReplay(source)
        out = self.root / "run"
        manifest = f.snapshot(out)
        archive = out / "replay-source"
        self.assertEqual(manifest["source_kind"], "prepared_minute")
        for n in (1, 2):
            self.assertEqual((archive / f"segment-{n}/result.json").read_bytes(),
                             (source / f"segment-{n}/result.json").read_bytes())
        concatenation = json.loads((archive / "concatenation.json").read_text())
        self.assertEqual(concatenation["tts_calls"], 2)
        self.assertFalse(concatenation["speed_changed"])
        self.assertFalse(concatenation["silence_added"])
        self.assertEqual([p["offset_seconds"] for p in concatenation["source_segments"]], [0, 28])
        with wave.open(str(archive / "sample.wav")) as wav:
            self.assertEqual(wav.readframes(24000 * 60), f.pcm)
        pcm, folder = f.prepare(out, {"id": "c0042", "replay_fixture_fingerprint": f.fingerprint}, f.plan["segments"][0], 1)
        self.assertEqual(pcm, f.pcm)
        self.assertEqual(json.loads((folder / "concatenation.json").read_text()), concatenation)
        result = json.loads((folder / "result.json").read_text())
        self.assertEqual(result["status"], "reused")
        self.assertEqual(result["cost_cny"], 0)
        self.assertFalse((source / "component-send.json").exists())
        self.assertFalse((source / "component-receipt.json").exists())

    def test_resealed_segment_model_voice_timestamp_plan_and_recovery_changes_are_rejected(self):
        cases = (
            ("segment-2/result.json", lambda d: d.update(voice="another-voice")),
            ("segment-2/result.json", lambda d: d.update(model="another-model")),
            ("segment-2/result.json", lambda d: d.update(audio_seconds=28)),
            ("segment-2/result.json", lambda d: d["alignment"].update(pcm_sha256="0" * 64)),
            ("segment-2/result.json", lambda d: d["alignment"]["words"][0].update(begin_time=-1)),
            ("prepared-plan.json", lambda d: d["segments"][1].update(instruction="另一指令")),
            ("prepared-plan.json", lambda d: d["segments"][1]["recovery"].update(expression="另一表情")),
            ("prepared-plan.json", lambda d: d["segments"][1]["actions"].pop()),
        )
        for index, (relative, change) in enumerate(cases):
            source = make_minute_preparation(self.root / f"bad-{index}")
            value = json.loads((source / relative).read_text())
            change(value)
            save(source / relative, value)
            manifest = json.loads((source / "manifest.json").read_text())
            manifest["files"][relative] = _sha((source / relative).read_bytes())
            save(source / "manifest.json", manifest)
            with self.subTest(case=index), self.assertRaises(ValueError):
                PreparedMinuteSemanticReplay(source)

    def test_manifest_paths_subdirectory_symlinks_and_duration_contract_fail_closed(self):
        source = make_minute_preparation(self.root / "escaped")
        manifest = json.loads((source / "manifest.json").read_text())
        manifest["files"]["../outside.json"] = "0" * 64
        save(source / "manifest.json", manifest)
        with self.assertRaises(ValueError):
            PreparedMinuteSemanticReplay(source)
        source = make_minute_preparation(self.root / "symlink")
        pcm = source / "segment-2/sample.pcm"
        target = self.root / "outside.pcm"
        target.write_bytes(pcm.read_bytes())
        pcm.unlink()
        pcm.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            PreparedMinuteSemanticReplay(source)
        for name, first, second in (("short", 26, 27), ("long", 29.5, 29.5)):
            with self.subTest(duration=name), self.assertRaisesRegex(ValueError, "54至58.5"):
                PreparedMinuteSemanticReplay(make_minute_preparation(self.root / name, first_seconds=first, second_seconds=second))


if __name__ == "__main__":
    unittest.main()
