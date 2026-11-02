# M4 阶段验收记录

> 本页是历史复制链的离线验收记录，不代表当前 once 路线或真实音画已通过。当前状态见 [HANDOVER](../HANDOVER.md)，接入方式见 [Studio once](m4-studio-once.md)。

日期：2026-11-02（UTC+08）。结论：**第二阶段代码与离线集成通过，M4 真实媒体验收未完成**。

## 精确证据

- 代码提交：`c0c7f94be9c5168992d97291d9af0655ecdc4713`。
- [Draft PR #6](https://github.com/patonypa816/hell-inc-game/pull/6)，基于 `codex/avatar-cloud-prep`；并未合入主线。
- [Actions 41340161273](https://github.com/patonypa816/hell-inc-game/actions/runs/41340161273)：153 项 Python、6 项浏览器检查全部通过，包含重复迁移、PostgreSQL、M1–M3 回归。
- Artifact `13097042718`：桌面与手机 Phase2 mock 截图已实际查看，无阻塞溢出或按钮布局问题；截图无真实数字人符合模拟测试目的。
- 第一阶段 `77f105a`：[Actions 41337298465](https://github.com/patonypa816/hell-inc-game/actions/runs/41337298465)，103 Python、5 浏览器通过。数量为累积，不能相加。

## 已覆盖

真实适配器实现对接假 HTTP/SSE、ffmpeg/ffprobe 和原动作调度器的假 socket，保留冻结原文、单次 TTS、动作对齐、PCM 哈希和原 S/GE/Q 发送时序。不存在用 FixtureAdapter 替换真实适配器再宣称后者测试成功。

持久创建尝试和 owned live ID、未知禁止自动重建、暂停与停止区别、正向 ended 回读、单接收者/epoch/TTL/人工恢复、秘密仅内存、显式代理与认证头隔离均有模拟或数据库回归。修复并覆盖跨场次历史关闭重放、迟到回调复活已关闭场次、迟到健康心跳自动恢复、旧回读异常暂停新场及取消边界。

固定 AliRTC SDK 7.1.9 静态下载、SHA-256 与重复安装验证通过；没有连接真实 RTC。浏览器测试以测试代码拦截固定路径/SRI的 SDK 加载并模拟轨道及解码帧，覆盖自动播放失败关闭门控、停止优先和状态回读。生产代码没有测试绕过开关。

## 未覆盖与剩余

- 未调用真实模型推理、TTS、Vidu 云场或 RTC；没有本次收费媒体验证或长期部署。
- 新人物“地狱集团首席执行官”只读已确认横版资产描述成功；尚未验证游戏发言、音色、动作、声画同步或新资产动作库。旧形象专属动作库不复用。
- RTC CSP候选源来自固定SDK静态证据，实际端点充分性、音频自动播放和供应商SDK日志/遥测行为仍需实际联调检查。
- 发送完成、客户端心跳、人工确认都不能单独证明外部观众听到。完整 OBS 与直播链路另验。
- 接收器结束后局部历史提示可能保留上次上报字样；权威media状态与禁用门控正确，是非阻塞文案观察。
- 实际收费测试需明确时长、预算、停止与异常兜底；应用定时停止不等于供应商硬计费上限。

## 失败与修正记录

`6789b828` 的首次 CI 在旧测试仍把 real mode 视为非法400处失败。Phase2已支持real但默认禁用，更新为409 real_media_disabled，并另保留非法mode400、原会话不变与组件未创建断言。

`ce1b33ac` 的153项Python和原5项浏览器通过，但新增浏览器mock只拦appendChild而产品使用append，导致SDK模拟未载入。修正测试拦截真实路径并校验SRI与单次加载后，`c0c7f94` 全部通过。未改变生产门控以迁就测试。

## 后续止费保护与一次真实短测

`3daa079ba11a55d1c3f2080e9a894ba0b11f043c` 在 [Actions 41343548981](https://github.com/patonypa816/hell-inc-game/actions/runs/41343548981) 通过172项Python、6项浏览器。覆盖持久stop latch、停止早于/交错启动、在途创建返回后挂断、重启只对DB-owned ID主动挂断、cleanup HTTP与正式workspace隔离。TTS默认严格保存SSE原始音频，不依赖未知下载host；损坏/截断不重合成。15秒目标脚本m4-live-smoke-v1为74码点单段，实际时长必须据一次真实TTS测量，不截音/填充。

2026-11-01 19:57:37 UTC一次真实start，约32.4秒后stop，19:58:13 UTC正向ended与cloud_end_confirmed=true。仅1场，0规划、0TTS、0台词发送；控制器错误receiver_evidence_timeout，未保存足够逐步诊断，不能确定具体接收失败环节。供应商24计费秒/24积分，最终账单未核实。已恢复默认关闭媒体/收费开关；无自动重试。

本场录像GET成功但enabled=false、无video_url。源代码与供应商回读共同确认录制未开启；没有保留实际创建请求体，所以不能宣称具有完整HTTP请求抓取证据。没有录像不证明Vidu未生成；renderer_ready也不单独证明可见视频输出。

后续修复将原Studio默认云端录制与自动下载分开，记录recording_requested而不伪称已保存录像；同时以SDK公开getAudioTrack/getVideoTrack按同远端UID绑定。新修复的通过状态以其精确提交CI为准。真实RTC、音画内容和浏览器证书问题仍待验证，不能将新增mock测试当作二次实际联调。
