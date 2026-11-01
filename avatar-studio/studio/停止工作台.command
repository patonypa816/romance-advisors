#!/bin/zsh
cd -- "${0:A:h}/.." || exit 1
exec .venv/bin/python -B studio/stopper.py
