# 数字人底座

《恋爱军师团》使用本目录的 [Studio](studio/README.md) 作为数字人工作台。游戏安装、用户自行配密钥、OBS 和抖音直播伴侣的接线见[项目首页](../README.md)。这个目录也保留了上游独立工作台的源码和接口，供单独开发数字人应用时参考。

| 路径 | 用途 |
| --- | --- |
| `studio/` | 游戏使用的工作台。处理模型输出、TTS、动作计划、Vidu 连接和浏览器播放。 |
| `live/`、`server.py`、`static/` | 上游独立工作台、弹幕调度与舞台页。其控制接口见[工作台 API](WORKBENCH_API.md)。 |
| `tools/fetch_vendor.sh` | 下载并校验播放所需的 AliRTC 浏览器 SDK。 |
| `tests/` | 独立工作台的离线测试和模拟服务。 |

单独运行上游入口时，在本目录建立 Python 虚拟环境、安装 `requirements.txt` 并运行 `sh tools/fetch_vendor.sh`。用 `.venv/bin/python server.py` 启动本机服务，打开 `http://127.0.0.1:28900/workbench`。创建真实会话会使用操作者自己的 Vidu 账号。游戏联调请按 Studio 说明启动对应入口。

上游为 [Gary（曹淦）的 ai-live-avatar](https://github.com/garycaogan/ai-live-avatar)，版权声明见 [MIT 许可证](LICENSE)。
