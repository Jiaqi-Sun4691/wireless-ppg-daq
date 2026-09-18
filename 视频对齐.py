#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把运动相机的视频时间轴对到采集 CSV 的时间轴上。

为什么需要：相机和电脑是两套独立时钟。运动相机标称 30fps 往往实际是
29.97（30000/1001），播放器按 30fps 计时就会越走越慢——1 小时差约 3.6 秒。
这个偏差是固定的，可以直接从文件里读出来，不必靠"首尾各拍一次手机再调速"
去手工测量。

用法：
    python3 视频对齐.py 视频.mp4 采集.csv
    python3 视频对齐.py 视频.mp4 采集.csv --sync-video 12.40 --sync-csv 65230

    --sync-video  视频里同步事件出现的秒数（拍手/LED 闪/遮镜头那一刻）
    --sync-csv    同一事件在 CSV 里的 timestamp(ms)
    两个都给出后，会输出可直接使用的换算公式和校验表。
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path


def probe(video: Path) -> dict:
    """Read the camera's real time base out of the container."""
    exe = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
    if not Path(exe).is_file() and not shutil.which("ffprobe"):
        raise SystemExit("找不到 ffprobe，请先安装 ffmpeg")
    out = subprocess.run(
        [
            exe, "-v", "error", "-select_streams", "v:0",
            "-show_entries",
            "stream=avg_frame_rate,r_frame_rate,nb_frames,duration:"
            "format=duration,start_time:format_tags=creation_time",
            "-of", "json", str(video),
        ],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(out.stdout)
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    return {
        "avg_fps": stream.get("avg_frame_rate"),
        "nominal_fps": stream.get("r_frame_rate"),
        "frames": stream.get("nb_frames"),
        "stream_duration": stream.get("duration"),
        "duration": fmt.get("duration"),
        "created": (fmt.get("tags") or {}).get("creation_time"),
    }


def csv_span(path: Path) -> tuple[int, int, int]:
    """First/last master timestamp (ms) and row count."""
    first = last = None
    rows = 0
    with path.open("r", encoding="utf-8", errors="ignore", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header:
            raise SystemExit("CSV 是空的")
        try:
            col = [h.strip() for h in header].index("timestamp(ms)")
        except ValueError:
            raise SystemExit("CSV 里没有 timestamp(ms) 列")
        for row in reader:
            if len(row) <= col:
                continue
            try:
                value = int(row[col])
            except ValueError:
                continue
            if first is None:
                first = value
            last = value
            rows += 1
    if first is None:
        raise SystemExit("CSV 里没有可用的时间戳")
    return first, last, rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("csv")
    ap.add_argument("--sync-video", type=float, default=None,
                    help="同步事件在视频里的秒数")
    ap.add_argument("--sync-csv", type=float, default=None,
                    help="同步事件在 CSV 里的 timestamp(ms)")
    args = ap.parse_args()

    video, data = Path(args.video), Path(args.csv)
    for f in (video, data):
        if not f.is_file():
            raise SystemExit(f"找不到文件：{f}")

    info = probe(video)
    nominal = Fraction(info["nominal_fps"]) if info["nominal_fps"] else None
    average = Fraction(info["avg_fps"]) if info["avg_fps"] else None
    fps = average or nominal
    if not fps or fps == 0:
        raise SystemExit("无法从视频读出帧率")

    print("=" * 62)
    print("相机时基")
    print("=" * 62)
    print(f"  标称帧率   : {nominal}  ({float(nominal):.4f} fps)")
    print(f"  平均帧率   : {average}  ({float(average):.4f} fps)")
    print(f"  总帧数     : {info['frames']}")
    print(f"  容器时长   : {info['duration']} 秒")
    if info["created"]:
        print(f"  文件创建时间: {info['created']}")

    # A player assuming a whole-number fps runs slow by exactly this factor.
    rounded = Fraction(round(float(fps)))
    scale = float(rounded / fps)
    print()
    print("=" * 62)
    print("漂移")
    print("=" * 62)
    if abs(scale - 1.0) < 1e-9:
        print("  帧率是整数，没有 29.97 这类偏差。")
        print("  若仍有漂移，多半来自相机晶振误差，需要用两个同步点测量。")
    else:
        print(f"  若按 {int(rounded)}fps 计时，视频时间需要乘以 {scale:.9f}")
        print(f"  即每小时累积 {abs(scale - 1) * 3600:.2f} 秒")
        for minutes in (10, 30, 60):
            print(f"     录 {minutes:>3} 分钟 → 末尾错位 {abs(scale-1)*minutes*60:5.2f} 秒")

    first, last, rows = csv_span(data)
    span = (last - first) / 1000.0
    print()
    print("=" * 62)
    print("采集 CSV")
    print("=" * 62)
    print(f"  样本数     : {rows:,}")
    print(f"  主控跨度   : {span:.2f} 秒（{span/60:.1f} 分）")
    print(f"  首/末时间戳: {first} → {last} ms")

    print()
    print("=" * 62)
    print("对齐")
    print("=" * 62)
    if args.sync_video is None or args.sync_csv is None:
        print("  只要给一个同步点，就能得到完整换算公式：")
        print("    1) 开始录制后做一个相机看得见的动作（拍手 / 遮一下镜头 / LED 闪）")
        print("    2) 在视频里找到那一帧的秒数            → --sync-video")
        print("    3) 在 CSV/GUI 里找到同一时刻的时间戳   → --sync-csv")
        print()
        print("  漂移已经由帧率算出，所以只需要这一个点，")
        print("  不必再在结束时拍第二次手机。")
        return

    # video_seconds -> csv milliseconds
    print(f"  同步点：视频 {args.sync_video:.3f}s  ↔  CSV {args.sync_csv:.0f}ms")
    print()
    print("  换算公式（视频秒 → CSV 毫秒）:")
    print(f"     csv_ms = {args.sync_csv:.0f} + (video_s - {args.sync_video:.3f}) "
          f"* {1/scale:.9f} * 1000")
    print()
    print("  校验：")
    print(f"    {'视频时刻':>10} {'→ CSV 时间戳':>16} {'是否在采集范围':>16}")
    for t in (0, span * 0.25, span * 0.5, span * 0.75, span):
        vs = args.sync_video + t
        ms = args.sync_csv + (vs - args.sync_video) / scale * 1000
        ok = "在范围内" if first <= ms <= last else "超出范围"
        print(f"    {vs:>9.1f}s {ms:>15.0f} {ok:>16}")
    print()
    print("  若要把视频重采样到与数据同一时基：")
    print(f"     ffmpeg -i \"{video.name}\" -filter:v \"setpts={scale:.9f}*PTS\" "
          f"-r {int(rounded)} 对齐后.mp4")


if __name__ == "__main__":
    main()
