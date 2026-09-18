#!/bin/zsh

SCRIPT_DIRECTORY="${0:A:h}"
cd "$SCRIPT_DIRECTORY" || exit 1

source "$SCRIPT_DIRECTORY/查找Python.sh"
if ! find_python; then
  echo "$PYTHON_MISSING_REASON"
  echo ""
  echo "按回车键关闭窗口。"
  read
  exit 1
fi

ppg_stop_previous

export MPLCONFIGDIR="$HOME/Library/Caches/PPGCollector/matplotlib"
/bin/mkdir -p "$MPLCONFIGDIR"
STARTED_AT=$(/bin/date +%s)
"${PPG_ARCH_PREFIX[@]}" "$PYTHON_BINARY" "启动采集界面.py" &
PYTHON_PID=$!
ppg_remember "$PYTHON_PID"
wait "$PYTHON_PID"
EXIT_STATUS=$?
RUNTIME=$(( $(/bin/date +%s) - STARTED_AT ))

# 只有“刚启动就挂”才值得报错。跑了一会儿之后的非零退出码，通常是被关掉
# 或者被新实例替换了。
if [[ $EXIT_STATUS -ne 0 && $RUNTIME -lt 10 ]]; then
  echo ""
  echo "采集界面启动失败，错误代码：$EXIT_STATUS"
  echo "按回车键关闭窗口。"
  read
fi
exit $EXIT_STATUS
