#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从采集 CSV 重建波形录屏（命令行入口）。

界面里也有同一个功能：「数据文件」页右键 →「生成波形录屏」。
两边调的是同一份 ppg_collector/rebuild.py，出来的视频一模一样。
这个入口的用处是批量补齐，以及不想开界面的时候。

    python3 重建录屏.py "某次采集的文件夹"
    python3 重建录屏.py --all                     # 补齐所有缺录屏的采集
    python3 重建录屏.py 文件夹 --out /tmp/x.mp4   # 另存，不写回原位置
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ppg_collector.rebuild import rebuild_session


def main() -> None:
    parser = argparse.ArgumentParser(description="从采集 CSV 重建波形录屏")
    parser.add_argument("folder", nargs="?", help="某次采集的文件夹")
    parser.add_argument("--all", action="store_true", help="补齐保存目录里所有缺录屏的采集")
    parser.add_argument("--root", default=str(Path.home() / "Documents" / "PPG Data"))
    parser.add_argument("--out", help="另存到指定路径（默认写回该次采集的文件夹）")
    args = parser.parse_args()

    if args.all:
        root = Path(args.root)
        folders = sorted(
            d for d in root.iterdir()
            if d.is_dir() and next(d.glob("*.csv"), None) and not next(d.glob("*.mp4"), None)
        )
        if not folders:
            print("没有缺录屏的采集。")
            return
        print(f"待重建 {len(folders)} 次：")
        for folder in folders:
            print(f"  · {folder.name}")
    elif args.folder:
        folders = [Path(args.folder)]
    else:
        parser.error("给一个文件夹，或者用 --all")

    for position, folder in enumerate(folders, start=1):
        csv_path = next(folder.glob("*.csv"), None)
        if csv_path is None:
            print(f"[{position}/{len(folders)}] {folder.name}: 没有 CSV，跳过")
            continue
        print(f"\n[{position}/{len(folders)}] {folder.name}")

        def report(done: int, total: int, _position=position) -> None:
            if done and done % 2000 == 0:
                print(f"    {done / total:5.1%}  {done:,}/{total:,} 帧", flush=True)

        try:
            result = rebuild_session(
                csv_path,
                Path(args.out) if args.out else None,
                progress=report,
            )
        except (ValueError, RuntimeError) as exc:
            print(f"  ✗ {exc}")
            continue
        print(f"  ✓ {result.frames:,} 帧 / {result.seconds} 秒"
              f"（{result.rows:,} 行，耗时 {result.elapsed / 60:.1f} 分）")
        print(f"    {result.video_path}")
        print(f"    {result.mapping_path}")


if __name__ == "__main__":
    main()
