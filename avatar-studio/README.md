# 数字人 Studio

来自本机 `ai-live-avatar/studio` 的御书房工作台副本，保留角色上下文、语义动作规则、模型编排、播放器、离线测试和两张使用中的形象底图；本机原项目没有移动。工作台与游戏独立，数字人表演不决定游戏评分或奖励。

`examples/` 保存脱敏后的四条待机动作规则和角色设定。动作库、形象和动作 ID 已替换为占位符：它们不是可直接启用的云端资源。需在本地重新关联真实资源并经过原有同步、摘要和绑定校验；人设样例可通过界面填写，不能直接覆盖运行状态文件。默认底图是 `studio/assets/study-facing-fresh-smile-720x1280-v7.png`；另一张是原本选用的形象，迁移后需重新选择。运行状态、会话、音视频、日志、本机密钥和其他历史素材未迁移。

## 环境与离线检查

需要 Python 3、`requirements.txt` 中的 aiohttp、Pillow、NumPy；语音转换还需要 `ffmpeg` 和 `ffprobe`。依赖须预先安装；以下检查本身不请求模型或开启云场：

```sh
python3 -m venv .venv
# 首次安装需访问包源；或使用预先准备好的离线包
.venv/bin/python -m pip install -r requirements.txt
cd studio
CLOUDBASE_ENV_ID=example-test-env ../.venv/bin/python -B -m unittest test_credentials_env test_cloudbase_ai
cd ..
```

这组凭据与历史 CloudBase 适配器离线测试已在迁移时通过。完整测试还包括绑定本地回环地址的 HTTP 合同测试；受限沙箱可能禁止绑定。逐字对齐测试依赖的实录素材因含会话内容未迁移，因此该组在缺失素材时跳过。Node SDK 功能测试需要先安装 `studio/sdk` 的依赖，本次未运行。

播放器需要阿里云 RTC Web SDK 7.1.9。此第三方文件不随源码分发；在允许访问网络的环境，可运行 `sh tools/fetch_vendor.sh` 下载并核对 SHA-256。真实播放还需要浏览器、RTC 网络、OBS；本机直播链路另需相应音频设备和直播软件。

本机启动：`.venv/bin/python -B studio/server.py`；使用工作台预览而非 OBS 接收可加 `--preview`。服务只监听本机，启动或打开页面不会自动创建 Vidu 云场；手动开场、模型筛选与回复、TTS、动作规划、Vidu 素材同步和会话可能产生外部调用及费用。结束后仍须按界面保护流程确认云端结束。不要将发送成功视为视觉或音频播放完成。

## 凭据与云环境

程序不自动读取 `.env`。由运行进程的环境注入下列变量；不要把真实值提交到仓库或写入日志：

| 变量 | 用途 |
| --- | --- |
| `BAILIAN_API_KEY` | 百炼文本大脑和 TTS API 密钥 |
| `BAILIAN_WORKSPACE_ID` | 百炼北京地域工作空间 ID；与密钥一起提供 |
| `VIDU_API_KEY` | Vidu 数字人组件 API 密钥 |

环境变量完整配置时优先使用；未提供对应变量时保留原凭据文件回退逻辑，但迁移后的相对路径已变化，原机文件不会自动跟随仓库迁移。只提供百炼两个变量中的一个会报错，不会混用另一来源。百炼请求发往 `{workspace}.cn-beijing.maas.aliyuncs.com`，Vidu 请求发往 `api.vidu.cn`；TTS 音频下载、WebSocket 和 RTC 还可能需要额外网络目的地，须按实际服务返回和运行环境验证。

历史 CloudBase 适配器并非当前默认大脑。若日后明确启用，必须自行提供 `CLOUDBASE_ENV_ID`、匹配的本地配置和私有凭据文件；迁移副本不包含原账户标识或凭据。

GitHub 仓库存储代码；GitHub Actions 仅在工作流显式引用 Secrets 时向任务提供凭据。Codex Cloud 调试则需在该环境配置变量和必要网络访问，两者配置互不自动同步。离线代码和单元测试不需要密钥。有限的真实 API 测试须单独授权并控制费用；本次迁移没有调用模型或验证云端媒体链路。
