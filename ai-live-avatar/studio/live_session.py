"""Local preparation queue, TTS and one Vidu session.

Starting the process has no cloud side effect. The browser starts the single live.
"""
from __future__ import annotations

import asyncio
import argparse
import fcntl
import shutil
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import time
import uuid

import aiohttp
from aiohttp import web
from motion_component import MotionComponent as PersistentComponent
from tts_live import MODEL as TTS_MODEL, VOICE, synthesize_segment
from speech_markup import SSML_VERSION
from action_schema import (ACTION_POST_SEND_SECONDS, READING_LEAD_SECONDS, compile_reading_timeline, parse_plan,
                           validate_short_performance_plan, short_performance_input_mode,
                           validate_continuous_speech_plan, continuous_speech_input_mode,
                           CONTINUOUS_MIN_AUDIO_SECONDS, CONTINUOUS_SECOND_GE_SECONDS,
                           CONTINUOUS_SECOND_S_SECONDS, validate_long_speech_plan,
                           validate_long_action_replay_plan,
                           long_speech_input_mode, long_speech_required_audio_seconds,
                           LONG_SPEECH_MAX_TEXT, LONG_SPEECH_MAX_GROUPS, LONG_SPEECH_GROUP_INTERVAL)
from semantic_speech import (compile_semantic_timeline, validate_alignment, SemanticSpeechError, LocalActionAlignment,
                             validate_ssml_action_anchors, speech_transcription,
                             alignment_reason_message, compile_dialogue_semantic_timeline,
                             DIALOGUE_ACTION_KIND, DIALOGUE_GE_LEAD_SECONDS, DIALOGUE_S_TO_Q_SECONDS)
from cloudbase_ai import ModelError
from diagnostics import Diagnostics, scrub

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ARTIFACTS = ROOT.parent / "artifacts"
STUDY = ARTIFACTS / "zhuyuanzhang-study-ab-20261029"
VARIANTS = {
    "A": {"image": "study-facing-fresh-smile-720x1280-v7.png", "title": "Terra · 整套读弹幕与自然接话",
          "goal": "结合上下文自然选择整套读弹幕或直接接话；整套保留前倾、眯眼、1秒后轻声短读与回正，不把动作拆开随机。"},
}
from interaction_delivery import is_motion_item

SOURCE_FILES = ("external_game.py", "live_interactions.py", "interaction_delivery.py", "interaction_fairness.py", "server.py", "automation.py", "cloudbase_ai.py", "bailian_ai.py", "comment_intake.py", "dyhub_client.py", "live_session.py", "credentials.py", "component_live.py", "tts_live.py", "brain-context.json", "brain_bridge.py", "action_schema.py", "motion_component.py", "diagnostics.py", "director.py", "avatar_library.py", "fixed_replay.py", "semantic_replay.py", "semantic_speech.py", "speech_markup.py", "timed_action_planner.py", "idle_motion.py", "idle_action_library.py")
PREVIOUS_COMPONENT_DIRS = (
    "zhuyuanzhang-terra-ge-overlay-20261029",
    "zhuyuanzhang-terra-action-rules-20261029",
    "zhuyuanzhang-component-log-test-20261029", "vidu-type9-return-seated-20261029",
    "vidu-simple-triple-no-tail-enabled-20261029", "vidu-simple-triple-no-tail-20261029",
    "vidu-simple-triple-disabled-20261029", "vidu-simple-triple-log-test-20261029",
    "vidu-action-log-test-20261029", "vidu-audio-default-library-20261029",
    "vidu-action-console-20261029", "vidu-action-disabled-20261029",
    "vidu-action-only-20261029", "vidu-action-state-timestamp-20261029",
    "vidu-type9-type97-ab-20261029", "zhuyuanzhang-structured-idle-20261029",
    "zhuyuanzhang-study-ab-20261029",
)


class RunGuard:
    """与上一轮共用入口锁，避免另一页同时创建收费会话。"""
    def __init__(self, path):
        self.path, self.file = Path(path), None

    def __enter__(self):
        self.file = self.path.open("a+")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RuntimeError("已有御书房入口运行；先结束并退出上一场") from None
        return self

    def __exit__(self, *args):
        fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()

from credentials import load_bailian, load_vidu

TERMINAL = {"ended", "error", "stop_unconfirmed", "creation_unknown"}


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


class LiveSession:
    def __init__(self, out: Path, variant: str, image: Path | None = None, *, short_performance_test=False,
                 continuous_speech_test=False, long_speech_test=False, replay_fixture=None):
        if variant not in VARIANTS:
            raise ValueError("本轮只使用已采用的A底图")
        if sum(bool(value) for value in (short_performance_test, continuous_speech_test, long_speech_test)) > 1:
            raise ValueError("单S、双动作组与长文试验不能同时启用")
        self.variant = variant
        self.short_performance_test = bool(short_performance_test)
        self.continuous_speech_test = bool(continuous_speech_test)
        self.long_speech_test = bool(long_speech_test)
        self.semantic_speech = not any((self.short_performance_test, self.continuous_speech_test, self.long_speech_test))
        self.semantic_replay = bool(getattr(replay_fixture, "semantic_speech", False))
        if replay_fixture is not None and not (self.semantic_replay and self.semantic_speech
                                               or not self.semantic_replay and self.long_speech_test):
            raise ValueError("固定素材必须匹配其语义或长文回放模式")
        self.replay_fixture = replay_fixture
        self.long_speech_action_kind = replay_fixture.action_kind if replay_fixture else "S"
        self.long_action_group_count = replay_fixture.long_action_group_count if replay_fixture else 1
        self.long_speech_group_interval = (replay_fixture.group_interval if replay_fixture
                                           else LONG_SPEECH_GROUP_INTERVAL)
        selected = Path(image) if image is not None else HERE / "assets" / VARIANTS[variant]["image"]
        # Capture exact input before starting any local server or paid request.
        raw = selected.read_bytes()
        if not raw or len(raw) >= 20_000_000:
            raise ValueError("底图为空或超过20MB")
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=False)
        self.diagnostics = Diagnostics(self.out)
        self.image = self.out / ("input-image" + selected.suffix.lower())
        self.image.write_bytes(raw)
        self.variant_info = {"variant": variant, **VARIANTS[variant], "source_image_path": str(selected.resolve()),
                             "used_image_path": str(self.image.resolve()), "image_sha256": hashlib.sha256(raw).hexdigest(),
                             "rule_version": "R-S0-S-L-Q-GE-3.2-whole-bundle", "max_simultaneous_short_actions": 2,
                             "gaze_expression_overlay": "every_R_S0_S_L_Q_instruction",
                             "reading_excerpt_preferred_chars": [2, 6], "reading_preference_is_hard_limit": False,
                             "reading_can_be_incomplete": True,
                             "reading_lead_seconds": READING_LEAD_SECONDS,
                             "reading_selection": "contextual_whole_bundle_or_direct",
                             "reading_history_window": 6,
                             "first_answer_action_after_entry_seconds": 1.0,
                             "reading_and_first_answer_audio_prepared_before_motion": True,
                             "sequential_node_count_limit": None, "short_to_q_seconds": 1.5,
                             "minimum_post_action_seconds": ACTION_POST_SEND_SECONDS,
                             "post_action_kinds": ["S", "L", "Q"]}
        if self.semantic_speech:
            self.variant_info.update(rule_version="semantic-speech-A-GE1.1-S1.5-Q-v1", semantic_speech=True,
                                     timed_action_planning="after_same_tts",
                                     dialogue_action_policy=None if self.replay_fixture else "A",
                                     semantic_action_kind=DIALOGUE_ACTION_KIND, independent_ge=True,
                                     speech_text_limit=120, max_simultaneous_long_actions=None,
                                     gaze_expression_overlay="semantic_anchor_GE_S_Q",
                                     minimum_action_window_seconds=DIALOGUE_S_TO_Q_SECONDS,
                                     semantic_ge_lead_seconds=DIALOGUE_GE_LEAD_SECONDS,
                                     short_to_q_seconds=DIALOGUE_S_TO_Q_SECONDS, max_simultaneous_short_actions=2,
                                     first_answer_action_after_entry_seconds=None,
                                     reading_recovery_before_answer_seconds=1.0)
        if self.short_performance_test:
            self.variant_info.update(short_performance_test=True,
                                     rule_version="short-performance-GE-0.5-S-1.5-Q",
                                     max_simultaneous_short_actions=1, sequential_node_count_limit=1,
                                     gaze_expression_overlay="GE_lead_then_S_and_Q",
                                     reading_selection="disabled", reading_lead_seconds=0,
                                     first_answer_action_after_entry_seconds=0.5,
                                     short_performance_lead_seconds=0.5)
        if self.continuous_speech_test:
            self.variant_info.update(continuous_speech_test=True,
                                     rule_version="continuous-speech-two-GE-S-Q-groups-v1",
                                     max_simultaneous_short_actions=1, sequential_node_count_limit=2,
                                     gaze_expression_overlay="two_independent_GE_S_Q_groups",
                                     reading_selection="disabled", reading_lead_seconds=0,
                                     first_answer_action_after_entry_seconds=0.5,
                                     continuous_second_ge_audio_seconds=CONTINUOUS_SECOND_GE_SECONDS,
                                     continuous_second_s_audio_seconds=CONTINUOUS_SECOND_S_SECONDS,
                                     continuous_min_audio_seconds=CONTINUOUS_MIN_AUDIO_SECONDS,
                                     audio_segments=1, action_groups=2)
        if self.long_speech_test:
            self.variant_info.update(long_speech_test=True,
                                     rule_version="long-speech-GE-S-Q-groups-v1",
                                     max_simultaneous_short_actions=1,
                                     sequential_node_count_limit=LONG_SPEECH_MAX_GROUPS,
                                     gaze_expression_overlay="independent_GE_S_Q_groups",
                                     reading_selection="disabled", reading_lead_seconds=0,
                                     first_answer_action_after_entry_seconds=0.5,
                                     speech_text_limit=LONG_SPEECH_MAX_TEXT,
                                     planned_group_interval_seconds=self.long_speech_group_interval,
                                     audio_segments=1)
        if self.replay_fixture:
            self.variant_info.update(fixed_replay=self.replay_fixture.public())
            if self.semantic_replay:
                minute = getattr(self.replay_fixture, "source_kind", None) == "prepared_minute"
                self.variant_info.update(rule_version="fixed-semantic-minute-A-v1" if minute else "fixed-semantic-S-GE-comparison-v2",
                                         timed_action_planning=("concatenated_prepared_verified_anchors" if minute else
                                                                "prepared_verified_anchors" if self.replay_fixture.public().get("source_kind") == "prepared"
                                                                else "reused_verified_final_anchors"),
                                         gaze_expression_overlay=("semantic_anchor_GE_S_Q" if self.replay_fixture.ge_lead_seconds is not None
                                                                  else "semantic_anchor_S_Q_embedded_GE"),
                                         semantic_ge_lead_seconds=self.replay_fixture.ge_lead_seconds,
                                         independent_ge=self.replay_fixture.ge_lead_seconds is not None,
                                         minimum_action_window_seconds=1.5, short_to_q_seconds=1.5,
                                         max_simultaneous_short_actions=2,
                                         max_simultaneous_long_actions=None)
                if minute:
                    self.variant_info.update(speech_text_limit=240, max_audio_seconds=60,
                                             source_tts_segments=2, source_kind="prepared_minute")
            elif self.long_speech_action_kind == "L":
                paired = self.long_action_group_count == 2
                self.variant_info.update(rule_version="fixed-two-long-action-GE-L-Q-groups-v1" if paired else "fixed-long-action-GE-0.5-L-window-Q-v1",
                                         sequential_node_count_limit=self.long_action_group_count,
                                         gaze_expression_overlay="two_GE_L_Q_groups" if paired else "one_GE_L_Q",
                                         planned_group_interval_seconds=None,
                                         long_action_group_count=self.long_action_group_count,
                                         long_action_hold_seconds=None if paired else self.replay_fixture.audio_seconds,
                                         long_action_group_holds_seconds=self.replay_fixture.public()["group_holds_seconds"],
                                         second_l_audio_seconds=self.replay_fixture.public()["second_l_audio_seconds"],
                                         second_l_semantic_anchor=self.replay_fixture.public()["second_l_anchor"],
                                         last_q_audio_seconds=self.replay_fixture.public()["last_q_audio_seconds"],
                                         q_to_next_l_seconds=self.replay_fixture.public()["q_to_next_l_seconds"])
        save(self.out / "variant.json", self.variant_info)
        snapshot = self.out / "source-used"
        snapshot.mkdir()
        for name in SOURCE_FILES:
            shutil.copyfile(HERE / name, snapshot / name)
        mode = {"short_performance_test": self.short_performance_test}
        if self.semantic_speech:
            mode["semantic_speech"] = True
            if not self.replay_fixture:
                mode.update(dialogue_action_policy="A", semantic_action_kind=DIALOGUE_ACTION_KIND,
                            ge_lead_seconds=DIALOGUE_GE_LEAD_SECONDS,
                            short_to_q_seconds=DIALOGUE_S_TO_Q_SECONDS)
        if self.continuous_speech_test:
            mode["continuous_speech_test"] = True
        if self.long_speech_test:
            mode["long_speech_test"] = True
        if self.replay_fixture:
            mode.update(fixed_replay=True, long_speech_group_interval=self.long_speech_group_interval,
                        long_speech_action_kind=self.long_speech_action_kind,
                        long_action_group_count=self.long_action_group_count,
                        replay_fixture_fingerprint=self.replay_fixture.fingerprint)
            if self.semantic_replay:
                mode.update(semantic_replay=True, semantic_action_kind="S",
                            ge_lead_seconds=self.replay_fixture.ge_lead_seconds)
                if getattr(self.replay_fixture, "source_kind", None) == "prepared_minute":
                    mode.update(source_kind="prepared_minute", max_audio_seconds=60,
                                max_text_codepoints=240, source_tts_segments=2)
        save(snapshot / "run-mode.json", mode)
        self.brain_context = json.loads((HERE / "brain-context.json").read_text())
        self.brain_context["study_variant"] = dict(self.variant_info)
        self.brain_context["short_performance_test"] = self.short_performance_test
        self.brain_context["continuous_speech_test"] = self.continuous_speech_test
        self.brain_context["long_speech_test"] = self.long_speech_test
        self.brain_context["semantic_speech"] = self.semantic_speech
        if self.replay_fixture:
            self.brain_context["fixed_replay"] = self.replay_fixture.public()
        save(self.out / "brain-context.json", self.brain_context)
        save(self.out / "source-manifest.json", {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in snapshot.iterdir() if p.is_file()})
        self.prefix = "/live/" + secrets.token_urlsafe(24)
        self.brain_token = secrets.token_urlsafe(32)
        self.diagnostics.register_secret(self.brain_token)
        self.origin = None
        self.http = None
        self.component = None
        self.avatar_id = None
        self.persona_epoch = 0
        self.started = False
        self.stopping = False
        self.failure = None
        self.cleanup_errors = []
        self.worker_fault = None
        self.comments = []
        self.signal = asyncio.Event()
        self.work = asyncio.Queue()
        self.background = set()
        self.browser_seen = time.monotonic()
        self.totals = {"input_tokens": 0, "output_tokens": 0, "cost_cny": 0.0, "audio_seconds": 0.0,
                       "usage_unverified_segments": 0}
        self.worker = None
        self.watchdog = None
        self.timed_action_planner = None
        self.timed_action_task = None
        # 音频只留在执行器内存中；状态快照不能携带PCM或凭据。
        self._prepared_comments = {}
        self._preparation_generations = {}
        self._preparing_originals = {}
        self._timed_action_tasks = set()
        self._external_preparation_tasks = set()
        self.delivery_gate = None
        self.on_delivery_started = None
        self.on_delivery_finished = None

    def apply_character(self, settings):
        """Replace every identity-bearing prompt field, retaining action rules."""
        context = copy.deepcopy(self.brain_context)
        context["operator_settings"] = dict(settings)
        context["character"] = {
            "identity": settings["name"] + "的AI虚构演绎；不是本人或真实历史记录",
            "temperament": settings["persona"], "speech": settings["reply_style"]}
        # The protocol example demonstrates shape, not a former persona's speech.
        examples = context.get("plan_contract", {}).get("segments", [])
        if examples:
            examples[0]["text"] = "这个主意很有意思。"
        if len(examples) > 1:
            examples[1]["text"] = "请再说说具体的难处。"
        save(self.out / "brain-context.json", context)
        self.brain_context = context

    def apply_avatar(self, image, asset_id, settings, *, reset_history=False):
        """Bind image provenance, component input and persona in one sync boundary.

        No await occurs here, so HTTP/automation cannot observe half of a role
        change. Call only while idle or after an operator confirms a sent switch.
        """
        from avatar_library import inspect_image
        raw = Path(image).read_bytes()
        extension, _, _, digest = inspect_image(raw)
        used = self.out / ("input-image-" + digest[:16] + "." + extension)
        used.write_bytes(raw)
        info = {**self.variant_info, "image": used.name, "avatar_id": asset_id,
                "title": settings["name"], "source_image_path": str(Path(image).resolve()),
                "used_image_path": str(used.resolve()), "image_sha256": digest}
        self.apply_character(settings)
        self.brain_context["study_variant"] = dict(info)
        save(self.out / "brain-context.json", self.brain_context)
        save(self.out / "variant.json", info)
        self.image, self.variant_info, self.avatar_id = used, info, asset_id
        if self.component is not None:
            self.component.image = used
        if reset_history:
            self.persona_epoch += 1
        self.event("avatar_identity_applied", avatar_id=asset_id, image_sha256=digest,
                   persona_epoch=self.persona_epoch, confirmed_switch=reset_history)

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.background.add(task)
        def finished(done):
            self.background.discard(done)
            if not done.cancelled() and done.exception() is not None:
                self.record_failure("background_cleanup", done.exception())
        task.add_done_callback(finished)
        return task

    def record_failure(self, stage, error):
        # Never archive exception messages: provider errors may contain URLs or keys.
        detail = {"stage": stage, "error_type": type(error).__name__}
        if getattr(error, "errno", None) in {5, 13, 24, 28, 30}:
            detail["errno"] = error.errno
        if self.failure is None:
            self.failure = detail
        else:
            self.cleanup_errors.append(detail)
        self.event("session_failure", **detail)

    def event(self, event, **fields):
        self.diagnostics.record("local", event, details=fields)
        try:
            with (self.out / "events.jsonl").open("a") as f:
                f.write(json.dumps(self.diagnostics.clean({"time": time.time(), "event": event, **fields}), ensure_ascii=False) + "\n")
        except (OSError, UnicodeError):
            self.diagnostics.record("local", "legacy_app_event_write_failed")

    def archive_comments(self):
        # 归档保留内部调度依据；浏览器只获得下面的展示投影。
        return [{k: v for k, v in c.items() if k not in {"lease", "plan", "fingerprint"}} for c in self.comments]

    def public_comments(self):
        fields = {"id", "status", "created_at", "input_kind", "operator_mode", "interaction_mode",
                  "interaction_kind", "topic_round", "nickname", "text", "answer", "error", "reading",
                  "segments_sent", "semantic_speech", "long_speech_test", "replay_fixture_fingerprint",
                  "generated_text_chars", "action_group_count"}
        result = []
        for c in self.comments[-100:]:
            row = {key: copy.deepcopy(value) for key, value in c.items() if key in fields}
            if c.get("unified_turn"):
                row["served_kinds"] = list(c.get("served_kinds", []))
                row["welcome_batch_size"] = sum(j.get("meta", {}).get("batch_size", 0)
                    for j in c.get("interaction_jobs", []) if j["kind"] == "welcome")
            groups = [{"actions": c.get("motion_actions", [])}] if is_motion_item(c) else c.get("delivery", [])
            row["actions"] = [m["description"] for part in groups for action in part.get("actions", [])
                              for m in action.get("motions", [])]
            row["delivery"] = [{"actions": [{"anchor": copy.deepcopy(a.get("anchor"))} for a in part.get("actions", [])],
                                "timeline": [{k: node[k] for k in ("role", "group_index", "at_seconds") if k in node}
                                             for node in part.get("timeline", [])]} for part in c.get("delivery", [])]
            row["motion_receipts"] = [{"motion_events": [{k: event[k] for k in ("seq_id", "actual_interval_seconds") if k in event}
                                                        for event in receipt.get("motion_events", [])]}
                                      for receipt in c.get("motion_receipts", [])]
            result.append(row)
        return result

    def snapshot(self):
        value = {"variant": self.variant_info, "session": self.component.public(), "started": self.started,
                 "comments": self.public_comments(), "totals": dict(self.totals),
                 "failure": self.failure, "cleanup_errors": list(self.cleanup_errors),
                 "worker_fault": self.worker_fault}
        # 固定回放使用原样本音频；当前资产的覆盖只影响新合成。
        value["tts_voice"] = (VOICE if getattr(self, "replay_fixture", None)
                              else getattr(self, "tts_voice", None) or VOICE)
        if hasattr(self, "brain_public"):
            value["brain"] = self.brain_public()
        return value

    def checkpoint(self):
        save(self.out / "conversation.json", {"comments": self.archive_comments(), "totals": self.totals,
             "failure": self.failure, "cleanup_errors": self.cleanup_errors, "worker_fault": self.worker_fault})
        external_checkpoint = getattr(self, "external_checkpoint", None)
        if external_checkpoint:
            external_checkpoint()

    async def startup(self, app):
        self.http = aiohttp.ClientSession()
        self.component = PersistentComponent(self.http, self.out, self.image, self.diagnostics,
                                             short_performance_test=self.short_performance_test,
                                             continuous_speech_test=self.continuous_speech_test,
                                             long_speech_test=self.long_speech_test,
                                             long_speech_group_interval=self.long_speech_group_interval,
                                             long_speech_action_kind=self.long_speech_action_kind,
                                             long_action_group_count=self.long_action_group_count)
        self.worker = asyncio.create_task(self.process_answers())
        # The host independently supervises control, brain and OBS liveness.

    async def shutdown(self, app, *, close_planner=None):
        # Every resource gets its own cleanup boundary, including disk failures.
        try:
            await self.request_stop("本机服务关闭")
        except Exception as error:
            self.record_failure("shutdown_stop", error)
        if close_planner:
            # 外部准备已收回；旧主讲/互动任务也要在本场 HTTP 关闭前完成取消记账。
            try:
                await close_planner()
            except (Exception, asyncio.CancelledError) as error:
                self.record_failure("shutdown_planner", error)
        if self.worker and not self.worker.done():
            self.work.put_nowait(None)
        if self.worker:
            try:
                await self.worker
            except (Exception, asyncio.CancelledError) as error:
                self.record_failure("shutdown_worker", error)
        if self.watchdog:
            self.watchdog.cancel()
            try:
                await self.watchdog
            except asyncio.CancelledError:
                pass
            except Exception as error:
                self.record_failure("shutdown_watchdog", error)
        try:
            await self.component.close()
        except Exception as error:
            self.record_failure("shutdown_component", error)
        if self.background:
            await asyncio.gather(*self.background, return_exceptions=True)
        try:
            save(self.out / "state-final.json", self.snapshot())
        except Exception as error:
            self.record_failure("shutdown_archive", error)
        finally:
            await self.http.close()
            self.diagnostics.record("local", "diagnostics_closed")
            self.diagnostics.closed = True

    def latch_stop(self):
        # HTTP入口先同步关闸，不能等后台协程获得执行机会才禁止新动作。
        self.stopping = True
        task = getattr(self, "timed_action_task", None)
        if task and not task.done():
            task.cancel()
        for task in getattr(self, "_timed_action_tasks", set()):
            if not task.done():
                task.cancel()
        for task in tuple(getattr(self, "_external_preparation_tasks", set())):
            if not task.done():
                task.cancel()
        getattr(self, "_prepared_comments", {}).clear()
        if self.component:
            self.component.latch_stop()

    async def request_stop(self, reason):
        self.latch_stop()
        for c in self.comments:
            if c["status"] in {"pending", "processing", "ready", "preparing", "prepared", "synthesizing", "planning_actions"}:
                c["status"] = "cancelled"
        self.signal.set()
        first_error = None
        try:
            self.checkpoint()
        except Exception as error:
            first_error = error
            self.record_failure("stop_checkpoint", error)
        try:
            result = await self.component.stop(reason)
        except (Exception, asyncio.CancelledError) as error:
            self.record_failure("component_stop", error)
            if first_error is None:
                first_error = error
        # 云 stop 已经请求后才等本地收费 TTS 收尾；慢请求不能推迟停场。
        preparations = tuple(getattr(self, "_external_preparation_tasks", set()))
        if preparations:
            await asyncio.gather(*preparations, return_exceptions=True)
        if first_error is not None:
            raise first_error
        return result

    async def fail_worker(self, error):
        if self.worker_fault:
            return
        self.worker_fault = {"code": "answer_worker_failed", "error_type": type(error).__name__}
        self.record_failure("answer_worker", error)
        self.latch_stop()
        # This worker will never restart paid work. Release queued ownership so
        # join() and callers can observe a terminal result instead of an orphan.
        while not self.work.empty():
            pending = self.work.get_nowait()
            try:
                if pending is not None:
                    pending.update(status="cancelled", external_worker_pending=False,
                                   cancel_reason="answer_worker_failed")
                    self.discard_prepared(pending)
            except Exception as cleanup_error:
                self.record_failure("worker_queue_cleanup", cleanup_error)
            finally:
                self.work.task_done()
        try:
            await self.request_stop("发送工作循环故障")
        except (Exception, asyncio.CancelledError) as cleanup_error:
            self.record_failure("worker_stop_cleanup", cleanup_error)

    async def prepare_audio(self, key, workspace, comment, part, label):
        """先备好本段声音；调用者决定何时允许人物开始动作。"""
        if self.cancel_before_delivery(comment):
            return None
        if getattr(self, "replay_fixture", None):
            # 固定对照直接复用已核验PCM；不读取语音凭据，也不重新合成或重复记费。
            pcm, segment_out = self.replay_fixture.prepare(self.out, comment, part, label)
            self.totals["audio_seconds"] += len(pcm) / (PersistentComponent.RATE * 2)
            self.event("replay_audio_prepared", comment_id=comment["id"], segment=label,
                       pcm_sha256=hashlib.sha256(pcm).hexdigest(), reused=True, new_tts_cost_cny=0)
            self.checkpoint()
            return pcm, segment_out
        comment["status"] = "synthesizing"
        self.checkpoint()
        segment_out = self.out / "tts" / f'{comment["id"]}-{label}'
        self.event("tts_start", comment_id=comment["id"], segment=label)
        tts_options = {"long_speech_test": True} if getattr(self, "long_speech_test", False) else {}
        if getattr(self, "semantic_speech", False) and label != "read":
            tts_options["word_timestamps"] = True
        if getattr(self, "tts_voice", None) is not None:
            tts_options["voice"] = self.tts_voice
        if part.get("ssml") is not None:
            tts_options["ssml"] = part["ssml"]
            # 读法展开与动作都以同次音频为据；不从XML字符位置推算时间。
            tts_options["word_timestamps"] = True
        synthesis = synthesize_segment(self.http, key, workspace, segment_out,
                                       part["text"], part["instruction"], **tts_options)
        cancelled = False
        if id(comment) in getattr(self, "_preparing_originals", {}):
            # 撤销本地准备不能假装已发出的收费调用没有发生。收回结果记账，
            # 然后丢弃音频；不会自动重试，也不会把准备失败传播给当前发送器。
            task = asyncio.create_task(synthesis)
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    # 全场停止与关闭流程可能各撤销一次；每次都保护已发出的
                    # TTS，而不是第二次撤销就把远端结果和费用证据一同丢掉。
                    cancelled = True
            result = task.result()
        else:
            result = await synthesis
        if result.get("usage_verified"):
            for field in ("input_tokens", "output_tokens", "cost_cny"):
                value = result.get(field) or 0
                self.totals[field] += value
                comment["usage"][field] += value
        else:
            self.totals["usage_unverified_segments"] += 1
            comment["usage"]["unverified_segments"] = comment["usage"].get("unverified_segments", 0) + 1
        self.totals["audio_seconds"] += result.get("audio_seconds") or 0
        self.event("tts_result", comment_id=comment["id"], segment=label, status=result["status"],
                   request_id=result.get("request_id"), usage_verified=result.get("usage_verified"))
        # 已发生的TTS消耗仍需入账；撤销只阻止后续发送，不回退计费。
        if cancelled:
            comment["status"] = "cancelled"
            return None
        if self.cancel_before_delivery(comment):
            return None
        if result["status"] != "ok":
            comment["status"], comment["error"] = "error", "语音生成未完成：" + result["status"]
            return None
        pcm = Path(result["pcm_path"]).read_bytes()
        if hashlib.sha256(pcm).hexdigest() != result["pcm_sha256"]:
            raise RuntimeError("音频核验失败")
        if tts_options.get("word_timestamps"):
            # 时间戳只绑定这次实际合成的PCM，不沿用其他回复的时间点。
            part["alignment"] = result.get("alignment", {"status": "unavailable", "reason": "timestamps_missing"})
        if part.get("ssml") is not None:
            part["transcription_text"] = speech_transcription(part["text"], part["ssml"],
                part.get("alignment"), len(pcm) / (PersistentComponent.RATE * 2), result["pcm_sha256"])
        return pcm, segment_out

    def semantic_replay_limits(self):
        # The minute fixture verifies two ordinary TTS results before this
        # narrow playback exception. Dynamic speech keeps its 30s/120 limit.
        if getattr(getattr(self, "replay_fixture", None), "source_kind", None) == "prepared_minute":
            return {"max_audio_seconds": 60, "max_text_codepoints": 240}
        return {}

    async def prepare_timed_actions(self, comment, part, pcm, segment_out, number):
        """同次音频生成后再定位动作；规划期间仍可取消，任何音画都尚未发送。"""
        seconds = len(pcm) / (PersistentComponent.RATE * 2)
        digest = hashlib.sha256(pcm).hexdigest()
        alignment = part.get("alignment")
        if getattr(self, "semantic_replay", False):
            # 固定对照保留准备素材或已播样本的锚点，避免两轮重新规划改变条件。
            fixture = self.replay_fixture
            if (number != 1 or comment.get("replay_fixture_fingerprint") != fixture.fingerprint
                    or part != fixture.plan["segments"][0] or digest != fixture.pcm_sha256):
                raise ValueError("语义固定回放素材已改变")
            limits = self.semantic_replay_limits()
            validate_alignment(alignment, part["text"], seconds, pcm_sha256=digest, **limits)
            timeline = compile_semantic_timeline(part["actions"], part["recovery"], alignment,
                                                 seconds, action_kind="S", ge_lead_seconds=fixture.ge_lead_seconds,
                                                 **limits)
            if timeline != part["timeline"]:
                raise ValueError("语义固定回放时序已改变")
            return not self.cancel_before_delivery(comment)
        if not part["actions"]:
            part["timeline"] = []
            return True
        # 在收费模型请求前验证时间戳确实属于将要播放的这一份PCM。
        LocalActionAlignment(alignment, part["text"], seconds, pcm_sha256=digest)
        planner = getattr(self, "timed_action_planner", None)
        if not callable(planner):
            raise ModelError("timed_action_planner_unavailable")
        if self.cancel_before_delivery(comment):
            return False
        expiry_source = getattr(self, "_preparing_originals", {}).get(id(comment), comment)
        if number == 1 and expiry_source.get("expires_at", float("inf")) <= time.time():
            comment["status"] = "expired"
            return False
        draft = copy.deepcopy(part["actions"])
        comment["status"] = "planning_actions"
        part["action_planning"] = {"phase": "tts_aligned", "status": "planning"}
        save(segment_out / "timed-action-input.json", {
            "text": part["text"], "draft_actions": draft, "recovery": part["recovery"],
            "audio_seconds": seconds, "pcm_sha256": digest, "alignment": alignment})
        self.event("timed_action_planning_started", comment_id=comment["id"], segment=number,
                   audio_seconds=seconds, pcm_sha256=digest, action_group_count=len(draft))
        self.checkpoint()
        # part也承载运行状态；只传内容字段，SSML用于排除改写读法内的候选锚点。
        content = {key: copy.deepcopy(part[key]) for key in ("text", "instruction", "actions", "recovery")}
        if part.get("ssml") is not None:
            content["ssml"] = part["ssml"]
        task = asyncio.create_task(planner(content, copy.deepcopy(alignment), seconds,
                                           copy.deepcopy(self.brain_context), source_text=comment.get("text", "")))
        background = id(comment) in getattr(self, "_preparing_originals", {})
        tasks = getattr(self, "_timed_action_tasks", None)
        if tasks is None:
            tasks = self._timed_action_tasks = set()
        tasks.add(task)
        if not background:
            self.timed_action_task = task
        try:
            # 主线换题/撤销由队列标志通知；不要等一个迟到模型结果再决定是否发送。
            while True:
                if self.cancel_before_delivery(comment):
                    part["action_planning"]["status"] = "cancelled"
                    return False
                if number == 1 and expiry_source.get("expires_at", float("inf")) <= time.time():
                    comment["status"] = "expired"
                    part["action_planning"]["status"] = "expired"
                    return False
                if task.done():
                    break
                await asyncio.wait({task}, timeout=0.1)
            result = task.result()
            actions = result["actions"]
            # 只采纳锚点变化，不能借二阶段计划修改台词、动作、眼神或表情。
            if (len(actions) != len(draft) or any(
                    {k: v for k, v in new.items() if k != "anchor"} !=
                    {k: v for k, v in old.items() if k != "anchor"}
                    for new, old in zip(actions, draft))):
                raise ModelError("timed_action_plan_invalid")
            LocalActionAlignment(alignment, part["text"], seconds, pcm_sha256=digest).verify_actions(actions)
            validate_ssml_action_anchors(part["text"], part.get("ssml"), actions)
            timeline = compile_dialogue_semantic_timeline(actions, part["recovery"], alignment, seconds)
            part["actions"] = copy.deepcopy(actions)
            part["timeline"] = timeline
            part["action_planning"] = {**result["planning"], "status": "ready",
                                       "pcm_sha256": digest, "audio_seconds": seconds}
            save(segment_out / "timed-action-plan.json", {
                "text": part["text"], "draft_actions": draft, "actions": actions,
                "timeline": timeline, "planning": part["action_planning"]})
            self.event("timed_action_plan_accepted", comment_id=comment["id"], segment=number,
                       actions=actions, timeline=timeline, pcm_sha256=digest)
            return True
        except Exception as error:
            part["action_planning"]["status"] = "error"
            raise
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            tasks.discard(task)
            if getattr(self, "timed_action_task", None) is task:
                self.timed_action_task = None

    def cancel_before_delivery(self, comment):
        """旧计划可在首次发送尝试前撤销；全局停止始终具有更高优先级。"""
        original = getattr(self, "_preparing_originals", {}).get(id(comment))
        if original is not None and self.cancel_before_delivery(original):
            comment["status"] = "cancelled"
            return True
        if self.stopping or (not comment.get("delivery_started") and (
                comment.get("cancel_requested") or comment.get("status") == "cancelled")):
            comment["status"] = "cancelled"
            return True
        return False

    def _preparation_fingerprint(self, comment):
        """把素材与生效语境一起绑定，避免旧音频被套到换过的台词上。"""
        value = {"plan": comment["plan"], "context": getattr(self, "brain_context", {}),
                 "binding": {key: comment.get(key) for key in (
                     "topic_id", "topic_version", "predecessor_id", "return_context_version",
                     "replay_fixture_fingerprint")}}
        # 不论游戏或普通互动，换音色/模型/解析版本都不能沿用旧声音与动作时序。
        value["speech_identity"] = {"voice": getattr(self, "tts_voice", None) or VOICE,
                                    "tts_model": TTS_MODEL, "ssml_version": SSML_VERSION}
        if comment.get("requires_prepared"):
            # 游戏缓存还绑定实际声音、角色和场次；同台词不代表同一份可播放素材。
            value["external"] = {key: comment.get(key) for key in (
                "external_owner_id", "external_generation", "external_job_id",
                "external_submission_mode", "external_request_fingerprint")}
            value["identity"] = {"voice": getattr(self, "tts_voice", None) or VOICE,
                "tts_model": TTS_MODEL, "avatar_id": self.avatar_id, "persona_epoch": self.persona_epoch,
                "image_sha256": self.variant_info.get("image_sha256"), "variant": self.variant_info}
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _preparation_media_fingerprint(comment, cached):
        # PCM 留在内存；同时核对字时间/最终计划与每段实际字节，防止缓存损坏后串配。
        audio = ([cached["reading"]] if cached.get("reading") else []) + cached["segments"]
        parts = [(len(pcm), hashlib.sha256(pcm).hexdigest()) for pcm, _ in audio]
        if len(cached["segments"]) != len(comment["plan"].get("segments", [])):
            raise ValueError("prepared_segment_count_changed")
        return hashlib.sha256(json.dumps({"plan": comment["plan"], "audio": parts},
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def external_delivery_seconds(self, comment, cached):
        # 只估本地发送器占用：保留真实 PCM、GE 前导、尾动作和原 4 秒排空。
        part = comment["plan"]["segments"][0]
        pcm = cached["segments"][0][0]
        timeline = part.get("timeline", [])
        preroll = max(0.0, -min((n["audio_at_seconds"] for n in timeline), default=0.0))
        tail = max((n["audio_at_seconds"] + 1.0 for n in timeline), default=0.0)
        cooldown = (max(0.0, getattr(self.component, "_motion_not_before", 0) - time.monotonic())
                    if timeline else 0.0)
        return cooldown + preroll + max(len(pcm) / (PersistentComponent.RATE * 2)
                                        + getattr(self.component, "DRAIN_SECONDS", 4.0), tail) + .5

    def discard_prepared(self, comment):
        comment_id = comment["id"] if isinstance(comment, dict) else comment
        getattr(self, "_prepared_comments", {}).pop(comment_id, None)
        generations = getattr(self, "_preparation_generations", None)
        if generations is None:
            generations = self._preparation_generations = {}
        generations[comment_id] = generations.get(comment_id, 0) + 1

    def _cached_preparation(self, comment):
        cached = getattr(self, "_prepared_comments", {}).get(comment["id"])
        if cached:
            try:
                valid = cached["fingerprint"] == self._preparation_fingerprint(comment)
                if valid and comment.get("requires_prepared"):
                    valid = cached.get("media_fingerprint") == self._preparation_media_fingerprint(comment, cached)
            except (KeyError, TypeError, ValueError, AttributeError):
                valid = False
            if valid:
                return cached
            self.discard_prepared(comment)
            raise ValueError("预备素材的身份、音频或字时间已改变；本条不会自动重新付费合成")
        return None

    async def prepare_comment(self, comment):
        """只准备一个完整回复，不领取唯一发送器，也不改变主讲书签。"""
        locks = getattr(self, "_preparation_locks", None)
        if locks is None:
            locks = self._preparation_locks = {}
        lock = locks.setdefault(comment["id"], asyncio.Lock())
        async with lock:
            if (self.cancel_before_delivery(comment) or comment.get("delivery_started")
                    or comment.get("status") in {"expired", "error", "sent"}):
                return False
            if comment.get("expires_at", float("inf")) <= time.time():
                comment["status"] = "expired"
                return False
            try:
                if self._cached_preparation(comment):
                    comment["status"] = "prepared"
                    return True
                fingerprint = self._preparation_fingerprint(comment)
                generations = getattr(self, "_preparation_generations", None)
                if generations is None:
                    generations = self._preparation_generations = {}
                generation = generations.get(comment["id"], 0)
                # 模型只修改隔离的计划；迟到结果通过复核后才回写，不能覆盖新主题。
                prepared = copy.deepcopy(comment)
                prepared["usage"] = comment["usage"]
                originals = getattr(self, "_preparing_originals", None)
                if originals is None:
                    originals = self._preparing_originals = {}
                originals[id(prepared)] = comment
                comment["status"] = "preparing"
                self.event("answer_preparation_started", comment_id=comment["id"])
                self.checkpoint()
                def preparation_current():
                    # 隔离副本保护台词，取消与过期则必须读取仍在变化的原条目。
                    # 每次await返回后、下一次收费调用前重新判断。
                    if self.cancel_before_delivery(comment):
                        return False
                    gate = getattr(self, "external_preparation_gate", None)
                    if comment.get("requires_prepared") and callable(gate) and not gate(comment):
                        comment.update(status="cancelled", cancel_requested=True)
                        return False
                    if comment.get("expires_at", float("inf")) <= time.time():
                        comment["status"] = "expired"
                        return False
                    if (generations.get(comment["id"], 0) != generation
                            or self._preparation_fingerprint(comment) != fingerprint):
                        comment["status"] = "cancelled"
                        return False
                    return True
                try:
                    if not preparation_current():
                        return False
                    self._validate_delivery_plan(prepared)
                    reading_audio, segments = None, []
                    if is_motion_item(prepared):
                        key, workspace = "", ""
                    else:
                        key, workspace = ("", "") if getattr(self, "replay_fixture", None) else load_bailian()
                        if key:
                            self.diagnostics.register_secret(key)
                        reading = prepared["plan"]["reading"]
                        if reading is not None:
                            reading_audio = await self.prepare_audio(key, workspace, prepared, reading, "read")
                            if reading_audio is None:
                                comment["status"] = prepared["status"]
                                return False
                            if not preparation_current():
                                return False
                        for number, part in enumerate(prepared["plan"]["segments"], 1):
                            if not preparation_current():
                                return False
                            audio = await self.prepare_audio(key, workspace, prepared, part, number)
                            if audio is None:
                                comment["status"] = prepared["status"]
                                if prepared.get("error"):
                                    comment["error"] = prepared["error"]
                                return False
                            if not preparation_current():
                                return False
                            if getattr(self, "semantic_speech", False):
                                if not await self.prepare_timed_actions(prepared, part, *audio, number):
                                    comment["status"] = prepared["status"]
                                    return False
                            segments.append(audio)
                    if not preparation_current():
                        return False
                    comment["plan"] = prepared["plan"]
                    if "delivery" in comment:
                        comment["delivery"] = prepared["plan"].get("segments", [])
                    if "reading" in comment:
                        comment["reading"] = prepared["plan"].get("reading")
                    cache = getattr(self, "_prepared_comments", None)
                    if cache is None:
                        cache = self._prepared_comments = {}
                    cached = {"fingerprint": self._preparation_fingerprint(comment),
                              "reading": reading_audio, "segments": segments}
                    if comment.get("requires_prepared"):
                        cached["media_fingerprint"] = self._preparation_media_fingerprint(comment, cached)
                    cache[comment["id"]] = cached
                    comment["status"] = "prepared"
                    self.event("answer_preparation_finished", comment_id=comment["id"], segments=len(segments))
                    return True
                finally:
                    key = ""
                    originals.pop(id(prepared), None)
            except asyncio.CancelledError:
                self.discard_prepared(comment)
                if not comment.get("delivery_started"):
                    comment["status"] = "cancelled"
                return False
            except Exception as exc:
                self.discard_prepared(comment)
                if not self.cancel_before_delivery(comment):
                    comment["status"] = "error"
                    comment["preparation_error"] = {"type": type(exc).__name__, "code": getattr(exc, "code", None)}
                    comment["error"] = "本条内容准备失败；未发送，本条不会自动重试"
                self.event("answer_preparation_error", comment_id=comment["id"], error_type=type(exc).__name__)
                return False
            finally:
                comment["preparation_status"] = comment.get("status", "error")
                if comment.get("status") == "error":
                    comment.setdefault("preparation_error", {"type": "PreparationError", "code": None})
                self.checkpoint()

    def _validate_delivery_plan(self, comment):
        if getattr(self, "short_performance_test", False):
            mode = short_performance_input_mode(comment)
            validate_short_performance_plan(comment["plan"], motion_only=mode == "motion")
        if getattr(self, "continuous_speech_test", False):
            continuous_speech_input_mode(comment)
            validate_continuous_speech_plan(comment["plan"])
        if getattr(self, "long_speech_test", False):
            long_speech_input_mode(comment)
            if self.long_speech_action_kind == "L":
                validate_long_action_replay_plan(comment["plan"], group_count=self.long_action_group_count)
            else:
                validate_long_speech_plan(comment["plan"], group_interval=self.long_speech_group_interval)
        if getattr(self, "replay_fixture", None):
            if (comment.get("replay_fixture_fingerprint") != self.replay_fixture.fingerprint
                    or comment["plan"] != self.replay_fixture.plan):
                raise ValueError("固定回放计划与核验素材不一致")

    def _begin_delivery(self, comment):
        if comment.get("delivery_started"):
            return not self.cancel_before_delivery(comment)
        if self.cancel_before_delivery(comment):
            return False
        gate = getattr(self, "delivery_gate", None)
        if callable(gate) and not gate(comment):
            # 门禁可能已确认预留失效并取消；不能把终态覆写回prepared永久占住素材。
            if not comment.get("cancel_requested") and comment.get("status") not in {"cancelled", "expired", "error", "sent"}:
                comment["status"] = "prepared"
            return False
        # 闸门与领取之间无await，新互动不能在准备协程返回时抢先漏过复核。
        self.component.comment_id = comment["id"]
        self.component.job_id = comment.get("external_job_id")
        comment["delivery_started"] = True
        comment["status"] = "sending"
        hook = getattr(self, "on_delivery_started", None)
        if callable(hook):
            hook(comment)
        return True

    async def send_motion_answer(self, comment):
        plan = comment["plan"]
        if self.cancel_before_delivery(comment):
            return
        if comment.get("expires_at", float("inf")) <= time.time():
            comment["status"] = "expired"
            return
        if comment.get("input_kind") == "external_game":
            # 无声A仍完整保留GE前导与S后的Q窗口，但没有音频排空阶段。
            cooldown = max(0.0, getattr(self.component, "_motion_not_before", 0) - time.monotonic())
            if cooldown > 0:
                # 过渡表演是可错过的机会，不能在上轮Q冷却后再开始而挤占正式台词。
                comment.update(status="expired", cancel_reason="external_motion_cooldown")
                return
            comment["external_send_seconds"] = cooldown + DIALOGUE_GE_LEAD_SECONDS + DIALOGUE_S_TO_Q_SECONDS + .5
        if not self._begin_delivery(comment):
            return
        comment["reading_status"] = "skipped"
        # 保守记录开始尝试；不把此标记误作实际写入或画面完成证据。
        self.checkpoint()
        try:
            options = {"dialogue_motion": True} if comment.get("input_kind") == "external_game" else {}
            receipt = await self.component.send_motion_expression(
                plan["actions"], recovery=plan["recovery"], number=1, **options)
            comment.setdefault("motion_receipts", []).append(receipt)
            comment["actions_sent"] = receipt.get("actions_sent", len(receipt.get("motion_events", [])))
            comment["motion_group_status"] = receipt.get("status", "sent")
            comment["status"] = "sent"
        except BaseException:
            # 停止时也保留已经成功写入的部分，不能伪称整组发送完成。
            receipt = getattr(self.component, "last_motion_receipt", {})
            if receipt.get("comment_id") == comment["id"]:
                comment.setdefault("motion_receipts", []).append(dict(receipt))
                comment["actions_sent"] = receipt.get("actions_sent", 0)
                comment["motion_group_status"] = receipt.get("status", "error")
            raise

    async def process_answers(self):
        try:
            await self._process_answers()
        except asyncio.CancelledError as error:
            if getattr(self, "_worker_interrupted", False):
                await self.fail_worker(error)
            raise
        except Exception as error:
            await self.fail_worker(error)

    async def _process_answers(self):
        while True:
            comment = await self.work.get()
            number = None
            try:
                if comment is None:
                    return
                self.event("answer_dequeued", comment_id=comment["id"],
                           job_id=comment.get("external_job_id"),
                           queue_wait_ms=round(max(0, time.monotonic() - comment.get(
                               "queued_at_monotonic", time.monotonic())) * 1000))
                # ready条目可在仍排队时被撤销；不能再次领取并覆写状态。
                if comment.get("status") in {"cancelled", "expired", "error", "sent"}:
                    continue
                if self.cancel_before_delivery(comment):
                    continue
                if self.stopping or self.component.public()["state"] in TERMINAL:
                    comment["status"] = "cancelled"
                    continue
                if comment.get("expires_at", float("inf")) <= time.time():
                    comment["status"] = "expired"
                    continue
                cached = self._cached_preparation(comment)
                if (comment.get("requires_prepared") or comment.get("status") == "prepared") and cached is None:
                    comment["failure_code"] = "prepared_cache_invalid"
                    raise ValueError("预备素材已失效；本条不会自动重新付费合成")
                self._validate_delivery_plan(comment)
                if is_motion_item(comment):
                    await self.send_motion_answer(comment)
                    continue
                if cached or getattr(self, "replay_fixture", None):
                    key, workspace = "", ""
                else:
                    key, workspace = load_bailian()
                    self.diagnostics.register_secret(key)
                reading = comment["plan"]["reading"]
                reading_pcm = None
                reading_out = None
                if reading is not None:
                    comment["reading_status"] = "synthesizing"
                    prepared = cached["reading"] if cached else await self.prepare_audio(
                        key, workspace, comment, reading, "read")
                    if prepared is None:
                        comment["reading_status"] = comment["status"]
                        continue
                    reading_pcm, reading_out = prepared
                    comment["reading_status"] = "ready"
                    comment["reading_audio_seconds"] = len(reading_pcm) / (PersistentComponent.RATE * 2)
                for number, part in enumerate(comment["plan"]["segments"], 1):
                    if self.cancel_before_delivery(comment):
                        break
                    # 短读和第一句回答都准备成功后才允许R开始，避免探头后
                    # 因第二次TTS等待而悬停；这不等于云端播放完成可观测。
                    prepared = cached["segments"][number - 1] if cached else await self.prepare_audio(
                        key, workspace, comment, part, number)
                    if prepared is None:
                        break
                    pcm, segment_out = prepared
                    if self.cancel_before_delivery(comment):
                        break
                    if getattr(self, "continuous_speech_test", False):
                        seconds = len(pcm) / (PersistentComponent.RATE * 2)
                        # 两组必须有足够的同一份真实音频承载；不拉慢语速、不补静音，
                        # 也不在声音结束后临时挤入未安排好的动作。
                        if seconds < CONTINUOUS_MIN_AUDIO_SECONDS:
                            comment["status"] = "error"
                            comment["error"] = (f"本次音频 {seconds:.2f} 秒，连续口播试验至少需要 "
                                                f"{CONTINUOUS_MIN_AUDIO_SECONDS:g} 秒；未发送动作或语音，本条不会自动重试")
                            comment["delivery_error"] = {"code": "continuous_audio_too_short",
                                                         "audio_seconds": seconds,
                                                         "minimum_seconds": CONTINUOUS_MIN_AUDIO_SECONDS}
                            self.event("continuous_audio_rejected", comment_id=comment["id"],
                                       **comment["delivery_error"])
                            break
                    if getattr(self, "long_speech_test", False):
                        seconds = len(pcm) / (PersistentComponent.RATE * 2)
                        needed = (sum(a["delay_seconds"] for a in part["actions"]) + part["actions"][-1]["hold_seconds"]
                                  if self.long_speech_action_kind == "L" else
                                  long_speech_required_audio_seconds(
                                      part["actions"], group_interval=self.long_speech_group_interval))
                        if seconds < needed:
                            groups = len(part["actions"])
                            comment["status"] = "error"
                            comment["error"] = (f"本次音频 {seconds:.2f} 秒，{groups} 个动作组至少需要 "
                                                f"{needed:g} 秒；未发送本条音画，不删动作、不自动重试")
                            comment["delivery_error"] = {"code": "long_audio_too_short",
                                                         "audio_seconds": seconds, "minimum_seconds": needed,
                                                         "action_group_count": groups}
                            self.event("long_audio_rejected", comment_id=comment["id"],
                                       **comment["delivery_error"])
                            break
                    semantic = getattr(self, "semantic_speech", False)
                    if semantic:
                        if not cached and not await self.prepare_timed_actions(comment, part, pcm, segment_out, number):
                            break
                        # 规划协程清理子任务时也会await；在返回执行器后再同步
                        # 关一次撤销闸门，覆盖恰在清理期间收到的换题/取消。
                        if self.cancel_before_delivery(comment):
                            break
                        comment["semantic_speech"] = True
                        self.event("semantic_timeline_compiled", comment_id=comment["id"], segment=number,
                                   timeline=part["timeline"], alignment_status=(part.get("alignment") or {}).get("status"))
                    # Expiry is checked again after TTS: a slow synthesis must
                    # not start an answer to a now stale automatic comment.
                    # Once speech has begun, finish the accepted short answer.
                    if number == 1 and comment.get("expires_at", float("inf")) <= time.time():
                        comment["status"] = "expired"
                        comment["reading_status"] = "expired" if reading else "skipped"
                        break
                    if comment.get("input_kind") == "external_game":
                        # Estimate local sender occupancy from actual PCM and the
                        # compiled GE/S/Q timeline, including pre-roll and drain.
                        # This is deliberately not a claim of cloud render completion.
                        timeline = part.get("timeline", [])
                        preroll = max(0.0, -min((n["audio_at_seconds"] for n in timeline), default=0.0))
                        tail = max((n["audio_at_seconds"] + 1.0 for n in timeline), default=0.0)
                        audio_seconds = len(pcm) / (PersistentComponent.RATE * 2)
                        drain = getattr(self.component, "DRAIN_SECONDS", 4.0)
                        cooldown = (max(0.0, getattr(self.component, "_motion_not_before", 0) - time.monotonic())
                                    if timeline else 0.0)
                        comment["external_send_seconds"] = cooldown + preroll + max(audio_seconds + drain, tail) + .5
                    if not self._begin_delivery(comment):
                        break
                    reading_payload = None
                    if number == 1 and reading_pcm is not None:
                        reading_payload = {"text": reading["text"], "pcm": reading_pcm}
                        if not semantic:
                            part["timeline"] = compile_reading_timeline(
                                reading, part["actions"], part["recovery"],
                                len(reading_pcm) / (PersistentComponent.RATE * 2))
                        comment["reading_status"] = "sending"
                    comment["status"] = "sending"
                    self.checkpoint()
                    sender = self.component.send_aligned_expression if semantic else self.component.send_expression
                    # Dynamic replies always choose A explicitly. Historical
                    # replays below retain their recorded comparison policy.
                    options = ({"alignment": part.get("alignment"), "action_kind": DIALOGUE_ACTION_KIND,
                                "ge_lead_seconds": DIALOGUE_GE_LEAD_SECONDS,
                                "allow_tail_q_after_audio": True, "local_action_anchors": True} if semantic else {})
                    if getattr(self, "semantic_replay", False):
                        options["action_kind"] = "S"
                        options["ge_lead_seconds"] = self.replay_fixture.ge_lead_seconds
                        options["allow_tail_q_after_audio"] = False
                        options["local_action_anchors"] = False
                        options.update(self.semantic_replay_limits())
                    if part.get("ssml") is not None:
                        options.update(ssml=part["ssml"], transcription_text=part["transcription_text"])
                    receipt = await sender(pcm, part["text"], part["actions"], number,
                                           recovery=part["recovery"], reading=reading_payload, **options)
                    comment.setdefault("motion_receipts", []).append(receipt)
                    save(segment_out / "component-send.json", receipt)
                    if reading_payload is not None:
                        comment["reading_status"] = "sent"
                        comment["reading_segments_sent"] = 1
                        save(reading_out / "component-send.json", {
                            "group_id": receipt["group_id"], "reading_text": reading["text"],
                            "reading_audio_seconds": comment["reading_audio_seconds"],
                            "reading_sent_complete": receipt.get("reading_sent_complete", False)})
                    comment["segments_sent"] += 1
                else:
                    comment["status"] = "sent"
                # Do not retain another copy of a provider credential after this reply.
            except asyncio.CancelledError:
                if comment is not None:
                    self._worker_interrupted = True
                    comment.update(status="cancelled", cancel_reason="answer_worker_cancelled",
                                   error="发送工作循环被取消；本条不会自动重试")
                    partial = getattr(self.component, "last_motion_receipt", {})
                    if (partial.get("comment_id") == comment["id"]
                            and not any(r.get("group_id") == partial.get("group_id")
                                        and r.get("segment_index") == partial.get("segment_index")
                                        for r in comment.get("motion_receipts", []))):
                        comment.setdefault("motion_receipts", []).append(copy.deepcopy(partial))
                raise
            except Exception as exc:
                if comment is not None:
                    cancelled = self.cancel_before_delivery(comment)
                    comment["status"] = "cancelled" if cancelled else "error"
                    comment["error"] = ("结束请求打断了本轮处理；已发出部分以录像为准" if self.stopping
                                        else "本轮处理未完成（" + type(exc).__name__ + "）")
                    if isinstance(exc, SemanticSpeechError):
                        comment["delivery_error"] = {"code": exc.code, **exc.details}
                        message = {"alignment_unavailable": "本次语音时间戳无法可靠定位动作",
                                   "anchor_window_conflict": "动作锚点间隔或结尾时长不足，无法容纳完整动作窗口"}.get(exc.code, "语义动作计划无法执行")
                        if exc.code == "alignment_unavailable":
                            message += "：" + alignment_reason_message(exc.details.get("reason"))
                        comment["error"] = message + "（" + exc.code + "）；本条不会自动重试"
                    if isinstance(exc, ModelError) and not cancelled:
                        comment["model_error_code"] = exc.code
                        message = {"timed_action_input_invalid": "动作编排输入校验未通过",
                                   "timed_action_plan_invalid": "时间戳动作计划校验未通过",
                                   "timed_action_planner_unavailable": "时间戳动作编排尚未连接"}.get(
                                       exc.code, "时间戳动作编排失败")
                        comment["error"] = f"{message}（{exc.code}）；本条不会自动重试"
                    partial = getattr(self.component, "last_motion_receipt", {})
                    if (getattr(self, "semantic_speech", False) and partial.get("comment_id") == comment["id"]
                            and partial.get("mode") == "semantic_speech"
                            and partial.get("segment_index") == number):
                        # A stopped round still needs an auditable receipt for the
                        # frames already written; it is not a successful delivery.
                        receipt = copy.deepcopy(partial)
                        comment.setdefault("motion_receipts", []).append(receipt)
                        partial_out = self.out / "tts" / f'{comment["id"]}-{partial["segment_index"]}'
                        if partial_out.is_dir():
                            save(partial_out / "component-send.json", receipt)
                    self.event("answer_error", comment_id=comment["id"], error_type=type(exc).__name__,
                               delivery_error=comment.get("delivery_error"))
            finally:
                key = ""
                final_error = None
                try:
                    if comment is not None and comment.get("input_kind") == "external_game":
                        comment["external_worker_pending"] = False
                    if comment is not None and getattr(self.component, "transport_unknown", None):
                        comment["delivery_outcome"] = "unknown"
                    if (comment is not None and comment.get("status") in {"error", "cancelled"}
                            and comment.get("reading_status") not in {"sent", "skipped"}):
                        comment["reading_status"] = comment["status"]
                    owned_delivery = (comment is not None and comment.get("delivery_started")
                                      and self.component.comment_id == comment["id"])
                    self.component.comment_id = None
                    self.component.job_id = None
                    if owned_delivery:
                        self.discard_prepared(comment)
                        hook = getattr(self, "on_delivery_finished", None)
                        if callable(hook):
                            hook(comment)
                    elif comment is not None and comment.get("status") in {"error", "cancelled", "expired"}:
                        self.discard_prepared(comment)
                    self.checkpoint()
                    if comment is not None:
                        self.event("answer_local_receipt", comment_id=comment["id"],
                                   job_id=comment.get("external_job_id"), status=comment["status"],
                                   segments_sent=comment.get("segments_sent", 0),
                                   delivery_outcome=comment.get("delivery_outcome", ""))
                except Exception as error:
                    final_error = error
                finally:
                    # Queue accounting must survive every hook/archive failure.
                    self.work.task_done()
                if final_error is not None:
                    raise final_error

    def assert_previous_cloud_ended(self):
        # 保留旧场与本轮结束凭据；不能删除标记来绕过不确定的收费状态。
        roots = [HERE / "data", ARTIFACTS/"zhuyuanzhang-terra-read-react-20261029", *(ARTIFACTS/name for name in PREVIOUS_COMPONENT_DIRS)]
        markers = [marker for root in roots for marker in (root/"runs").glob("*/start-attempted.json")]
        for marker in markers:
            if marker.parent == self.out:
                continue
            receipt = marker.parent / "component-receipt.json"
            safe = json.loads(receipt.read_text()) if receipt.exists() else {}
            if (safe.get("creation_outcome_unknown")
                    or not (safe.get("cloud_end_confirmed") is True or safe.get("no_cloud_session") is True)):
                raise web.HTTPConflict(text="上一场云端结束尚未确认，禁止创建下一场")

    async def verify_previous_cloud_ended(self, key):
        roots = [HERE / "data", ARTIFACTS/"zhuyuanzhang-terra-read-react-20261029", *(ARTIFACTS/name for name in PREVIOUS_COMPONENT_DIRS)]
        verified, seen = [], set()
        for root in roots:
            for path in (root/"runs").glob("*/component-receipt.json"):
                receipt = json.loads(path.read_text())
                if receipt.get("creation_outcome_unknown"):
                    raise web.HTTPConflict(text="旧场创建结果不明，禁止再创建")
                live_id = str(receipt.get("live_id") or "")
                if not live_id or live_id in seen:
                    continue
                if not live_id.isdigit():
                    raise web.HTTPConflict(text="旧场标识不符合预期")
                if receipt.get("cloud_end_confirmed") is True:
                    # ended 是已回读并持久化的终态；不让历史查询故障阻塞未来开场。
                    verified.append({"live_id": live_id, "status": "ended", "source": "saved_confirmation"})
                    seen.add(live_id)
                    continue
                async with self.http.get(PersistentComponent.BASE+f"/live/v1/lives/{live_id}",
                                         headers={"Authorization": "Token "+key},
                                         timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status != 200:
                        raise web.HTTPConflict(text="旧场结束状态回读未完成")
                    live = (await response.json()).get("live") or {}
                    if live.get("status") != "ended":
                        raise web.HTTPConflict(text="旧场尚未确认结束")
                    verified.append({"live_id": live_id, "status": "ended", "source": "cloud_readback"})
                    seen.add(live_id)
        save(self.out/"preflight-previous-ended.json", verified)

