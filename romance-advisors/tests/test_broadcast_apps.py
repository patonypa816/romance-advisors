"""Synthetic OBS files and mocked macOS; never open or modify real apps."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from romance import broadcast_apps as apps


class SetupFixture:
    def __init__(self, root):
        self.root = root
        self.scene_path = root / "basic/scenes/dedicated.json"
        self.scene_path.parent.mkdir(parents=True)
        self.profile_path = root / "basic/profiles/dedicated/basic.ini"
        self.profile_path.parent.mkdir(parents=True)
        self.collection = {"name": "专用配置", "virtual-camera": {"type2": 3}, "sources": [
            {"name": apps.SOURCE, "id": "browser_source", "monitoring_type": 2,
             "settings": {"url": "http://127.0.0.1:55173/static/obs-output.html", "width": 1080,
                          "height": 1920, "reroute_audio": True}},
            {"name": "专用节目", "id": "scene", "settings": {"items": [{"name": apps.SOURCE, "visible": True}]}}]}
        self.profile_path.write_text("[General]\nName=专用配置\n[Video]\nBaseCX=1080\nBaseCY=1920\nOutputCX=1080\nOutputCY=1920\n[Audio]\nMonitoringDeviceName=BlackHole 2ch\n")
        self.save()

    def save(self):
        self.scene_path.write_text(json.dumps(self.collection, ensure_ascii=False))

    def setup(self):
        return apps.inspect_setup(self.root)

    def log(self, *, tail="", start=None):
        start = start or datetime(2026, 11, 7, 14, 0, 0)
        path = self.root / "logs" / f"{start:%Y-%m-%d %H-%M-%S}.txt"
        path.parent.mkdir(exist_ok=True)
        path.write_text("Command Line Arguments: --profile 专用配置 --collection 专用配置 --scene 专用节目 --startvirtualcam\n"
                        "base resolution:   1080x1920\noutput resolution: 1080x1920\nBlackHole 2ch\n"
                        f"- source: '{apps.SOURCE}' (browser_source)\nSwitched to scene '专用节目'\n"
                        "Starting Virtual Camera output to Program\n" + tail)
        return start.timestamp()


class InspectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = SetupFixture(Path(self.temp.name))

    def test_unique_existing_setup_returns_port_without_writes_or_secrets(self):
        before = {p: p.read_bytes() for p in Path(self.temp.name).rglob("*") if p.is_file()}
        setup = self.fixture.setup()
        self.assertTrue(setup["ready"], setup)
        self.assertEqual(setup["port"], 55173)
        self.assertEqual(setup["profile"], "专用配置")
        self.assertEqual(setup["scene"], "专用节目")
        self.assertEqual(before, {p: p.read_bytes() for p in Path(self.temp.name).rglob("*") if p.is_file()})

    def test_unsafe_url_is_rejected_without_echoing_credentials(self):
        for url in ("http://user:SECRET@127.0.0.1:55173/static/obs-output.html", "http://0.0.0.0:55173/static/obs-output.html",
                    "http://127.0.0.1:55173/static/obs-output.html?token=SECRET", "http://127.0.0.1:55173/static/index.html"):
            self.fixture.collection["sources"][0]["settings"]["url"] = url
            self.fixture.save()
            result = self.fixture.setup()
            self.assertFalse(result["ready"])
            self.assertIsNone(result["port"])
            self.assertNotIn("SECRET", json.dumps(result))

    def test_ambiguous_collection_not_guessed(self):
        self.fixture.scene_path.with_name("another.json").write_bytes(self.fixture.scene_path.read_bytes())
        self.assertFalse(self.fixture.setup()["ready"])

    def test_source_visibility_dimensions_audio_and_program_are_required(self):
        changes = [(lambda d: d["sources"][1]["settings"]["items"][0].update(visible=False)),
                   (lambda d: d["sources"][0]["settings"].update(width=720)),
                   (lambda d: d["sources"][0]["settings"].update(reroute_audio=False)),
                   (lambda d: d["sources"][0].update(monitoring_type=0)),
                   (lambda d: d["virtual-camera"].update(type2=4))]
        original = json.dumps(self.fixture.collection)
        for change in changes:
            with self.subTest(change=change):
                self.fixture.collection = json.loads(original)
                change(self.fixture.collection)
                self.fixture.save()
                self.assertFalse(self.fixture.setup()["ready"])

    def test_wrong_profile_resolution_and_monitor_block_preparation(self):
        text = self.fixture.profile_path.read_text()
        for altered in (text.replace("OutputCX=1080", "OutputCX=720"), text.replace("BlackHole 2ch", "Speakers")):
            self.fixture.profile_path.write_text(altered)
            self.assertFalse(self.fixture.setup()["ready"])

    def test_only_current_process_live_program_log_counts(self):
        setup = self.fixture.setup()
        started = self.fixture.log()
        self.assertTrue(apps._log_status(setup, started)["confirmed"])
        self.assertFalse(apps._log_status(setup, started + 20)["confirmed"])
        for tail in ("==== Virtual Camera Stop\n", "==== Shutting down\n", "Switched to scene '其他场景'\n",
                     "Starting Virtual Camera output to Preview\n"):
            self.fixture.log(tail=tail)
            self.assertFalse(apps._log_status(setup, started)["confirmed"], tail)


class PrepareTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = SetupFixture(Path(self.temp.name))
        self.setup = self.fixture.setup()
        self.enterContext(patch.object(apps.sys, "platform", "darwin"))
        self.enterContext(patch.object(Path, "is_dir", return_value=True))
        self.open = self.enterContext(patch.object(apps, "_open"))
        self.process = self.enterContext(patch.object(apps, "_process_started", return_value=100))
        self.log = self.enterContext(patch.object(apps, "_log_status", return_value={"confirmed": True}))
        self.ws = self.enterContext(patch.object(apps, "_websocket_status", AsyncMock(side_effect=OSError("unavailable"))))

    async def test_running_correct_obs_reuses_current_log_without_password_or_new_launch(self):
        result = await apps.prepare_apps(self.setup, wait_seconds=0)
        self.assertTrue(result["obs_ready"])
        self.assertEqual(result["obs_evidence"], "current_process_log")
        self.assertTrue(result["companion_open"])
        self.assertFalse(result["ready"])
        self.assertEqual(len(result["manual_steps"]), 3)
        self.open.assert_not_called()

    async def test_cold_start_uses_exact_existing_flags_and_opens_companion(self):
        self.process.side_effect = [None, 100, None, 101]
        result = await apps.prepare_apps(self.setup, wait_seconds=0)
        self.assertTrue(result["obs_ready"])
        self.assertTrue(result["companion_open"])
        self.assertEqual(self.open.call_args_list[0].args[0], ["-a", apps.OBS_APP, "--args", "--profile", "专用配置",
            "--collection", "专用配置", "--scene", "专用节目", "--startvirtualcam"])
        self.assertEqual(self.open.call_args_list[1].args[0], ["-a", apps.COMPANION_APP])

    async def test_running_wrong_scene_never_switches_restarts_or_trusts_old_log(self):
        self.ws.side_effect = None
        self.ws.return_value = {"confirmed": False, "evidence": "websocket", "issue": "运行中的配置不一致"}
        result = await apps.prepare_apps(self.setup, wait_seconds=0)
        self.assertFalse(result["obs_ready"])
        self.assertIn("不一致", result["issues"][0])
        self.open.assert_not_called()

    async def test_missing_current_evidence_is_manual_not_ready(self):
        self.log.return_value = {"confirmed": False}
        result = await apps.prepare_apps(self.setup, wait_seconds=0)
        self.assertFalse(result["obs_ready"])
        self.assertTrue(result["issues"])
        self.open.assert_not_called()

    async def test_modified_configuration_rejected_before_launch(self):
        self.fixture.collection["sources"][0]["settings"]["url"] = "http://127.0.0.1:60000/static/obs-output.html"
        self.fixture.save()
        result = await apps.prepare_apps(self.setup, wait_seconds=0)
        self.assertFalse(result["obs_ready"])
        self.open.assert_not_called()
        self.process.assert_not_called()

    async def test_os_open_failure_is_bounded_and_no_false_ready(self):
        self.process.return_value = None
        self.open.side_effect = OSError("system permission denied")
        result = await apps.prepare_apps(self.setup, wait_seconds=0)
        self.assertFalse(result["obs_ready"])
        self.assertFalse(result["companion_open"])
        self.assertTrue(result["issues"])


class WebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup = {"scene": "专用节目", "profile": "专用配置", "collection": "专用配置",
                      "url": "http://127.0.0.1:55173/static/obs-output.html"}
        self.active = True
        self.streaming = False
        self.recording = False
        self.scene = "专用节目"
        self.calls = []

        async def call(request, data=None):
            self.calls.append(request)
            values = {"GetCurrentProgramScene": {"currentProgramSceneName": self.scene},
                      "GetProfileList": {"currentProfileName": "专用配置"},
                      "GetSceneCollectionList": {"currentSceneCollectionName": "专用配置"},
                      "GetVideoSettings": {"baseWidth": 1080, "baseHeight": 1920, "outputWidth": 1080, "outputHeight": 1920},
                      "GetInputSettings": {"inputSettings": {"url": self.setup["url"], "width": 1080, "height": 1920, "reroute_audio": True}},
                      "GetSceneItemList": {"sceneItems": [{"sourceName": apps.SOURCE, "sceneItemEnabled": True}]},
                      "GetInputAudioMonitorType": {"monitorType": "OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT"},
                      "GetVirtualCamStatus": {"outputActive": self.active},
                      "GetStreamStatus": {"outputActive": self.streaming},
                      "GetRecordStatus": {"outputActive": self.recording}}
            if request == "StartVirtualCam":
                self.active = True
                return {}
            return values[request]

        self.obs = SimpleNamespace(call=call, ws=SimpleNamespace(close=AsyncMock()))
        self.connect = AsyncMock(return_value=self.obs)
        module = SimpleNamespace(OBS=SimpleNamespace(connect=self.connect))
        spec = SimpleNamespace(loader=SimpleNamespace(exec_module=Mock()))
        self.enterContext(patch.object(apps.importlib.util, "spec_from_file_location", return_value=spec))
        self.enterContext(patch.object(apps.importlib.util, "module_from_spec", return_value=module))

    async def test_correct_running_output_read_only_and_never_reads_password(self):
        result = await apps._websocket_status(self.setup)
        self.assertTrue(result["confirmed"])
        self.assertFalse(any(request.startswith(("Set", "Start", "Stop", "Remove")) for request in self.calls))
        self.assertIsNone(self.connect.call_args.args[2])
        self.obs.ws.close.assert_awaited_once()

    async def test_only_inactive_correct_output_can_start_virtual_camera(self):
        self.active = False
        result = await apps._websocket_status(self.setup)
        self.assertTrue(result["confirmed"])
        self.assertEqual(self.calls.count("StartVirtualCam"), 1)
        self.assertFalse(any(request.startswith(("Set", "Stop", "Remove")) for request in self.calls))

    async def test_wrong_scene_and_active_stream_never_changed(self):
        self.scene = "另一个节目"
        self.active = False
        self.streaming = True
        result = await apps._websocket_status(self.setup)
        self.assertFalse(result["confirmed"])
        self.assertNotIn("StartVirtualCam", self.calls)
        self.assertNotIn("SetCurrentProgramScene", self.calls)

    async def test_inactive_camera_during_stream_or_record_is_manual(self):
        for streaming, recording in ((True, False), (False, True)):
            self.active, self.streaming, self.recording = False, streaming, recording
            self.calls.clear()
            result = await apps._websocket_status(self.setup)
            self.assertFalse(result["confirmed"])
            self.assertNotIn("StartVirtualCam", self.calls)


if __name__ == "__main__":
    unittest.main()
