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


FINALE_DURATION_MS = 6800


class StaleResult(Exception):
    pass


def ident():
    return uuid.uuid4().hex


def clean_text(value, field, limit=12000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"invalid_{field}")
    return value.strip()


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
        self.rng = random.Random()
        self._validation_error = None
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
        for table in self._round_inputs:
            for row in self.data[table]:
                self._round_inputs[table].setdefault(row["round_id"], []).append(row)
        for row in self.data["comments"]:
            self._session_comments.setdefault(row["session_id"], []).append(row)

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
        result = await self._call("prepare", {"previous_titles": [e.get("title", "") for e in self.data["episodes"][-8:]]}, token)
        for role in ("man", "woman"):
            character = result.get(role)
            if not isinstance(character, dict) or type(character.get("age")) is not int or not 18 <= character["age"] <= 120:
                raise ValueError("invalid_adult_character")
            for field in ("name", "personality", "background", "speech_style"):
                clean_text(character.get(field), field)
        woman = result["woman"]
        if not isinstance(woman.get("private_preferences"), (str, list, dict)) or not woman["private_preferences"]:
            raise ValueError("invalid_private_preferences")
        hints = result.get("public_hints")
        if not isinstance(hints, list) or not hints or len(hints) > 12 or not all(isinstance(h, str) and h.strip() for h in hints):
            raise ValueError("invalid_public_hints")
        prepared = {key: result[key] for key in ("man", "woman", "public_hints")}
        prepared.update(title=clean_text(result.get("title"), "title"), shared_background=clean_text(result.get("shared_background"), "background"), opening=clean_text(result.get("opening"), "opening"))
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
        proposal = {"id": ident(), "round_id": data["round_id"], "author_id": author_id, "nickname": nickname, "text": content}
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

    def accept_comment(self, event, *, blocked_reason=None):
        """Record one local simulated comment and its window-bound receipt."""
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
            return copy.deepcopy(existing)

        r = self.current_round
        phase = r["phase"] if r else None
        command = re.fullmatch(r"投([1-4])", payload["text"].strip())
        comment = dict(payload, id=payload["event_id"], received_at=datetime.now(timezone.utc).isoformat(),
                       session_id=self.data["session_id"], episode_id=self.data["episode_id"],
                       round_id=self.data["round_id"], round_number=r["number"] if r else None,
                       phase=phase, kind="vote" if command else "chat", result="ignored",
                       reason="当前未开放征集或投票，仅记录评论。", proposal_id=None, vote_id=None, option_id=None)
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
            if command:
                comment["reason"] = "当前为建议征集阶段，投票尚未开放。"
            else:
                proposal = self._append_proposal(pending, payload["author_id"], payload["nickname"], payload["text"], avatar_url=avatar_url)
                comment.update(kind="proposal", result="candidate", reason="已收到，保留为本轮待编选评论；不会覆盖该参与者之前的建议。", proposal_id=proposal["id"])
        elif self.episode and self.episode["status"] == "ACTIVE" and phase == "VOTING":
            if command:
                option = next((o for o in r["options"] if o["number"] == int(command[1])), None)
                if option:
                    existing_vote = self._get("votes", self.data["round_id"] + ":" + payload["author_id"])
                    if existing_vote is not None:
                        pending["votes"].append(dict(existing_vote))
                    vote = self._upsert_vote(pending, payload["author_id"], payload["nickname"], option["id"])
                    comment.update(result="voted", reason="投票已记录；截止前最后一张有效票为准。", vote_id=vote["id"], option_id=option["id"])
                else:
                    comment["reason"] = "该编号没有对应选项，未改动已有有效票。"
            else:
                comment["reason"] = "当前为投票阶段，仅完整“投1”至“投4”计票；本条仅记录。"
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
        receipt = await self.performer.perform(copy.deepcopy(record), r["reaction"])
        if token != self._token or self.current_round is not r or r["phase"] != "WAITING_REPLY":
            raise StaleResult()
        if (not isinstance(receipt, dict) or receipt.get("status") != "sent"
                or receipt.get("send_complete") is not True
                or any(receipt.get(key, record[key]) != record[key]
                       for key in ("job_id", "owner_id", "generation"))):
            raise ValueError("invalid_performance_receipt")
        # Confirmed local send-queue progress releases the text. Vidu supplies
        # no render-complete receipt, so never claim confirmed playback.
        r["performance"] = dict(record, **receipt)
        r["performance"]["man_released"] = True
        r["performance"]["playback_confirmed"] = False
        self._save()
        self._metric("man_public", outcome="committed", job_id=record["job_id"])

    async def _reply(self, token):
        r = self.current_round
        man_message = next(m for m in self._messages() if m["round_id"] == r["id"] and m["role"] == "man")
        partner = {key: self.episode["man"][key] for key in ("name", "age")}
        payload = {"woman": self.episode["woman"], "partner": partner, "shared_background": self.episode["shared_background"], "messages": [{"role": m["role"], "text": m["text"]} for m in self._messages()], "score": self.episode["score"], "reply_to_message": {"id": man_message["id"], "text": man_message["text"]}}
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
        self._message("woman", message, r["id"], man_message["id"])
        r.update(before=before, proposed_delta=delta, delta=after - before, after=after, phase="DONE")
        self.episode.update(score=after, status="SUCCEEDED" if after == 100 else "FAILED" if after == 0 else "ACTIVE")
        if expected != "continue":
            # The final reply, score, clock and awards are a single durable
            # transition. Read/poll/restart must never begin a second finale.
            self.episode["finale"] = {"episode_id": self.episode["id"],
                "status": self.episode["status"], "started_at": time.time(),
                "duration_ms": FINALE_DURATION_MS, "awards": self._episode_awards()}
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

    def _episode_awards(self):
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
                if p["id"] in recognized:
                    suggestions[p["author_id"]] = row(p["author_id"], p["nickname"],
                        safe_avatar_url(p.get("avatar_url")) or suggestions.get(p["author_id"], {}).get("avatar_url"))
            if r["selection_source"] != "audience":
                continue
            selected = next(o for o in r["options"] if o["id"] == r["selected_option_id"])
            author = selected["author"]
            stats = authors.setdefault(author["id"], row(author["id"], author["nickname"], author.get("avatar_url")))
            stats.update(nickname=author["nickname"], avatar_url=safe_avatar_url(author.get("avatar_url"))
                         or suggestions.get(author["id"], {}).get("avatar_url") or stats["avatar_url"])
            stats["adopted"] += 1
            delta = r["delta"]
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
            candidate = self.rng.choice(candidates) if candidates else None
            worst = authors.get(candidate, suggestions.get(candidate))
            reason = "random_suggestion"
        return {"best": dict(best, selection_reason="highest_positive_rate") if best else None,
                "worst": dict(worst, rate=worst["negative"] / worst["adopted"] if worst["adopted"] else 0,
                              selection_reason=reason) if worst else None}

    def _boards(self):
        episode_ids = {e["id"] for e in self.data["episodes"] if e["session_id"] == self.data["session_id"]}
        authors = {}
        for r in self.data["rounds"]:
            if r["episode_id"] not in episode_ids or r["phase"] != "DONE" or r["selection_source"] != "audience":
                continue
            option = next(o for o in r["options"] if o["id"] == r["selected_option_id"])
            author = option["author"]
            row = authors.setdefault(author["id"], {"author_id": author["id"], "nickname": author["nickname"], "adopted": 0, "positive": 0, "negative": 0})
            row.update(nickname=author["nickname"], adopted=row["adopted"] + 1, positive=row["positive"] + (r["delta"] > 0), negative=row["negative"] + (r["delta"] < 0))
        def board(kind):
            rows = [dict(row, rate=row[kind] / row["adopted"]) for row in authors.values()]
            return sorted(rows, key=lambda row: (-row["rate"], -row["adopted"], row["author_id"]))
        return {"best": board("positive"), "worst": board("negative")}

    def public_state(self):
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
            if performance is not None:
                public_round["performance"] = {"status": performance["status"], "man_released": performance.get("man_released") is True, "playback_confirmed": False}
        messages = self._messages() if episode else []
        if r and r.get("performance") is not None and r["performance"].get("man_released") is not True:
            messages = [m for m in messages if not (m["round_id"] == r["id"] and m["role"] == "man")]
        now = time.time()
        finale = copy.deepcopy(episode.get("finale")) if episode and episode["status"] in {"SUCCEEDED", "FAILED"} else None
        if finale:
            finale["elapsed_ms"] = max(0, int((now - finale["started_at"]) * 1000))
        return copy.deepcopy({"session": {"id": self.data["session_id"]}, "episode": public_episode, "round": public_round, "messages": [{k: m[k] for k in ("id", "role", "text", "round_id")} for m in messages], "leaderboards": self._boards(), "finale": finale, "busy": self.busy, "error": self.data["error"]})

    def operator_state(self, *, incremental=False, session_id=None, comment_cursor=None,
                       proposal_version=None, vote_version=None, comment_limit=200):
        state = self.public_state()
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

