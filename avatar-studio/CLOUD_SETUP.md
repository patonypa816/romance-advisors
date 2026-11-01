# Cloud 准备（不启动演出）

适用仓库：`patonypa816/hell-inc-game`。先检出
`codex/avatar-cloud-prep`（从数字人迁移基线
`289c33bef9ebb575827e649c70878d89549c5e50` 创建的准备分支）。
不要在缺少 `avatar-studio/` 的 main 或旧 M3 检出上运行。
此工作只补数字人运行准备，**不实现 M4 游戏演出接口**。

## 一次任务中的准备

从仓库根目录运行：

```sh
sh avatar-studio/tools/prepare_cloud.sh
```

脚本使用自身位置解析路径，可重复运行：复用 `avatar-studio/.venv`，
按 `avatar-studio/requirements.txt` 安装 aiohttp、Pillow、NumPy；
已匹配 SHA-256 的 RTC SDK 不重复下载。未匹配时只下载到临时文件，
核对仓库已有的 7.1.9 SHA-256 后原子替换，失败不覆盖旧文件。
Python 依赖仍沿用原有版本范围，未声称锁定可复现版本。

基础工具：Python 3.11 或更新版本（含 venv/pip；运行代码使用 asyncio.timeout）、sh、curl、sha256sum 或 shasum。
包安装需要包源访问，RTC 文件需要 `g.alicdn.com`。
`ffmpeg`、`ffprobe` 用于音频转换，脚本只报告缺失，不自动安装系统软件。
当前默认百炼大脑不要求历史 CloudBase Node helper；只有要运行该 helper 的离线测试时使用：

```sh
sh avatar-studio/tools/prepare_cloud.sh --with-legacy-node
```

该选项需要已有 Node/npm，在 `studio/sdk` 执行
`npm ci --ignore-scripts --no-audit --no-fund`。不会启动 helper 或请求模型。

脚本不读取或 source `.env`，不读取凭据文件，不打印密钥，不启动服务、
自动播放、开云场或创建资源。默认不安装游戏依赖、不修改游戏数据库。
如网络策略阻断安装或下载，报告具体目标，勿绕过限制。

**任务内执行脚本 ≠ 已发布 Cloud 环境设置。** 本次任务的文件和依赖
不保证带到新任务；若要所有新任务自动准备，应由用户在 Cloud UI
选择此仓库/分支，并将上面命令保存为环境 setup。脚本本身不会发布、
更新环境配置或设置任何 Secret；GitHub Secrets 也不会自动同步至 Cloud。

## 用户在 Cloud UI 配置

- 保留现有 `DASHSCOPE_API_KEY` 网络 Secret；代码仅当
  `BAILIAN_API_KEY` **完全未设置**时将它视为别名。
- 配置 `BAILIAN_WORKSPACE_ID` 环境变量，必须与百炼密钥对应。
- 配置 `VIDU_API_KEY` 为网络 Secret，允许目标 `api.vidu.cn`。
- 保留百炼已允许的实际 workspace 北京域名：
  `{workspace}.cn-beijing.maas.aliyuncs.com`。不要扩大为任意域名。

显式 `BAILIAN_API_KEY` 优先，包括空串/无效值：它们仍报错，不被别名掩盖。
密钥（任一名称）和 workspace 必须成对提供；只提供一个不会回退混用
本机凭据。来源可用性门禁与实际读取使用相同别名规则。没有任何环境覆盖
时保留原本机凭据文件回退，不复制原机文件。无需把真实值发给助手或提交 Git。
以上是配置要求，**不是已经完成配置的声明**。音频下载、WebSocket、RTC
后续可能需要额外目的地；必须根据实际服务返回单独验证，不能据此默认放宽。

## 目录与资产

- Python 服务：`avatar-studio/studio/server.py`；页面资源：
  `avatar-studio/studio/static/`。
- RTC 7.1.9 文件实际落在
  `avatar-studio/static/vendor/aliyun-rtc-sdk-7.1.9.js`，
  由 server.py 的专用路由提供；不要误放到 `studio/static/vendor/`。
- 默认底图：
  `studio/assets/study-facing-fresh-smile-720x1280-v7.png`。
- 保留的另一张底图：
  `studio/assets/study-facing-subtle-smile-exact-9x16-918x1632-v5.png`。
  仅有图片并不等于绑定了云端形象资源。
- 本地运行目录：`studio/data/`（Git 忽略）。形象目录为
  `avatar-library.json`，缓存为 `avatar-images/<sha256>.<extension>`；
  待机规则为 `idle-action-library.json`。
- `local.json` 包含控制/播放器 token，不属于可公开导入的非敏感设置；
  `runtime.json`、runs、会话、日志、RTC/推流凭据也不得搬入 Git。
- `examples/idle-action-library.example.json` 和
  `examples/persona-settings.example.json` 是脱敏样例，
  ID 是占位符，不可当真实资源使用。

## 非敏感配置恢复与验证计划（尚未执行）

1. 本地工作者在原项目内筛出必要的角色设定、已审阅动作规则及既有资源
   关联；保留原件，不把整个 data 或凭据搬来。只在 Git 外恢复真实 ID。
   不新建云资源，不改变已调好的指令、停留/恢复时长、发送与队列/帧规则。
2. 先离线检查 JSON 结构、类型、大小和图片路径/摘要；动作规则必须通过
   现有 IdleActionLibrary 校验（版本、ID、描述 SHA-256、三行指令、
   hold/cooldown/weight 等），形象必须使用现有 AvatarLibrary 的格式、
   图片和设置校验。不得用导入“修复”破坏文件或绕过损坏配置拒绝。
3. 人设通过现有设置验证入口填写，不能将样例直接覆盖 local.json。
   形象用既有本地导入/选择流程，核对选中资源、可用底图及 settings。
   占位 ID 继续保持未绑定，不能伪装为已验证。
4. 恢复真实 ID 后，经允许的只读同步核对现有云资源：形象匹配、
   动作库 ID 与 avatar_id 绑定、动作名称和 description_sha256 一致。
   不自动调用同步：这需要用户已配置凭据且在允许的网络范围内。
   保留默认关闭/visual_verified 状态；真实视觉验收前不得标为已验证。
5. 到此只能声称配置准备完成。真实播放还需浏览器/RTC、音频链路及 OBS
   等环境；开场、TTS/模型、素材创建和收费会话不在本准备脚本中。
   不能把发送成功当成观众实际听完/看完。

## 离线回归

只用 dummy 环境值；测试不读取真实密钥或调用模型：

```sh
cd avatar-studio
.venv/bin/python -B -m unittest discover -s tools -p 'test_cloud_prepare.py'
(cd studio && ../.venv/bin/python -B -m unittest test_credentials_env test_bailian_config)
sh -n tools/prepare_cloud.sh
sh -n tools/fetch_vendor.sh
```

shell 测试使用临时目录和假 curl/hash/python，不会下载或安装。
覆盖 SDK 已验证跳过、重复运行、校验失败保留旧文件、临时文件清理、
复用 venv、不加载 .env、不启动服务。真实 SDK 下载和完整演出链路仍需在
选定 Cloud/媒体环境中验证；离线通过不替代它们。
