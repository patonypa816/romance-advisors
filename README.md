# 恋爱军师团与数字人工作台

本仓库整理《恋爱军师团》本地游戏及其 `ai-live-avatar` 数字人底座。游戏负责征集、投票、人物聊天和结算；Studio 负责模型回复、语音、动作及 Vidu 音画连接。游戏、Studio 所属的 `ai-live-avatar/` 与可选评论采集器 `dyhub/` 应保持当前同级目录结构。

这是代码恢复包，不包含原机器的数据库、凭据、资产库、音色绑定或录制文件。启动本地服务不会自动开收费云场；创建真实游戏剧情会调用模型并消耗额度或产生费用，自动联调还会使用 TTS 和 Vidu。开始真实调用前请核对账户及路由。

本仓库保留旧 HELL INC 的提交历史、分支和 PR。数字人工作台基于 [Gary 的 ai-live-avatar](https://github.com/garycaogan/ai-live-avatar)。

相关项目：[天工开帧 · 赛博小雅直播版](https://github.com/cyber-xiaoya/cyber-xiaoya-live)，可参考它如何将观众弹幕接入 Vidu 数字人直播。

## 《恋爱军师团》当前定版角色

《恋爱军师团》在 GitHub 开发和测试，实际节目从本机工作台运行。两处共用这份源码和角色定版记录。

| 项目 | 定版值 |
| --- | --- |
| Vidu 角色名称 | 咖啡馆里的温柔凝视 |
| Vidu 资产 ID | `1015851675149152256` |
| 百炼 TTS 音色 | `longtian_v3.1` |
| 游戏展示图 | `romance-advisors/static/assets/preview-male-3x4.png` |
| 展示图 SHA-256 | `30294e6e775dcdb55aa689bdd290256ecf2cfe06a3a967c0c27de52d5a9cef00` |

2026-11-05 已在本机 Studio 的状态接口核对选中资产与音色。运行 `shasum -a 256 romance-advisors/static/assets/preview-male-3x4.png` 可核对游戏展示图。

首次配置本机 Studio 时，在资产库同步并选择表中的 ID，按[配置示例](config-examples/README.md#资产与音色)绑定音色，再核对选中资产与音色。Studio 独立试验可自行选择其他角色。

## 目录

| 路径 | 用途 |
| --- | --- |
| `romance-advisors/romance/` | 单进程 SQLite 游戏、模型适配和数字人桥接 |
| `romance-advisors/static/` | 工作台、手机预览、透明叠层及字体 |
| `ai-live-avatar/studio/` | 当前数字人工作台、源码、测试和必要静态资源 |
| `ai-live-avatar/live/`、根 `server.py` 等 | 保留的上游底座入口；游戏联调使用 Studio 入口 |
| `dyhub/` | 从原机器单独收录的 DyHub 采集源码，保留本机修改；依赖与运行数据不入库 |
| `config-examples/` | 无真实密钥的本机配置模板 |
| `HANDOVER.md` | 当前行为、恢复边界和后续检查 |

## 1 配置本机环境

支持当前 macOS 使用方式；源码使用 Unix 文件锁，`.command` 使用 zsh。安装 Python **3.11 或更新版本**、FFmpeg（须同时提供 `ffmpeg` 和 `ffprobe`），以及可播放音视频的浏览器。Node.js 用于前端测试和可选评论采集；运行 DyHub 需要 Node.js **20 或更新版本**以及 Chrome/Chromium。Node.js 不是正式百炼 HTTP 大脑的必需运行环境。

从仓库根目录执行下列命令；如果解释器名称不同，换成实际安装的 Python 3.11+：

```sh
python3.11 --version
ffmpeg -version
ffprobe -version
python3.11 -m venv romance-advisors/.venv
romance-advisors/.venv/bin/python -m pip install -r romance-advisors/requirements.txt
python3.11 -m venv ai-live-avatar/.venv
ai-live-avatar/.venv/bin/python -m pip install -r ai-live-avatar/requirements.txt
sh ai-live-avatar/tools/fetch_vendor.sh
```

两个虚拟环境分别安装项目依赖，不复制旧机器的 `.venv`。最后一步从供应方下载 AliRTC Web SDK，并校验固定 SHA-256；该 SDK 未随本包重新分发，播放器需要下载后的 `ai-live-avatar/static/vendor/aliyun-rtc-sdk-7.1.9.js`。

版本依据：本副本 140 个 Python 文件均通过 Python 3.11 语法解析，且正常路径使用 `asyncio.timeout`。这不等同于已在所有 Python 版本与系统上运行验证。

## 2 配置外部凭据引用

密钥由用户自行填写，文件放在仓库外。不要把密钥写进源码、命令行参数或 Git。Studio 当前仍从这个历史相对位置读取**凭据文件路径**：

```text
artifacts/zhuyuanzhang-qwen31-libai-test-20261029/local-settings.json
```

这个路径位于仓库根目录下，不在 `ai-live-avatar/` 内；需要保留其名称才能匹配现有代码。建立引用文件：

```sh
mkdir -p artifacts/zhuyuanzhang-qwen31-libai-test-20261029
cp config-examples/local-settings.example.json artifacts/zhuyuanzhang-qwen31-libai-test-20261029/local-settings.json
chmod 600 artifacts/zhuyuanzhang-qwen31-libai-test-20261029/local-settings.json
```

自行编辑复制后的文件，把两个占位路径改为真实的**绝对路径**，不要使用 `~`。普通百炼凭据 CSV 使用纵向键值行，格式见 [配置模板说明](config-examples/README.md)；Vidu 凭据文件只存用户自己的单个密钥。两份真实凭据均留在仓库外，并限制为仅本人可读。

游戏默认复用同一百炼 CSV 引用；也可通过 `ROMANCE_CREDENTIALS_FILE` 或游戏服务的 `--credentials-file` 指定另一份外部 CSV。模板本身不能用于真实调用。

### 可选 Token Plan

只有确认使用该套餐时，才复制 `config-examples/qwen-route.example.json`，分别保存到：

- `romance-advisors/data/qwen-route.json`：游戏文字模型路由。
- `ai-live-avatar/studio/data/qwen-route.json`：Studio 文字模型路由。

创建所需 `data/` 目录，再把 `api_key_file` 改为仓库外 Token Plan 密钥文件的绝对路径；文件只放自己的套餐密钥。两个路由独立配置，不会自动相互同步。显式配置套餐路由后，错误配置或缺失套餐密钥会报错，不自动回退普通 API；游戏显式传入 `--credentials-file` 的调用保留原 CSV 接口。

Token Plan 路由只改变文字模型的账户/计费入口，不覆盖 Studio TTS 的普通百炼 CSV，也不覆盖 Vidu。消耗套餐额度不代表 TTS、Vidu 或所有调用免费。模型默认值及可选 `bailian-brain.json` 位置见模板说明。

## 3 先启动 Studio 再启动游戏

这台 Mac 的正式节目从 [开播准备.command](开播准备.command) 进入。双击后按已保存的 OBS 专用节目源端口启动或复用 Studio、DyHub 和游戏服务，打开 OBS 虚拟摄像机、直播伴侣及工作台。首次需已有竖屏场景、BlackHole 路由和已构建的新版 DyHub。伴侣内核对音画后由操作者点开播。以下手动入口供单独运行和排障。

第一个终端从仓库根进入底座，以预览模式启动：

```sh
cd ai-live-avatar
.venv/bin/python -B studio/server.py --preview
```

打开终端输出中的本机工作台 `url`，检查模型配置和数字人资产。终端中的工作台、播放器 URL 含本机访问令牌，不要提交或公开。只启动服务、打开页面不创建付费 Vidu 会话。仓库保留了一张启动必需的默认底图，避免首次打开失败；它不代表已恢复原机器选择的数字人资产或音色。

第二个终端从仓库根进入游戏：

```sh
cd romance-advisors
.venv/bin/python -B -m romance.launcher
```

游戏启动器会打开本机页面，数字人桥接默认读取同级 `ai-live-avatar/studio/data/runtime.json`。上述命令把工作台预览设为数字人接收端。

**使用 OBS 输出节目时**，从两个项目各自目录启动：在 `ai-live-avatar/` 执行 `.venv/bin/python -B studio/server.py`，在 `romance-advisors/` 执行 `.venv/bin/python -B -m romance.server --obs-output`。以游戏终端打印的本机地址打开工作台。给 OBS 浏览器源填写同一游戏地址加 `/static/obs-output.html`，设置为 1080 × 1920、30 fps。每次使用随机端口重新启动游戏后更新来源地址。将 OBS 虚拟摄像机输出设为节目（Program），在抖音直播伴侣竖屏场景中选择 OBS Virtual Camera。手机预览的对照框、抖音底图和排版示例仅用于工作台。[手机预览与节目输出](romance-advisors/docs/04-本地工作台使用.md#916预览与透明输出)

在 Studio 同步并选择用户自己的 Vidu 资产；创建新资产或开启会话可能产生费用。音色使用本机 `data/local.json` 中的 `tts_voices` 映射，按模板说明配置自己的合法音色；该映射未随 Git 保存。不要用模板整文件覆盖自动生成的 `local.json`，其中包含访问令牌和其他设置。

正式工作台用已保存的公开房号等候开播。发现同一平台直播场次、间隔至少 5 秒复核后开始收弹幕和已配置的礼物，每场直播有独立双榜。明确下播后，工作台断开本场采集并结束归属本场的数字人，显示停场结果。点「启动数字人」才开始计费口播，点「开始游戏」切入游戏，仍可手动结束数字人。详见[本地工作台使用](romance-advisors/docs/04-本地工作台使用.md#正式直播的数字人和游戏开关)。

工作台首页的「约会前照镜子」支持六档本地试玩，无需启动数字人。真实礼物模式需配置房间、礼物 ID、六档可驱动资产和双向 HTTPS 转场。[操作说明](romance-advisors/docs/04-本地工作台使用.md#约会前照镜子)

停止真实联调时，先用游戏页面的停止按钮并核对云端结束状态；之后退出本地服务。不要把杀进程当成已经结束收费云场的证明。

### 可选：启动仓库内 DyHub

从仓库根目录手动安装依赖并构建，首次运行前确认 Node.js ≥20 与 Chrome/Chromium 可用。这里沿用本机已验证的禁用安装脚本方式，锁文件保留原有依赖与镜像地址：

```sh
cd dyhub
npm ci --ignore-scripts
npm run build
```

然后双击 `ai-live-avatar/studio/启动评论采集.command`，或从仓库根目录执行：

```sh
zsh ai-live-avatar/studio/启动评论采集.command
```

该启动器使用仓库内 `dyhub/`，固定为浏览器内核，管理端只监听 `127.0.0.1:8757`。缺少构建、Node.js 版本不符或端口占用时退出，不自动安装、不接管其他服务。打开管理台本身不等于订阅直播间；由操作者在游戏或 Studio 明确连接自己的授权房间。结束采集在该终端按 Control-C，游戏“停止接收”只停止游戏订阅。

本机快照版本为 0.2.0，基于 `77ab1fbb3ac6cb0886af40cf32865d998d9e6aab` 并保留 5 个本机修改文件，来源与检查命令见 [本机 DyHub 说明](dyhub/LOCAL_IMPORT.md)。源码入库不恢复原机器的 Cookie、账号登录、浏览器资料或历史运行数据。确需登录时，由用户在本机 DyHub 管理台操作；不要将 Cookie 粘到聊天、源码或 Git。`dyhub/data/`、`node_modules/`、`dist/` 和日志保持忽略。轻量内核及其第三方签名源码一并保留，但此启动器不自动切换内核。

## 4 离线检查

下列命令使用项目测试入口，测试中的模型、TTS 和 Vidu 由合成夹具或本地模拟替代；不用于启动真实场次。测试会写临时数据，个别回归会在忽略的 `artifacts/` 下生成报告。

```sh
cd romance-advisors
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests .venv/bin/python -B -m unittest discover -s tests -p 'test_*.py'
```

```sh
cd ai-live-avatar
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=studio .venv/bin/python -B -m unittest discover -s studio -p 'test_*.py'
```

前端检查在 `romance-advisors/` 目录执行：

```sh
node --test tests/test_*.cjs
```

`studio/sdk/` 的 CloudBase helper 为历史适配与测试保留，若使用它再执行该目录的 `npm ci`，不提交 `node_modules/`。

测试通过、模型请求成功、音频或动作发送成功，都不等于观众已经听到声音或看见正确动作；实际音画、延迟和自然度需另做场内验收。

## 文档与许可

现行游戏说明在 [romance-advisors/README.md](romance-advisors/README.md)，底座说明在 [Studio README](ai-live-avatar/studio/README.md)。原文档中的 `artifacts/` 报告链接属于原机器本地证据，未随本包上传，不能当作仓库内可打开的附件。

保留 [ai-live-avatar/LICENSE](ai-live-avatar/LICENSE) 的上游 MIT 版权声明，以及 [字体 OFL](romance-advisors/static/fonts/OFL.txt)。DyHub 主项目保留 [MIT 许可](dyhub/LICENSE)；其轻量内核第三方签名组件另有 [AGPL-3.0 来源说明](dyhub/third_party/douyin-sign/README.md)及[完整许可证](dyhub/third_party/douyin-sign/LICENSE)，不能把整包笼统视为 MIT。再次分发、修改或提供网络服务时，应分别核对适用条款。用户账户资产、真实配置、历史数据库、录音录像和云端会话记录均需另行管理；从 Git 恢复代码不等于恢复这些状态。

