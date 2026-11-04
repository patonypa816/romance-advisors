"""Routing checks use synthetic keys; no cloud request is made."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from romance.model import load_credentials, ModelError, QwenModel, TOKEN_PLAN_BASE


class TokenPlanRoutingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.key = self.root / "key.txt"
        self.key.write_text("sk-sp-synthetic-plan-credential-only\n")
        self.route = self.root / "route.json"
        self.write_route()
        patcher = patch("romance.model.QWEN_ROUTE_FILE", self.route)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_route(self, **changes):
        value = {"billing": "token_plan", "api_key_file": str(self.key)}
        value.update(changes)
        self.route.write_text(json.dumps(value))

    def test_explicit_route_wins_over_ordinary_environment_without_copying_key(self):
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "sk-ordinary-stale-key",
                                   "DASHSCOPE_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1"}):
            value = load_credentials()
            self.assertEqual(value.endpoint, TOKEN_PLAN_BASE + "/chat/completions")
            self.assertEqual(value.billing_route, "token_plan")
            self.assertTrue(value.key.startswith("sk-sp-"))
            public = QwenModel().public()
            self.assertEqual(public["billing_route"], "token_plan")
            self.assertTrue(public["ready"])
            self.assertNotIn("sk-sp-", json.dumps(public) + repr(value))
            self.assertNotIn("sk-sp-", self.route.read_text())

    def test_bad_route_or_key_never_falls_back_to_ordinary_credentials(self):
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "sk-ordinary-stale-key",
                                   "DASHSCOPE_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1"}):
            for key in ("sk-ordinary-key", "sk-sp-two lines\nother", "", "sk-sp-" + "x" * 5000):
                self.key.write_text(key)
                with self.assertRaises(ModelError):
                    load_credentials()
            self.write_route(api_key_file=str(self.root / "missing.txt"))
            with self.assertRaises(ModelError):
                load_credentials()
            self.route.write_text("broken")
            with self.assertRaises(ModelError):
                load_credentials()

    def test_plan_key_rotates_by_reference_and_route_cannot_override_host(self):
        before = load_credentials().key
        self.key.write_text("sk-sp-synthetic-rotated-credential\n")
        self.assertNotEqual(load_credentials().key, before)
        self.write_route(base_url="https://invalid.example")
        with self.assertRaises(ModelError):
            load_credentials()
        self.write_route(api_key_file="relative-key.txt")
        with self.assertRaises(ModelError):
            load_credentials()
