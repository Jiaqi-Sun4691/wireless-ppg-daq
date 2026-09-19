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
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg

from .plot import (
    CHANNELS,
    MAPPING_SUFFIX,
    REBUILT_SUFFIX,
    PLOT_WINDOW,
    SIM_WINDOW_MS,
    build_ppg_figure,
    correlation,
    format_correlation,
)
from .recorder import configure_ffmpeg

FPS = 20
# 分段少于这个帧数就不值得单起一个进程（启动 + 拼接的开销更大）。
MIN_FRAMES_PER_WORKER = 400
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


def _encoder(ffmpeg_path: str, out_path: Path, title: str) -> subprocess.Popen:
    """开一个吃裸 RGBA、吐 h264 的 ffmpeg。

    不用 matplotlib 的 FFMpegWriter：它的 grab_frame() 内部会 savefig()，
    等于把整张图重画一遍，blitting 缓存的背景就白费了。
    """
    command = [
        ffmpeg_path, "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{WIDTH}x{HEIGHT}", "-pix_fmt", "rgba",
        "-framerate", str(FPS), "-loglevel", "error", "-i", "pipe:",
        # 画面是细线条加文字，4:2:0 色度抽样对它伤害最大：实测把抽样关掉
        # （yuv444p）PSNR 从 37.5 升到 52.1 dB，而文件反而更小——4:2:0 糊
        # 掉彩色线产生的伪影本身就很占码率。单纯降 CRF 只换来 0.3 dB，说明
        # 瓶颈一直在色度而不在量化。
        # High 4:4:4 Predictive 这个 profile 已验证 macOS AVFoundation
        # （QuickTime、访达预览）能正常解码。
        "-vcodec", "h264", "-crf", "18", "-pix_fmt", "yuv444p",
        "-metadata", "title=PPG IMU Plot Recording (rebuilt from CSV)",
        "-metadata", f"comment=rebuilt from {title}",
        "-y", str(out_path),
    ]
    return subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )


def render_range(
    csv_path: Path,
    out_path: Path,
    first_frame: int,
    last_frame: int,
    ffmpeg_path: str,
    counter=None,
    progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    total_frames: int | None = None,
) -> list[tuple]:
    """把 [first_frame, last_frame) 这一段画成一个 MP4，返回帧时间对照行。

    切成多段分别渲染再拼，是因为这活儿在单核上跑不满机器：每帧真正的开销
    是 matplotlib 画那几个文字对象，纯 CPU，分到几个核上近似线性加速。
    """
    timestamps, values, wall = load_session(csv_path)
    parts = build_ppg_figure(figsize=(WIDTH / DPI, HEIGHT / DPI), dpi=DPI)
    parts.status_text.set_text("Status: RECORDING")
    canvas = FigureCanvasAgg(parts.figure)
    dynamic = parts.dynamic_artists
    # 抓背景之前必须把动态部分藏起来，否则它们的初始值（"N/A"、
    # "System time: --"）会被烤进背景，之后每帧画上新值就是一层叠影。
    for artist in dynamic:
        artist.set_visible(False)
    canvas.draw()
    background = canvas.copy_from_bbox(parts.figure.bbox)
    for artist in dynamic:
        artist.set_visible(True)

    process = _encoder(ffmpeg_path, out_path, csv_path.name)
    mapping: list[tuple] = []
    try:
        for frame in range(first_frame, last_frame):
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
                    # 开头窗口没填满：那段数据从没进过 CSV，留空而不是补 0。
                    series = np.concatenate(
                        [np.full(PLOT_WINDOW - series.size, np.nan), series]
                    )
                parts.lines[key].set_ydata(series)
            parts.time_text.set_text(f"System time: {wall[end - 1]}")

            canvas.restore_region(background)
            for artist in dynamic:
                parts.figure.draw_artist(artist)
            canvas.blit(parts.figure.bbox)
            process.stdin.write(canvas.buffer_rgba())

            mapping.append(
                (frame, round(frame / FPS, 3), int(timestamps[end - 1]), wall[end - 1])
            )
            if counter is not None:
                with counter.get_lock():
                    counter.value += 1
            elif progress is not None and frame % 200 == 0:
                progress(frame - first_frame, last_frame - first_frame)
    except BaseException:
        process.stdin.close()
        process.kill()
        process.wait()
        out_path.unlink(missing_ok=True)
        raise

    process.stdin.close()
    if process.wait() != 0:
        error = process.stderr.read().decode("utf-8", "replace").strip()
        out_path.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg 编码失败：{error or '未知错误'}")
    return mapping


def rebuild_session(
    csv_path: Path,
    out_path: Path | None = None,
    progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    workers: int | None = None,
) -> RebuildResult:
    """把一次采集画成 MP4。

    progress(done, total) 会被定期回调；should_cancel() 返回 True 就中止。
    每次调用都新建图，所以后台线程跑它不会碰到界面正在画的那张。

    帧数多的时候自动切成几段丢给多个进程并行渲染，最后用 concat + -c copy
    拼起来——不重新编码，所以拼接本身不损失画质。
    """
    ffmpeg_path = configure_ffmpeg()
    if ffmpeg_path is None:
        raise RuntimeError("找不到 ffmpeg，无法写 MP4。装法：brew install ffmpeg")

    timestamps, _values, _wall = load_session(csv_path)
    if timestamps.size < 2:
        raise ValueError(f"{csv_path.name} 只有 {timestamps.size} 行，没什么可画的")

    # 不写成 <stem>.mp4：那是采集时实时录屏的名字，重建不该把它覆盖掉。
    out_path = out_path or csv_path.with_name(csv_path.stem + REBUILT_SUFFIX)
    span_ms = int(timestamps[-1] - timestamps[0])
    frame_count = max(1, int(span_ms / 1000 * FPS) + 1)
    started = time.monotonic()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时名，完成后才改成正式名：中途取消或崩掉不会留下半截 MP4 让人
    # 误以为是完整的。临时名要保住 .mp4 后缀——ffmpeg 靠它决定封装格式。
    partial = out_path.with_name(out_path.stem + ".partial.mp4")

    if workers is None:
        # 只用性能核；能效核跑这活儿反而拖慢整体（要等最慢那一段）。
        workers = max(1, (os.cpu_count() or 2) - 2)
    # 段太短的话，进程启动和拼接的开销就把收益吃掉了。
    workers = max(1, min(workers, frame_count // MIN_FRAMES_PER_WORKER or 1))

    if workers == 1:
        mapping = render_range(
            csv_path, partial, 0, frame_count, ffmpeg_path,
            progress=progress, should_cancel=should_cancel,
            total_frames=frame_count,
        )
    else:
        mapping = _render_parallel(
            csv_path, partial, frame_count, ffmpeg_path, workers,
            progress, should_cancel,
        )

    partial.replace(out_path)

    # 帧号 ↔ 时间戳对照表：标注时不用猜某一帧是几点。
    mapping_path = out_path.with_name(out_path.stem + MAPPING_SUFFIX)
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


def _render_parallel(
    csv_path: Path,
    partial: Path,
    frame_count: int,
    ffmpeg_path: str,
    workers: int,
    progress: Callable[[int, int], None] | None,
    should_cancel: Callable[[], bool] | None,
) -> list[tuple]:
    bounds = [round(frame_count * i / workers) for i in range(workers + 1)]
    temporary = Path(tempfile.mkdtemp(prefix="ppg_rebuild_"))
    project_root = Path(__file__).resolve().parent.parent
    pieces: list[Path] = []
    processes: list[subprocess.Popen] = []

    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(project_root)
    # 故意让子进程继承父进程的 MPLCONFIGDIR。给它们各自指一个新目录看着更
    # "干净"，实际是每个进程都要重建一遍 matplotlib 字体缓存——实测 8 个
    # 进程为此在开头空转了 10 秒，比渲染本身还久。缓存是只读共享的，父进程
    # 这时已经建好图，缓存必然是热的。

    try:
        for index in range(workers):
            first, last = bounds[index], bounds[index + 1]
            if last <= first:
                continue
            piece = temporary / f"part{index:03d}.mp4"
            pieces.append(piece)
            processes.append(subprocess.Popen(
                [sys.executable, "-m", "ppg_collector.rebuild_worker",
                 str(csv_path), str(piece), str(first), str(last), ffmpeg_path],
                cwd=str(project_root), env=environment,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            ))

        while any(process.poll() is None for process in processes):
            if should_cancel is not None and should_cancel():
                for process in processes:
                    process.kill()
                raise Cancelled
            if progress is not None:
                progress(_count_progress(pieces), frame_count)
            time.sleep(0.3)

        for process, piece in zip(processes, pieces):
            if process.returncode != 0:
                error = process.stderr.read().decode("utf-8", "replace").strip()
                raise RuntimeError(
                    f"渲染 {piece.name} 失败：{error.splitlines()[-1] if error else '未知错误'}"
                )

        # concat demuxer + -c copy：只把各段的码流串起来，不重新编码，
        # 所以拼接这一步本身不损失画质。
        listing = temporary / "pieces.txt"
        listing.write_text(
            "".join(f"file '{piece.name}'\n" for piece in pieces), encoding="utf-8"
        )
        result = subprocess.run(
            [ffmpeg_path, "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(listing), "-c", "copy", "-y", str(partial)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"拼接分段失败：{result.stderr.strip() or '未知错误'}")

        mapping: list[tuple] = []
        for piece in pieces:
            with piece.with_suffix(".map").open(newline="", encoding="utf-8") as handle:
                mapping.extend(tuple(row) for row in csv.reader(handle))
        return mapping
    except BaseException:
        for process in processes:
            if process.poll() is None:
                process.kill()
        partial.unlink(missing_ok=True)
        raise
    finally:
        for process in processes:
            process.wait()
        shutil.rmtree(temporary, ignore_errors=True)


def _count_progress(pieces: list[Path]) -> int:
    """各段子进程写在 .progress 里的已完成帧数，加起来。"""
    done = 0
    for piece in pieces:
        try:
            done += int(piece.with_suffix(".progress").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return done
