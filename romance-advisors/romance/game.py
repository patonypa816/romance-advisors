"""Single-process game controller. Network calls never hold SQLite transactions."""
from __future__ import annotations

import copy
import asyncio
from datetime import datetime, timezone
import math
import random
import re
import time
import uuid

from .storage import Storage
from .expression import REACTION_TTS_STYLE, build_plan
from .diagnostics import PerformanceMetrics
from .model import ModelInputTooLarge
from .viewer_avatar import safe_avatar_url
from .appearance import appearance_snapshot


FINALE_DURATION_MS = 6800


# The overlay polls every 0.9s. Give a fast reply time to show its typing
# bubble across polls while allowing a slow model response to use that time.
WOMAN_TYPING_SECONDS = 2.4


class StaleResult(Exception):
    pass


def ident():
    return uuid.uuid4().hex


def clean_text(value, field, limit=12000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"invalid_{field}")
    return value.strip()


def vote_number(text: str) -> int | None:
    """Read a standalone ballot without altering the original comment receipt."""
    match = re.fullmatch(r"(?:投[ \t]*)?([1-4])", text.strip())
    return int(match[1]) if match else None


class Game:
    def __init__(self, db_path, model, *, input_char_budget=24000, performer=None):
        self.storage = Storage(db_path)
        self.model = model
        self.data = self.storage.load()
        self.run_id = ident()
        self.metrics = PerformanceMetrics()
        self._versions = {"proposals": 0, "votes": 0}
        self._input_revisions = {"proposals": {}, "votes": {}}
        self._reindex()
        self.busy = False
        self._token = 0
        self._woman_typing_token = None
        self.rng = random.Random()
        self._validation_error = None
        self._prepare_candidate = None
        self.input_char_budget = input_char_budget
        self.performer = performer

    def _get(self, table, key):
        return self._by_id[table].get(key)

    def _reindex(self):
        """Rebuild after full restore/save; receipt commits update these in place."""
        self._counts_cache = None
        self._by_id = {table: {row["id"]: row for row in self.data[table]} for table in Storage.TABLES}
        self._round_inputs = {"proposals": {}, "votes": {}}
        self._session_comments = {}
        self._proposal_comments = {}
        for table in self._round_inputs:
            for row in self.data[table]:
                self._round_inputs[table].setdefault(row["round_id"], []).append(row)
        for row in self.data["comments"]:
            self._session_comments.setdefault(row["session_id"], []).append(row)
            if row.get("proposal_id"):
                self._proposal_comments[row["proposal_id"]] = row

    def _changed(self, changes):
        for table in self._versions:
            if changes.get(table):
                self._versions[table] += 1
                for key in changes[table]:
                    self._input_revisions[table][key] = self._versions[table]

    def _metric_ids(self):
        active_run = getattr(self.performer, "run_id", None) if getattr(self.performer, "active", False) else None
        return dict(run_id=active_run or self.run_id, session_id=self.data["session_id"],
                    episode_id=self.data["episode_id"], round_id=self.data["round_id"])

    def _metric(self, stage, **kwargs):
        self.metrics.record(stage, **self._metric_ids(), **kwargs)

    @property
    def episode(self):
        return self._get("episodes", self.data["episode_id"])

    @property
    def current_round(self):
        return self._get("rounds", self.data["round_id"])

    def _save(self, pending=None):
        try:
            changes = self.storage.save(self.data if pending is None else pending)
        except Exception:
            if pending is None:
                self.data = self.storage.load()
                self._reindex()
            raise
        if pending is not None:
            self.data.update(pending)
        self._changed(changes)
        self._reindex()

    def _save_inputs(self, changes):
        # Do not publish even the receipt until all effective inputs are durable.
        meta = {key: value for key, value in self.data.items() if key not in Storage.TABLES}
        committed = self.storage.save_changes(changes, meta=meta)
        for table, records in changes.items():
            for record in records:
                existing = self._get(table, record["id"])
                if existing is not None:
                    existing.update(record)
                else:
                    self.data[table].append(record)
                    self._by_id[table][record["id"]] = record
                    if table in self._round_inputs:
                        self._round_inputs[table].setdefault(record["round_id"], []).append(record)
                    elif table == "comments":
                        self._session_comments.setdefault(record["session_id"], []).append(record)
                        if record.get("proposal_id"):
                            self._proposal_comments[record["proposal_id"]] = record
        self._changed(committed)

    def _idle(self):
        if self.busy:
            raise ValueError("operation_in_progress")

    def _phase(self, phase):
        if not self.current_round or self.current_round["phase"] != phase:
            raise ValueError(f"requires_{phase.lower()}")

    async def _call(self, task, payload, token):
        if self._validation_error:
            payload = dict(payload, validation_error=self._validation_error)
        started = time.monotonic()
        # Keep the original request scope even if an abort changes the live round
        # before this request returns. Its timing is still about the old request.
        metric_ids = self._metric_ids()
        self.metrics.record(f"model_{task}_start", **metric_ids)
        try:
            result = await self.model.generate(task, copy.deepcopy(payload))
        except asyncio.CancelledError:
            self.metrics.record(f"model_{task}_end", started=started, outcome="cancelled", **metric_ids)
            raise
        except Exception as exc:
            self.metrics.record(f"model_{task}_end", started=started, outcome="error", **metric_ids,
                                code="model_input_too_large" if isinstance(exc, ModelInputTooLarge) else "model_error")
            raise
        self.metrics.record(f"model_{task}_end", started=started, outcome="ok", **metric_ids)
        if token != self._token:
            raise StaleResult()
        if not isinstance(result, dict):
            raise ValueError("invalid_model_result")
        return result

    async def _run(self, function):
        self._idle()
        self._token += 1
        token = self._token
        self.busy = True
        self._validation_error = (self.data.get("error") or {}).get("code")
        if self._validation_error and not self._validation_error.startswith("invalid_"):
            self._validation_error = None
        self.data["error"] = None
        try:
            await function(token)
        except StaleResult:
            pass
        except Exception as exc:
            if token == self._token:
                stage = self.current_round["phase"] if self.current_round else "PREPARING"
                # Provider exception strings may contain credentials or raw text.
                code = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(("invalid_", "requires_")) else "model_or_storage_error"
                if isinstance(exc, ModelInputTooLarge):
                    self.data["error"] = {"stage": stage, "code": "model_input_too_large", "retryable": False,
                                          "message": "本步骤的完整模型请求超过本地输入上限，原样重试无法恢复；请结束本期或调整输入方案后再开始。",
                                          "metadata": exc.safe_metadata}
                else:
                    self.data["error"] = {"stage": stage, "code": code, "message": "生成或保存失败，可重试当前步骤。"}
                self._save()
        finally:
            if token == self._token:
                self.busy = False
        return self.public_state()

    def abort_episode(self):
        self._token += 1  # Results from an already-running request lose write authority.
        self._prepare_candidate = None
        self.busy = False
        changed_proposals = set()
        if self.episode and self.episode["status"] in ("ACTIVE", "PREPARING"):
            self.episode["status"] = "TECH_ENDED"
        if self.current_round and self.current_round["phase"] not in ("DONE", "ABORTED"):
            round_ = self.current_round
            selection_pending = round_["phase"] in ("COLLECTING", "SELECTING")
            round_["phase"] = "ABORTED"
            proposals = self._round_inputs["proposals"].get(round_["id"], [])
            selection = {item["proposal_id"]: item for item in round_.get("proposal_selection", [])}
            if selection_pending:
                for proposal in proposals:
                    # An abort invalidates an in-flight result, but never erases
                    # a selection that was already committed before the abort.
                    selection.setdefault(proposal["id"], {"proposal_id": proposal["id"],
                        "status": "unrecorded", "reason": "本轮已终止，未完成编选。", "reason_source": "system"})
            if proposals:
                if selection:
                    round_["proposal_selection"] = [selection[p["id"]] for p in proposals if p["id"] in selection]
                changed_proposals = {p["id"] for p in proposals}
        self.data["error"] = None
        self._save()
        # Publish the phase-dependent projection only after its durable commit.
        self._changed({"proposals": changed_proposals})
        return self.public_state()

    async def new_session(self):
        self.abort_episode()
        session = {"id": ident()}
        self.data["sessions"].append(session)
        self.data.update(session_id=session["id"], episode_id=None, round_id=None, error=None)
        self._save()
        return self.public_state()

    def bind_live_broadcast(self, room_id, live_id):
        """Bind a collector-confirmed broadcast without changing the story.

        The public room number is stable; live_id must be the platform's
        per-broadcast identifier. Neither page loads nor game sessions define
        a broadcast. Keep the registry in existing transactional JSON metadata.
        """
        room_id = clean_text(room_id, "broadcast_room", 128)
        live_id = clean_text(live_id, "broadcast_live_id", 256)
        if not room_id.isascii() or not room_id.isdigit():
            raise ValueError("invalid_broadcast_room")
        broadcasts = self.data.get("live_broadcasts", [])
        broadcast = next((item for item in broadcasts
                          if item["room_id"] == room_id and item["live_id"] == live_id), None)
        if broadcast is None:
            broadcast = {"id": ident(), "platform": "douyin", "room_id": room_id,
                         "live_id": live_id, "bound_at": datetime.now(timezone.utc).isoformat()}
        if self.data.get("live_broadcast_id") == broadcast["id"]:
            return copy.deepcopy(broadcast)
        pending = dict(self.data, live_broadcast_id=broadcast["id"],
                       live_broadcasts=broadcasts if broadcast in broadcasts else [*broadcasts, broadcast])
        self._save(pending)
        return copy.deepcopy(broadcast)

    def _comment_broadcast(self, source, context):
        """Only a trusted adapter keyword can attach the current broadcast."""
        if source != "dyhub" or not isinstance(context, dict):
            return None
        active = self.data.get("live_broadcast_id")
        return next((item["id"] for item in self.data.get("live_broadcasts", [])
                     if item["id"] == active and item["room_id"] == context.get("room_id")
                     and item["live_id"] == context.get("live_id")), None)

    async def new_episode(self):
        self._idle()
        if not self.data["session_id"]:
            await self.new_session()
        self.abort_episode()
        episode = {"id": ident(), "session_id": self.data["session_id"], "status": "PREPARING", "score": 50}
        self.data["episodes"].append(episode)
        self.data.update(episode_id=episode["id"], round_id=None)
        self._save()
        return await self._run(self._prepare)

    async def _prepare(self, token):
        candidate = self._prepare_candidate if self._validation_error == "invalid_private_preferences" else None
        payload = {"previous_titles": [e.get("title", "") for e in self.data["episodes"][-8:]]}
        if candidate is not None:
            payload["private_preferences_context"] = candidate
        result = await self._call("prepare", payload, token)
        if candidate is not None:
            # The existing single correction fills one missing field. It must
            # not silently replace the characters/story that already validated.
            repaired_woman = result.get("woman")
            preferences = repaired_woman.get("private_preferences") if isinstance(repaired_woman, dict) else None
            result = copy.deepcopy(candidate)
            result["woman"]["private_preferences"] = preferences
        self._prepare_candidate = None
        for role in ("man", "woman"):
            character = result.get(role)
            if not isinstance(character, dict) or type(character.get("age")) is not int or not 18 <= character["age"] <= 120:
                raise ValueError("invalid_adult_character")
            for field in ("name", "personality", "background", "speech_style"):
                clean_text(character.get(field), field)
        hints = result.get("public_hints")
        if not isinstance(hints, list) or not hints or len(hints) > 12 or not all(isinstance(h, str) and h.strip() for h in hints):
            raise ValueError("invalid_public_hints")
        prepared = {key: result[key] for key in ("man", "woman", "public_hints")}
        prepared.update(title=clean_text(result.get("title"), "title"), shared_background=clean_text(result.get("shared_background"), "background"), opening=clean_text(result.get("opening"), "opening"))
        try:
            preferences = result["woman"].get("private_preferences")
            if isinstance(preferences, str):
                preferences = preferences.strip()
                if not preferences:
                    raise ValueError("invalid_private_preferences")
            elif not isinstance(preferences, (list, dict)) or not preferences:
                raise ValueError("invalid_private_preferences")
        except ValueError:
            # Hold only a candidate whose other required fields passed. Never
            # persist an invalid draft or publish its opening before correction.
            self._prepare_candidate = copy.deepcopy(prepared)
            self._prepare_candidate["woman"].pop("private_preferences", None)
            raise
        prepared["woman"]["private_preferences"] = preferences
        self.episode.update(prepared, status="ACTIVE")
        self._message("woman", prepared["opening"], None)
        # Opening and first collection round are committed together.
        self._start_round()

    def _start_round(self):
        if not self.episode or self.episode["status"] != "ACTIVE":
            raise ValueError("requires_active_episode")
        if self.current_round and self.current_round["phase"] not in ("DONE", "ABORTED"):
            raise ValueError("requires_finished_round")
        number = 1 + sum(r["episode_id"] == self.episode["id"] for r in self.data["rounds"])
        round_ = {"id": ident(), "episode_id": self.episode["id"], "number": number, "phase": "COLLECTING", "options": [], "decision": None, "selection_locked": False, "selected_option_id": None, "selection_source": None, "reaction": None, "delta": None}
        self.data["rounds"].append(round_)
        self.data.update(round_id=round_["id"], error=None)
        self._save()

    def start_round(self):
        self._idle()
        self._start_round()
        return self.public_state()

    def submit_proposal(self, author_id, nickname, text, *, avatar_url=None):
        self._idle()
        self._phase("COLLECTING")
        self._append_proposal(self.data, author_id, nickname, text, avatar_url=avatar_url)
        self._save()
        return self.public_state()

    def _append_proposal(self, data, author_id, nickname, text, *, avatar_url=None):
        author_id, nickname, content = clean_text(author_id, "author", 256), clean_text(nickname, "nickname", 256), clean_text(text, "proposal", 2000)
        # Eligibility is decided at collection close. A later greeting must not
        # erase an earlier suggestion; each received candidate keeps its source.
        proposal = {"id": ident(), "round_id": data["round_id"], "author_id": author_id, "nickname": nickname, "text": content,
                    "source": "local"}
        if avatar_url := safe_avatar_url(avatar_url):
            proposal["avatar_url"] = avatar_url
        data["proposals"].append(proposal)
        return proposal

    def vote(self, author_id, nickname, option_id):
        self._idle()
        self._phase("VOTING")
        self._upsert_vote(self.data, author_id, nickname, option_id)
        self._save()
        return self.public_state()

    def _upsert_vote(self, data, author_id, nickname, option_id):
        option = next((o for o in self.current_round["options"] if o["id"] == option_id or str(o["number"]) == str(option_id)), None)
        if not option:
            raise ValueError("invalid_option")
        author_id, nickname = clean_text(author_id, "author", 256), clean_text(nickname, "nickname", 256)
        vote_id = data["round_id"] + ":" + author_id
        vote = (self._get("votes", vote_id) if data is self.data else
                next((v for v in data["votes"] if v["id"] == vote_id), None))
        if vote:
            vote.update(option_id=option["id"], nickname=nickname)
        else:
            vote = {"id": vote_id, "round_id": data["round_id"], "author_id": author_id, "nickname": nickname, "option_id": option["id"]}
            data["votes"].append(vote)
        return vote

    def accept_comment(self, event, *, blocked_reason=None, source="local", live_broadcast=None):
        """Record a comment; source is supplied by the adapter, never the body."""
        if source not in {"local", "dyhub"}:
            raise ValueError("invalid_comment_source")
        if not isinstance(event, dict) or not {"expected_round_id", "expected_phase"} <= event.keys():
            raise ValueError("invalid_comment_context")
        payload = {"event_id": clean_text(event.get("event_id"), "comment_id", 256),
                   "author_id": clean_text(event.get("author_id"), "author", 256),
                   "nickname": clean_text(event.get("nickname"), "nickname", 256),
                   "text": clean_text(event.get("text"), "comment", 2000)}
        # Keep the received text for history and exact retry checks; only the
        # command parser and effective proposal normalize surrounding whitespace.
        payload["text"] = event["text"]
        avatar_url = safe_avatar_url(event.get("avatar_url"))
        if avatar_url:
            payload["avatar_url"] = avatar_url
        for key in ("expected_round_id", "expected_phase"):
            value = event[key]
            if value is not None:
                value = clean_text(value, "comment_context", 256)
            payload[key] = value
        existing = self._get("comments", payload["event_id"])
        if existing:
            if any(existing.get(key) != value for key, value in payload.items()):
                raise ValueError("invalid_comment_conflict")
            if existing.get("avatar_url") != avatar_url:
                raise ValueError("invalid_comment_conflict")
            if existing.get("source", source) != source:
                raise ValueError("invalid_comment_conflict")
            return copy.deepcopy(existing)

        r = self.current_round
        phase = r["phase"] if r else None
        number = vote_number(payload["text"])
        comment = dict(payload, id=payload["event_id"], source=source, received_at=datetime.now(timezone.utc).isoformat(),
                       session_id=self.data["session_id"], episode_id=self.data["episode_id"],
                       round_id=self.data["round_id"], round_number=r["number"] if r else None,
                       phase=phase, kind="vote" if number is not None else "chat", result="ignored",
                       reason="当前未开放征集或投票，仅记录评论。", proposal_id=None, vote_id=None, option_id=None)
        broadcast_id = self._comment_broadcast(source, live_broadcast)
        if broadcast_id:
            comment["live_broadcast_id"] = broadcast_id
        # Copy only this author's effective input, never the whole archive. The
        # active round retains its identity while a model request is awaiting.
        pending = {"round_id": self.data["round_id"], "proposals": [], "votes": []}
        if payload["expected_round_id"] != self.data["round_id"] or payload["expected_phase"] != phase:
            comment.update(result="stale", reason="发送时的回合或阶段已结束，未计入当前窗口。")
        elif blocked_reason:
            comment["reason"] = blocked_reason
        elif self.busy:
            comment["reason"] = "正在生成，本条评论仅记录，不参与建议或投票。"
        elif self.episode and self.episode["status"] == "ACTIVE" and phase == "COLLECTING":
            if number is not None:
                comment["reason"] = "当前为建议征集阶段，投票尚未开放。"
            else:
                proposal = self._append_proposal(pending, payload["author_id"], payload["nickname"], payload["text"], avatar_url=avatar_url)
                proposal["source"] = source
                if broadcast_id:
                    proposal["live_broadcast_id"] = broadcast_id
                comment.update(kind="proposal", result="candidate", reason="已收到，保留为本轮待编选评论；不会覆盖该参与者之前的建议。", proposal_id=proposal["id"])
        elif self.episode and self.episode["status"] == "ACTIVE" and phase == "VOTING":
            if number is not None:
                option = next((o for o in r["options"] if o["number"] == number), None)
                if option:
                    existing_vote = self._get("votes", self.data["round_id"] + ":" + payload["author_id"])
                    if existing_vote is not None:
                        pending["votes"].append(dict(existing_vote))
                    vote = self._upsert_vote(pending, payload["author_id"], payload["nickname"], option["id"])
                    comment.update(result="voted", reason="投票已记录；截止前最后一张有效票为准。", vote_id=vote["id"], option_id=option["id"])
                else:
                    comment["reason"] = "该编号没有对应选项，未改动已有有效票。"
            else:
                comment["reason"] = "请单发当前选项编号，例如“1”“投1”或“投 1”；本条仅记录。"
        # The receipt and its effective proposal/vote commit in one transaction.
        # A failed write leaves the live state (including an in-flight round) alone.
        self._save_inputs({"comments": [comment], "proposals": pending["proposals"], "votes": pending["votes"]})
        return copy.deepcopy(comment)

    def _messages(self):
        return [m for m in self.data["messages"] if m["episode_id"] == self.episode["id"]]

    def _man_payload(self):
        # Shared identity is explicit; her preferences and full private profile stay out.
        partner = {key: self.episode["woman"][key] for key in ("name", "age")}
        return {"man": self.episode["man"], "partner": partner, "shared_background": self.episode["shared_background"], "messages": [{"role": m["role"], "text": m["text"]} for m in self._messages()]}

    async def close_collection(self):
        self._idle()
        if self.current_round and self.current_round["phase"] == "VOTING":
            return self.public_state()
        self._phase("COLLECTING")
        self.current_round["phase"] = "SELECTING"
        self._save()
        self._metric("collection_closed", outcome="committed")
        return await self._run(self._advance)

    async def close_voting(self):
        self._idle()
        if self.current_round and self.current_round["phase"] == "DONE":
            return self.public_state()
        self._phase("VOTING")
        self.current_round["vote_snapshot"] = self._counts()
        self.current_round["phase"] = "DECIDING"
        self._save()
        self._metric("votes_frozen", outcome="committed")
        return await self._run(self._advance)

    def _counts(self):
        key = (self.data["round_id"], self._versions["votes"], tuple(o["id"] for o in self.current_round["options"]))
        if self._counts_cache is not None and self._counts_cache[0] == key:
            return dict(self._counts_cache[1])
        counts = {o["id"]: 0 for o in self.current_round["options"]}
        for vote in self._round_inputs["votes"].get(self.data["round_id"], []):
            if vote["option_id"] in counts:
                counts[vote["option_id"]] += 1
        self._counts_cache = (key, counts)
        return dict(counts)

    async def _advance(self, token):
        r = self.current_round
        if r["phase"] == "SELECTING":
            proposals = self._round_inputs["proposals"].get(r["id"], [])
            # Text-budget overload is explicit; there is no fixed candidate-count pool.
            included, budget, seen_text = [], self.input_char_budget, {}
            selection, budget_exhausted = {}, False
            for proposal in proposals:
                if proposal["text"] in seen_text:
                    selection[proposal["id"]] = {"proposal_id": proposal["id"], "status": "duplicate",
                        "reason": "与同文字候选合并，未单独送模型。", "reason_source": "system",
                        "duplicate_of": seen_text[proposal["text"]]}
                    continue
                if budget_exhausted or len(proposal["text"]) > budget:
                    # Preserve the existing first-over-budget cutoff, while giving
                    # every remaining candidate a truthful delivery receipt.
                    budget_exhausted = True
                    selection[proposal["id"]] = {"proposal_id": proposal["id"], "status": "not_submitted",
                        "reason": "本轮候选文字预算已到截止位置，未送模型。", "reason_source": "system"}
                    continue
                included.append(proposal)
                seen_text[proposal["text"]] = proposal["id"]
                budget -= len(proposal["text"])
            payload = self._man_payload()
            payload["proposals"] = [{k: p[k] for k in ("id", "author_id", "nickname", "text")} for p in included]
            # Only a genuinely empty candidate set can skip the selector. A
            # nonempty set filtered out by the existing budget still calls it.
            output = await self._call("select", payload, token) if proposals else {"options": []}
            items = output.get("options")
            if not isinstance(items, list) or len(items) > 4:
                raise ValueError("invalid_options")
            options, used, selected_authors, author_limited = [], set(), set(), set()
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("invalid_option")
                p = next((p for p in included if p["id"] == item.get("proposal_id")), None)
                if not p or p["id"] in used or type(item.get("conflict")) is not bool:
                    raise ValueError("invalid_option_source")
                used.add(p["id"])
                summary = clean_text(item.get("summary"), "summary", 2000)
                conflict_reason = clean_text(item.get("reason"), "conflict_reason")
                # Apply the per-person option limit only after the selector has
                # identified suggestions. Keep its first choice and original ID.
                if p["author_id"] in selected_authors:
                    author_limited.add(p["id"])
                    continue
                selected_authors.add(p["author_id"])
                options.append({"id": ident(), "number": len(options) + 1, "text": summary, "proposal_id": p["id"], "author": {"id": p["author_id"], "nickname": p["nickname"]}, "conflict": item["conflict"], "reason": conflict_reason})
                if avatar_url := safe_avatar_url(p.get("avatar_url")):
                    options[-1]["author"]["avatar_url"] = avatar_url
            # Reasons are observability metadata, not permission to advance the
            # game. Missing/malformed legacy output must not strand the show.
            reasons, assessment_ids, repeated = {}, set(), set()
            assessments = output.get("assessments")
            included_ids = {p["id"] for p in included}
            if isinstance(assessments, list):
                for item in assessments:
                    if not isinstance(item, dict):
                        continue
                    proposal_id, reason = item.get("proposal_id"), item.get("reason")
                    if not isinstance(proposal_id, str) or proposal_id not in included_ids:
                        continue
                    if proposal_id in assessment_ids:
                        repeated.add(proposal_id)
                    assessment_ids.add(proposal_id)
                    if isinstance(reason, str) and reason.strip() and len(reason) <= 240:
                        reasons[proposal_id] = reason.strip()
            option_by_proposal = {o["proposal_id"]: o for o in options}
            # Suggestion recognition is separate from the four display slots.
            # Accept only submitted source IDs; never let model metadata forge
            # an author, change an option, award points or admit a late comment.
            recognized = set(option_by_proposal)
            suggestion_ids = output.get("suggestion_proposal_ids")
            classification = "options_fallback"
            if isinstance(suggestion_ids, list) and len(suggestion_ids) <= len(included):
                recognized.update(key for key in suggestion_ids if isinstance(key, str) and key in included_ids)
                classification = "model"
            # Text-deduplicated suggestions retain each original author's chance.
            recognized.update(p["id"] for p in proposals
                              if selection.get(p["id"], {}).get("duplicate_of") in recognized)
            for proposal in included:
                proposal_id = proposal["id"]
                option = option_by_proposal.get(proposal_id)
                reason = reasons.get(proposal_id) if proposal_id not in repeated else None
                reason_source = "model" if reason else "system" if option else "unrecorded"
                if proposal_id in author_limited:
                    reason = "同一参与者本轮最多一个投票选项，已保留其另一条建议。"
                    reason_source = "system"
                selection[proposal_id] = {"proposal_id": proposal_id,
                    "status": "selected" if option else "not_selected",
                    "reason": reason or ("已入选投票选项。" if option else "模型未返回未入选原因。"),
                    "reason_source": reason_source}
                if option:
                    selection[proposal_id]["option_id"] = option["id"]
            r.update(options=options, proposal_selection=[selection[p["id"]] for p in proposals],
                     suggestion_proposal_ids=[p["id"] for p in proposals if p["id"] in recognized],
                     suggestion_classification=classification,
                     input_count=len(proposals), model_input_count=len(included), phase="VOTING" if options else "DECIDING")
            if not options:
                r["vote_snapshot"] = {}
            self._save()
            # The stored proposals themselves did not change, but their operator
            # projection did. Re-emit them to incremental clients after commit.
            self._changed({"proposals": {p["id"] for p in proposals}})
        if r["phase"] == "DECIDING":
            self._decide()
        if r["phase"] == "DRAFTING":
            await self._draft(token)
        if r["phase"] == "WAITING_REPLY":
            await self._perform_reaction(token)
            await self._reply(token)

    def _decide(self):
        r = self.current_round
        if r["decision"] is None:
            counts = r["vote_snapshot"]
            maximum = max(counts.values(), default=0)
            top = self.rng.choice([o for o in r["options"] if counts[o["id"]] == maximum]) if maximum else None
            dice = self.rng.random() if top and top["conflict"] else None
            branch = "no_options" if not r["options"] else "no_votes" if not top else "alternative" if dice is not None and dice < .25 else "highest"
            r["decision"] = {"top_id": top["id"] if top else None, "dice": dice, "branch": branch}
            if branch == "highest":
                r.update(selected_option_id=top["id"], selection_source="audience", selection_locked=True)
            elif branch == "no_options":
                r.update(selection_source="self", selection_locked=True)
        r["phase"] = "DRAFTING"
        self._save()

    async def _draft(self, token):
        r = self.current_round
        branch = r["decision"]["branch"]
        allowed = [o["id"] for o in r["options"] if branch != "alternative" or o["id"] != r["decision"]["top_id"]]
        if r["selection_locked"]:
            allowed = [r["selected_option_id"]] if r["selected_option_id"] else []
        payload = self._man_payload()
        # The original source remains available if the short summary loses a qualifier.
        payload.update(options=[{"id": o["id"], "proposal_id": o["proposal_id"], "original_text": self._get("proposals", o["proposal_id"])["text"], "summary": o["text"], "author": {key: o["author"][key] for key in ("id", "nickname")}, "votes": r["vote_snapshot"].get(o["id"], 0)} for o in r["options"]], allowed_option_ids=allowed, allow_self=r["selection_source"] == "self" if r["selection_locked"] else True, selected_option_id=r["selected_option_id"], selection_locked=r["selection_locked"], selection_context=branch)
        if self.performer and self.performer.enabled:
            context_reader = getattr(self.performer, "expression_context", None)
            if context_reader is not None:
                # The audience has heard the man's asides too. Carry his mood
                # into this draft, but never treat an unsent next candidate as
                # history or pass delivery context into the woman's reply.
                context = context_reader()
                for key in ("recent_performances", "current_performance"):
                    if key in context:
                        payload[key] = copy.deepcopy(context[key])
        output = await self._call("draft", payload, token)
        selected = output.get("selected_option_id")
        if selected is not None and (not isinstance(selected, str) or selected not in allowed):
            raise ValueError("invalid_selected_source")
        if selected is None and not payload["allow_self"]:
            raise ValueError("invalid_self_selection")
        if "selected_option_id" not in output:
            raise ValueError("invalid_selected_source")
        # A valid source survives even when the accompanying prose needs repair.
        r.update(selected_option_id=selected, selection_source="audience" if selected else "self", selection_locked=True)
        self._save()
        try:
            message = clean_text(output.get("message"), "man_message")
            reaction = clean_text(output.get("reaction"), "reaction", 120)
            # Generate words, voice and visible expression together, then freeze.
            # The common voice style leaves each episode's character and mood intact.
            instruction = REACTION_TTS_STYLE + clean_text(output.get("reaction_instruction"), "reaction_instruction", 100)
            plan = build_plan(reaction, instruction, output.get("reaction_expression"), ssml=output.get("ssml"))
        except ValueError as error:
            # Keep only known field presence/types, never raw model content or
            # arbitrary returned keys. This distinguishes omitted prose from
            # malformed expression while keeping diagnostics operator-only.
            r["draft_failure"] = {
                "code": str(error),
                "fields": {field: {"present": field in output,
                                   "type": type(output[field]).__name__ if field in output else None}
                           for field in ("selected_option_id", "reaction", "reaction_instruction",
                                         "reaction_expression", "ssml", "message")},
            }
            raise
        r.pop("draft_failure", None)
        if self.performer and self.performer.enabled and r.get("performance") is None:
            # Bind the frozen reaction before committing its matching message.
            # Retries keep this request identity rather than creating a new show.
            r["performance"] = self._prepare_performance(r["id"], reaction, instruction, plan)
        r["reaction"] = reaction
        r["reaction_instruction"] = instruction
        r["reaction_plan"] = plan
        self._message("man", message, r["id"])
        r["phase"] = "WAITING_REPLY"
        self._save()
        if r.get("performance") is None:
            self._metric("man_public", outcome="committed")

    def _prepare_performance(self, round_id, reaction, instruction=None, plan=None):
        record = self.performer.prepare(round_id, reaction)
        if (not isinstance(record, dict) or record.get("status") != "pending"
                or record.get("man_released") is not False
                or not {"job_id", "owner_id", "generation"} <= record.keys()):
            raise ValueError("invalid_performance_record")
        record = copy.deepcopy(record)
        # Old stored rounds have no cue; omit it so the avatar retains the
        # original neutral instruction rather than rewriting an old performance.
        if instruction is not None:
            record["instruction"] = instruction
        if plan is not None:
            record["plan"] = copy.deepcopy(plan)
        return record

    async def _perform_reaction(self, token):
        r = self.current_round
        record = r.get("performance")
        if record is None:
            if not self.performer or not self.performer.enabled:
                return
            record = self._prepare_performance(r["id"], r["reaction"], r.get("reaction_instruction"), r.get("reaction_plan"))
            r["performance"] = record
            self._save()
        if record.get("status") == "sent" and record.get("man_released") is True:
            return
        # An existing pending show is never silently skipped when the operator
        # stops integration. The performer must reject a stopped/old owner.
        if self.performer is None:
            raise ValueError("invalid_performance_unavailable")
        def check_current_round():
            if token != self._token or self.current_round is not r or r["phase"] != "WAITING_REPLY":
                raise StaleResult()

        def on_message_action():
            check_current_round()
            # The line was saved at draft time. Reveal that same bubble while
            # the typing gesture is under way; the woman still waits for its tail.
            if not record["man_released"]:
                read_appearance = getattr(self.performer, "message_appearance", None)
                appearance = read_appearance() if read_appearance else None
                if appearance is not None:
                    # The draft is already immutable. Save the visible portrait on the
                    # round, in the same commit that first makes its message public.
                    man = next(m for m in self._messages() if m["role"] == "man" and m["round_id"] == r["id"])
                    r["man_appearance"] = {"message_id": man["id"], "portrait": copy.deepcopy(appearance)}
                record["man_released"] = True
                self._save()
                self._metric("man_public", outcome="committed", job_id=record["job_id"])

        has_message_motion = "message_motion_plan" in record
        if has_message_motion:
            receipt = await self.performer.perform(copy.deepcopy(record), r["reaction"],
                                                   on_message_action=on_message_action)
        else:
            # Pending rounds frozen by an older workbench have only a speech job.
            receipt = await self.performer.perform(copy.deepcopy(record), r["reaction"])
        check_current_round()
        if (not isinstance(receipt, dict) or receipt.get("status") != "sent"
                or receipt.get("send_complete") is not True
                or any(receipt.get(key, record[key]) != record[key]
                       for key in ("job_id", "owner_id", "generation"))):
            raise ValueError("invalid_performance_receipt")
        if has_message_motion:
            motion = receipt.get("message_motion_receipt")
            if (not record["man_released"] or not isinstance(motion, dict)
                    or motion.get("job_id") != record["job_id"] + ":typing"
                    or motion.get("generation") != record["generation"]
                    or motion.get("short_action_sent") is not True
                    or motion.get("status") != "sent" or motion.get("send_complete") is not True):
                raise ValueError("invalid_performance_receipt")
        r["performance"] = {**record, **receipt, "man_released": True, "playback_confirmed": False}
        self._save()
        if not has_message_motion:
            self._metric("man_public", outcome="committed", job_id=record["job_id"])

    def _message_portrait(self, message):
        if message["role"] == "man":
            round_ = self._get("rounds", message["round_id"])
            snapshot = (round_ or {}).get("man_appearance")
            if snapshot and snapshot["message_id"] == message["id"]:
                return {"portrait": copy.deepcopy(snapshot["portrait"])}
        return {}

    def _previous_visible_portrait(self, current):
        # A version can change twice while the same face returns. Compare the
        # two published messages, not the sequence of gifts between them.
        for old in reversed(self._messages()):
            if old["role"] == "man" and old["id"] != current["id"]:
                return self._message_portrait(old).get("portrait")
        return appearance_snapshot("normal")

    async def _reply(self, token):
        r = self.current_round
        man_message = next(m for m in self._messages() if m["round_id"] == r["id"] and m["role"] == "man")
        partner = {key: self.episode["man"][key] for key in ("name", "age")}
        payload = {"woman": self.episode["woman"], "partner": partner, "shared_background": self.episode["shared_background"], "messages": [{"role": m["role"], "text": m["text"]} for m in self._messages()], "score": self.episode["score"], "reply_to_message": {"id": man_message["id"], "text": man_message["text"]}}
        payload["reply_to_message"].update(self._message_portrait(man_message))
        portrait = payload["reply_to_message"].get("portrait")
        changed = False
        if portrait:
            previous = self._previous_visible_portrait(man_message)
            changed = bool(previous and previous["key"] != portrait["key"])
            payload["appearance_comparison"] = {
                "changed": changed, "previous_description": previous["description"] if previous else None,
            }
        self._woman_typing_token = token
        live_performer = self.performer if self.performer and self.performer.enabled else None
        reveal_at = time.monotonic() + WOMAN_TYPING_SECONDS
        output = await self._call("reply", payload, token)
        message = clean_text(output.get("message"), "woman_message")
        delta = output.get("delta")
        if type(delta) not in (int, float) or not math.isfinite(delta):
            raise ValueError("invalid_delta")
        before = self.episode["score"]
        after = max(0, min(100, before + delta))
        expected = "success" if after == 100 else "failure" if after == 0 else "continue"
        if output.get("ending") != expected:
            raise ValueError("invalid_ending_score_consistency")
        chat_delta = after - before
        impression = 0
        if portrait:
            proposed = output.get("impression_delta", 0)
            if type(proposed) is not int or not -2 <= proposed <= 2:
                raise ValueError("invalid_impression_delta")
            if changed and expected == "continue":
                # Only dialogue can conclude an episode. Allow a small opposite
                # impression too, including when the chat score is fractional.
                impression = max(1 - math.ceil(after), min(math.ceil(100 - after) - 1, proposed))
                after += impression
        remaining = reveal_at - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)
        # A stop or cast change can happen even after the model has returned.
        if (token != self._token or self.current_round is not r or r["phase"] != "WAITING_REPLY"
                or (live_performer is not None and not live_performer.enabled)):
            raise StaleResult()
        self._message("woman", message, r["id"], man_message["id"])
        r.update(before=before, proposed_delta=delta, delta=after - before, after=after, phase="DONE")
        if portrait:
            r.update(chat_delta=chat_delta, impression_delta=impression, proposed_impression=proposed)
        self.episode.update(score=after, status="SUCCEEDED" if after == 100 else "FAILED" if after == 0 else "ACTIVE")
        if expected != "continue":
            # The final reply, score, clock and awards are a single durable
            # transition. Read/poll/restart must never begin a second finale.
            awards = self._episode_awards()
            worst = awards["worst"] or {}
            # Reuse a random winner who also qualifies for the audience view.
            # With only real suggestions both screens must show the same winner.
            drawn_author = worst.get("author_id") if worst.get("selection_reason") == "random_suggestion" else None
            self.episode["finale"] = {"episode_id": self.episode["id"],
                "status": self.episode["status"], "started_at": time.time(),
                "duration_ms": FINALE_DURATION_MS, "awards": awards,
                "live_awards": self._episode_awards(live_only=True, drawn_author=drawn_author)}
        self._save()
        self._metric("woman_committed", outcome="committed")

    def _message(self, role, content, round_id, parent_id=None):
        self.data["messages"].append({"id": ident(), "episode_id": self.episode["id"], "round_id": round_id, "role": role, "text": content, "parent_id": parent_id})

    async def retry(self):
        self._idle()
        if (self.data.get("error") or {}).get("code") == "model_input_too_large":
            return self.public_state()
        if self.episode and self.episode["status"] == "PREPARING":
            return await self._run(self._prepare)
        if self.current_round and self.current_round["phase"] in ("SELECTING", "DECIDING", "DRAFTING", "WAITING_REPLY"):
            return await self._run(self._advance)
        return self.public_state()

    def _live_proposal(self, proposal):
        """Old DyHub receipts prove origin without a migration or nickname rules."""
        if not proposal:
            return False
        if "source" in proposal:
            return proposal["source"] == "dyhub"
        receipt = self._proposal_comments.get(proposal["id"], {})
        if "source" in receipt:
            return receipt["source"] == "dyhub" and receipt.get("author_id") == proposal["author_id"]
        # Before explicit provenance existed only the collector generated both
        # room-scoped SHA-256 namespaces. Require the matching durable receipt.
        return (receipt.get("author_id") == proposal["author_id"]
                and receipt.get("round_id") == proposal["round_id"]
                and bool(re.fullmatch(r"dyhub:viewer:[0-9a-f]{64}", proposal["author_id"]))
                and bool(re.fullmatch(r"dyhub:message:[0-9a-f]{64}", receipt.get("id", ""))))

    def _live_option(self, option):
        proposal = self._get("proposals", option.get("proposal_id"))
        return bool(self._live_proposal(proposal) and proposal["author_id"] == option["author"]["id"])

    def _episode_awards(self, *, live_only=False, drawn_author=None):
        """One episode only; called inside final settlement, never on reads.

        Session leaderboards retain their original rates and ordering. Random
        entertainment awards never become negative outcomes or leaderboard data.
        """
        authors, suggestions = {}, {}

        def row(author_id, nickname, avatar_url=None):
            return {"author_id": author_id, "nickname": nickname,
                    "avatar_url": safe_avatar_url(avatar_url), "adopted": 0,
                    "positive": 0, "negative": 0, "score_delta": 0,
                    "positive_delta": 0, "negative_delta": 0, "rate": 0}

        for r in self.data["rounds"]:
            if r["episode_id"] != self.episode["id"] or r["phase"] != "DONE":
                continue
            recognized = set(r.get("suggestion_proposal_ids", []))
            # Old records cannot prove unselected comments were suggestions.
            recognized.update(o["proposal_id"] for o in r["options"] if "proposal_id" in o)
            recognized.update(item["proposal_id"] for item in r.get("proposal_selection", [])
                              if item.get("duplicate_of") in recognized)
            for p in self._round_inputs["proposals"].get(r["id"], []):
                if p["id"] in recognized and (not live_only or self._live_proposal(p)):
                    suggestions[p["author_id"]] = row(p["author_id"], p["nickname"],
                        safe_avatar_url(p.get("avatar_url")) or suggestions.get(p["author_id"], {}).get("avatar_url"))
            if r["selection_source"] != "audience":
                continue
            selected = next(o for o in r["options"] if o["id"] == r["selected_option_id"])
            if live_only and not self._live_option(selected):
                continue
            author = selected["author"]
            stats = authors.setdefault(author["id"], row(author["id"], author["nickname"], author.get("avatar_url")))
            stats.update(nickname=author["nickname"], avatar_url=safe_avatar_url(author.get("avatar_url"))
                         or suggestions.get(author["id"], {}).get("avatar_url") or stats["avatar_url"])
            stats["adopted"] += 1
            delta = r.get("chat_delta", r["delta"])
            stats["positive"] += delta > 0
            stats["negative"] += delta < 0
            stats["score_delta"] += delta
            stats["positive_delta"] += max(0, delta)
            stats["negative_delta"] += min(0, delta)
            stats["rate"] = stats["positive"] / stats["adopted"]

        best = min(authors.values(), key=lambda value: (-value["rate"], -value["adopted"], value["author_id"]), default=None)
        negative = [value for value in authors.values() if value["negative_delta"] < 0]
        if negative:
            worst = min(negative, key=lambda value: (value["negative_delta"], value["author_id"]))
            reason = "most_negative_points"
        else:
            candidates = sorted(suggestions)
            candidate = drawn_author if drawn_author in suggestions else None
            if candidate is None and candidates:
                candidate = self.rng.choice(candidates)
            worst = authors.get(candidate, suggestions.get(candidate))
            reason = "random_suggestion"
        return {"best": dict(best, selection_reason="highest_positive_rate") if best else None,
                "worst": dict(worst, rate=worst["negative"] / worst["adopted"] if worst["adopted"] else 0,
                              selection_reason=reason) if worst else None}

    def _boards(self, *, live_only=False):
        broadcast_id = self.data.get("live_broadcast_id") if live_only else None
        if live_only and not broadcast_id:
            # Historical DyHub origin proves a real viewer, but cannot prove
            # which broadcast they joined. Keep such history in debug only.
            return {"best": [], "worst": []}
        episode_ids = {e["id"] for e in self.data["episodes"] if e["session_id"] == self.data["session_id"]}
        authors = {}
        for r in self.data["rounds"]:
            if ((not broadcast_id and r["episode_id"] not in episode_ids)
                    or r["phase"] != "DONE" or r["selection_source"] != "audience"):
                continue
            option = next(o for o in r["options"] if o["id"] == r["selected_option_id"])
            if live_only and not self._live_option(option):
                continue
            if broadcast_id:
                # Attribute a result to the accepted suggestion, not the later
                # settlement time. An old/in-flight round can finish normally
                # without awarding its points to the next broadcast.
                proposal = self._get("proposals", option.get("proposal_id"))
                if proposal.get("live_broadcast_id") != broadcast_id:
                    continue
            author = option["author"]
            row = authors.setdefault(author["id"], {"author_id": author["id"], "nickname": author["nickname"], "adopted": 0, "positive": 0, "negative": 0})
            contribution = r.get("chat_delta", r["delta"])
            row.update(nickname=author["nickname"], adopted=row["adopted"] + 1, positive=row["positive"] + (contribution > 0), negative=row["negative"] + (contribution < 0))
        def board(kind):
            rows = [dict(row, rate=row[kind] / row["adopted"]) for row in authors.values()]
            return sorted(rows, key=lambda row: (-row["rate"], -row["adopted"], row["author_id"]))
        return {"best": board("positive"), "worst": board("negative")}

    def public_state(self, *, live_only=False):
        episode, r = self.episode, self.current_round
        public_episode = None
        if episode:
            public_episode = {"id": episode["id"], "title": episode.get("title", "准备新故事"), "man_name": episode.get("man", {}).get("name", ""), "woman_name": episode.get("woman", {}).get("name", ""), "hints": episode.get("public_hints", []), "score": episode["score"], "status": episode["status"]}
        public_round = None
        if r:
            counts = r["vote_snapshot"] if "vote_snapshot" in r else self._counts()
            committed = any(m["round_id"] == r["id"] and m["role"] == "man" for m in self.data["messages"])
            performance = r.get("performance")
            if performance is not None and performance.get("man_released") is not True:
                committed = False
            selected = next((o for o in r["options"] if o["id"] == r["selected_option_id"]), None)
            public_round = {"id": r["id"], "number": r["number"], "phase": r["phase"], "options": [{"id": o["id"], "number": o["number"], "text": o["text"], "author": o["author"], "votes": counts.get(o["id"], 0)} for o in r["options"]], "selected_option_id": r["selected_option_id"] if committed else None, "selected_author": selected["author"] if committed and selected else None, "selection_source": r["selection_source"] if committed else None, "reaction": r["reaction"] if committed else None, "delta": r["delta"]}
            public_round["woman_typing"] = (self.busy and self._woman_typing_token == self._token
                                            and r["phase"] == "WAITING_REPLY" and committed
                                            and not getattr(self.performer, "stopping", False))
            if performance is not None:
                public_round["performance"] = {"status": performance["status"], "man_released": performance.get("man_released") is True, "playback_confirmed": False}
        messages = self._messages() if episode else []
        if r and r.get("performance") is not None and r["performance"].get("man_released") is not True:
            messages = [m for m in messages if not (m["round_id"] == r["id"] and m["role"] == "man")]
        now = time.time()
        finale = copy.deepcopy(episode.get("finale")) if episode and episode["status"] in {"SUCCEEDED", "FAILED"} else None
        if finale:
            live_awards = finale.pop("live_awards", None)
            if live_only:
                if live_awards is not None:
                    finale["awards"] = live_awards
                else:
                    # Historical awards stay frozen: suppress an unproven winner
                    # without drawing a replacement during polling or restart.
                    live_authors = {p["author_id"] for r_ in self.data["rounds"]
                                    if r_["episode_id"] == episode["id"]
                                    for p in self._round_inputs["proposals"].get(r_["id"], [])
                                    if self._live_proposal(p)}
                    finale["awards"] = {kind: award if award and award["author_id"] in live_authors else None
                                        for kind, award in finale["awards"].items()}
            finale["elapsed_ms"] = max(0, int((now - finale["started_at"]) * 1000))
        public_messages = [dict(id=m["id"], role=m["role"], text=m["text"], round_id=m["round_id"],
                                **self._message_portrait(m)) for m in messages]
        return copy.deepcopy({"session": {"id": self.data["session_id"]}, "episode": public_episode, "round": public_round, "messages": public_messages, "leaderboards": self._boards(live_only=live_only), "finale": finale, "busy": self.busy, "error": self.data["error"]})

    def operator_state(self, *, live_only=False, incremental=False, session_id=None, comment_cursor=None,
                       proposal_version=None, vote_version=None, comment_limit=200):
        state = self.public_state(live_only=live_only)
        state["diagnostics"] = {"episode": copy.deepcopy(self.episode), "round": copy.deepcopy(self.current_round),
                                "messages": copy.deepcopy(self._messages()) if self.episode else []}
        current_session, current_round = self.data["session_id"], self.data["round_id"]
        round_ = self.current_round
        selection = {item["proposal_id"]: item for item in (round_ or {}).get("proposal_selection", [])}
        versions = {table: f"{self.run_id}:{current_round}:{self._versions[table]}" for table in self._versions}
        for table, supplied in (("proposals", proposal_version), ("votes", vote_version)):
            if not incremental or supplied != versions[table]:
                rows = self._round_inputs[table].get(current_round, [])
                prefix = f"{self.run_id}:{current_round}:"
                revision = supplied[len(prefix):] if isinstance(supplied, str) and supplied.startswith(prefix) else ""
                valid = len(revision) <= 20 and revision.isascii() and revision.isdecimal() and 0 <= int(revision) <= self._versions[table]
                reset = not valid
                if incremental and not reset:
                    rows = [row for row in rows if self._input_revisions[table].get(row["id"], 0) > int(revision)]
                state[table] = copy.deepcopy(rows)
                if table == "proposals":
                    for row in state[table]:
                        assessment = selection.get(row["id"])
                        if assessment is None:
                            pending = round_ and round_["phase"] in ("COLLECTING", "SELECTING")
                            assessment = {"status": "pending" if pending else "unrecorded",
                                "reason": "等待本轮筛选完成。" if pending else "本轮筛选原因未记录。",
                                "reason_source": "system" if pending else "unrecorded"}
                        row["selection"] = {key: value for key, value in assessment.items() if key != "proposal_id"}
                if incremental:
                    state["proposal_reset" if table == "proposals" else "vote_reset"] = reset
        comments = self._session_comments.get(current_session, [])
        if incremental:
            reset = (session_id != current_session or type(comment_cursor) is not int
                     or not 0 <= comment_cursor <= len(comments))
            cursor = 0 if reset else comment_cursor
            limit = min(500, max(1, int(comment_limit)))
            end = min(len(comments), cursor + limit)
            state["comments"] = copy.deepcopy(comments[cursor:end])
            state.update(session_id=current_session, comment_cursor=end, reset=reset, has_more=end < len(comments),
                         proposal_version=versions["proposals"], vote_version=versions["votes"])
        else:
            state["comments"] = copy.deepcopy(comments)
        episodes = {e["id"]: e for e in self.data["episodes"] if e["session_id"] == self.data["session_id"]}
        state["comment_rounds"] = [{"id": r["id"], "number": r["number"], "episode_id": r["episode_id"],
                                    "title": episodes[r["episode_id"]].get("title", "准备新故事")}
                                   for r in self.data["rounds"] if r["episode_id"] in episodes]
        return state

    def close(self):
        self.storage.close()

