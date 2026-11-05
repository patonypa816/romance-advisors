#!/bin/zsh
# 只启动本机采集服务；工作台显式订阅房间后才会抓取评论。
# 端口冲突由 dyhub 拒绝，不接管或停止任何既有服务。
set -eu
studio_dir="${0:A:h}"
dyhub_dir="${studio_dir}/../../dyhub"
if [[ ! -f "${dyhub_dir}/dist/index.js" ]]; then
  print '未找到仓库内 DyHub 构建。请先在仓库根目录的 dyhub/ 中执行 npm ci --ignore-scripts 和 npm run build。'
  print '此启动器不会自动安装依赖，也不会读取原实验目录。'
  exit 1
fi
node_bin="$(command -v node || true)"
if [[ -z "$node_bin" && -x /opt/homebrew/bin/node ]]; then
  node_bin=/opt/homebrew/bin/node
fi
if [[ -z "$node_bin" ]]; then
  print '未找到 Node.js 20 或更新版本，采集服务未启动。'
  exit 1
fi
if ! "$node_bin" -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 20 ? 0 : 1)'; then
  print 'DyHub 需要 Node.js 20 或更新版本，采集服务未启动。'
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
