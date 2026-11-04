#!/bin/zsh
# 只启动本机采集服务；工作台显式订阅房间后才会抓取评论。
# 端口冲突由 dyhub 拒绝，不接管或停止任何既有服务。
set -eu
studio_dir="${0:A:h}"
dyhub_dir="${studio_dir}/../../artifacts/dyhub-smoke-20261030/dyhub"
if [[ ! -f "${dyhub_dir}/dist/index.js" ]]; then
  print '未找到已验证的 dyhub 构建，请检查采集目录。'
  exit 1
fi
node_bin="$(command -v node || true)"
if [[ -z "$node_bin" && -x /opt/homebrew/bin/node ]]; then
  node_bin=/opt/homebrew/bin/node
fi
if [[ -z "$node_bin" ]]; then
  print '未找到 Node.js，采集服务未启动。'
  exit 1
fi
if /usr/sbin/lsof -nP -iTCP:8757 -sTCP:LISTEN -t >/dev/null 2>&1; then
  print '8757端口已被使用，未启动新服务。请核对原服务，不会停止它。'
  exit 1
fi
cd "$dyhub_dir"
export DYHUB_HOST=127.0.0.1 DYHUB_PORT=8757 DYHUB_COLLECTOR=browser DYHUB_TEST_SKIP_META=1
print '本机评论服务：http://127.0.0.1:8757'
print '在工作台填写自己的抖音房间数字 ID，再点击连接评论来源。'
print '工作台停止接收只关闭订阅；结束采集请在此窗口按 Control-C。'
exec "$node_bin" dist/index.js
