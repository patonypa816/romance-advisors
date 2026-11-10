# DyHub · 本机抖音事件采集器

《恋爱军师团》通过本机 DyHub 订阅操作者有权接入的直播间。DyHub 把评论、头像、进房和礼物转换成统一事件；游戏端按直播场次去重、计票和记账。源码基于 [ymstar/dyhub](https://github.com/ymstar/dyhub)，导入基准为 `77ab1fb`。

## 启动

安装 Node.js 20+ 和 Chrome 或 Chromium。在本目录执行：

```sh
npm ci --ignore-scripts
npm run build
```

从仓库根运行 `ai-live-avatar/studio/启动评论采集.command`，或使用已配置的「开播准备」。浏览器采集器默认监听 `127.0.0.1:8757`。在游戏工作台保存自己的公开房号或直播页链接；如需登录态，使用自己的账号在本机管理台操作。[Cookie 配置](docs/cookie-guide.md) · [游戏接入说明](../romance-advisors/docs/05-抖音评论与头像.md)

## 事件与接口

| 入口 | 用途 |
| --- | --- |
| `GET /api/health` | 查询服务身份和功能。 |
| `POST /api/rooms/connect` | 用 `roomId` 连接自己的房间。 |
| `GET /api/events` | 订阅 SSE（服务器推送事件）；可用 `roomId`、`types` 筛选。 |
| `/ws` | 用 WebSocket 订阅事件，支持同样的筛选参数。 |

公开房号用于选择房间，平台场次标识区分同一房间的不同直播。事件含类型、来源房间和事件 ID；评论与礼物使用不同数据字段。请从接收事件中取得实际值，示例占位符由操作者替换。

## 来源与许可

DyHub 主代码使用 [MIT 许可证](LICENSE)，轻量内核中第三方签名组件的[来源说明](third_party/douyin-sign/README.md)和 [AGPL-3.0 许可证](third_party/douyin-sign/LICENSE)随源码保留。上游项目亦致谢 [skmcj/dycast](https://github.com/skmcj/dycast) 和 [saermart/DouyinLiveWebFetcher](https://github.com/saermart/DouyinLiveWebFetcher)。
