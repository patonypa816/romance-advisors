# 本机 DyHub 源码副本

本目录用于放入 `patonypa816/romance-advisors` 仓库根目录，与 `romance-advisors/`、`ai-live-avatar/` 同级。它是普通源码目录，不是 Git 子模块，也不包含嵌套 `.git`。

## 来源与保留范围

源码来自本机已有的 `AI直播数字人/artifacts/dyhub-smoke-20261030/dyhub`，没有用网上的新版本替换。本机 Git 上游是 <https://github.com/ymstar/dyhub>，分支 `main`，基准提交 `77ab1fbb3ac6cb0886af40cf32865d998d9e6aab`；`package.json` 标记版本 `0.2.0`。原锁文件顶层版本仍为 `0.1.0`，依赖版本与镜像地址按原文件保留。

以下 5 个文件保留本机未提交的改动：

- `src/collector/browser.ts`：保留 Chromium 沙箱及正常自动化标记。
- `src/collector/collector.ts`：保留 `DYHUB_TEST_SKIP_META=1` 跳过主播资料额外请求的开关。
- `src/pipeline/normalizer.ts`、`src/proto/douyin.proto.ts`、`src/types/events.ts`：保留财富等级、成员动作及礼物字段的缺失值与显式零值处理。

本机尚未提交的 `test/normalizer.test.ts` 一并保留。`test/avatar.test.ts` 是本次新增的合成协议检查，验证评论中的头像 URL、回退及缺失情况，不访问直播间或真实头像。

没有导入运行目录、Cookie、账号数据、环境文件、日志、依赖缓存或构建产物。上游 agent 指令、计划、发布工作流和非必需控制台截图未引入。源码静态 UI 图标、构建配置、锁文件及相关文档已保留。

## 构建与本地启动

需要 Node.js 20 或更新版本，默认浏览器采集内核还需要系统 Chrome/Chromium。从本仓库根目录执行：

```sh
cd dyhub
npm ci --ignore-scripts
npm run typecheck
node --import tsx --test test/*.test.ts
npm run build
DYHUB_HOST=127.0.0.1 DYHUB_PORT=8757 DYHUB_COLLECTOR=browser DYHUB_TEST_SKIP_META=1 npm start
```

此命令让管理台只监听本机。只有显式连接房间才开始采集；本次整理没有执行启动命令，没有连接真实直播间。最终整合已将 `ai-live-avatar/studio/启动评论采集.command` 改为仓内 `../../dyhub`，并补充 Node.js 版本、缺构建及端口占用检查。目录中的 `integration.patch` 是导入时针对旧文档准备的历史交接补丁，已由整合改动取代，不应再次应用；当前操作以根 README 为准。

`data/` 中的运行状态由新实例自行生成；本次没有迁移旧实例的 Cookie 或其他状态。不要把旧运行目录整个复制过来。原 `.gitignore` 排除了 `data/`、`.env*`、`node_modules/`、`dist/`、日志和研究抓包目录；本次也为 `.dockerignore` 补充运行状态与环境文件排除项。

## 与头像功能的边界

采集器在标准事件 `user.avatar` 中提供头像 URL；它本身没有“参与本游戏”的概念，也不负责下载或长期保存图片。参与资格与保存时机由游戏消费端决定。本次只整理采集器，未覆盖游戏终局或头像桥接代码。

## 许可证

主项目的 [MIT LICENSE](LICENSE) 保留原文。`third_party/douyin-sign/a_bogus.js` 与 `sign.js` 按原源码保留，其 [来源说明](third_party/douyin-sign/README.md) 标注上游 `saermart/DouyinLiveWebFetcher` 的 AGPL-3.0；本次补充完整 [AGPL-3.0 LICENSE](third_party/douyin-sign/LICENSE)，不能把这些第三方脚本笼统标为 MIT。

完整许可于本次整理时从 <https://raw.githubusercontent.com/saermart/DouyinLiveWebFetcher/main/LICENSE> 下载，SHA-256 为 `7f8f48e4266aa8fd3033dfaa4bb7f6e83a60c6e099f82d4959b81de90e67cd8f`。保留上游说明不代表已经就整个组合项目完成法律判断；本次未改变仓库可见性或发布方式。

## 最终整合修正

保留上述 5 处本机源码修改后，另作了窄范围接入修正：SSE 显式重连会重建 closed/error 会话；直接运行时默认监听 127.0.0.1，Docker 示例端口只发布到宿主机回环地址，镜像使用非 root 用户且不关闭 Chromium 沙箱。Docker 路径未运行验收，不属于当前本机启动保证。启动器与恢复说明已完成仓内路径接入。
