# 被 启动采集界面.command 和 PPG采集控制台.app 共同 source。
# 作用：挑一个能真正跑起来的 Python —— 不同电脑装的版本和位置都不一样，
# 写死一个路径的话，换台机器就直接启动失败。
#
# 成功：PYTHON_BINARY 为可执行文件路径，返回 0
# 失败：PYTHON_MISSING_REASON 为可以直接念给用户听的中文原因，返回 1
#
# 启动时必须用 $PPG_ARCH_PREFIX 前缀，别直接调 $PYTHON_BINARY。

# python.org 的 python3 是 x86_64 + arm64 的通用二进制，而 numpy 这类带
# 原生扩展的包装的往往只有本机架构那一份。LaunchServices 启动脚本型 .app
# 时可能挑 x86_64 那一半，于是 import numpy 就会报
# "incompatible architecture (have 'arm64', need 'x86_64')"。
# 显式钉到本机架构上，探测和真正启动都用它——否则探测在 arm64 下通过了，
# App 却以 x86_64 起来，等于没探。
#
# 判断硬件架构不能用 uname -m：进程一旦被 Rosetta 转译，它报的是"x86_64"，
# 照着它钉就正好钉在坏的那一半上。hw.optional.arm64 读的是硬件，
# 在两种进程里都返回 1。
typeset -ga PPG_ARCH_PREFIX
PPG_ARCH_PREFIX=()
() {
  [[ -x /usr/bin/arch ]] || return
  if [[ "$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null)" == "1" ]]; then
    PPG_ARCH_PREFIX=(/usr/bin/arch -arm64)
  else
    PPG_ARCH_PREFIX=(/usr/bin/arch -x86_64)
  fi
}

find_python() {
  PYTHON_BINARY=""
  PYTHON_MISSING_REASON=""

  local candidates=()
  # 想指定某个解释器时：export PPG_PYTHON=/path/to/python3
  [[ -n "$PPG_PYTHON" ]] && candidates+=("$PPG_PYTHON")
  local version
  for version in 3.14 3.13 3.12 3.11 3.10; do
    candidates+=("/Library/Frameworks/Python.framework/Versions/$version/bin/python3")
  done
  candidates+=(
    /opt/homebrew/bin/python3
    /usr/local/bin/python3
    "$(command -v python3 2>/dev/null)"
    /usr/bin/python3
  )

  local found_but_incomplete=""
  local candidate missing
  for candidate in "${candidates[@]}"; do
    [[ -z "$candidate" || ! -x "$candidate" ]] && continue
    # 缺哪个库就报哪个，而不是笼统地说“启动失败”。
    missing=$("${PPG_ARCH_PREFIX[@]}" "$candidate" - <<'PY' 2>/dev/null
import sys
if sys.version_info < (3, 10):
    print("PYTHON_TOO_OLD"); raise SystemExit
# 真的 import 一遍。find_spec 只看文件在不在，架构不对的扩展模块它照样
# 说"有"，等启动时才炸——那正是这个探测要防的情况。
need = {"tkinter": "tkinter", "serial": "pyserial",
        "numpy": "numpy", "matplotlib": "matplotlib"}
missing = []
for module, package in need.items():
    try:
        __import__(module)
    except Exception:
        missing.append(package)
print(" ".join(missing))
PY
)
    [[ $? -ne 0 ]] && continue
    if [[ -z "$missing" ]]; then
      PYTHON_BINARY="$candidate"
      return 0
    fi
    [[ "$missing" == "PYTHON_TOO_OLD" ]] && continue
    # tkinter 装不上只能换解释器，剩下三个 pip 就能补，所以优先提示后者。
    [[ -z "$found_but_incomplete" && "$missing" != *tkinter* ]] \
      && found_but_incomplete="$candidate|$missing"
  done

  if [[ -n "$found_but_incomplete" ]]; then
    local python_path="${found_but_incomplete%%|*}"
    local package_list="${found_but_incomplete##*|}"
    PYTHON_MISSING_REASON="缺少运行库：${package_list// /、}。请在终端执行：
${python_path} -m pip install ${package_list}"
    return 1
  fi

  PYTHON_MISSING_REASON="没有找到可用的 Python 3.10 及以上版本（需要自带 tkinter）。
建议到 python.org 下载安装 macOS 版 Python，然后执行：
python3 -m pip install -r requirements.txt"
  return 1
}

# 只结束"上一次由本程序启动的那个界面进程"。
# 原来用 pgrep -f "启动采集界面.py" 广搜再 kill -9：那会匹配到任何命令行里
# 含这串字的进程——打开着同名文件的编辑器、正在 grep 它的终端——然后连坐
# 杀掉。改成记 PID 文件，并在杀之前核对这个 PID 现在跑的确实是 Python。
PPG_PID_FILE="$HOME/Library/Caches/PPGCollector/gui.pid"

ppg_stop_previous() {
  [[ -f "$PPG_PID_FILE" ]] || return 0
  local old="$(< "$PPG_PID_FILE")"
  [[ "$old" == <-> ]] || { /bin/rm -f "$PPG_PID_FILE"; return 0; }
  # PID 会被系统回收，所以必须确认它现在还是我们那个进程再动手。
  local command_line="$(/bin/ps -p "$old" -o command= 2>/dev/null)"
  if [[ "$command_line" == *[Pp]ython* && "$command_line" == *ppg* ]] \
     || [[ "$command_line" == *[Pp]ython* && "$command_line" == *.py* ]]; then
    /bin/kill "$old" 2>/dev/null
    /bin/sleep 1
    /bin/ps -p "$old" >/dev/null 2>&1 && /bin/kill -9 "$old" 2>/dev/null
  fi
  /bin/rm -f "$PPG_PID_FILE"
}

ppg_remember() {
  /bin/mkdir -p "${PPG_PID_FILE:h}"
  /bin/echo "$1" > "$PPG_PID_FILE"
}
