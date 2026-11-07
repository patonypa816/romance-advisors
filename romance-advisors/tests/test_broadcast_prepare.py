"""Offline tests of the one-click orchestration; every service/app seam is isolated."""
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

from romance import broadcast_apps, broadcast_prepare as prepare, launcher


class PrepareTests(unittest.TestCase):
    def test_prepares_services_and_apps_with_saved_obs_port_without_starting_avatar(self):
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            studio = Path(td)
            (studio / ".venv/bin").mkdir(parents=True)
            (studio / ".venv/bin/python").touch()
            stack.enter_context(patch.object(prepare, "STUDIO", studio))
            setup = {"ready":True, "port":55173, "issues":[]}
            stack.enter_context(patch.object(broadcast_apps, "inspect_setup", return_value=setup))
            open_apps = stack.enter_context(patch.object(broadcast_apps, "prepare_apps", new_callable=AsyncMock))
            open_apps.return_value = {"obs_ready":True,"companion_open":True,"issues":[],"manual_steps":[]}
            run = stack.enter_context(patch.object(prepare.subprocess, "run"))
            hub = stack.enter_context(patch.object(prepare, "ensure_dyhub"))
            game = stack.enter_context(patch.object(launcher, "ensure_running", return_value={"url":"http://127.0.0.1:55173"}))
            read = stack.enter_context(patch.object(prepare, "local_json", return_value={"auto":{"state":"waiting"}}))
            page = stack.enter_context(patch.object(prepare.webbrowser, "open"))
            with redirect_stdout(io.StringIO()):
                prepare.prepare()
            self.assertEqual(run.call_args.args[0][-1], "--prepare-obs")
            hub.assert_called_once_with()
            game.assert_called_once_with(obs_output=True, port=55173)
            read.assert_called_once_with("http://127.0.0.1:55173/api/source")
            open_apps.assert_awaited_once_with(setup)
            page.assert_called_once_with("http://127.0.0.1:55173")

    def test_bad_saved_obs_configuration_stops_before_launching_any_process(self):
        with patch.object(broadcast_apps, "inspect_setup", return_value={"ready":False,"issues":["缺少专用节目源"]}), \
                patch.object(prepare.subprocess, "run") as run:
            with self.assertRaisesRegex(SystemExit, "专用节目源"):
                prepare.prepare()
            run.assert_not_called()

    def test_game_with_wrong_mode_or_address_is_never_replaced(self):
        with patch.object(launcher.subprocess, "Popen") as start:
            with patch.object(launcher, "running", return_value={"url":"http://127.0.0.1:1234"}):
                with self.assertRaisesRegex(SystemExit,"端口不同|端口与 OBS"):
                    launcher.ensure_running(obs_output=True,port=55173)
            with patch.object(launcher, "running", return_value={"url":"http://127.0.0.1:55173"}), \
                    patch.object(launcher.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps({"obs_output":False}).encode())):
                with self.assertRaisesRegex(SystemExit,"预览模式"):
                    launcher.ensure_running(obs_output=True,port=55173)
            start.assert_not_called()

    def test_healthy_dyhub_must_be_this_checkouts_listener(self):
        response = {"app":"dyhub", "pid":1234, "features":{"live_status":True}}
        with patch.object(prepare.socket, "socket") as sock, patch.object(prepare, "local_json", return_value=response), \
                patch.object(prepare.subprocess, "check_output") as owners, patch.object(prepare.subprocess, "Popen") as start:
            sock.return_value.__enter__.return_value.connect_ex.return_value = 0
            owners.side_effect = ["1234\n","p1234\nfcwd\nn/other/dyhub\n"]
            with self.assertRaisesRegex(SystemExit, "8757"):
                prepare.ensure_dyhub()
            owners.side_effect = ["1234\n",f"p1234\nfcwd\nn{prepare.DYHUB.resolve()}\n"]
            prepare.ensure_dyhub()
            start.assert_not_called()
