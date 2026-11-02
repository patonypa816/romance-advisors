# M4 第一步：游戏事实 → 原 Studio 主动安排 once

这是 **first-slice 手动 CLI 输入接缝**，不是自动事件驱动，也不是新演出引擎。三份 freeze 响应仅借用本轮已经存在的事实导出，不规定未来游戏输入必须永久经过这套 freeze 协议；后续自动接入另按实际需求收敛。原 Studio 独立运行，游戏不启动它、不创建控制 token、不配置持久授权、不新建云场。后续主持计划、TTS、完整音频下载、动作对齐、SGEQ、PCM 分包发送及停止均由原工作台负责。

仓内 Studio 副本现向游戏表达模块收窄：新的表达计划不包含 `reading` 和 `observation`，不再生成短读前导；正常动作 `recovery`、同次 PCM 与字时间绑定、S/GE/Q 不变。旧回放和历史记录仅在读取边界兼容，不改变已有文件。本机原口播工作台不受本仓修改影响。

## 已核对的旧合同

基准：`avatar-studio/studio/server.py`、`director.py`，提交 `2e076b66ebd7b1db25d8c20914713ab7f4983f77`。

- 既有本机控制页 `/studio/<control_token>/` 下 `POST director/submit`。
- `Origin` 必须与原 Studio 的 `http://127.0.0.1:<port>` 一致；保留原鉴权/本机边界，不接受播放器 URL、远程 URL、查询凭据或重定向。
- body 只有 `{"text":"1–600 字主持安排","mode":"once"}`，没有 `rounds`。
- 原 handler 要求音画已就绪且无待处理模型错误；once 返回 `{"id":"c0001"}`，不是播放完成回执。
- once 是主持内容安排，不是逐字朗读；原大脑生成表达/计划，由操作者在原工作台核对。输入禁止改变事实，不代表模型实际表达已通过验收。

## 默认只预览（无网络）

从当前受保护游戏 `/api/performance-sources/freeze` 导出同一比赛、同一版本的**两条 competition_comment 和一条 competition_result 响应**，按此顺序存为 JSON 数组。只使用已采纳点评及已结算结果，不重新评分、改变资产或修改点评。

```sh
PYTHONPATH=src python tools/studio_once.py --sources /private/game-freeze.json
```

工具完整拼接每个来源原有 segments，保留点评与结果事实；不为满足 600 字截断、拆单、追加队列或重新生成事实。超限就明确报错。保留 source ID、submission ID、版本、来源哈希，内容哈希只检测意外改动，不证明文件来自游戏服务；操作员仍须核对导出来源。主持安排中的点评没有额外猜测作者姓名，获奖姓名完全取既有结果文本。

## 后续经单独授权才提交

实际旧服务连接与收费执行本轮**未配置、未调用**。由操作者在 Studio 所在运行边界提供已有 `STUDIO_CONTROL_URL` 环境变量（控制页 URL 含敏感 token，不写入代码、参数、日志或公开页面），确认现有场次就绪、费用范围允许后：

```sh
PYTHONPATH=src python tools/studio_once.py --sources /private/game-freeze.json \
  --submit --receipt /private/new-once-receipt.json
```

`--submit` 会触发原 Studio 正常流程，可能立即调用收费模型/TTS/媒体，不是免费的连通性测试。工具不替用户开启 Studio 或场次。

回执文件以 0600 独占创建，网络请求前先持久写入 unknown、实际请求 body、body 哈希及来源引用，再更新 HTTP 状态和已验证的原 Studio id。只保存安全的 `error_type` 分类（如 timeout、connect、invalid_response），不保存控制 URL、原始响应或异常原文。已有同名回执拒绝发送；这不是跨文件全局防重，**不得换文件盲目重发**。未知、超时、非 200 或未知响应都停止，不自动重试。拿不到 id 时在原工作台按时间和内容核对。HTTP accepted 仅说明接纳安排；播放是否完成、计划是否保真必须另在原工作台与真实音画中确认。

本接缝不引用 `real_performance`、`avatar_compat`、`avatar_transport` 或 `record_only`，不新增服务、数据库表或通用回执系统。游戏页面不再展示旧媒体控制；此 CLI 仍是手动接缝，不代表已完成自动 UI 集成。历史场次仅保留受保护的 stop/reconcile 救援入口。

## 验证范围

新增 `tests/test_studio_once.py`：事实原样/来源不变、超长拒绝、哈希和比赛范围、原受控入口和 Origin、经源码核对的 once 返回格式、unknown 不重试/秘密不外泄、提交前私有回执及同文件拒重、新处理链未被加载。测试使用离线 fixture，不是模型、TTS、Vidu 或实际播放验收。

本次还将上述固定提交的原 `Director.submit` 临时加载到纯离线测试对象，实际验证本适配 body 被接纳、返回 `id`，且 once 附带 `rounds` 会被拒绝；只替代其 enqueue/event 存储对象，未启动原 server 或任何模型/媒体流程。
