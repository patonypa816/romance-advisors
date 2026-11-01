# M4：数字人规则复用与未接通边界

本页描述 **离线协议切片**，不是 M4 真实音画验收。HTTP 服务默认装配无音画 `FixtureAdapter`；Phase 2 另有双开关、显式配置及接收者门控的真实适配器代码，但本轮仅用模拟供应商验证，未请求真实模型/TTS/Vidu/RTC。`send_completed` 仅表示本次协议发送调用完成，不能解释为观众已听完。

## 选择性复用与许可证

来源为同一仓库 `patonypa816/hell-inc-game` 提交 `2e076b66ebd7b1db25d8c20914713ab7f4983f77`，由 GitHub 只读接口读取。`src/hell_inc/avatar_rules` 的两份 Python 文件逐字节保留原文件（通过 Git blob SHA 核对）：

| 本地文件 | 原文件 | Git blob SHA | SHA-256 |
| --- | --- | --- | --- |
| `avatar_rules/action_schema.py` | `avatar-studio/studio/action_schema.py` | `a0e2264c660d08c4a4cf4e5628fe0918a21c51f7` | `8d16558e7f6f2d992a197fd3e607b8ae1cada05fb4d8abe62b3c8b5f45dc5b23` |
| `avatar_rules/semantic_speech.py` | `avatar-studio/studio/semantic_speech.py` | `7842e4e08ecc49b85e8ac8cc1dc0c86b8ef9fb8d` | `0e5163849db2994f14229cdd65120a92405ef36bcf0eba700ab2e898a1fbfc72` |

原 `avatar-studio/LICENSE` 是 MIT，Copyright (c) 2026 Gary (曹淦)。许可证全文随 `avatar_rules/LICENSE` 保留，并嵌入 `avatar_rules/__init__.py`，以便仅打包 Python 文件时亦不丢失声明。只复用自包含纯解析器/编译器，未导入完整 Director、Automation、Studio UI、LiveSession、云客户端或形象绑定。更新规则时必须显式复核来源、同步快照及测试，不静默跟随旧工作台变化。

## 冻结内容与两阶段准备

`avatar_compat.freeze_stage1(frozen_text, plan)` 要求 `plan` 只有 `text/instruction/actions/recovery` 四字段，台词必须逐字相同。沿用原动作/恢复规则校验，但不采用旧 `_text` 的去空白返回值：游戏台词的空白也保持原样。不可变对象持有 JSON 字符串，外部修改原 dict 不影响冻结值。

`prepare_semantic(stage1, synthesize, plan_anchors, cancelled)` 是**未装配的依赖注入接缝**：

1. 注入的合成函数只接收冻结四字段，调用一次；必须返回 `pcm/alignment/sample_rate/channels/sample_format`。仅接受 `bytes`、24 kHz、单声道、s16le、整采样且不超过 30 秒。字节本身不能证明编码格式，格式由合成依赖显式声明；真实供应商解码链还需独立验收。
2. SHA-256 绑定这一份未经补静音、裁剪或重新合成的 PCM。存在动作时，先调用原 `LocalActionAlignment` 验证其归属，再调用第二阶段函数。
3. 第二阶段只允许锚点变化；动作数量、motions、gaze、expression、恢复姿态、instruction、text 全部不能修改。锚点必须原文连续、可覆盖、可唯一核验；不猜词时间。
4. 使用原 `compile_dialogue_semantic_timeline` 编译全部动作，再允许发送。无动作时不调用第二阶段、不要求无必要的动作对齐、不额外发 Q。

`bind_audio` 可用既有离线数据独立验证相同边界。`PreparedSemantic` 持有不可变 PCM、内容、对齐与完整时间线；`send_prepared` 会重新校验其一致性。

## 保留的工作与发送规则

| 规则 | 本切片执行位置/限制 |
| --- | --- |
| 正式对话 S，而非历史 L；GE 提前 1.1 秒；S → Q 1.5 秒；Q 为“正常坐姿” | 原纯编译器实际复用；历史 L/.5 编译器保留但本接缝不调用 |
| 成功 Type9 间隔至少 1 秒，不能压缩 Q 到下一组间隔 | 原纯编译器验证窗口；真实写入节流属于原 `MotionComponent` |
| Type9 等待不得占住 PCM/Type10 socket 锁；实际首 PCM 时间与发送字节进度共同约束 | 只读核对 `motion_component.py` 的 `_write_motion`、`_run_aligned_remaining`；本切片不重写调度器、不宣称其运行时验证完成 |
| 内容持有发送权时禁止 idle；成功 idle 写入后才推进 cooldown；受保护 Q 恢复；旧 epoch 失效 | 只读核对 `idle_motion.py`；未装配 idle，未复制旧形象资源关联，未宣称新形象的 idle 已验证 |

`send_prepared(prepared, owned_send_aligned_expression, still_valid)` 只委托调用方已经拥有的原 `MotionComponent.send_aligned_expression`，显式传递 S、1.1、尾 Q 与本地锚点选项；不直接写 Type9，不绕过恢复/所有权检查，不自行创建 session。Phase 2 的 `RealPerformanceAdapter` 已通过这个接缝委托原 scheduler；默认仍禁用真实调用，**不代表真实媒体验收通过**。未来调用方仍必须证明会话 epoch、唯一 sender、唯一 RTC 接收者、接收就绪及持久发送闸门；函数存在不等于这些条件成立。

## 离线 Fixture 与真实媒体分离

`FixtureAdapter` 的 `prepare_hook`/`send_hook` 仅用于注入测试屏障，不用 sleep 冒充音频时长。返回 `fixture_receipt`，明确 `audio_generated=false`、`provider_called=false`、`media_verified=false`、`audience_heard=false`；不会生成 PCM 或供应商收据。取消发生在 send hook 内时允许本段完成，后续段由服务取消，不能伪称远端已中止。

本次本地 smoke 实际运行了：冻结所有权、文本/语音 instruction/恢复字段二阶段篡改拒绝、错 PCM hash 拒绝、GE/S/Q 时间、无动作跳过规划、注入假 sender 参数、fixture 确定性与无音频证据，以及 Python 编译检查。另以 `PYTHONPATH=src /workspace/shared/hell-inc-m1/.venv/bin/python -m unittest discover -s tests -p test_m4_policy.py -v` 实际运行 16 项离线策略/接缝测试全部通过（该虚拟环境没有 pytest；未将 pytest 调用失败当作通过）。持久服务与 HTTP/CI 结果由集成验收另记，不将这些 smoke 当作完整回归或真实调度器运行证明。

## 新形象与版式

用户新建形象 **地狱集团首席执行官**，父任务只读核验尺寸 **1672 × 941**。本切片不默认旧朱元璋形象，不保存/套用旧 idle 动作库绑定，不编辑、裁切或缩放图片；资产 ID 由未来真实接入显式核验和配置。

最终直播整体仍是 **9:16**。数字人只占其中横屏区域，`avatar_aspect` 默认 `null`，用户可选 **16:9** 或 **4:3**。这是布局偏好元数据，不代表资产经过任何图片处理，也不强制屏幕分配比例。

剩余真实验收：显式授权后的模型/TTS/Vidu、真实音画发送、RTC 实播接收、停止及云端结束回读；新形象资源/动作绑定独立核验。以上完成前，M4 保持 **媒体验收待完成**。


## Phase 2 原运行时选择性复用

新增 `avatar_transport` 保留六份固定来源文件及 MIT，详见同目录 `MANIFEST.md` 的原 Git blob/当前 SHA-256/窄补丁列表。保持原 MotionComponent 调度器；只做包相对导入、显式 avatar_id 不读旧底图、on_created 持久化回调先于 WSS、默认请求云录制、独立默认禁自动录像下载、文本空白/显式声音保留、短不透明凭据脱敏和私有文件权限等必要补丁。未导入 Director、Automation、旧工作台或旧形象 idle 绑定。

`RealPerformanceAdapter` 用原 `tts_live.synthesize_segment`、受限 `timed_action_planner` 和原 `MotionComponent.send_aligned_expression`。stage1 请求只能返回表达计划；stage2 只能选择完整合法 schedule ID；台词、声音 instruction、动作数及内容、恢复姿态被冻结。实际 SDK、供应商和形象操作仍未执行。RTC 心跳与人工确认是两道门，心跳失效后必须重新确认及显式恢复；正常取消允许当前段完成，不宣称远端已中止。

实际本地模拟验证包含原 SSE→ffmpeg/ffprobe→PCM/锚点→完整合法规划→原 scheduler/假 socket，以及假时钟下 Type9 间隔、PCM 不被动作等待阻塞；这些不等于真实网络、实际音画或旧 idle 资源效果验证。运行时/HTTP/数据库完整证据以本轮最终 CI 记录为准。

### 停止闩锁与重启止费修复

`component_live.bind_owned_for_stop` 只给一个全新、未启动的本地对象绑定本场已持久化 ID；它只能调用原 `stop/_finish` 的同 ID Type1 → Type5 → 关闭 → GET 回读路径，不调用创建/RTC/发音方法。调度器未改写。运行时及数据库均保留 stop latch；在途创建返回 ID 后先持久化再立即停止。模拟测试实际执行了原 `_finish` 与假 WSS 帧，断言无 POST、无音频，未确认结束仍 unknown；这不是实际供应商止费证明。

TTS 默认使用完整 SSE inline 音频而非未知下载主机，保留一次合成、完整性/容器/PCM hash/时间戳核验及私有证据。完整性失败不补第二次 TTS，不自动回退下载；来源适配的具体修改与 SHA 见 `avatar_transport/MANIFEST.md`。


### 录制配置忠实复用修正

固定迁移源 `component_live.py` 的原 create 请求明确为 `managed_rtc.enable_recording=true`。M4 Phase 2 曾为避免自动下载而一并传入 false，属于迁移配置偏差。现默认恢复请求 true，允许 `HELL_INC_MEDIA_RECORDING_ENABLED=0` 明确关闭；自动下载另行保持关闭。`recording_requested` 仅保存有效请求配置，不等于平台确认启用、生成录像或本地已保存。真实 component body 的 true/false 测试均使用模拟 HTTP；不据此补称此前真实场次请求已完整捕获，也不对已结束场次补录或转码。
