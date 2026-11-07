#!/bin/zsh
set -eu
cd -- "${0:A:h}/romance-advisors"
if [[ ! -x .venv/bin/python ]]; then
  print '请先按项目 README 配置游戏工作台的本机运行环境。'
  exit 1
fi
exec .venv/bin/python -B -m romance.broadcast_prepare
