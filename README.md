# 恋爱军师团 · 抖音互动游戏与 AI 数字人工作台

女生发来一句「你怎么还没走？」，男主该怎么回？在《恋爱军师团》里，直播间的观众就是他的军师。大家在弹幕里支招、投票，数字人面对镜头说出自己的想法，再低头给女生发消息。女生看到的只有正式聊天；她怎么回、亲密度怎么变，会成为观众下一轮支招的依据。

这是一套在 Mac 上运行的直播节目。可以只玩文字版，也可以接入 Vidu 实时数字人，用 OBS 合成竖屏画面，再从抖音直播伴侣播出。

[项目来历](#项目来历) · [游戏与改造方案](#游戏与改造方案) · [作者的数字人配置](#恋爱军师团当前定版角色) · [从零配置](#1-配置本机环境) · [OBS 与抖音](#obs-与抖音直播伴侣) · [离线检查](#离线检查)

## 项目来历

做这个项目时，我参考的原方案是天工开帧的「赛博小雅直播版」：观众发来弹幕，数字人能在直播中接话、做动作。《恋爱军师团》沿用这种互动思路，把观众的输入进一步变成有投票、有作者、有剧情后果的一轮游戏。[天工开帧 · 赛博小雅直播版](https://github.com/cyber-xiaoya/cyber-xiaoya-live)

代码上，数字人工作台基于 Gary（曹淦）的 `ai-live-avatar`。本仓库在 `ai-live-avatar/studio/` 中实现游戏表演、外接百炼语音和动作调度。抖音事件采集使用仓内的 DyHub 副本，保留了该节目需要的礼物、场次识别和重连处理。[Gary 的数字人底座](https://github.com/garycaogan/ai-live-avatar) · [DyHub 上游](https://github.com/ymstar/dyhub) · [仓内导入记录](dyhub/LOCAL_IMPORT.md)

| 时间 | 仓库里发生了什么 |
| --- | --- |
| 2026-11-01 至 11-04 | 最初用 HELL INC 做互动游戏实验，后来将《恋爱军师团》和配套 Studio 收入主分支。旧分支和 PR 仍可追溯。[初期设计](https://github.com/patonypa816/romance-advisors/commit/b5899934497a664f1272e12b9faaa0d8bb795416) · [游戏与 Studio 入库](https://github.com/patonypa816/romance-advisors/commit/1ade378ca6e87dec5f47495a523636282474690c) |
| 2026-11-05 | 接入抖音评论和终局评奖，定下咖啡馆男主与发消息动作，把人物、聊天、投票和榜单合成一个 OBS 节目页。[采集与终局](https://github.com/patonypa816/romance-advisors/pull/10) · [竖屏节目输出](https://github.com/patonypa816/romance-advisors/commit/21743e6f1c912629c24526f13873f96adb52f6fe) |
| 2026-11-07 | 增加开播准备入口、按抖音场次计榜和六档造型玩法，修复「投 1」等口令的识别。[对应版本](https://github.com/patonypa816/romance-advisors/commit/0812276784b7b84d8902fe67737c36ff9a0761ea) |

## 游戏与改造方案

**一场直播可以玩多期，一期故事可以有多轮聊天。** 每期生成一对虚构成年角色、各自的性格与当前处境，亲密度从 50 开始。以女生的开场消息为起点，每轮按下面的顺序进行。

1. **征集建议。** 观众可以投稿一句原话，也可以给行动方向。同一人先支招、后闲聊，两条都保留。征集截止后，模型从实际投稿中选出最多四项，每位作者最多占一个选项。冒险或可能翻车的建议也能入选。没有合适建议时，男主自拟回复。
2. **投票与裁决。** 观众发送对应编号，例如 `1`、`投1` 或 `投 1`。同一人以截止前最后一张有效票为准。男主通常采纳最高票，出现并列时程序从并列项中抽取一次。只有最高票与男主人设冲突时，才有 25% 的机会改选或自拟。关票后的票数和裁决都保存，刷新页面不会重抽。
3. **口播、发消息、结算。** 数字人可以对观众说出选择的理由。正式男主消息进入屏幕聊天，女生依据她知道的事回应。游戏把女生回复与分数变化一起写入 SQLite，再显示结果。亲密度到 100 为成功、到 0 为失败。游戏把被采纳的建议记回原作者，同一次抖音直播中的成绩可跨期计榜。

礼物驱动的是另一项**造型值**。例如在作者的一钻手测配置中，小心心（ID `463`）正向、抖音（ID `164`）负向；从 50 分累计加到 60，男主从灰卫衣换到精致档。亲密度仍由聊天结算。工作台的「开始免费试玩」可用模拟加减分看效果；真实收礼与云端换形还需要账号、房间和资产配置。[玩法与计分](romance-advisors/docs/01-产品需求.md) · [一钻手测步骤](romance-advisors/docs/04-本地工作台使用.md#一钻手测)

### 从哪里开始改

游戏采用单进程和 SQLite。模型写人物、选项摘要和台词；程序掌管窗口、票数、建议作者、已发消息与分数。保留这条分工，改剧情时就不容易让模型顺手改掉观众的票。

| 想改什么 | 从哪里下手 | 怎样检查 |
| --- | --- | --- |
| **换人物或节目题材** | 从 [model.py](romance-advisors/romance/model.py) 的 `prepare`、`draft`、`reply` 三组提示词入手。例如把咖啡馆约会改成久别重逢，沿用「观众献策 → 当事人选择 → 对方回应」。如果要改成职场谈判，还需在 [game.py](romance-advisors/romance/game.py) 和前端一起替换亲密度、胜负条件及文案。 | 用一条有明确作者的建议走完一轮，核对男主保留原意、对方只知道公开聊天、奖励归实际作者。 |
| **调快或放慢节奏** | 先在工作台的自动推进设置里调征集和投票秒数。要改平票或人设冲突规则，再看 [game.py](romance-advisors/romance/game.py) 的 `_decide()`。 | 用两位模拟观众试平票、改票、关票后再发票，然后刷新，核对最终结果和作者。 |
| **换节目版式** | [renderer.js](romance-advisors/static/renderer.js) 绘制聊天、选项与榜单，[scene-layout.js](romance-advisors/static/scene-layout.js) 定位人物和镜头。可先在工作台调边距和人物位置，再到 [OBS 节目页](romance-advisors/static/obs-output.html)检查效果。 | 用四个选项和一条长回复检查手机裁切，再到 OBS 看完整的 1080×1920 画面。 |
| **换礼物、门槛或造型** | 游戏侧配置正负礼物 ID 与分档门槛，计分和连击去重见 [appearance.py](romance-advisors/romance/appearance.py)。换脸或服装还需在 Studio 给同一角色绑定六档资产。 | 先模拟跨档再回退，确认仅造型值变化。真实礼物另核对收件数、去重、实际画面与恢复口播档。 |

## 《恋爱军师团》当前定版角色

下表是作者这套咖啡馆节目的配置记录。复现相同接法时，可以使用自己账号有权驱动的角色及音色。

| 环节 | 作者使用的方案 |
| --- | --- |
| 文字 | 游戏和 Studio 使用百炼 `qwen3.8-flash`、北京地域、关闭思考。作者为两端分别配置了 Token Plan 文字路由。 |
| 语音 | 普通百炼通道调用 `qwen-audio-3.1-tts-flash`，六档使用同一音色 `longtian_v3.1`。 |
| 人物与动作 | Vidu 实时数字人。Studio 取已确定的台词、合成语音并调度眼神、表情与局部动作。浏览器通过 AliRTC 接收云端音画。 |
| 普通口播 | 「咖啡馆里的温柔凝视」，黑针织衫托腮，Vidu 资产 ID `1015851675149152256`。六档中对应 `stellar`。 |
| 进入与退出游戏 | 启用真实六档时，开局从 50 分的 `normal` 灰卫衣开始。结束游戏后回到本场口播档 `stellar`。 |

| 0 · 鱼头 | 50 · 普通 | 80–100 · 巨帅 |
| :---: | :---: | :---: |
| <img src="romance-advisors/static/assets/appearance/fish.jpg" width="145" alt="鱼头完整遮住男主的脸"> | <img src="romance-advisors/static/assets/appearance/normal.jpg" width="145" alt="灰卫衣的普通造型"> | <img src="romance-advisors/static/assets/appearance/stellar.jpg" width="145" alt="黑针织衫的咖啡馆口播造型"> |

默认六档依次为 `fish`（0）、`rough`（1–19）、`tired`（20–39）、`normal`（40–59）、`refined`（60–79）、`stellar`（80–100）。图中是仓内预览素材。口播[展示原图](romance-advisors/static/assets/preview-male-3x4.png)的 SHA-256 为 `30294e6e775dcdb55aa689bdd290256ecf2cfe06a3a967c0c27de52d5a9cef00`，换机器后可据此核对。云端可驱动资产由各自的 Vidu 账号管理。

## 1 配置本机环境

作者使用 macOS；一键准备依赖 macOS 应用、zsh 和 Unix 文件锁。克隆整个仓库，让 `romance-advisors/`、`ai-live-avatar/`、`dyhub/` 保持同级。在希望存放项目的目录执行以下命令，需要 Python 3.11+ 和可用的 `ffmpeg`、`ffprobe`。如使用其他受支持的 Python 版本，替换命令中的解释器名。

```sh
git clone https://github.com/patonypa816/romance-advisors.git
cd romance-advisors
python3.11 -m venv romance-advisors/.venv
romance-advisors/.venv/bin/python -m pip install -r romance-advisors/requirements.txt
python3.11 -m venv ai-live-avatar/.venv
ai-live-avatar/.venv/bin/python -m pip install -r ai-live-avatar/requirements.txt
ffmpeg -version
ffprobe -version
sh ai-live-avatar/tools/fetch_vendor.sh
```

最后一步下载并校验 AliRTC 浏览器 SDK 7.1.9。只玩文字版时，安装游戏端的 Python 依赖、配置文字模型并启动游戏即可；接收抖音事件时，再准备 Node.js 20+、Chrome/Chromium 并构建 DyHub。

## 2 配置数字人账号、语音与造型

1. **准备两个外部凭据文件。** 按[配置模板](config-examples/README.md)在仓库外建立百炼 CSV 和 Vidu 密钥文本，由操作者填写。CSV 按行放 `apiKey`、`workspaceId`、`apiHost`，使用同一个北京工作空间；Vidu 文件只放一个密钥。真实文件设为仅本人可读。
2. **让 Studio 找到文件。** 它在下面这个历史路径查找 `local-settings.json`。首次从仓库根目录执行以下命令，再将文件中的 `bailian_credentials_file` 和 `vidu_credentials_file` 改成上述两个外部文件的绝对路径。这里保存的是路径，真实密钥留在仓库外。

   ```sh
   mkdir -p artifacts/zhuyuanzhang-qwen31-libai-test-20261029
   cp -n config-examples/local-settings.example.json artifacts/zhuyuanzhang-qwen31-libai-test-20261029/local-settings.json
   chmod 600 artifacts/zhuyuanzhang-qwen31-libai-test-20261029/local-settings.json
   ```

3. **选择文字路由。** 通常游戏和 Studio 可共用上述普通百炼凭据。纯文字游戏也可用 `ROMANCE_CREDENTIALS_FILE` 指向自有 CSV。若复现作者的 Token Plan 接法，将 [qwen-route.example.json](config-examples/qwen-route.example.json) 分别放到 `romance-advisors/data/qwen-route.json` 与 `ai-live-avatar/studio/data/qwen-route.json`，创建所需目录，填入仓库外套餐密钥文件的绝对路径（`api_key_file`）。Studio 可选的模型设置见 [bailian-brain.example.json](config-examples/bailian-brain.example.json)。两个项目的文字路由独立；显式配置出错会报错。语音仍走普通百炼，渲染仍走 Vidu。
4. **选角色并绑定音色。** 按下一节启动 Studio 预览，在资产库同步并选择有权使用的 Vidu 资产。正常停场、退出 Studio 后，按 [tts-voices.example.json](config-examples/tts-voices.example.json) 把「资产 ID → 音色 ID」合并进自动生成的 `ai-live-avatar/studio/data/local.json` 的 `tts_voices` 字段。保留原文件中的端口、令牌和其他映射。重启预览后核对选中资产和 `tts_voice`。新安装的通用兜底音色为 `libai_v3.1`。
5. **可选，启用真实六档换形。** 本地静态试玩可直接使用。云端换形需要自己的六个就绪资产、各自缓存原图的 SHA-256、同一音色绑定，以及云端能访问的升级和降级两支 HTTPS 短片。将[关闭状态的模板](config-examples/external-appearance.example.json)填写为 `ai-live-avatar/studio/data/external-appearance.json`，给这组人物设定稳定的 `character_id`，将 `broadcast_key` 设为开场选中资产所属的档位，准备齐全后设 `enabled` 为 `true`，在下次正常启动时读取。作者本机的资产库和远端短片地址由账号自行管理。[Studio 六档配置](ai-live-avatar/studio/EXTERNAL_APPEARANCE.md)

## 3 先启动 Studio 再启动游戏

**桌面预览**用两个终端，各自从仓库根目录开始。第一个启动 Studio，让工作台预览接收音画。

```sh
cd ai-live-avatar
.venv/bin/python -B studio/server.py --preview
```

打开终端打印的本机工作台地址。Studio 地址可能包含本机访问令牌，请保留在自己电脑上。第二个终端启动游戏。

```sh
cd romance-advisors
.venv/bin/python -B -m romance.launcher
```

游戏从同级 Studio 的 `studio/data/runtime.json` 找到数字人。想先了解规则，可在游戏工作台打开「本地调试」（`/?debug=1`），模拟两位观众的投稿和投票。「开始免费试玩」适合看六档外观；创建真实文字剧情会请求模型，点「启动数字人」会创建计费云场，口播会用到 TTS。[工作台使用说明](romance-advisors/docs/04-本地工作台使用.md)

## OBS 与抖音直播伴侣

作者在同一台 Mac 上运行游戏、Studio、DyHub、OBS 和直播伴侣。**画面**从游戏节目页进入 OBS，再由 OBS Virtual Camera 送给伴侣。**声音**从同一节目页进入 OBS 调音台，由 OBS 的监听设备送入 BlackHole 2ch（虚拟声卡），伴侣把它当作音频输入。**弹幕与礼物**走另一条路，由 DyHub 从本次直播间送回游戏。

### 首次配置 OBS

1. 安装 [OBS Studio](https://obsproject.com/download)、[BlackHole 2ch](https://github.com/ExistentialAudio/BlackHole) 和抖音直播伴侣。作者的准备器从 `/Applications/OBS.app` 和 `/Applications/Douyin Webcast Mate.app` 打开这两个桌面应用。若之前使用桌面预览，先正常结束云场、退出该模式的服务。在两个终端分别进入 `ai-live-avatar/` 和 `romance-advisors/`，前者运行 `.venv/bin/python -B studio/server.py`，后者运行 `.venv/bin/python -B -m romance.server --obs-output`。这次由 OBS 节目页独占数字人接收端。记下**游戏终端**打印的 `http://127.0.0.1:<端口>`。
2. 在 OBS 新建一个专用**配置文件（Profile）**和同名的**场景集合**，例如都叫「恋爱军师团」。把基础画布和输出分辨率都设为 **1080×1920**，帧率用 **30 fps**。集合内建一个节目场景。
3. 在该场景添加一个可见的**浏览器源**，来源名称精确填写 **恋爱军师团专用节目页**。网址填 `http://127.0.0.1:<游戏端口>/static/obs-output.html`，宽 1080、高 1920，勾选「通过 OBS 控制音频」。这个地址同时包含人物、聊天、投票和榜单；`/overlay` 只提供透明游戏组件。只让一个场景使用这个专用源，方便准备器识别。
4. 在 OBS 的「高级音频属性」把该源设为**监听和输出**，在 OBS 设置中将**音频监听设备**选为 **BlackHole 2ch**。把虚拟摄像机的输出选为 **Program（节目）**，启动虚拟摄像机。
5. 在抖音直播伴侣新建竖屏场景，视频来源选择 **OBS Virtual Camera**，音频输入选择 **BlackHole 2ch**。看伴侣里是否有完整的竖屏游戏界面；有实际声音时，看伴侣电平并试听，检查是否有静音或同一声音被桌面采集重复送入。可先本地录一小段回放，同时检查声音、人物嘴形和手机边缘裁切。

首次配置并检查完毕后，保存配置。若开了数字人，先确认云场已正常结束，再正常退出手动打开的 OBS。第一次双击「开播准备」时，让准备器用保存的配置文件、场景集合和场景名启动 OBS，以便它核对本次运行的确是专用节目。

**首次手动启动**由游戏分配空闲端口；**下次一键准备**从已保存的浏览器源 URL 读取端口，在同一地址启动或复用本项目。准备器要求专用源、场景和配置唯一匹配。若提示端口冲突或已有预览进程，先在空闲时正常退出原实例再准备。[OBS 配置检查源码](romance-advisors/romance/broadcast_apps.py)

### 接收抖音事件与日常开播

首次从仓库根目录构建采集器。仓内版本包含本项目使用的直播场次和礼物字段。

```sh
cd dyhub
npm ci --ignore-scripts
npm run build
```

随后双击根目录的 [开播准备.command](开播准备.command)。它检查已保存的 OBS 设置，启动或复用本机 Studio、DyHub 和游戏，准备虚拟摄像机并打开直播伴侣、游戏工作台。采集器管理地址是 `http://127.0.0.1:8757`；如需单独排查采集，可使用[采集器启动入口](ai-live-avatar/studio/启动评论采集.command)。需要登录态时由操作者在本机管理台处理。[DyHub 本机说明](dyhub/LOCAL_IMPORT.md)

| 操作者做什么 | 怎样确认 |
| --- | --- |
| 在游戏工作台保存自己的数字房号或直播页链接，在伴侣核对画面、音频来源后点击「开始直播」。 | 工作台检测开播，间隔至少 5 秒复核同一平台场次后接入。在自己的直播间发一条可辨认的弹幕，看工作台是否收到。 |
| 点「启动数字人」进入普通口播，需要互动剧情时点「开始游戏」。 | 在伴侣和观众端分别确认能听见台词、看见动态人物。游戏收票期间，用当前有效编号核对票数；同一场直播的正式榜可跨游戏期延续。 |
| 点「结束游戏」返回普通口播；整场结束时在直播伴侣下播。 | 游戏在连续确认本场下播后停止订阅，请求结束归属本场的数字人。看工作台回读的云端结束结果。若平台状态未知或掉线，用「结束数字人」处理并核对，再退出本机服务。 |

## 离线检查

以下命令分别从仓库根目录执行。测试用合成数据检查游戏规则、前端和 Studio，适合改完一部分后复查对应行为。

```sh
(cd romance-advisors && PYTHONPATH=.:tests .venv/bin/python -B -m unittest discover -s tests -p 'test_*.py')
(cd romance-advisors && node --test tests/test_*.cjs)
(cd ai-live-avatar && PYTHONPATH=studio .venv/bin/python -B -m unittest discover -s studio -p 'test_*.py')
(cd dyhub && npm run typecheck && node --import tsx --test test/*.test.ts)
```

在 11 月 7 日版本的离线回归中，Studio 有一项[音频发送节奏测试](ai-live-avatar/studio/test_prepared_pcm_overlap.py)因模拟工作台缺少 `performance_holding` 状态，在音频准备阶段退出。

现场要另外验证三件事：观众是否同时收到音画，人物低头与聊天气泡是否对齐，以及真实礼物是否触发预期造型并保持后续口播。发送回执只覆盖本机发送这一步。已知的重复碎念、气泡时序和礼物待验项见[交接记录](HANDOVER.md)。

## 源码、素材与许可

[游戏代码](romance-advisors/romance/) · [前端与节目页](romance-advisors/static/) · [Studio 工作台](ai-live-avatar/studio/README.md) · [DyHub](dyhub/) · [配置模板](config-examples/README.md)。程序运行后生成的数据库、登录态、令牌、录制和本机资产绑定由操作者保存在自己的机器上；旧文档中的 `artifacts/` 链接指向本地验收记录。

各组件保留自己的许可：[数字人底座 MIT](ai-live-avatar/LICENSE)、[字体 OFL](romance-advisors/static/fonts/OFL.txt)、[DyHub MIT](dyhub/LICENSE) 以及[第三方抖音签名组件的 AGPL-3.0 说明](dyhub/third_party/douyin-sign/README.md)和[许可证全文](dyhub/third_party/douyin-sign/LICENSE)。仓库根目录目前没有为整个组合项目声明统一许可证。
