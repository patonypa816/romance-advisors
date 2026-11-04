#!/bin/zsh
cd -- "${0:A:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  print '请先在本目录执行：python3 -m venv .venv，然后 .venv/bin/python -m pip install -r requirements.txt'
  read '?按回车退出。'
  exit 1
fi
.venv/bin/python -m romance.launcher
