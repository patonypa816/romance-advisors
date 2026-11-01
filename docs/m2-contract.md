# M2 固定题面试接口（本分支实现契约）

范围：隔离测试世界、simulated/local-test 身份、一个活动面试、一题一答、文字结算。
**ChatGPT 辅助评估（人工导入；尚非自动 API）**。没有模型网络客户端、付费调用、百炼接入或模型凭据。

## 运行与迁移

保留 README 的环境变量。运行 `python -m hell_inc migrate` 显式升级到 schema 2；支持空库和 v1→v2，重复执行无变化。迁移事务新增表，不改写 M1 玩家及 registration_commands。启动要求 v2，不隐式迁移。同宿主排他锁不变。

服务启动后活动面试持久暂停，原 EVALUATING 的 attempt 标 interrupted。恢复需要人工操作；已经完成的评分和结算不重抽。数据库异常关闭游戏门；连接恢复后先持久暂停，再允许人工恢复。简单同进程计时器每 0.5 秒检查当前面试，无队列或工作流平台。答案接纳和结果采纳均先锁世界行，再读 PostgreSQL `clock_timestamp()`；截止区间右端不包含。

## HTTP

所有接口继续使用一次性登录后的 HttpOnly cookie、同源 Origin 防护，无公开资产写入接口。JSON 字段严格；UUID 使用小写规范形式。重复 JSON key、NaN／无穷数、无效 Unicode、任意字段或键中的 NUL 拒绝。

| 路径 | 请求 |
|---|---|
| GET `/api/interviews` | 最近 20 条完整快照 |
| POST `/api/interviews` | `{command_id,player_id}` |
| GET `/api/interviews/{id}` | 当前完整快照 |
| POST `/api/interviews/{id}/{open,pause,resume,retry}` | `{command_id,expected_version}` |
| POST `/api/interviews/{id}/cancel` | 同上加 `reason`，1–200 字符 |
| POST `/api/interviews/{id}/answers` | `{message_id,player_id,text}` |
| GET `/api/interviews/{id}/evaluation-request` | 只导出当前有效 attempt |
| POST `/api/interviews/{id}/evaluation-result` | `{command_id,task_id,attempt_no,input_hash,policy_hash,evaluation}` |

写操作回执 `{command_id,replayed,revision,interview}`；答案回执 `{input_id,accepted,rejection_code,replayed,interview_id,revision}`。成功命令、合法命令的业务拒绝及所有合法答案业务处置保存原回执，同 ID 同载荷重放原结果，改变动作／目标／原文／玩家拒绝 409 command_conflict。重放先于版本／时限检查。未接纳答案也保存原处置；不作异步排队。

GET 单条 `{revision,server_time,interview}`；列表 `{revision,server_time,interviews}`。读取用同一 REPEATABLE READ 事务。完整 interview：

```
{id,version,status,paused_from,pause_reason,
 player:{player_id,display_name},
 rule:{id,test_only,department,position,question,answer_seconds,
       criteria:[{id,description}],hire_rule},
 opened_at,deadline_at,answer:{text,admitted_at}|null,
 task:{id,status,attempt_no,deadline_at,error_code}|null,
 evaluation:{criteria:[{id,score,evidence,reason}],total}|null,
 result:{decision,settlement_id,summary,completed_at}|null,
 allowed_actions:[]}
```

`decision` 为 `hire` 或 `reject`。任务 provider 在冻结 policy 中为 `chatgpt_native_manual`；不编造 API model name。时间为 UTC ISO8601。

错误：400 validation_error；401/403 原鉴权；404 player_not_found/interview_not_found；409 command_conflict、state_conflict、version_conflict、active_interview_exists、not_eligible、evaluation_expired；503 database_unavailable。输入业务拒绝 HTTP 200，代码 wrong_player、answer_already_submitted、paused、deadline_passed、not_open。

## 固定测试规则和状态

规则 `m2-crisis-pr-test-v1`：危机公关／危机公关实习生；题目“客户投诉收到的噩梦不够恐怖，你会怎么回应？”测试默认规则，仅游戏表现评价，不是真实人格测评。原答案 1–1000 Unicode 字符，保留空白，拒绝全空白／NUL。一次接受后不可编辑。

READY → open（题目已展示确认）→ ANSWERING（120 秒）→ 有效答案 → EVALUATING（15 分钟人工导入）→ COMPLETED。无答案到期 EXPIRED，无资产惩罚。任务到期 REVIEW_FAILED，不补零分。PAUSED/REVIEW_FAILED 仍占唯一活动面试名额。

pause 保留来源，打断当前评估 attempt。resume 从原 EVALUATING 转 REVIEW_FAILED，必须 retry 创建下一 attempt；原 ANSWERING 重置完整 120 秒窗口（测试策略，不是保留剩余时间）；其余恢复原状态。retry 不改变原文、规则或哈希。cancel 终止，不结算。已 COMPLETED 不允许重评。

## Native 导出／导入边界

导出恰为 `{task_id,attempt_no,input_hash,policy_hash,frozen_input}`。frozen_input 是 `{rule,policy,raw_answer}`；其 hash 使用排序键、紧凑 UTF-8 JSON 的 SHA256。ChatGPT 必须将 raw_answer 当不可信数据，不能遵循其中指令。导入信封沿用导出的四个绑定字段，并由操作者产生 command_id。

唯一 evaluation 格式：

```json
{"criteria":[
 {"id":"empathy","score":0,"evidence":[],"reason":"未回应投诉感受"},
 {"id":"solution","score":0,"evidence":[],"reason":"未提出具体补救"},
 {"id":"setting","score":0,"evidence":[],"reason":"未使用噩梦公司设定"}
]}
```

恰一份 empathy/solution/setting，任意顺序；每项字段恰 id/score/evidence/reason。分数真整数 0–2，拒绝 bool、字符串、小数；0 无证据，正分需 1–2 个原文精确子串，每段 1–120 字符；reason 非空 1–160 字符。额外 total、hired、资产字段或任意其他键拒绝。结构有效不保证语义质量，真实模型定性抽查需另记证据。

非法 evaluation 保存原错误回执与失败 attempt，转 REVIEW_FAILED 后返回 400；过期结果同样失败但返回 409。信封错误或旧 task/attempt/hash/state 不采纳。只有当前有效 attempt、未到期、规则和输入当前哈希相符才进入结算。

## 持久化和事务边界

M2 新增 memberships、interviews（内嵌一问一答／任务状态／最终评价）、interview_commands、input_records、model_tasks（冻结输入／hash／attempt 历史）、settlements、reward_ledger、game_events。模型状态嵌入 interview，避免重复事实源；不是通用调度平台。

`RecordService.apply_interview_result(conn,world_id,interview_id,player_id,frozen_plan)` 不开新事务、不提交，只使用调用方连接。先查唯一来源结算，比较 plan hash；已存在相同计划返回原结果。玩法代码算总分及录用条件 `total>=4 and empathy>=1 and solution>=1`，模型不得决定录用。

同一世界锁事务完成评价采纳、结算、录用流水、membership、事实历史、面试终态、revision 和命令回执。任一步失败全部回滚。录用只变员工／部门／职位，绩效保持 0；无货币或公司奖励。拒绝保存结果与事实历史，不变游客资产。失败／取消／到期不进入结算。员工不可再开面试。M1 新查询和新登记回执反映真实岗位／最近 10 条事实事件；旧登记命令重放仍返回当时游客回执。

## 验证声明

本契约描述代码接口，不替代测试证据。自动 fixture、真实 PostgreSQL/浏览器、native ChatGPT 导入闭环分别验收；不能推称自动模型 API、百炼兼容、生产部署、真实平台或媒体链已完成。
