# 通用数字人表演模块

这份文档对应 `avatar-studio` 的通用表演入口。旧 HELL INC. 玩法已暂停，游戏代码保留为历史；本模块不负责评分、结算、弹幕筛选、业务优先级或自动续开直播。Mac 上原口播工作台不作原地替换。

供应商合同与原调校规则见 [Vidu 能力边界](VIDU_BOUNDARIES.md)。

## 一个完整任务，一条既有执行链

调用方决定事实、角色设定、要表达什么、何时提交及是否打断。模块接收一项**有限完整任务**，内部负责自然分段、原语音/动作准备、串行发送及任务内书签。不能把业务恢复点和表演发送进度混为同一状态。

```
intent：表达意图 → 表达脑生成有限台词/动作 → 自动分块
script：固定原稿 → 自动分块 → 可选只编排本块声音/动作
motion：动作意图 → 原纯动作规划（不生成 TTS）
                          ↓
          原 Automation / LiveSession 单发送队列
                          ↓
       原 TTS → 同次 PCM / 字时间 → 原候选 schedule_id
                          ↓
             原 S/GE/Q + Vidu 同源音视频
```

没有第二个媒体发送器、微服务、工作流 DSL 或人工 ready 审批层。`comment/once/topic` 仍是兼容的口播入口，不再作为通用任务的内容类型。通用任务等待期间，旧入口不能抢占其内部块；口播适配层如需插话，应先通过控制接口明确安排，而不能依赖模型猜测。

## 输入与调用

启动仍用原工作台：

```sh
cd avatar-studio
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# 本机需另备 ffmpeg / ffprobe 和 RTC SDK，见 README / CLOUD_SETUP。
sh tools/fetch_vendor.sh
.venv/bin/python -B studio/server.py --preview
```

Python 3.11+；当前 CI 使用 3.12。API Key / workspace 从运行环境注入；程序不自动加载 `.env`。启动进程不会创建 Vidu 场次。先在原控制台选择形象、角色并建立同源音画连接，再显式提交任务。运行时控制地址来自被忽略的 `studio/data/runtime.json`，不要提交或转发它。CLI 不创建或自动关闭 Vidu 场次。

```sh
# 在 avatar-studio 目录执行。提交会在已有云场实际产生模型/语音和表演调用。
.venv/bin/python tools/performance.py submit examples/performance-script.json
.venv/bin/python tools/performance.py status
.venv/bin/python tools/performance.py pause local-script-001
.venv/bin/python tools/performance.py resume local-script-001
.venv/bin/python tools/performance.py cancel local-script-001
```

更换内容时使用新的 `request_id`。同场同 ID 同载荷返回原任务；同 ID 不同载荷拒绝。去重只在本进程当前场次内，重启不自动恢复或重演任务。运行结果不确定时先读状态和本场记录，不能换 ID 盲重播。

HTTP 沿用本机控制端原有鉴权地址与 Origin 约束：

- POST `/studio/<control-token>/performance/submit`：提交 JSON。
- GET `/studio/<control-token>/performance`：获取所有任务计划和进度。
- POST `.../performance/pause|resume|cancel`：`{"request_id":"..."}`。
- 全场立即停止仍用原控制台的结束会话功能，不是任务 cancel。暂停任务不会停止 Vidu 会话或计费。

输入仅包含：

| 字段 | 含义 |
| --- | --- |
| request_id | 调用方的本场请求标识，1–100 字 |
| kind | intent / script / motion |
| text | 完整表达意图、固定原稿或纯动作要求；本地最多 16000 字 |
| instruction | 可选语音演绎要求，1–160 字 |
| context | 可选且有界的表达背景、可信事实、受众信息；不是工具权限 |
| auto_actions | 仅 script 使用，默认 false；true 时表达脑只为已分好的原稿块编排表现 |
| block_chars | 20–120 的本地分块目标，默认 80；不是供应商硬限制，也不保证音频时长 |

角色身份与声音仍由原工作台当前角色/形象配置提供。本轮不建立多角色自动切换调度器。未完成或暂停任务须先完成/取消后再切换角色，避免已准备稿突然换人。

## 长内容与固定稿

- 先按句末、自然段、分句/单词边界切割；超长单句使用有界技术切分，保留全部原字符。不是每逢第 80 个字必断。
- 固定稿保持原文、空白、标点和顺序；原稿各块拼接与输入完全相同。边界处的空白不另造静音音频。只有空白且无法形成有效块的异常排版会明确报错，不吞掉它。
- 固定稿默认不调用表达脑，直接沿原 TTS/发送链；`auto_actions=true` 时，模型改字或改分块即失败，不默默替用户改稿。
- 意图模式允许表达脑写台词，程序再将完整语义计划变成内部短块。154 字的单段不再仅因原 120 字单块约束被整体拒绝。
- 动作锚点必须落在本块原文；分块避开锚点，重复短语的 occurrence 按本块重新计算。不能切完 PCM 后套旧字时间，不能跨块悄悄改变动作顺序或删掉动作。
- 每块进入同一原 TTS 和真实 PCM 对齐流程，再用原候选排程与 S/GE/Q。当前原执行链仍有约 30 秒单块音频约束，真实语速和动作窗口不足会明确失败；不人为拖慢、不补静音、不自动重试。
- 意图生成仍受现有模型单次输出预算（当前 reply 4096 tokens）与返回容量约束；超出时报告截断/容量错误，**不把残缺输出当完整任务**。本版支持有限长表达和长固定稿，不声称无限长自动续写。长稿内容已确定时优先 script。
- 复用原准备槽，在当前块发送期间最多预备下一块；只有前块成功发送收尾才推进书签并放行下一块。这不是零间隙连播，真实 TTS、动作收尾与网络耗时可能形成块间停顿，需本地视觉/听觉验收。

## 进度与控制

任务状态为 `accepted / preparing / playing / paused / completed / cancelled / failed / unknown`。

- `sent_blocks`、`sent_text` 只在原发送器成功返回后增长。`blocks` 为计划，可从原场次 `plans`、`tts/*/component-send.json` 和 comments 对照实际投送。
- `completed` 只指任务所有块及发送收尾完成，**不等于画面演对、观众已听完或 Vidu 会话已结束**。
- pause 在当前块结束后暂停；准备中的结果可保留，resume 不重播已发块。暂停任务保留已准备块，但让后续未暂停任务先执行；恢复后从实发书签继续，不重新合成已准备块。
- cancel 撤销尚未发送的块。当前已经开始的块自然收尾，随后不再发后续块。需要立即停场用原会话 stop；不能承诺撤回供应商已缓存的音画。
- `partial_delivery` 表示存在已发部分或已开始发送尝试，具体以原发送记录为准。已开始的块发生网络/发送异常时任务为 unknown，不自动重演。
- 取消后迟到的模型/准备结果不能重新进入发送队列。诊断保存是尽力而为，失败不挡取消或停止。
- 当前场次结束/进程重启后不自动接管未完任务。先核对真实结果，由调用方决定下一步。

## 本地调教与 GitHub 协作

1. 保留原口播目录。通用模块另 clone 整个仓库，检出 `codex/avatar-universal-performance`（PR 合并后以目标分支为准）。**不要默认 main 已含这些未合并开发内容。**
2. 开始前检查 `git status`，先保存已有修改再 fetch/pull。当地网络、依赖、密钥和 RTC/播放器按本机配置，不复制云端运行状态文件。
3. 本地从共同基底建立 `codex/avatar-tuning-<日期>`，看真实画面调提示词、角色配置、分块目标和原动作参数。代码提交保留对比记录。
4. `git add` 只选本次实际调教文件，检查 diff 后 commit、push，再以 PR 比较并合入共同版本。云端 fetch 后接续。两边改同一段代码要解决冲突，不能整目录上传覆盖，也不 force push。
5. 回退用单独 revert 提交或检出之前已验证的版本；不要删除/重写历史掩盖差异。两端不能同时控制同一个 Vidu 云场。

可共享内容与位置：

- 人设：用以下导出/导入，只携带 `name/persona/reply_style`，通过原设置入口验证。导出文件不会包含 control token、密钥、会话、形象缓存。导入仅允许在原工作台允许修改角色的空闲状态。
- 表达提示词与动作写法：`studio/cloudbase_ai.py`；默认角色/动作说明：`studio/brain-context.json`。
- 实际 S/GE/Q 调教：原 `semantic_speech.py`、`action_schema.py` 等代码常量仍是执行源，不复制另一份 JSON 参数导致不一致。改动需随测试一起提交。
- 分块目标与演绎要求：调用样例 JSON 的 `block_chars`、`instruction`；固定稿可直接作为可重复比较的样例。
- 真实 asset ID、音色/底图绑定及待机动作资源需按原资产库在本机重新关联，现有账户可见资源不等于本机已有缓存。本版没有把所有私有资源状态转成可携带配置。

```sh
# 导出文件若已存在会拒绝覆盖，先比较旧版再决定如何更新。
.venv/bin/python tools/performance.py export-persona examples/my-persona.json
.venv/bin/python tools/performance.py import-persona examples/my-persona.json
```

不要提交：`.env`、API Key、control/player token、`runtime.json`、`studio/data/`、录制音视频、带签名下载地址、缓存、运行数据库或个人登录状态。也不要整份复制 `local.json` 来“同步调教”，其中含本机控制凭据。

## 验证边界

本轮自动验证使用假模型、假 TTS/PCM 与假连接，覆盖实际原 Studio 的准备和队列接缝。它验证分段完整性、进度、取消和调用关系，**不能验证画面自然度或供应商真实执行效果**。真实本地验收需同时核对 task 计划、原发送记录和 Vidu 最终音视频。演出完成后按原流程确认云场结束。

固定稿的账本保存原文，包括空白；旧 TTS 合成入口仍会去除段首尾空白，并拒绝 `< > [ ] ［ ］` 标签类字符。原文无损不表示任意字符均可合成，也不意味着语音逐字符发音。未另造 TTS 通道。
