#!/bin/zsh
# PhoneMic：启动菜单栏管理图标（若已在运行则不重复启动）
cd "$(dirname "$0")"
if pgrep -f PhoneMicMenu.py >/dev/null 2>&1; then
  echo "菜单栏图标已在运行（看屏幕右上角 ● / ◐ / ○）"
  exit 0
fi

PYTHON_BIN="./.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

nohup "$PYTHON_BIN" PhoneMicMenu.py >/dev/null 2>&1 &
disown
echo "已启动，请看屏幕右上角菜单栏图标"
