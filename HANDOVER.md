# 当前状态

更新日期：2026-11-01。本文件只描述当前分支；历史见 walkthrough。

## 本轮实现范围

已编写 M1 隔离测试世界的 Python / aiohttp 服务、SQLAlchemy async / PostgreSQL 数据层、显式基线迁移、受保护游客登记与查询 API、TypeScript / Vite 操作页，以及自动化测试和 GitHub Actions。启动与操作入口见 [README](README.md)。

- 稳定身份由 world + simulated + local-test + subject_id 区分；昵称更新不换档，同名不同身份不混档。
- 世界行锁串行写事务；档案、revision、命令原回执同时提交。重放保留当时结果，同 ID 不同载荷拒绝；没有变化不增加 revision。
- 仅游客登记与查询，无绩效修改、录用、奖励或虚构经历。
- 同宿主 OS 排他锁阻止同名世界重复启动；没有跨机接管。
- 启动不迁移；一次性 fragment 登录、HttpOnly / SameSite=Strict cookie、同源检查、会话到期与重启失效。令牌不写访问日志或仓库。
- P1 仅保留待实现接口文档，未加入真实采集或离线监看接口。

## 当前验证证据

**M1 隔离模拟身份功能已通过当前自动化验收。** 代码提交 `76b26419310453f0098286375f91e7245d620ea9` 的 [Actions 运行 41325088938](https://github.com/patonypa816/hell-inc-game/actions/runs/41325088938) 全部成功；开发交付见 [PR #1](https://github.com/patonypa816/hell-inc-game/pull/1)。

- PostgreSQL 17 显式迁移连续执行两次通过。
- 16 项 Python 测试通过：5 API、2 输入契约、2 实例锁、7 档案事务。
- 2 项 Playwright 浏览器测试通过，包含主登记查询路径与登录 fragment 导航回归；前端生产构建通过。
- 当前本地环境已运行 compileall、4 项无需数据库的测试和依赖检查；真实 PostgreSQL 与完整浏览器证据来自上述 Actions，而非本地运行。
- 首轮登录 fragment 检查失败已修复并完整复验。详细行为、提交和限制见 [M1 验收记录](docs/m1-acceptance.md)。

这是隔离功能验收，不代表生产服务或长期在线预览已经部署；Actions 临时数据库不承诺跨 job 保留。

## 没有交付的部分

尚未部署公开服务或阿里云生产资源，没有调用付费模型、Vidu、RTC、真实抖音采集、Studio、OBS 或直播伴侣。没有导入真实玩家、浏览器会话或凭据。M2–M6 和 P1–P3 均未完成。

GitHub 保存源码，不保存运行数据库；开发数据长期持久化和备份恢复演练仍需在选定运行宿主单独确认。当前页面与模拟身份不代表真实平台身份已验证。

## 接续

1. 通过 README 在选定隔离宿主显式迁移并启动；长期服务、数据保留和备份恢复单独确认。
2. M1 自动化验收已通过，下一开发步骤是 M2 固定题面试；只随玩法增加录用与结算能力，不扩建通用后台。
3. P1、真实身份／采集、媒体链与生产部署仍分别验收，不能由本次 CI 推定完成。
