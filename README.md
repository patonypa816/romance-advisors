# HELL INC. 地狱招聘局

面向抖音中文观众的互动直播游戏。本分支实现 **M1 隔离测试世界的游客登记与查档**：稳定模拟身份、昵称更新、命令防重、真实 PostgreSQL 持久化。没有真实抖音接入、面试、资产修改、AI、数字人或收费云服务。实际验证结论见 [HANDOVER](HANDOVER.md)。

## 开发运行

需要 Python 3.12、PostgreSQL 17、Node.js 22。数据库必须是专用开发库；不要连接正式玩家数据。以下命令是项目运行入口，不代表已有部署。

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-lock.txt
python -m pip install --no-deps -e .
cd frontend && npm ci && npm run build && cd ..
# 在当前终端设置 HELL_INC_DATABASE_URL；不要把真实凭据提交到仓库。
# URL 格式：postgresql+asyncpg://USER:PASSWORD@HOST:5432/DEVELOPMENT_DB
python -m hell_inc migrate
python -m hell_inc serve
```

默认只监听 `127.0.0.1:8080`。启动后在交互终端显示一次性登录链接；浏览器打开后前端立即清除 fragment，通过同源 POST 换取 HttpOnly / SameSite=Strict cookie。链接 10 分钟有效、只能兑换一次；会话默认 1 小时，重启后失效。不要分享链接、录制登录终端或把链接加入文档。

无交互终端时显式使用 `python -m hell_inc serve --login-file /tmp/hell-inc-login-unique`：文件必须不在当前仓库目录下且尚不存在，权限为 `0600`。读取后删除该临时文件；重启使用新的文件名。不会将令牌放入标准输出、访问日志或 localStorage。

| 环境变量 | 默认／要求 |
| --- | --- |
| `HELL_INC_DATABASE_URL` | 必填，`postgresql+asyncpg://`，仅隔离开发库 |
| `HELL_INC_WORLD_ID` | `hell-inc-dev`；模拟世界不与正式世界混用 |
| `HELL_INC_HOST` / `HELL_INC_PORT` | `127.0.0.1` / `8080` |
| `HELL_INC_ORIGIN` | `http://127.0.0.1:8080`；反向代理必须设置浏览器实际 HTTPS 源，不含路径或末尾 `/` |
| `HELL_INC_SESSION_SECONDS` | `3600`，上限 `86400` |
| `HELL_INC_FRONTEND_DIR` | `frontend/dist` |
| `HELL_INC_LOCK_DIR` | `/tmp/hell-inc-locks`；同宿主实例必须共享，不能为了重复启动而改路径 |

非回环访问必须使用 HTTPS；代理不得缓存 `/api/`、记录 Cookie／登录请求体或改变配置源。没有验证公共部署，不自动暴露公网端口。默认关闭访问日志。启动不创建表，只检查版本并幂等建立世界行；迁移必须单独运行。更新先停旧实例，再迁移、启动。OS 锁按世界名保守排他（不同隔离环境使用不同 world_id），避免数据库地址别名绕过。OS 排他锁是单宿主保护，不是跨机器租约；迁机先确认旧机已停。

## 最小使用路径

1. 使用本次启动链接登录，输入 `subject_id=visitor-001`、昵称“小林”，登记游客。
2. 按稳定 ID 或昵称查询；同 ID 改昵称仍是原 `player_id`，不同 ID 使用相同昵称仍是两人。
3. 空经历显示为空；游客没有职位、绩效为 0，没有任何资产修改入口。
4. 遇网络不明，使用页面原操作重试；保留同一 `command_id` 与载荷。同命令不同载荷拒绝。旧命令重放返回当时结果，不冒充最新档案。
5. 停止服务后重启并用新链接登录，重新查询；数据在 PostgreSQL 中，登录态在内存中。

服务函数在 `src/hell_inc/records/service.py`，输入契约在 `contracts.py`。每次变更锁定世界行，档案、世界版本和命令回执同事务提交；无变化或重放不增加世界版本。读取使用一致数据库快照，不虚构经历。

## 验证与边界

```sh
python -m compileall -q src
python -m unittest discover -s tests -v
cd frontend && npm run build
```

数据库与浏览器集成检查的具体环境变量、命令和结果由测试文件及 GitHub Actions 定义；跳过 PostgreSQL 的本地检查不算持久化验收。开发库的长期备份与恢复不由 GitHub 源码提交提供，尚未进行生产备份恢复演练。

## 文档

- [HANDOVER](HANDOVER.md)：当前状态与实际验证
- [AGENTS](AGENTS.md)：协作约束
- [SPEC](docs/SPEC.md)：唯一产品规格与 M1–M6 顺序
- [接口约定](docs/m1-contract.md)：M1 已实现接口与 P1 待实现边界
- [walkthrough](walkthrough.md)：实际历史

下一步为 M2 固定题面试，不扩建通用管理平台。最终云端媒体→本机 OBS→直播伴侣→抖音链路仍需独立验收。
