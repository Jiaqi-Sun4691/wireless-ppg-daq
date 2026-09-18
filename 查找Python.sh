# 被 启动采集界面.command 和 PPG采集控制台.app 共同 source。
# 作用：挑一个能真正跑起来的 Python —— 不同电脑装的版本和位置都不一样，
# 写死一个路径的话，换台机器就直接启动失败。
#
# 成功：PYTHON_BINARY 为可执行文件路径，返回 0
# 失败：PYTHON_MISSING_REASON 为可以直接念给用户听的中文原因，返回 1

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
    missing=$("$candidate" - <<'PY' 2>/dev/null
import importlib.util, sys
if sys.version_info < (3, 10):
    print("PYTHON_TOO_OLD"); raise SystemExit
need = {"tkinter": "tkinter", "serial": "pyserial",
        "numpy": "numpy", "matplotlib": "matplotlib"}
print(" ".join(p for m, p in need.items() if importlib.util.find_spec(m) is None))
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
