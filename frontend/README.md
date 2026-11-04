# M1 操作页

Node 24；`npm ci && npm run build` 生成 `dist/`，由 Python 应用同源提供。
只使用模拟身份 `simulated / local-test`；登记结果来自真实 PostgreSQL API，不在浏览器保存替代档案。

开发热更新：后端设置 `HELL_INC_ORIGIN=http://127.0.0.1:5173`，前端运行 `npm run dev`。固定端口 5173，`/api` 默认代理至 `http://127.0.0.1:8080`；目标可用 `HELL_INC_BACKEND_URL` 设置。不要开放公网端口或关闭 Origin 检查。

登录链接 `/#token=…` 的 fragment 会在首个异步请求前移除，再交换 HttpOnly session；令牌不写入浏览器存储。请从启动器取得链接，不提交令牌或 Cookie。

登记失败时原 command_id 和载荷保留在当前页面内存，恢复连接后使用「重试 / 确认原登记」。明确修改表单才建立新操作。刷新页面会丢失未确认操作编号：结果未知时先重连重试，不要刷新。页面禁止离线、数据库不可用或未登录时登记。

`npm run test:e2e` 使用 `../tests/e2e/playwright.config.ts`；数据库等实际测试前提见根文档。
