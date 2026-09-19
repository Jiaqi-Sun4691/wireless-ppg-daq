"""从采集 CSV 重建波形录屏 MP4。

录屏是 CSV 的确定性函数——同一段数据喂进同一套绘图代码，画出来就是同一条
波形。所以视频是派生物，CSV 才是原件：视频丢了能再生成，采集时掉一行数据
则不可逆。这也是为什么采集过程默认不再实时编码视频，改成事后按需生成。

和实时录屏的两处区别，都是有意的：

1. 时间轴是准的。实时录屏靠界面定时器抓帧，渲染跟不上就掉帧——实测目标
   20 fps 只抓到 11~13 fps 却按 20 fps 写盘，于是播放比真实快约 1.6 倍，
   而且快多少取决于当时的界面负载。重建版第 k 帧固定对应采集开始后
   k/FPS 秒，视频时长等于真实采集时长，另出一份帧号↔时间戳对照表。
   要和行车视频并排对齐，只有后者站得住。

2. 开头那 400 点窗口是空的。界面一连上设备就在画，点"开始采集"时 deque
   里早攒满了，而 CSV 只从开始采集那一刻记起——那段数据从没落过盘。这里
   留空（NaN，不画线）而不是补 0：0 看着像信号掉到底，标注时会被当成真
   事件。长采集里只影响开头约 19 秒。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Callable

import numpy as np
from matplotlib.animation import FFMpegWriter

from .plot import (
    CHANNELS,
    PLOT_WINDOW,
    SIM_WINDOW_MS,
    build_ppg_figure,
    correlation,
    format_correlation,
)
from .recorder import configure_ffmpeg

FPS = 20
# 界面里那张图被 Tk 按窗口拉伸到 1560x550，历史录屏都是这个尺寸，沿用它。
WIDTH, HEIGHT, DPI = 1560, 550, 120

CHANNEL_KEYS = tuple(key for key, _label, _color in CHANNELS)


class Cancelled(Exception):
    """用户中途取消。"""


@dataclass(slots=True)
class RebuildResult:
    video_path: Path
    mapping_path: Path
    frames: int
    seconds: float
    rows: int
    elapsed: float


def load_session(csv_path: Path):
    """读出画波形需要的三路 PPG 和时间列。"""
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        index = {name: position for position, name in enumerate(header)}
        missing = [name for name in ("timestamp(ms)", *CHANNEL_KEYS) if name not in index]
        if missing:
            raise ValueError(
                f"{csv_path.name} 少了这些列：{'、'.join(missing)}。"
                "这次采集没有勾选全部三路 PPG，画不出原来的波形图。"
            )
        stamp_at = index.get("system_time")
        timestamps: list[int] = []
        values: dict[str, list[float]] = {key: [] for key in CHANNEL_KEYS}
        wall: list[str] = []
        for row in reader:
            if len(row) != len(header):
                continue
            try:
                stamp = int(row[index["timestamp(ms)"]])
                sample = [float(row[index[key]]) for key in CHANNEL_KEYS]
            except ValueError:
                continue
            timestamps.append(stamp)
            for key, value in zip(CHANNEL_KEYS, sample):
                values[key].append(value)
            wall.append(row[stamp_at] if stamp_at is not None else "")
    return (
        np.asarray(timestamps, dtype=np.int64),
        {key: np.asarray(value, dtype=float) for key, value in values.items()},
        wall,
    )


def rebuild_session(
    csv_path: Path,
    out_path: Path | None = None,
    progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> RebuildResult:
    """把一次采集画成 MP4。

    progress(done, total) 会被定期回调；should_cancel() 返回 True 就中止。
    每次调用都新建一张图，所以后台线程跑它不会碰到界面正在画的那张。
    """
    if configure_ffmpeg() is None:
        raise RuntimeError("找不到 ffmpeg，无法写 MP4。装法：brew install ffmpeg")

    timestamps, values, wall = load_session(csv_path)
    if timestamps.size < 2:
        raise ValueError(f"{csv_path.name} 只有 {timestamps.size} 行，没什么可画的")

    out_path = out_path or csv_path.with_suffix(".mp4")
    span_ms = int(timestamps[-1] - timestamps[0])
    frame_count = max(1, int(span_ms / 1000 * FPS) + 1)

    parts = build_ppg_figure(figsize=(WIDTH / DPI, HEIGHT / DPI), dpi=DPI)
    parts.status_text.set_text("Status: RECORDING")

    writer = FFMpegWriter(fps=FPS, metadata={
        "title": "PPG IMU Plot Recording (rebuilt from CSV)",
        "comment": f"rebuilt from {csv_path.name}",
    })
    started = time.monotonic()
    mapping: list[tuple] = []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 先写到临时名，完成后才改成正式名：中途取消或崩掉不会留下半截 MP4
    # 让人误以为是完整的。
    # 扩展名必须还是 .mp4——ffmpeg 靠它决定封装格式，".mp4.partial" 会让它
    # 直接报错退出。
    partial = out_path.with_name(out_path.stem + ".partial.mp4")

    try:
        with writer.saving(parts.figure, str(partial), dpi=DPI):
            for frame in range(frame_count):
                if should_cancel is not None and should_cancel():
                    raise Cancelled
                target = timestamps[0] + frame * 1000 // FPS
                end = int(np.searchsorted(timestamps, target, side="right"))
                if end <= 0:
                    continue
                start = max(0, end - PLOT_WINDOW)

                recent = timestamps[start:end] >= timestamps[end - 1] - SIM_WINDOW_MS
                finger, wrist, other = (values[key][start:end][recent] for key in CHANNEL_KEYS)
                parts.corr_texts["fw"].set_text(
                    f"Finger ↔ Wrist: {format_correlation(correlation(finger, wrist))}")
                parts.corr_texts["fo"].set_text(
                    f"Finger ↔ Other: {format_correlation(correlation(finger, other))}")
                parts.corr_texts["wo"].set_text(
                    f"Wrist ↔ Other: {format_correlation(correlation(wrist, other))}")

                for key in CHANNEL_KEYS:
                    series = values[key][start:end]
                    if series.size < PLOT_WINDOW:
                        series = np.concatenate(
                            [np.full(PLOT_WINDOW - series.size, np.nan), series]
                        )
                    parts.lines[key].set_ydata(series)
                parts.time_text.set_text(f"System time: {wall[end - 1]}")

                writer.grab_frame()
                mapping.append(
                    (frame, round(frame / FPS, 3), int(timestamps[end - 1]), wall[end - 1])
                )
                if progress is not None and frame % 200 == 0:
                    progress(frame, frame_count)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    partial.replace(out_path)

    # 帧号 ↔ 时间戳对照表：标注时不用猜某一帧是几点。
    mapping_path = out_path.with_name(out_path.stem + "_帧时间对照.csv")
    with mapping_path.open("w", newline="", encoding="utf-8") as handle:
        writer_csv = csv.writer(handle)
        writer_csv.writerow(["frame", "video_seconds", "timestamp(ms)", "system_time"])
        writer_csv.writerows(mapping)

    if progress is not None:
        progress(frame_count, frame_count)
    return RebuildResult(
        video_path=out_path,
        mapping_path=mapping_path,
        frames=frame_count,
        seconds=round(frame_count / FPS, 1),
        rows=int(timestamps.size),
        elapsed=round(time.monotonic() - started, 1),
    )
