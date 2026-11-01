# M1 与 P1 首批接口约定

日期：2026-11-01。状态：本分支已编写 M1 接口实现，实际验证见 HANDOVER；P1 仍为待实现约定，无对应服务。范围：开发环境隔离测试世界；真实平台写入、模型与 Vidu 不属于这一阶段。

依据：[当前实施规格](SPEC.md)。`local-test` 是隔离测试身份域／模式名，不表示要求访问某台个人电脑。实际云端预览路由与登录交接方式需按选定开发宿主验证；不默认暴露公网端口。

## 共有格式

- 所有 JSON 使用 snake_case。错误为 `{"error":{"code":"...","message":"中文说明"}}`。
- `revision` 是数据库世界行上的单调整数；只在数据实际变化时递增。日期 ISO 8601 UTC。
- 同源请求；受保护 API 使用 HttpOnly SameSite=Strict 登录 cookie。启动器通过 URL fragment 将一次性启动令牌交给前端，前端立即清理 fragment 后 POST `/api/session`，不存 localStorage。
- GET `/api/session`：`{"authenticated":true|false}`；POST `/api/session` 请求 `{"token":"..."}`，返回 `{"authenticated":true}`。
- GET `/api/health`：`{"status":"ok"|"unavailable","mode":"local-test","world_id":"hell-inc-dev"}`。UI 状态以真实响应为准。

## 档案 API

- GET `/api/players?q=`：`{"players":[Player],"revision":1,"server_time":"..."}`。列表包含 recent_events；只用于受保护操作页。
- GET `/api/players/{player_id}`：`{"player":Player,"revision":1,"server_time":"..."}`；不存在 404。
- POST `/api/players`：`{"command_id":"UUID","provider":"simulated","identity_scope":"local-test","subject_id":"visitor-001","display_name":"小林"}`。
- 注册返回 `{"player":Player,"revision":1,"replayed":false}`；重复相同 command 返回原结果并 replayed=true；同 command 不同载荷 409。相同身份、新 command 可更新昵称，不能另建档案。网络失败重试必须保留原 command 和载荷；用户明确改表单才建立新操作。
- Player：`{"player_id":"UUID","provider":"simulated","identity_scope":"local-test","subject_id":"visitor-001","display_name":"小林","role":"guest","department":null,"position":null,"performance":0,"joined_at":"...","updated_at":"...","recent_events":[]}`。
- display_name 1–80 字符，subject_id 1–128，identity_scope 1–64。ID 不容许首尾空白；不按昵称合并，不自动规范化或截断 ID。
- 本阶段不提供资产修改入口。不生成虚构历史。页面显示模拟身份与实际 PostgreSQL 持久化边界。

## 数据层 Python 边界

`records/service.py` 提供 `RecordService(engine: AsyncEngine, world_id: str)`：`await initialize_world()`、`await register(data: dict)`、`await list_players(query: str = "")`、`await get_player(player_id: str)`，返回上述 JSON 就绪 dict。

不存在抛 `PlayerNotFound`；命令冲突抛 `CommandConflict`；验证在 API contracts.py 执行，数据层仍保留 DB 唯一约束。initialize_world 幂等，只建世界记录，不隐式改表。迁移单独执行。

## P1 离线规范化边界（待实现，本分支不提供）

`platform/normalize.py` 提供 `normalize_chat(raw: dict, *, identity_scope: str) -> dict` 和 `NormalizationError(code)`。输入采用现有 dyhub 原始 SSE chat 对象：type/platform/id/roomId/ts/receivedAt/user{id,nickname}/data{content}。

输出字段：provider="douyin", message_id, room_id, subject_id, identity_scope, display_name, original_text, source_timestamp, received_timestamp。时间字段缺失为 null，不猜测单位或补原始时间。ID、昵称、正文逐字保留，不 trim、不截断、不散列；拒绝空 ID、非字符串 ID、不支持的平台、超限内容，避免内存无界。明确稳定作用域是配置假设，跨场稳定性未验证。

GET `/api/platform/monitor` 返回内存有界监看快照：source="recorded_sample", state="offline", events=[规范化消息], business_admission="not_connected"。POST `/api/platform/sample` 载入固定样例用于验收；只监看，不能显示游戏已接纳。不连接真实采集服务。


## M1 运行补充

- Python 导入前缀为 `hell_inc`；函数边界位于 `hell_inc.records.service`。
- 启动命令、同源配置、一次性链接领取和显式迁移见 README。登录令牌 10 分钟有效且只用一次；会话默认 1 小时，重启失效。所有写请求必须携带与配置完全一致的 `Origin`，不允许 CORS。
- 登录、健康接口不要求 cookie；档案列表、详情和登记均受保护。此页不是公开直播叠层。
- 仅允许 `provider=simulated`、`identity_scope=local-test`；拒绝额外输入字段，包括资产字段。
- 注册命令的原始业务载荷及 JSON 结果持久化；后续昵称变化不改变旧命令回执。无变化的新命令不增加 `revision`。
