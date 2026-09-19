"""渲染一段帧的子进程入口：python -m ppg_collector.rebuild_worker …

为什么不用 multiprocessing.Pool：它在 macOS 上走 spawn，子进程会重新执行
父进程的 __main__。界面里 __main__ 是启动采集界面.py，于是每个子进程都要
把整个 Tk 界面模块 import 一遍——又慢又容易出岔子（实测在非脚本入口下直接
FileNotFoundError）。这里起的是干净的子进程，只 import 需要的东西。

进度写在 <输出>.progress 里，父进程轮询文件而不是读管道：管道满了会把
子进程卡死，而这活儿一跑就是几分钟。
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from .rebuild import render_range


class _FileCounter:
    """把已完成帧数写进文件，父进程轮询。接口对齐 multiprocessing.Value。"""

    def __init__(self, path: Path, every: int = 50) -> None:
        self.path = path
        self.every = every
        self.value = 0

    def get_lock(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exception) -> None:
        # value 是在 with 块里被加的，这里负责落盘。
        if self.value % self.every == 0:
            try:
                self.path.write_text(str(self.value), encoding="utf-8")
            except OSError:
                pass


def main(argv: list[str]) -> int:
    csv_path, out_path, first, last, ffmpeg_path = argv
    out = Path(out_path)
    counter = _FileCounter(out.with_suffix(".progress"))
    mapping = render_range(
        Path(csv_path), out, int(first), int(last), ffmpeg_path, counter=counter
    )
    # 帧时间对照的这一段，交给父进程合并。
    with out.with_suffix(".map").open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(mapping)
    counter.path.write_text(str(counter.value), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
