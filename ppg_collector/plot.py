"""三路 PPG 实时波形图的唯一定义。

界面（app.py）和录屏重建（rebuild.py）都从这里取图，所以两边永远长得一样。
如果这份图分成两处各写一遍，改了界面而忘了重建，就会得到"看着像但对不上"
的视频——那种偏差最难发现。

布局沿用原采集脚本 dual-imu-_timeadded_副本.py：400 点窗口、三路 PPG、
Y 轴固定 0–4095、3 秒滑窗相关性。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from matplotlib.figure import Figure

PLOT_WINDOW = 400            # 曲线上保留多少个采样点
SIM_WINDOW_MS = 3000         # 相关性用多长的滑窗
Y_LIMITS = (0, 4095)         # PPG 是 12 位 ADC

CHANNELS: tuple[tuple[str, str, str], ...] = (
    ("finger", "Finger PPG", "tab:blue"),
    ("wrist", "Wrist PPG", "tab:orange"),
    ("other", "Other PPG", "tab:green"),
)


@dataclass(slots=True)
class PlotParts:
    """出图需要动的那些对象。"""

    figure: Figure
    axis: Any
    lines: dict[str, Any]
    time_text: Any
    status_text: Any
    corr_texts: dict[str, Any]

    @property
    def dynamic_artists(self) -> list[Any]:
        """每帧会变的那些，其余（坐标轴、刻度、标题）一帧都不变。

        顺序就是绘制顺序：线先画，图例后画——否则波形会盖住右上角的图例。
        重建录屏靠这个做 blitting，只重画这几样，比整图重绘快约 8 倍。
        """
        artists = list(self.lines.values())
        legend = self.axis.get_legend()
        if legend is not None:
            artists.append(legend)
        artists.append(self.time_text)
        artists.append(self.status_text)
        artists.extend(self.corr_texts.values())
        return artists


def build_ppg_figure(figsize: tuple[float, float] = (10, 6), dpi: int = 100) -> PlotParts:
    figure = Figure(figsize=figsize, dpi=dpi, facecolor="#FFFFFF")
    axis = figure.add_subplot(1, 1, 1)
    x_data = list(range(PLOT_WINDOW))
    initial = [0] * PLOT_WINDOW

    lines: dict[str, Any] = {}
    for key, label, color in CHANNELS:
        lines[key], = axis.plot(x_data, initial, label=label, color=color)

    axis.set_title(" ")
    axis.set_xlabel("Sample Index")
    axis.set_ylabel("Amplitude")
    axis.legend(loc="upper right")
    axis.set_ylim(*Y_LIMITS)

    common = dict(transform=axis.transAxes, va="top", color="black")
    time_text = axis.text(0.02, 1.04, "System time: --", fontsize=11, fontweight="bold", **common)
    status_text = axis.text(0.70, 1.04, "Status: PAUSED", fontsize=11, fontweight="bold", **common)
    axis.text(0.02, 0.97, "Real-time similarity", fontsize=11, fontweight="bold", **common)
    corr_texts = {
        "fw": axis.text(0.02, 0.90, "Finger ↔ Wrist: N/A", fontsize=10, **common),
        "fo": axis.text(0.02, 0.85, "Finger ↔ Other: N/A", fontsize=10, **common),
        "wo": axis.text(0.02, 0.80, "Wrist ↔ Other: N/A", fontsize=10, **common),
    }

    figure.subplots_adjust(left=0.09, right=0.98, bottom=0.10, top=0.90)
    return PlotParts(figure, axis, lines, time_text, status_text, corr_texts)


def correlation(first: np.ndarray, second: np.ndarray) -> float:
    if first.size < 10 or second.size < 10:
        return float("nan")
    # 常数序列的相关性没有定义，numpy 会给 nan 外加一条警告。
    if np.allclose(first, first[0]) or np.allclose(second, second[0]):
        return float("nan")
    return float(np.corrcoef(first, second)[0, 1])


def format_correlation(value: float) -> str:
    return "N/A" if np.isnan(value) else f"{value:.2f}"
