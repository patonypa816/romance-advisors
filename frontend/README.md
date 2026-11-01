# M1 档案 / M2 固定题面试操作页

Node 24；`npm ci && npm run build` 生成 `dist/`，由 Python 应用同源提供。
只使用模拟身份 `simulated / local-test`；登记结果来自真实 PostgreSQL API，不在浏览器保存替代档案。

开发热更新：后端设置 `HELL_INC_ORIGIN=http://127.0.0.1:5173`，前端运行 `npm run dev`。固定端口 5173，`/api` 默认代理至 `http://127.0.0.1:8080`；目标可用 `HELL_INC_BACKEND_URL` 设置。不要开放公网端口或关闭 Origin 检查。

登录链接 `/#token=…` 的 fragment 会在首个异步请求前移除，再交换 HttpOnly session；令牌不写入浏览器存储。请从启动器取得链接，不提交令牌或 Cookie。

登记失败时原 command_id 和载荷保留在当前页面内存，恢复连接后使用「重试 / 确认原登记」。明确修改表单才建立新操作。刷新页面会丢失未确认操作编号：结果未知时先重连重试，不要刷新。页面禁止离线、数据库不可用或未登录时登记。

`npm run test:e2e` 使用 `../tests/e2e/playwright.config.ts`；数据库等实际测试前提见根文档。


## M2 人工评估流程

1. 登记或查找游客，点击「查看详情」选择对象，然后「开始固定题面试」。
2. 核对固定题后点击「确认题目已展示，开始答题」；120 秒仅由服务器决定接纳，页面倒计时只是参考。
3. 提交最终回答，接纳后原文冻结。点击「导出评估任务」，复制完整 JSON 交给 ChatGPT，按冻结量表输出 `{"criteria":[…]}`。
4. 把 criteria JSON 粘贴至「ChatGPT 评价 JSON」，点击「导入评价并结算」。服务器校验证据、计算结果并事务更新档案；浏览器不计算录用或资产。

这是 **ChatGPT 辅助评估（人工导入；尚非自动 API）**。无自动评估调用、自动重试或付费请求。每次人工评估尝试有效期 15 分钟；失败不记零分，暂停会使当前尝试失效，恢复后须点击「重试评估（新尝试）」并重新导出。

网络结果未知时所有新面试写操作锁定，仅允许「确认原面试操作」重放内存中的同一编号和载荷。不要刷新页面或关闭标签页：内存中的未知操作编号不会跨页面保留。取消入口在「取消当前面试」折叠区域，须填写原因。所有动作的可用性以最新服务器快照为准。

## M3 创意赛（人工逐稿评价）

同页最下方“噩梦产品创意赛”独立于面试。先登记模拟游客或员工，创建比赛，确认题目已展示后开放投稿。选择作者、提交原文；关闭前可修改同一作品。首次接纳冻结作者资格；最多 12 位作者。关闭投稿冻结所有最终稿。

逐稿点击“导出此稿评估任务”，将完整冻结 JSON 交给原生 ChatGPT，按冻结规则返回 `{"criteria":[…],"comment":"…"}`，粘贴并导入。没有模型 API 自动调用，没有浏览器评分。任一任务失败时整轮展示和投票保持关闭，只有失败项可以重试；已采纳项不可重抽。导入前检查短评没有评分、排名或获奖暗示。

全部采纳后，下方逐份显示完整作品和短评。操作员核对短评并逐稿明确确认本地展示，才可开放投票；这不代表外部观众看过。选择投票人时读取受保护的个人投票版本，刷新页面也能改票，不暴露实时票数或 AI 分数。关闭投票后点击统一结算，由服务器原子写入结果、荣誉、员工绩效及档案；完成后才显示分数和最终票数。

所有按钮遵从服务器 `allowed_actions`，倒计时只供参考。网络结果未知时，页面锁定原操作 ID 与完整载荷，只能确认原操作；不能改载荷重试或继续其他写入。服务器快照过期、离线、登录失效时停止操作。暂停后人工恢复，中断评价逐稿重试；取消必须填写原因。全部内容以文本 DOM 渲染，不解释作品内 HTML 或指令。

浏览器定位：静态元素均有同名 `id` / `data-testid`（`competition-author`、`competition-text`、`competition-submit`、`competition-voter`、`competition-candidate`、`competition-vote` 等）。逐稿按钮使用 `data-testid="competition-export|competition-retry|competition-confirm-display"` 和 `data-submission-id`；展示条目使用 `data-testid="competition-display-work"`。

## M4 LOCAL 离线协议演练（不播放音画）

同页「游戏演出工作台」是操作入口，不是直播画布，也没有移植旧 Studio。

1. 比例默认未选择；可手动选 16:9 或 4:3，只显示数字人横向子区域的布局占位，不加载、裁剪或改写素材。最终直播仍为 9:16。拟用素材为「地狱集团首席执行官」，真实媒体尚未接入。
2. 显式「创建 LOCAL 演练会话」。不会创建云场、调用模型、TTS、Vidu 或播放音画。
3. 选择固定脚本并「冻结服务器原文」；也可填已结算面试 / 比赛 ID、已采纳短评的作品 ID。M2 已结算结果和 M3 合法展示短评 / 结算结果有直接冻结按钮。原文与分段不可编辑；服务端以持久游戏记录为权威再次核对。
4. 设置 30–900 秒有效期，「提交离线演出」生成稳定 ID 与 SHA-256。哈希包含会话、来源、逐段原文 / 意图和毫秒 UTC 失效时间，使用服务端规定的规范 JSON。服务端验证之后仍需显式「执行离线协议演练」。完成显示「离线演练完成」，不是观众已听到；证据中的 `media_verified` 和 `audience_heard` 均为 false。
5. 记录列表可选择详情、查询状态、取消未开始部分。取消 / 暂停分别展示未开始取消、当前段进行中、结果未知；不声称远端停止。未知记录不能重播，sending / unknown 未解决时不允许关闭会话。恢复发送门只手动触发，不复活已取消 / 过期 / 未知演出。

提交回执未知时，内存保留同一 ID 与完整载荷，仅原样确认；可查回同一记录消除未知回执。执行回执丢失时只查状态，不自动再次发送。请勿在回执未知时刷新页面：待确认请求仅在当前页面内存保存；已落库记录可重新查询。跨模块待定操作会锁住新写入；离线、登录变更、过期快照均关闭操作门。M4 不修改游戏资产，不自动确认 M3 展示或开启投票。

测试定位：所有 M4 静态元素均有相同 `id` / `data-testid`，前缀 `performance-`，包括 `session-create|session-pause|session-resume|session-close`、`aspect`、`source-kind|source-id|submission-id`、`freeze|frozen`、`expiry|submit|replay|execute|cancel|refresh|list|detail|notice`。动态列表为 `performance-select`；游戏按钮为 `interview-freeze-performance`、`competition-freeze-comment`（附 `data-submission-id`）、`competition-freeze-result`。

## M4 Phase 2：真实媒体控制代码（默认禁用，仍需真实联调）

真实链路代码与 mock 测试不代表已完成媒体验收。本轮没有启动云场、请求模型 / TTS、连接真实 RTC 或确认观众收到音画。

工作台折叠区「真实媒体控制」只在服务器声明 capability 后允许创建 **LOCAL real 暂停记录**；这一步不启动云场。真实启动还需服务端独立启用 real / paid 开关、配置新素材与供应商连接，并取得本次限额授权。勾选确认框本身不是授权。启动云场的命令 ID 在请求前固定，未知时只查创建尝试状态，不自动重试创建。普通演出提交回执未知时，独立停止 / 只读核查仍可用；安全命令不会覆盖原演出请求。

显式领取唯一 RTC 接收者后，接收令牌和 RTC 参数仅保留内存；不写 localStorage、sessionStorage、URL、日志或普通快照。接收器禁止本机麦克风、摄像头、屏幕采集，关闭本机音视频发布；视频 muted，声音由 SDK 独立播放以免双声。必须显式点击开启远端声音。它上报的 joined、live tracks、正在推进的解码画面与播放许可均标为 **client_reported**，不是观众证据。自动播放失败、track/解码丢失、心跳异常均保持门关闭；本页不主动自动重连、重新领取或转移接收者。

服务端收到健康接收者后，操作员观察音画并记录人工确认，然后仍需明确恢复发送门。人工确认不是自动验收；发送完成显示真实传输状态，不显示离线演练完成或观众听完。游戏暂停 / 发送暂停不等于结束收费云场。停止后只有供应商正向 ended 回读才标结束；未知可只读核查，不重新创建。已正向结束且无本机 preparing / sending 时，可关闭 real 会话，但旧 unknown 演出仍永不重播，证据保留。

### SDK 安装与验证（仅静态文件）

在 `frontend/` 中先运行 `./tools/fetch-rtc-sdk.sh`，再 `npm run build`。脚本固定 AliRTC **7.1.9** 官方 HTTPS 地址及 SHA-256，在临时文件校验成功后才替换目标；重复执行验证已安装文件，不重复下载。`public/vendor/` 不提交源码；Vite 将其复制进 `dist/vendor/`。没有新增 npm 依赖，不自动安装 SDK，不使用 CDN 运行时回退。

浏览器仅接受 `/vendor/aliyun-rtc-sdk-7.1.9.js` 和 `sha256-gQAzg1HOYiodJeJplyR+VbCRNfRYuQ50iZWzV84m43Y=`，通过 SRI 加载；未安装 / 哈希不符会明确不就绪。SDK 来源与原接收代码 MIT 许可见 `THIRD_PARTY.md`。本地已验证静态下载、哈希及第二次幂等执行；未使用它发出真实 RTC 请求。

固定 SDK 静态源可定位生产 `wss://rs.rtn.aliyuncs.com/roomserver/v1/ws`、`https://rs.rtn.aliyuncs.com/roomserver/v1/ws/version` 和 `https://gw.rtn.aliyuncs.com/gslb/v1/stsupdate`。这只是源码证据，不能证明它们足以完成真实接通。后端默认 CSP 仍须保持最小权限，真实允许源按单独审查配置，不放开全局通配符或所有遥测域；具体已实现策略以服务端契约为准。

接收区域置于 9:16 构图参考框内；数字人仍仅占用户选定 16:9 / 4:3 横向子区域，默认未选，不裁剪（`object-fit: contain`）。其他游戏画面未合成，此处不是 P2 直播画布。

Phase 2 测试定位：`performance-session-real-create`、`performance-paid-confirm`、`performance-media-start|stop|reconcile|confirm-ready|state`；`receiver-start|audio|leave|status|video`。测试通过测试脚本拦截固定 SDK 的 appendChild 并检查 URL / integrity、模拟轨道与帧证据；生产代码没有 mock / 绕过开关。浏览器 mock 不发真实 RTC 请求，不构成媒体质量证据。

接收器在 SDK 实例化前强制 `AliRtcEngine.setLogLevel(AliRtcLogLevel.NONE)`（固定版本常量为 5）。这关闭 SDK 控制台日志，但静态源码显示其内部仍可能缓存遥测日志；因此不能据此声称第三方 SDK 完全不记录信息，也不为日志 / 遥测域放开 CSP。真实联调前仍需审查供应商内部遥测边界。

### 唯一 Vidu 音画来源（同会话 / 频道 / 远端参与者）

最终音频与视频必须都从同一 owned Vidu live 的 managed RTC 输出接收。百炼 TTS 只在服务端生成 PCM，再连同已校验动作送入该 Vidu 会话；浏览器不直接下载、播放或混入百炼源音频。SDK 使用独立 audio 元素播放 RTC 音轨，并不意味着另接百炼声音。

接收器现在关闭“自动订阅所有远端”并在 join 前静音。它检查 SDK `channel` / `userId` 与受保护 claim 的频道和本地接收身份一致，锁定唯一非本地远端 UID，仅显式订阅该 UID 的摄像头轨与音轨。通过固定版本公开 `getAudioTrack(uid)` 与 `getVideoTrack({userId: uid, streamType: 0})` 获取同 UID 的原生 `MediaStreamTrack`，视频元素的 `srcObject` 必须包含相同视频轨，而非任意全局 PC 最近发出的轨道。用户开启声音只解除这个 UID 的静音。

出现第二 UID、本地接收 UID 被当作发送者、频道不匹配、绑定参与者离线或非空视频元素装载了不同轨道，立即静音并闩锁失败、上报 false、离开接收；不自动换人或重连。同一次接收尝试不能重新开始。它仍只是浏览器 client_reported 证据；不能凭此宣称真实供应商输出或观众收听已经验收。

不要求音视频必然使用同一个 JavaScript `RTCPeerConnection` 对象，也不允许凭“两条合成 PC 能录制”放松来源约束。固定 SDK 内有 Subscriber / Publisher / CommonController、复用及重连和能力检测路径；不能由 PC 对象数量推断供应商身份。需验证的是同一个 owned live 的 RTC grant、同一频道、同一远端 UID 的两条音视频轨及唯一接收者。

源码依据（固定提交 `2e076b66ebd7b1db25d8c20914713ab7f4983f77`）：
- `avatar-studio/studio/automation.py:reply_once` 调用大脑生成计划；`BailianAI` 继承结构化规划实现。
- `live_session.py` 合成并校验 PCM，`prepare_timed_actions` 基于同 PCM 时间戳只调整动作锚点，`process_answers` 调用 `send_aligned_expression`。
- `motion_component.py:send_aligned_expression` 与 `component_live.py:_audio` 将动作 / Type10 台词 / PCM 写入 owned live 的同一组件连接；创建响应提供该场 `managed_rtc`。
- 原 `static/player.js` 使用这份 RTC grant 入会，音画都来自 RTC；但原来的全订阅与按全局 track.kind 观察方式本身没有验证相同远端 UID，不能当作来源保障。
- 固定 SDK 7.1.9（同 SHA-256）公开 `getAudioTrack(uid)`、`getVideoTrack({userId,streamType})`、`getOnlineRemoteUsers()`、`subscribeRemoteMediaStream(uid,1,true,true)`；内部 remoteUser 按 UID 保存并返回原生轨道。

跨场切换还必须等待旧接收者的 claim / join 尝试退出，以及旧 SDK `leaveChannel` / `destroy` 清理完成。清理期间新场领取按钮与入口函数均拒绝开始；同一 teardown 共用 Promise。旧 generation 的迟到回执不得关闭新 claim、清空新视频或修改新场忙态。join 等待仍使用既有 20 秒上限，不增加自动重试。
