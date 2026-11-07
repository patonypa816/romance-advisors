"""Game-facing appearance state and the local, provider-free costume rehearsal."""
import asyncio
import time
import uuid

from aiohttp import web

from .appearance import (APPEARANCE_KEYS, AppearanceLedger, PersistentAppearanceService,
                         live_gift_readiness, validate_band_starts)
from .model import strict_json
from .avatar_client import AvatarClientError
from .appearance_gift_probe import GiftQuantityProbe


class AppearanceInputError(ValueError):
    """An operator-facing validation message generated on this workbench."""


BRIDGE_MESSAGES = {
    "appearance_disabled": "尚未确认六档绑定。启场前会核对 Studio 配置的资产和正负向 HTTPS 转场。",
    "appearance_config_invalid": "Studio 的六档资产或转场地址配置不完整。",
    "appearance_assets_not_ready": "Studio 资产库尚未备齐六档可用形象和底图。",
    "appearance_asset_binding_invalid": "底图指纹或音色与六档配置不符。",
    "appearance_voice_mismatch": "当前角色的音色与游戏绑定不一致。",
    "appearance_identity_unknown": "当前数字人不能对应到已配置的六档形象。",
    "appearance_unconfirmed": "换形结果待核实，请查看接收端画面。",
    "appearance_late_failure": "早先换形收到迟到失败，请核实本场画面。",
    "appearance_initializing": "正在核对开场普通造型。",
    "appearance_paused": "本期换形已暂停。",
    "appearance_restore_failed": "口播形象未恢复，请核对接收画面和停场状态。",
    "appearance_broadcast_binding_invalid": "当前选中的口播形象与配置的开场资产不一致。",
}


class CostumeRehearsal:
    """Only isolated preview uses clip duration; RTC requires bounded receiver health."""
    def __init__(self):
        self.ledger = None
        self.tier = "normal"
        self.transition = None
        self.timer = None

    def start(self, band_starts):
        if self.ledger is None:
            self.ledger = AppearanceLedger({"up": 1, "down": -1}, band_starts=band_starts)
            self.tier = "normal"
            self.transition = None

    def stop(self):
        if self.timer:
            self.timer.cancel()
        self.timer = self.ledger = self.transition = None

    def gift(self, direction, diamonds):
        if not self.ledger or direction not in {"up", "down"} or type(diamonds) is not int or not 1 <= diamonds <= 100:
            raise AppearanceInputError("请先启动试玩，模拟钻石须为 1 到 100 的整数。")
        event_id = uuid.uuid4().hex
        receipt = self.ledger.apply({"simulated": True, "type": "gift", "platform": "douyin",
            "roomId": self.ledger.room_id, "episodeId": self.ledger.episode_id, "id": event_id,
            "user": {"id": "local-rehearsal"}, "data": {"giftId": direction, "groupId": event_id,
            "diamondCount": diamonds, "repeatCount": 1}})
        if not receipt["accepted"]:
            raise AppearanceInputError("请先启动试玩，模拟钻石须为 1 到 100 的整数。")
        self.advance()

    def advance(self):
        if self.timer is not None or self.tier == self.ledger.target:
            return
        target = self.ledger.target
        up = APPEARANCE_KEYS.index(target) > APPEARANCE_KEYS.index(self.tier)
        self.transition = {"id": uuid.uuid4().hex, "at": time.time(),
                           "direction": "up" if up else "down", "from_tier": self.tier, "to_tier": target}

        def complete():
            self.tier = target
            self.timer = None
            self.advance()  # Gifts received during the clip have already updated the single latest target.
        self.timer = asyncio.get_running_loop().call_later(1.40, complete)


class AppearanceWorkbench:
    def __init__(self, workbench):
        self.wb = workbench
        self.service = PersistentAppearanceService(workbench.game)
        self.rehearsal = CostumeRehearsal()
        self.gift_probe = GiftQuantityProbe()

    def operator_test_status(self):
        """Admission for one explicit +20 test in the current formal broadcast."""
        program = self.wb.program
        ledger = self.service.snapshot()
        bridge = program.appearance_status()
        context = bridge.get("context") or {}
        owned = (program.game_episode_id is not None and ledger["episode_id"] == program.game_episode_id
                 and ledger["owner_id"] == program.run_id)
        receipt = ledger["operator_test"] if owned else None
        reason, message = "ready", "本期可测试加 20 造型分；会走真实换形，且本期只能使用一次。"
        if receipt:
            reason, message = "used", "本期测试已记账，不会再次加分或重发换形；请查看当前换形状态。"
        elif not (program.broadcast_mode and program.game_enabled and program.ready and program.run_id
                  and not program.stopping):
            reason, message = "game_not_ready", "请在正式直播的游戏就绪后使用本期测试。"
        elif program.sender_failed:
            reason, message = "sender_failed", "发送已暂停或失败，请先核对本场状态。"
        elif program.error or self.wb.error or self.wb.game.data.get("error"):
            reason, message = "program_paused", "当前游戏或直播有待处理错误，请先核对状态。"
        elif (not program.real_appearance_enabled or not ledger["active"] or ledger["simulated"]
              or live_gift_readiness(self.service.settings())["live_gift_mode"] not in {"verified", "one_diamond_test"}):
            reason, message = "live_gifts_inactive", "真实礼物造型账尚未激活，请先核对本期造型状态。"
        elif (ledger["episode_id"] != program.game_episode_id or ledger["owner_id"] != program.run_id
              or context.get("owner_id") != program.run_id or context.get("episode_id") != program.game_episode_id
              or context.get("generation") != program.client.generation
              or (self.wb.game.episode or {}).get("status") != "ACTIVE"):
            reason, message = "owner_mismatch", "本期游戏和造型账的直播归属不一致，请刷新后核对。"
        elif bridge.get("pending_confirmation"):
            reason, message = "confirmation_pending", "当前换形尚未完成，请等待自动衔接或核对故障。"
        elif (not bridge.get("ready") or not bridge.get("active")
              or (program._appearance_task is not None and not program._appearance_task.done())
              or ledger["target"] != bridge.get("confirmed_key")):
            reason, message = "appearance_busy", "正在换形或造型已暂停，请等待当前请求完成。"
        return {"eligible": reason == "ready", "reason": reason, "message": message,
                "run_id": program.run_id, "episode_id": program.game_episode_id,
                "used": receipt is not None, "receipt": receipt}

    def operator_test(self, body):
        program = self.wb.program
        # Bind the click to the screen's exact run and episode, even after a
        # delayed request arrives during a new story or a restarted broadcast.
        if (not isinstance(body, dict) or set(body) != {"run_id", "episode_id"}
                or not isinstance(body.get("run_id"), str) or not body["run_id"]
                or not isinstance(body.get("episode_id"), str) or not body["episode_id"]
                or body["run_id"] != program.run_id or body["episode_id"] != program.game_episode_id):
            raise AppearanceInputError("这次测试对应的直播或本期已改变，请刷新后核对。")
        status = self.operator_test_status()
        ledger = self.service.snapshot()
        if status["used"] and (ledger["owner_id"] != body["run_id"] or ledger["episode_id"] != body["episode_id"]):
            raise AppearanceInputError("这次测试对应的造型账已改变，请刷新后核对。")
        if not status["eligible"] and not status["used"]:
            raise AppearanceInputError(status["message"])
        receipt = self.service.apply_operator_test(body["run_id"], body["episode_id"])
        if receipt["accepted"]:
            # No await between durable credit and normal target admission:
            # another incoming real gift always reads this committed score.
            program.request_appearance(receipt["target"])
        return receipt

    def configure(self, body):
        if self.wb.program.active or self.rehearsal.ledger is not None:
            raise AppearanceInputError("请在本场直播或造型试玩结束后保存配置。")
        required = {"enabled", "room_id", "positive_gift_ids", "negative_gift_ids", "quantity_verified"}
        if set(body) - {"band_starts"} != required:
            raise AppearanceInputError("配置字段不完整。")
        room = body["room_id"]
        if type(body["enabled"]) is not bool or type(body["quantity_verified"]) is not bool or not isinstance(room, str):
            raise AppearanceInputError("请检查直播间和启用设置。")
        groups = (body["positive_gift_ids"], body["negative_gift_ids"])
        if any(not isinstance(ids, list) or len(ids) > 128 or any(not isinstance(i, str) or not i.isascii() or not i.isdigit() or not 1 <= len(i) <= 32 for i in ids) for ids in groups):
            raise AppearanceInputError("礼物 ID 请填写数字。")
        if set(groups[0]) & set(groups[1]):
            raise AppearanceInputError("同一礼物不能同时加分和减分。")
        if (room and (not room.isascii() or not room.isdigit() or len(room) > 32)
                or body["enabled"] and (not room or not all(groups))):
            raise AppearanceInputError("启用时请填写房间数字号和正、负向礼物 ID。")
        directions = {i: sign for sign, ids in zip((1, -1), groups) for i in ids}
        policy = live_gift_readiness({"enabled": body["enabled"], "room_id": room,
                                      "gift_directions": directions,
                                      "quantity_verified": body["quantity_verified"]})
        if body["enabled"] and not policy["can_enable_live_gifts"]:
            raise AppearanceInputError("自定义礼物启用前须核实单价和连击数量；未核量实播联测仅限正向463、负向164，且每个礼物1钻。")
        try:
            bands = validate_band_starts(body.get("band_starts"))
        except ValueError:
            raise AppearanceInputError("四个分界需逐档递增，50 分须落在普通档，100 分须落在巨帅档。") from None
        if self.service.snapshot()["active"]:
            # A cold start leaves old episode data available for inspection but
            # cannot keep accepting gifts under the old Program's ownership.
            self.service.close_episode()
        config = {"enabled": body["enabled"], "room_id": room, "gift_directions": directions,
                  "quantity_verified": body["quantity_verified"], "band_starts": bands}
        self.service.configure(config)

    def snapshot(self, operator=False):
        ledger = self.service.snapshot()
        config = self.service.settings()
        policy = live_gift_readiness(config)
        bridge = self.wb.program.appearance_status()
        preview = self.rehearsal
        local = preview.ledger is not None
        active = bool(ledger["active"] and bridge.get("active") and self.wb.program.game_enabled
                      and ledger["episode_id"] == self.wb.program.game_episode_id)
        directions = config["gift_directions"]
        # Audience pages only need to know whether the two illustrated rules apply.
        guide_configured = all(directions.get(gift_id) == sign
                              for gift_id, (_, sign) in GiftQuantityProbe.GIFTS.items())
        state = {"enabled": bool(config["enabled"]), "active": active or local,
                 "gift_guide_configured": guide_configured,
                 "mode": "rehearsal" if local else "rtc" if active else "off",
                 "rehearsal_active": local, "score": preview.ledger.score if local else ledger["score"],
                 "tier": preview.tier if local else bridge.get("confirmed_key"),
                 "target": preview.ledger.target if local else ledger["target"],
                 "transition": preview.transition if local else None}
        contribution = (ledger["operator_test"] if self.wb.program.game_episode_id is not None
                        and ledger["episode_id"] == self.wb.program.game_episode_id
                        and ledger["owner_id"] == self.wb.program.run_id else None)
        state["operator_test_contribution"] = ({key: contribution[key] for key in
            ("source", "delta", "score_before", "score", "at")} if contribution and not local else None)
        if operator:
            state["operator_test"] = self.operator_test_status()
            state["gift_probe"] = {**self.gift_probe.snapshot(),
                                  "available": self.gift_probe.eligible(config, config["room_id"])}
            state["settings"] = {"enabled": config["enabled"], "room_id": config["room_id"] if config["room_id"] != "unconfigured" else "",
                                 "band_starts": config.get("band_starts"),
                                 "positive_gift_ids": [k for k,v in directions.items() if v == 1],
                                 "negative_gift_ids": [k for k,v in directions.items() if v == -1],
                                 "quantity_verified": config["quantity_verified"]}
            if policy["live_gift_mode"] == "one_diamond_test":
                if bridge.get("pending_confirmation"):
                    message = "一钻实播联测：换形由程序自动衔接；动态画面、单击和连击仍待现场验收。"
                elif bridge.get("ready"):
                    message = "一钻实播联测已接通正常计分和换形，仅收小心心463加分、抖音164减分；单击和连击仍待现场验收。"
                else:
                    message = "已启用一钻实播联测，单击和连击仍待现场验收。" + BRIDGE_MESSAGES.get(bridge.get("message"), "将核对 Studio 六档绑定与动态画面。")
            elif directions and not config["quantity_verified"]:
                message = "已保存礼物 ID，单价和连击数量仍待核实；连接同一房间可查看一钻手测核量。"
            elif not config["enabled"]:
                message = "尚未启用真实礼物。"
            elif not config["quantity_verified"]:
                message = "已保存礼物 ID，单价和连击数量仍待核实。"
            else:
                code = bridge.get("message")
                if bridge.get("ready"):
                    message = "六档绑定就绪，本期正在接收指定礼物。"
                elif bridge.get("pending_confirmation"):
                    message = "正在自动衔接造型；失败或结果未知时暂停，不会自动重试。"
                else:
                    message = BRIDGE_MESSAGES.get(code, "将核对 Studio 六档资产、音色和正负向转场地址。")
            state["readiness"] = {**policy,
                                  "ready": bool(policy["live_gift_mode"] in {"verified", "one_diamond_test"} and bridge.get("ready")),
                                  "message": message}
            state["pending_confirmation"] = None
            state["receipt"] = bridge.get("receipt")
            state["automatic_progress"] = True
        return state

    def try_action(self, body):
        action = body.get("action")
        if (not isinstance(action, str) or action not in {"start", "up", "down", "stop"}
                or set(body) - ({"action", "diamonds"} if action in {"up", "down"} else {"action"})):
            raise AppearanceInputError("未知的造型试玩操作。")
        if action == "stop":
            self.rehearsal.stop()
            return
        wb = self.wb
        episode = wb.game.episode
        if wb.program.active or wb.game.busy or (wb.job and not wb.job.done()) or (episode and episode["status"] == "ACTIVE"):
            raise AppearanceInputError("请在本期剧情和数字人节目结束后开始本地试玩。")
        if action == "start":
            self.rehearsal.start(self.service.settings().get("band_starts"))
        else:
            self.rehearsal.gift(action, body.get("diamonds", 12))


def register_appearance_routes(app, wb):
    async def handle(request):
        try:
            receipt = None
            if request.method == "POST":
                body = strict_json(await request.text())
                action = request.match_info["action"]
                if action == "rehearsal":
                    wb.appearance.try_action(body)
                elif action == "settings":
                    intake = app["dyhub_intake"]
                    if intake.active:
                        raise AppearanceInputError("请先断开弹幕接收，保存配置后重新连接。")
                    wb.appearance.configure(body)
                elif action == "confirm":
                    if (set(body) - {"request_id", "outcome"} or not isinstance(body.get("request_id"), str)
                            or not body["request_id"] or body.get("outcome", "target_dynamic") not in ("target_dynamic", "original_dynamic")):
                        raise AppearanceInputError("请确认当前换形请求。")
                    await wb.program.confirm_appearance(body["request_id"], outcome=body.get("outcome", "target_dynamic"))
                elif action == "gift-probe":
                    intake = app["dyhub_intake"]
                    probe = wb.appearance.gift_probe
                    if (body or not intake.active or not probe.active
                            or not probe.eligible(wb.appearance.service.settings(), intake.room_id)):
                        raise AppearanceInputError("请先连接已保存的待核量直播间，保持真实礼物关闭。")
                    probe.reset_sample()
                elif action == "operator-test":
                    receipt = wb.appearance.operator_test(body)
                else:
                    raise web.HTTPNotFound()
            return web.json_response({"appearance": wb.appearance.snapshot(operator=True),
                                      **({"receipt": receipt} if receipt is not None else {})})
        except AppearanceInputError as exc:
            return web.json_response({"error": str(exc), "appearance": wb.appearance.snapshot(operator=True)}, status=409)
        except (ValueError, AvatarClientError):
            return web.json_response({"error": "造型操作未完成，请刷新状态后检查输入或当前请求。",
                                      "appearance": wb.appearance.snapshot(operator=True)}, status=409)
    app.router.add_get("/api/appearance", handle)
    app.router.add_post("/api/appearance/{action:rehearsal|settings|confirm|gift-probe|operator-test}", handle)
