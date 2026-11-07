# 游戏同角色六档换形

默认关闭，启动本机服务不会启用，也不会因此开云场。无密钥模板位于根目录 `config-examples/external-appearance.example.json`。实际配置由操作者自行放到 Studio 的 `data/external-appearance.json`，在进程启动时读取，不热重载、不入 Git；本次实现未写入真实配置、未开启模型/TTS/Vidu。

启用前需要六个**真实已就绪**资产和各自缓存原图的 SHA-256，六档键固定为 `fish/rough/tired/normal/refined/stellar`。每个资产都必须在原有 `local.json` 的 `tts_voices` 映射绑定到同一 `voice_id`，当前本场音色也须一致。`character_id` 是本机同一角色组的稳定名称；配置者仍须人工核对六张素材确实是同一人。`description` 只描述可见外貌，不写礼物或后台分数。`broadcast_key` 必须对应开场实际选中资产；它也是回普通口播时必须恢复的档位。不能把星耀原图标成普通档。

`transitions.up/down` 各填一支已批准、云端可访问的 HTTPS 短片。配置的公网地址格式会被校验；实现不会在未授权时联网下载、上传或验证播放。因此 readiness 仅说明本机绑定和格式齐全，实际播放、嘴眼动态、音色与录制仍须另行授权短场验收。缺 ID、图片、指纹、音色或合法 HTTPS 时，真实效果明确阻断；配置模板里的占位符不能用于开场。游戏未启用该效果时沿用原玩法；游戏明确启用并核验礼物数量后，前置检查不通过则在收费开场前停止。

## 私有本机接口

全部位于已有令牌保护的 `/studio/<token>/external/appearance` 下，并复用游戏的 owner/generation：

- `GET /external/appearance`：携带 owner/generation 查询当前 readiness、程序已采用键、外观版本、在途请求；只读。
- `POST /external/appearance/lifecycle`：`owner_id/generation/mode_epoch/episode_id/action`。`bind` 开本期权限，`release` 立即关闭；已 release 的同一期不能重新绑定，换期必须使用更大的 mode_epoch。
- `POST /external/appearance`：`owner_id/generation/mode_epoch/episode_id/request_id/target_key/purpose`。`purpose` 是 `game` 或关闭本期后的 `restore`；恢复目标只能是本场 broadcast_key。Studio 根据已采用键与目标键选择 up/down，不接受 URL、资产 ID 或客户端方向。初始不是普通档时，本期首个目标只能是 normal。
- `GET /external/appearance/<request_id>`：携带 owner/generation 查同一个请求。查询和重复同参数提交都不会再写 Type16，同 ID 改参数拒绝。
- 正常换形不调用确认接口。旧 `POST /external/appearance/<request_id>/confirm` 的 `target_dynamic` 已拒绝；仅保留明确归属失败后 `original_dynamic` 的人工核实原貌兼容入口。它不重发失败目标，也不能自行恢复已故障暂停的 Program；工作台正常流程无确认按钮。

状态由 `pending → written → applied` 推进；另有 `cancelled/failed/unconfirmed/retained`。`written` 只证明 WS 写入，当前 Type16 没有可依赖的成功 ack。Studio 在正常写出后先等 **3 秒转场保护**，再要求同一唯一接收端在保护期后继续产生新解码帧、音视频轨道和心跳保持健康；最多再等 **15 秒**。通过后仅记录 `program_adopted=true`、`evidence=transport_guard_healthy_receiver`，更新本场资产绑定并解除程序调度保护；原本已为目标档时不发 Type16，依据为 `existing_asset_binding`。`confirmed_key` 是兼容字段名，表示采用的绑定，不是视觉身份确认。

`operator_confirmed/visual_confirmed/playback_confirmed/success_acknowledged` 均保持 false，`success_ack_available=false`。这些保护和健康帧只用于程序采用；Type12、短片长度、健康帧及没有103均不能证明目标形象或短片已经播放成功。明确失败或接收端异常立即保持发送保护；缺新帧超时为 unconfirmed，不盲目重发，允许心跳和停场。已采用请求若收到迟到的可归属103，重新锁住并请求结束核实。
操作意图先写本场 external-game 归档再写网络。服务重启会换 generation，不恢复旧请求到发送队列；原有未知创建、未确认结束与唯一接收端保护继续生效。自动保护等待不持有应用锁，不妨碍停止。程序采用后的本场资产更新保持人物设置、声音和 persona_epoch，清掉预制缓存；不改变资产库默认选择，不清空剧情历史。

## 游戏侧契约

`Program.appearance_status()` 提供 `ready/message/active/confirmed_key/pending_confirmation/receipt/context/version`；安全状态不含过场地址。`begin_appearance_episode(id)` 等 Studio 自动采用普通档后才调用本期启用钩子，开始 50 分计分。`request_appearance(key)` 合并最新目标，正式口播到低头Q整组独占期间不发换形；待切目标优先于新可选闲聊。Program 仅查询原请求，不发送正常确认 POST；校验 applied 的所有权、档位、采用依据及无视觉/人工确认字段，并再次核对 Studio 健康状态和绑定。明确失败或未知立即停止推进；pending/written 最长 45 秒仍未收敛则故障暂停，该上限只用于失败，不产生成功。

`end_appearance_episode()` 先关闭收入，再等在途结果并自动恢复口播档；只有恢复已采用且 Studio 正常才交接回聊天。停止整个云场不为还原多发一次Type16。唯一发送者、未知创建、停止未确认等既有保护不变。
正式低头是口播的必要后半段，Program 使用前一任务回执 `motion_ready_in_seconds` 在正式占位内等待原Q冷却，再提交低头；该等待不缩短动作安全间隔，也不表示画面成功。

离线验证：Studio 的 `test_external_appearance.py` 走真实 HTTP 归属路由和真实 Type16 发送器，覆盖健康帧自动采用、保护前的帧不能代替保护后的进度、停帧/轨道丢失/换接收端、立即/迟到103及幂等。游戏的 `tests/test_automatic_appearance_protocol.py` 串起生产 Program、AvatarClient HTTP、ExternalAppearance 与共享 Type16 发送器；没有 `/confirm` 路由，覆盖合成礼物六档跨越、开局普通档、退出恢复、口播低头保护、未知不重试和查询期限。所有外部音频/WS/解码帧均为离线替身，保护时间缩短；不代表真实云端播放或视觉验收。历史人工确认兼容用例保留在 `test_real_appearance_program.py`，原两档模拟回合在 `tests/test_appearance_round.py`。
