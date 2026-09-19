from __future__ import annotations

from collections import deque
from datetime import datetime
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from .firmware import (
    AUTO_BOARD_SELECTION,
    DEFAULT_FQBN,
    FIRMWARE_TARGETS,
    FirmwareManager,
    FlashResult,
    mac_to_cpp_initializer,
    normalize_mac,
)
from .monitor import STATUS_PERIOD_MS, HealthLevel, HealthMonitor, NodeHealth
from .protocol import (
    DEVICE_IDS,
    DEVICE_LABELS,
    DataSample,
    LineKind,
    StatusPacket,
    csv_columns_for_devices,
    parse_serial_line,
)
import os
import signal
import subprocess

from .library import SessionFile, scan_sessions
from .plot import (
    PLOT_WINDOW,
    SIM_WINDOW_MS,
    build_ppg_figure,
    correlation,
    format_correlation,
)
from .rebuild import Cancelled, rebuild_session
from .recorder import SessionRecorder, configure_ffmpeg
from .serial_io import ReplayWorker, SerialEvent, SerialWorker, available_ports
from .settings import AppSettings, load_settings, save_settings


APP_TITLE = "PPG 多设备采集控制台"
DEMO_PORT = "__DEMO__"

COLORS = {
    "background": "#F4F7FB",
    "surface": "#FFFFFF",
    "navy": "#102A43",
    "blue": "#1677FF",
    "blue_hover": "#0F5FCC",
    "text": "#243B53",
    "muted": "#6B7C93",
    "border": "#D9E2EC",
    "good": "#19A974",
    "warning": "#F59E0B",
    "error": "#E5484D",
    "unknown": "#829AB1",
    "recording": "#D7263D",
}

LEVEL_COLORS = {
    HealthLevel.GOOD: COLORS["good"],
    HealthLevel.WARNING: COLORS["warning"],
    HealthLevel.ERROR: COLORS["error"],
    HealthLevel.UNKNOWN: COLORS["unknown"],
}


class DeviceCard(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        sensor_name: str,
        selection_var: tk.BooleanVar | None = None,
        selection_command: Any = None,
        compact: bool = False,
    ) -> None:
        super().__init__(
            parent,
            bg=COLORS["surface"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
            padx=10 if compact else 16,
            pady=8 if compact else 13,
        )
        self.title = title
        self.sensor_name = sensor_name
        self.compact = compact
        self.selection_check: ttk.Checkbutton | None = None
        self.columnconfigure(0, weight=1)

        if compact:
            self._build_compact(title, sensor_name, selection_var, selection_command)
            return

        header = tk.Frame(self, bg=COLORS["surface"])
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        tk.Label(
            header,
            text=title,
            bg=COLORS["surface"],
            fg=COLORS["navy"],
            font=("Helvetica Neue", 14, "bold"),
        ).grid(row=0, column=0, sticky="w")

        self.status_label = tk.Label(
            header,
            text="● 等待数据",
            bg=COLORS["surface"],
            fg=COLORS["unknown"],
            font=("Helvetica Neue", 11, "bold"),
        )
        self.status_label.grid(row=0, column=1, sticky="e")

        self.sensor_label = tk.Label(
            self,
            text=f"传感器：{sensor_name}",
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=("Helvetica Neue", 11),
            anchor="w",
        )
        self.sensor_label.grid(row=1, column=0, sticky="ew", pady=(13, 4))

        self.battery_label = tk.Label(
            self,
            text="电量：未上报",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Helvetica Neue", 10),
            anchor="w",
        )
        self.battery_label.grid(row=2, column=0, sticky="ew", pady=2)

        self.detail_label = tk.Label(
            self,
            text="等待主控数据",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Helvetica Neue", 10),
            anchor="w",
        )
        self.detail_label.grid(row=3, column=0, sticky="ew", pady=(2, 0))

        if selection_var is not None:
            self.selection_check = ttk.Checkbutton(
                self,
                text="启用此设备",
                variable=selection_var,
                command=selection_command,
                style="Card.TCheckbutton",
            )
            self.selection_check.grid(row=4, column=0, sticky="w", pady=(10, 0))
        else:
            tk.Label(
                self,
                text="时间戳与电脑时间固定保留",
                bg=COLORS["surface"],
                fg=COLORS["muted"],
                font=("Helvetica Neue", 9),
                anchor="w",
            ).grid(row=4, column=0, sticky="w", pady=(10, 0))

    def _build_compact(
        self,
        title: str,
        sensor_name: str,
        selection_var: tk.BooleanVar | None,
        selection_command: Any,
    ) -> None:
        """Single-row card used on the merged capture page."""
        header = tk.Frame(self, bg=COLORS["surface"])
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        tk.Label(
            header,
            text=title.replace(" ESP32", ""),
            bg=COLORS["surface"],
            fg=COLORS["navy"],
            font=("Helvetica Neue", 12, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self.status_label = tk.Label(
            header,
            text="● 等待",
            bg=COLORS["surface"],
            fg=COLORS["unknown"],
            font=("Helvetica Neue", 10, "bold"),
        )
        self.status_label.grid(row=0, column=1, sticky="e")

        # Sensor state, battery and age share one muted line to stay compact.
        self.sensor_label = tk.Label(
            self,
            text=sensor_name,
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=("Helvetica Neue", 9),
            anchor="w",
            justify="left",
        )
        self.sensor_label.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.battery_label = tk.Label(
            self,
            text="",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Helvetica Neue", 9),
            anchor="w",
        )
        self.battery_label.grid(row=2, column=0, sticky="ew")
        self.detail_label = tk.Label(self, text="", bg=COLORS["surface"])  # unused when compact
        if selection_var is not None:
            self.selection_check = ttk.Checkbutton(
                self,
                text="启用此设备",
                variable=selection_var,
                command=selection_command,
                style="Card.TCheckbutton",
            )
            self.selection_check.grid(row=3, column=0, sticky="w", pady=(4, 0))

    def update_state(
        self,
        level: HealthLevel,
        link_text: str,
        sensor_text: str,
        battery_text: str,
        detail_text: str,
    ) -> None:
        color = LEVEL_COLORS[level]
        self.status_label.configure(text=f"● {link_text}", fg=color)
        if self.compact:
            self.sensor_label.configure(text=sensor_text)
            uninformative = ("", "未上报", "未配置", "电量未接")
            extra = (
                detail_text
                if battery_text in uninformative
                else f"{battery_text} · {detail_text}"
            )
            self.battery_label.configure(text=extra)
            return
        self.sensor_label.configure(text=f"传感器：{sensor_text}")
        self.battery_label.configure(text=f"电量：{battery_text}")
        self.detail_label.configure(text=detail_text)


class PPGCollectorApp:
    # 400 点窗口、3 秒相关性窗——定义在 plot.py，这里只是取个别名，
    # 免得同一个数字在两个文件里各写一遍。
    ORIGINAL_PLOT_WINDOW = PLOT_WINDOW
    ORIGINAL_SIM_WINDOW_MS = SIM_WINDOW_MS
    PLOT_INTERVAL_MS = 50
    UI_INTERVAL_MS = 200
    EVENT_INTERVAL_MS = 35

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1360x880")
        self.root.minsize(1050, 700)
        self.root.configure(bg=COLORS["background"])
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.settings = load_settings()
        self.events: queue.Queue[SerialEvent] = queue.Queue()
        self.worker: SerialWorker | ReplayWorker | None = None
        self.connected = False
        self.connected_name = ""
        self.connect_started_at: float | None = None
        self.last_line_monotonic: float | None = None
        self.last_sample_monotonic: float | None = None
        self.last_sample: DataSample | None = None
        self.last_recorded_timestamp: int | None = None
        self.duplicate_timestamps = 0

        self.monitor = HealthMonitor(
            offline_ms=self.settings.offline_ms,
            delayed_ms=self.settings.delayed_ms,
            flat_seconds=self.settings.flat_seconds,
        )
        self.recorder = SessionRecorder()
        # 双击启动的 App 拿到的 PATH 不含 Homebrew，matplotlib 就找不到
        # ffmpeg，录屏会静默失败。开机时一次性定位好。
        self.ffmpeg_path = configure_ffmpeg()
        self.rebuild_thread: threading.Thread | None = None
        self.rebuild_cancel = False
        # 写进 launch.log。录屏失败是事后才发现的那种问题，启动时留一行，
        # 出事时不用猜"当时到底有没有找到 ffmpeg"。
        print(
            f"[ffmpeg] {self.ffmpeg_path}" if self.ffmpeg_path
            else f"[ffmpeg] 未找到（PATH={os.environ.get('PATH', '')}）",
            flush=True,
        )
        self.recording_started_at: float | None = None
        self.total_samples = 0
        self.parse_errors = 0
        self.info_lines = 0
        self.sample_times: deque[float] = deque(maxlen=200)
        self.last_video_frame_at = 0.0
        self.last_ui_update = 0.0
        self.last_plot_update = 0.0
        self.last_log_info = ""
        self.console_pending: deque[str] = deque(maxlen=5000)
        self.console_received_lines = 0
        self.console_displayed_lines = 0
        self.console_paused = False
        self.last_available_ports: tuple[str, ...] = ()
        self.last_firmware_ports: tuple[str, ...] = ()
        saved_firmware_board = self.settings.firmware_fqbn or AUTO_BOARD_SELECTION
        if saved_firmware_board == DEFAULT_FQBN:
            saved_firmware_board = AUTO_BOARD_SELECTION
        self.firmware_manager = FirmwareManager(fqbn=DEFAULT_FQBN)
        self.firmware_events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.firmware_thread: threading.Thread | None = None
        self.firmware_busy = False
        self.firmware_environment_ok = False
        self.firmware_step_index = 0
        self._firmware_row_sync = False
        self.firmware_completed: set[str] = set()

        self.plot_timestamps: deque[int] = deque(maxlen=self.ORIGINAL_PLOT_WINDOW)
        self.plot_data: dict[str, deque[float]] = {
            key: deque(maxlen=self.ORIGINAL_PLOT_WINDOW)
            for key in ("finger", "wrist", "other")
        }

        self.port_var = tk.StringVar(value=self.settings.preferred_port)
        self.baud_var = tk.StringVar(value=str(self.settings.baud_rate))
        self.output_var = tk.StringVar(value=self.settings.output_directory)
        self.prefix_var = tk.StringVar(value=self.settings.filename_prefix)
        self.video_var = tk.BooleanVar(value=self.settings.record_video)
        self.offline_var = tk.StringVar(value=str(self.settings.offline_ms))
        self.delayed_var = tk.StringVar(value=str(self.settings.delayed_ms))
        self.flat_var = tk.StringVar(value=str(self.settings.flat_seconds))

        self.connection_var = tk.StringVar(value="未连接")
        self.protocol_var = tk.StringVar(value="协议：等待识别")
        self.rate_var = tk.StringVar(value="0.0 Hz")
        self.samples_var = tk.StringVar(value="0")
        self.duration_var = tk.StringVar(value="00:00:00")
        # 墙上时钟的开始时刻。recording_started_at 是 monotonic，只能算
        # 时长，换算不回"几点开始的"——对不上行车视频和现场笔记。
        self.start_time_var = tk.StringVar(value="—")
        self.file_var = tk.StringVar(value="尚未开始采集")
        self.footer_var = tk.StringVar(value="请选择 Master 串口并连接。")
        self.corr_fw_var = tk.StringVar(value="N/A")
        self.corr_fo_var = tk.StringVar(value="N/A")
        self.corr_wo_var = tk.StringVar(value="N/A")
        self.console_count_var = tk.StringVar(value="已接收 0 行")
        self.console_autoscroll_var = tk.BooleanVar(value=True)
        self.firmware_port_var = tk.StringVar(value="")
        self.firmware_fqbn_var = tk.StringVar(
            value=saved_firmware_board
        )
        self.master_mac_var = tk.StringVar(value=self.settings.master_mac)
        self.mac_check_var = tk.StringVar(value="")
        self.firmware_environment_var = tk.StringVar(value="正在检查 Arduino 环境…")
        self.firmware_instruction_var = tk.StringVar(
            value="第一步：只连接 Master ESP32，然后检测并烧录。"
        )
        saved_devices = set(self.settings.selected_devices or DEVICE_IDS)
        # Fault alerting: a device must stay bad for a while before we shout,
        # so a single dropped ESP-NOW packet never triggers a false alarm.
        self.alert_bad_since: dict[str, float] = {}
        self.alert_stage: dict[str, int] = {}
        self.alert_last_sound: dict[str, float] = {}
        self.alert_active: set[str] = set()
        self.offline_announced: set[str] = set()
        self.offline_since: dict[str, float] = {}
        self._siren_process = None
        self.offline_last_speech: float = 0.0
        self.alert_sound_var = tk.BooleanVar(value=self.settings.alert_sound)
        self.alert_confirm_var = tk.StringVar(value=str(self.settings.alert_confirm_seconds))
        self.driver_var = tk.StringVar(value=self.settings.driver_name)
        self.other_person_var = tk.StringVar(value=self.settings.other_name)
        self.session_note_var = tk.StringVar(value="")
        self.subject_hint_var = tk.StringVar(value="")
        self.files_sessions: dict[str, SessionFile] = {}
        self.files_summary_var = tk.StringVar(value="")
        self.files_folder_var = tk.StringVar(value="")
        self.device_selection_vars = {
            device_id: tk.BooleanVar(value=device_id in saved_devices)
            for device_id in DEVICE_IDS
        }
        if not any(variable.get() for variable in self.device_selection_vars.values()):
            for variable in self.device_selection_vars.values():
                variable.set(True)
        self.device_selection_summary_var = tk.StringVar()
        self.overview_csv_selection_var = tk.StringVar(value="4 台设备 / 17 列")
        self.device_selection_controls: list[ttk.Widget] = []
        self.active_recording_devices: tuple[str, ...] = ()

        self._configure_styles()
        self._build_ui()
        self._refresh_ports(first_run=True)
        self.root.after(self.EVENT_INTERVAL_MS, self._poll_events)
        self.root.after(1000, self._auto_refresh_ports)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=COLORS["background"])
        style.configure("Surface.TFrame", background=COLORS["surface"])
        style.configure(
            "TLabel",
            background=COLORS["background"],
            foreground=COLORS["text"],
            font=("Helvetica Neue", 11),
        )
        style.configure(
            "Primary.TButton",
            background=COLORS["blue"],
            foreground="#FFFFFF",
            padding=(15, 8),
            font=("Helvetica Neue", 11, "bold"),
            borderwidth=0,
        )
        style.map("Primary.TButton", background=[("active", COLORS["blue_hover"])])
        style.configure(
            "Secondary.TButton",
            background="#E9F2FF",
            foreground=COLORS["blue"],
            padding=(13, 8),
            font=("Helvetica Neue", 10, "bold"),
            borderwidth=0,
        )
        style.configure(
            "Danger.TButton",
            background=COLORS["recording"],
            foreground="#FFFFFF",
            padding=(15, 8),
            font=("Helvetica Neue", 11, "bold"),
            borderwidth=0,
        )
        style.configure("TNotebook", background=COLORS["background"], borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            padding=(18, 10),
            font=("Helvetica Neue", 11, "bold"),
            background="#E8EEF5",
            foreground=COLORS["muted"],
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", COLORS["surface"])],
            foreground=[("selected", COLORS["navy"])],
        )
        style.configure(
            "Treeview",
            rowheight=31,
            background=COLORS["surface"],
            fieldbackground=COLORS["surface"],
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
        )
        style.configure(
            "Treeview.Heading",
            background="#EAF0F6",
            foreground=COLORS["navy"],
            font=("Helvetica Neue", 10, "bold"),
            padding=7,
        )
        style.configure(
            "Card.TCheckbutton",
            background=COLORS["surface"],
            foreground=COLORS["blue"],
            font=("Helvetica Neue", 10, "bold"),
        )
        style.map(
            "Card.TCheckbutton",
            background=[("active", COLORS["surface"])],
            foreground=[("disabled", COLORS["muted"])],
        )

    def _build_ui(self) -> None:
        self.root.grid_rowconfigure(2, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        self._build_header()
        self._build_control_bar()
        self._build_notebook()
        self._build_footer()

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=COLORS["navy"], padx=24, pady=15)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        tk.Label(
            header,
            text=APP_TITLE,
            bg=COLORS["navy"],
            fg="#FFFFFF",
            font=("Helvetica Neue", 21, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            header,
            text="3 路 PPG · 2 路 IMU · 数据与波形同步保存",
            bg=COLORS["navy"],
            fg="#B9CDE1",
            font=("Helvetica Neue", 10),
        ).grid(row=1, column=0, sticky="w", pady=(3, 0))

        self.header_status = tk.Label(
            header,
            textvariable=self.connection_var,
            bg=COLORS["navy"],
            fg="#B9CDE1",
            font=("Helvetica Neue", 11, "bold"),
        )
        self.header_status.grid(row=0, column=1, rowspan=2, sticky="e")

    def _build_control_bar(self) -> None:
        bar = tk.Frame(
            self.root,
            bg=COLORS["surface"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
            padx=20,
            pady=11,
        )
        bar.grid(row=1, column=0, sticky="ew", padx=16, pady=(14, 10))
        bar.columnconfigure(8, weight=1)

        tk.Label(
            bar,
            text="串口",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Helvetica Neue", 10, "bold"),
        ).grid(row=0, column=0, padx=(0, 7))
        self.port_combo = ttk.Combobox(
            bar,
            textvariable=self.port_var,
            width=28,
            state="readonly",
        )
        self.port_combo.grid(row=0, column=1, sticky="w")

        self.refresh_button = ttk.Button(
            bar,
            text="刷新",
            style="Secondary.TButton",
            command=self._refresh_ports,
        )
        self.refresh_button.grid(row=0, column=2, padx=(7, 4))

        self.connect_button = ttk.Button(
            bar,
            text="连接设备",
            style="Primary.TButton",
            command=self._toggle_connection,
        )
        self.connect_button.grid(row=0, column=3, padx=4)

        separator = ttk.Separator(bar, orient="vertical")
        separator.grid(row=0, column=4, sticky="ns", padx=12)

        self.start_button = ttk.Button(
            bar,
            text="● 开始采集",
            style="Danger.TButton",
            command=self._start_recording,
            state="disabled",
        )
        self.start_button.grid(row=0, column=5, padx=(4, 4))

        self.stop_button = ttk.Button(
            bar,
            text="停止并保存",
            style="Secondary.TButton",
            command=self._stop_recording,
            state="disabled",
        )
        self.stop_button.grid(row=0, column=6, padx=4)

        # Replaces the original script's "press Q in the plot" exit.
        self.quit_button = ttk.Button(
            bar,
            text="保存并退出",
            style="Secondary.TButton",
            command=self._on_close,
        )
        self.quit_button.grid(row=0, column=7, padx=(12, 4))

        self.recording_badge = tk.Label(
            bar,
            text="待机",
            bg="#EDF2F7",
            fg=COLORS["muted"],
            padx=12,
            pady=6,
            font=("Helvetica Neue", 10, "bold"),
        )
        self.recording_badge.grid(row=0, column=9, sticky="e")

    def _build_notebook(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=2, column=0, sticky="nsew", padx=16)

        # Three top-level pages, grouped by when they are used:
        # capture (every session) / serial+log (debugging) / setup (one-off).
        self.capture_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=12)
        self.serial_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=12)
        self.files_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=12)
        # Flashing is the very first thing a new rig needs, so it gets its own
        # top-level tab instead of hiding inside 设置.
        self.firmware_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=16)
        self.settings_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=18)

        # Panels inside the merged capture page.
        self.overview_tab = ttk.Frame(self.capture_tab, style="Surface.TFrame", padding=0)
        self.plot_tab = ttk.Frame(self.capture_tab, style="Surface.TFrame", padding=0)

        self.notebook.add(self.capture_tab, text="采集")
        self.notebook.add(self.serial_tab, text="串口与日志")
        self.notebook.add(self.files_tab, text="数据文件")
        self.notebook.add(self.firmware_tab, text="固件烧录")
        self.notebook.add(self.settings_tab, text="设置")

        self._build_capture_tab()
        self._build_overview_tab()
        self._build_plot_tab()
        self._build_serial_tab()
        self._build_files_tab()
        self._build_firmware_tab()
        self._build_settings_tab()

    def _build_capture_tab(self) -> None:
        """Subject row, device status strip, then the original plot."""
        page = self.capture_tab
        page.columnconfigure(0, weight=1)
        page.rowconfigure(2, weight=1)
        self._build_subject_row(page)
        self.overview_tab.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.plot_tab.grid(row=2, column=0, sticky="nsew")

    def _build_subject_row(self, parent: tk.Misc) -> None:
        """Who is driving / who is the reference — recorded per session.

        Different people's driving style may itself be the discriminating
        signal, so the identities have to be captured at collection time; they
        cannot be reconstructed from the CSV afterwards.
        """
        row = tk.Frame(parent, bg=COLORS["surface"])
        row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        row.columnconfigure(5, weight=1)

        def label(text, column):
            tk.Label(
                row, text=text, bg=COLORS["surface"], fg=COLORS["muted"],
                font=("Helvetica Neue", 10, "bold"),
            ).grid(row=0, column=column, padx=(0 if column == 0 else 14, 6))

        label("驾驶者", 0)
        self.driver_entry = ttk.Entry(row, textvariable=self.driver_var, width=14)
        self.driver_entry.grid(row=0, column=1, sticky="w")

        label("对照者", 2)
        self.other_entry = ttk.Entry(row, textvariable=self.other_person_var, width=14)
        self.other_entry.grid(row=0, column=3, sticky="w")

        label("备注", 4)
        self.note_entry = ttk.Entry(row, textvariable=self.session_note_var)
        self.note_entry.grid(row=0, column=5, sticky="ew")

        tk.Label(
            row, textvariable=self.subject_hint_var,
            bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Helvetica Neue", 9),
        ).grid(row=0, column=6, padx=(12, 0))

    def _build_overview_tab(self) -> None:
        """Compact status strip: five device cards in one row, metrics beside them."""
        page = self.overview_tab
        for column in range(5):
            page.columnconfigure(column, weight=1, uniform="cards")
        page.columnconfigure(5, weight=0)

        cards = {
            "master": ("Master ESP32", "USB 串口 / 主控"),
            "finger": ("Finger ESP32", "Finger PPG"),
            "wrist": ("Wrist ESP32", "Wrist PPG + IMU"),
            "other": ("Other ESP32", "Other PPG"),
            "wheel": ("Wheel ESP32", "Wheel IMU"),
        }
        self.device_cards: dict[str, DeviceCard] = {}
        for column, (node_id, (title, sensor_name)) in enumerate(cards.items()):
            card = DeviceCard(
                page,
                title,
                sensor_name,
                selection_var=self.device_selection_vars.get(node_id),
                selection_command=self._update_device_selection_summary,
                compact=True,
            )
            card.grid(row=0, column=column, sticky="nsew", padx=3)
            self.device_cards[node_id] = card
            if card.selection_check is not None:
                self.device_selection_controls.append(card.selection_check)

        session = tk.Frame(
            page,
            bg="#F1F6FC",
            highlightbackground=COLORS["border"],
            highlightthickness=1,
            padx=12,
            pady=8,
        )
        session.grid(row=0, column=5, sticky="nsew", padx=(8, 0))
        session.columnconfigure(1, weight=1)
        metrics = (
            ("频率", self.rate_var),
            ("样本", self.samples_var),
            ("开始", self.start_time_var),
            ("时长", self.duration_var),
            ("CSV", self.overview_csv_selection_var),
        )
        for row, (label, variable) in enumerate(metrics):
            tk.Label(
                session,
                text=label,
                bg="#F1F6FC",
                fg=COLORS["muted"],
                font=("Helvetica Neue", 9),
            ).grid(row=row, column=0, sticky="w", padx=(0, 10))
            tk.Label(
                session,
                textvariable=variable,
                bg="#F1F6FC",
                fg=COLORS["text"],
                font=("Helvetica Neue", 9, "bold"),
            ).grid(row=row, column=1, sticky="e")

    def _build_plot_tab(self) -> None:
        self.plot_tab.rowconfigure(0, weight=1)
        self.plot_tab.columnconfigure(0, weight=1)
        # 图本身定义在 plot.py，界面和录屏重建共用同一份——分开写两份的话，
        # 改了这边忘了那边，就会得到"看着像但对不上"的视频。
        parts = build_ppg_figure()
        self.figure = parts.figure
        self.ppg_axis = parts.axis
        self.plot_lines = parts.lines
        self.plot_time_text = parts.time_text
        self.plot_status_text = parts.status_text
        self.plot_corr_fw_text = parts.corr_texts["fw"]
        self.plot_corr_fo_text = parts.corr_texts["fo"]
        self.plot_corr_wo_text = parts.corr_texts["wo"]
        self.plot_canvas = FigureCanvasTkAgg(self.figure, master=self.plot_tab)
        self.plot_canvas.draw()
        self.plot_canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")

        tk.Label(
            self.plot_tab,
            text="波形与原采集脚本一致（400 点、三路 PPG、3 秒相关性）。所有操作都在顶部按钮，无需键盘。",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Helvetica Neue", 9),
        ).grid(row=1, column=0, sticky="w", pady=(5, 0))

    def _build_serial_tab(self) -> None:
        page = self.serial_tab
        page.rowconfigure(1, weight=1)
        page.columnconfigure(0, weight=1)

        toolbar = tk.Frame(page, bg=COLORS["surface"])
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        toolbar.columnconfigure(1, weight=1)

        tk.Label(
            toolbar,
            text="Master 串口实时输出",
            bg=COLORS["surface"],
            fg=COLORS["navy"],
            font=("Helvetica Neue", 18, "bold"),
        ).grid(row=0, column=0, sticky="w")

        tk.Label(
            toolbar,
            textvariable=self.console_count_var,
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Helvetica Neue", 10),
        ).grid(row=0, column=1, sticky="e", padx=12)

        ttk.Checkbutton(
            toolbar,
            text="自动滚动",
            variable=self.console_autoscroll_var,
        ).grid(row=0, column=2, padx=6)

        self.console_pause_button = ttk.Button(
            toolbar,
            text="暂停显示",
            style="Secondary.TButton",
            command=self._toggle_console_pause,
        )
        self.console_pause_button.grid(row=0, column=3, padx=6)

        ttk.Button(
            toolbar,
            text="清空",
            style="Secondary.TButton",
            command=self._clear_serial_console,
        ).grid(row=0, column=4, padx=(6, 0))

        console_frame = tk.Frame(page, bg="#0B1220")
        console_frame.grid(row=1, column=0, sticky="nsew")
        console_frame.rowconfigure(0, weight=1)
        console_frame.columnconfigure(0, weight=1)

        self.console_text = tk.Text(
            console_frame,
            wrap="none",
            state="disabled",
            bg="#0B1220",
            fg="#D7E3F4",
            insertbackground="#FFFFFF",
            selectbackground="#24476B",
            relief="flat",
            font=("Menlo", 10),
            padx=10,
            pady=9,
        )
        vertical_scroll = ttk.Scrollbar(
            console_frame,
            orient="vertical",
            command=self.console_text.yview,
        )
        horizontal_scroll = ttk.Scrollbar(
            console_frame,
            orient="horizontal",
            command=self.console_text.xview,
        )
        self.console_text.configure(
            yscrollcommand=vertical_scroll.set,
            xscrollcommand=horizontal_scroll.set,
        )
        self.console_text.grid(row=0, column=0, sticky="nsew")
        vertical_scroll.grid(row=0, column=1, sticky="ns")
        horizontal_scroll.grid(row=1, column=0, sticky="ew")

        tk.Label(
            page,
            text="上：Master 每一行原始输出（暂停显示不影响采集与保存）。下：程序运行日志。",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Helvetica Neue", 9),
            anchor="w",
        ).grid(row=2, column=0, sticky="ew", pady=(8, 0))

        self._build_run_log(page)

    def _build_run_log(self, page: tk.Misc) -> None:
        """Run log lives with the serial console instead of its own page."""
        page.rowconfigure(4, weight=1)

        tk.Label(
            page,
            text="运行日志",
            bg=COLORS["surface"],
            fg=COLORS["navy"],
            font=("Helvetica Neue", 13, "bold"),
        ).grid(row=3, column=0, sticky="w", pady=(14, 6))

        log_frame = tk.Frame(page, bg=COLORS["surface"])
        log_frame.grid(row=4, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_frame,
            height=9,
            wrap="word",
            state="disabled",
            bg="#F7F9FC",
            fg=COLORS["text"],
            relief="flat",
            font=("Menlo", 10),
            padx=10,
            pady=8,
        )
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")

    def _build_files_tab(self) -> None:
        """Browse, open and delete previously recorded sessions."""
        page = self.files_tab
        page.columnconfigure(0, weight=1)
        page.rowconfigure(2, weight=1)

        header = tk.Frame(page, bg=COLORS["surface"])
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(1, weight=1)
        tk.Label(
            header,
            text="已保存的采集",
            bg=COLORS["surface"],
            fg=COLORS["navy"],
            font=("Helvetica Neue", 16, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            header,
            textvariable=self.files_summary_var,
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Helvetica Neue", 10),
        ).grid(row=0, column=1, sticky="e", padx=10)
        ttk.Button(
            header,
            text="刷新列表",
            style="Secondary.TButton",
            command=self._refresh_files_list,
        ).grid(row=0, column=2)

        path_row = tk.Frame(page, bg=COLORS["surface"])
        path_row.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        path_row.columnconfigure(0, weight=1)
        tk.Label(
            path_row,
            textvariable=self.files_folder_var,
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Menlo", 9),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        ttk.Button(
            path_row,
            text="打开文件夹",
            style="Secondary.TButton",
            command=self._open_output_folder,
        ).grid(row=0, column=1, padx=(8, 0))

        columns = ("recorded", "subject", "rows", "duration", "size", "cols", "devices", "video")
        headings = {
            "recorded": "采集时间",
            "subject": "驾驶者 / 对照者",
            "rows": "样本数",
            "duration": "时长",
            "size": "大小",
            "cols": "CSV 列",
            "devices": "设备",
            "video": "录屏",
        }
        widths = {
            "recorded": 150, "subject": 160, "rows": 80, "duration": 70, "size": 80,
            "cols": 60, "devices": 300, "video": 60,
        }
        # extended：可以按住 shift / cmd 多选，一次删掉好几次采集。
        self.files_tree = ttk.Treeview(
            page, columns=columns, show="headings", height=12, selectmode="extended"
        )
        for column in columns:
            anchor = "w" if column in ("devices", "subject") else "center"
            self.files_tree.heading(column, text=headings[column])
            self.files_tree.column(column, width=widths[column], anchor=anchor)
        self.files_tree.grid(row=2, column=0, sticky="nsew")
        files_scroll = ttk.Scrollbar(page, orient="vertical", command=self.files_tree.yview)
        self.files_tree.configure(yscrollcommand=files_scroll.set)
        files_scroll.grid(row=2, column=1, sticky="ns")
        self.files_tree.bind("<<TreeviewSelect>>", lambda _event: self._update_files_buttons())
        self.files_tree.bind("<Double-1>", lambda _event: self._reveal_selected_session())
        # 直接在列表上操作：右键出菜单，Delete / Backspace 删除。
        self.files_menu = tk.Menu(self.files_tree, tearoff=0)
        self.files_menu.add_command(label="在访达中显示", command=self._reveal_selected_session)
        self.files_menu.add_command(
            label="打开 CSV", command=lambda: self._open_selected_session("csv")
        )
        self.files_menu.add_command(
            label="播放录屏", command=lambda: self._open_selected_session("video")
        )
        self.files_menu.add_command(
            label="生成波形录屏", command=self._rebuild_selected_session
        )
        self.files_menu.add_separator()
        self.files_menu.add_command(label="删除这次采集", command=self._delete_selected_session)
        # macOS 上右键落在 Button-2 还是 Button-3 取决于 Tk 版本和鼠标，
        # 三个都绑上；Control+左键是触控板用户的老习惯。
        for sequence in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
            self.files_tree.bind(sequence, self._show_files_menu)
        for sequence in ("<Delete>", "<BackSpace>"):
            self.files_tree.bind(sequence, lambda _event: self._delete_selected_session())

        actions = tk.Frame(page, bg=COLORS["surface"])
        actions.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.files_reveal_button = ttk.Button(
            actions,
            text="在访达中显示",
            style="Secondary.TButton",
            command=self._reveal_selected_session,
            state="disabled",
        )
        self.files_reveal_button.grid(row=0, column=0, padx=(0, 6))
        self.files_open_csv_button = ttk.Button(
            actions,
            text="打开 CSV",
            style="Secondary.TButton",
            command=lambda: self._open_selected_session("csv"),
            state="disabled",
        )
        self.files_open_csv_button.grid(row=0, column=1, padx=6)
        self.files_open_video_button = ttk.Button(
            actions,
            text="播放录屏",
            style="Secondary.TButton",
            command=lambda: self._open_selected_session("video"),
            state="disabled",
        )
        self.files_open_video_button.grid(row=0, column=2, padx=6)
        self.files_rebuild_button = ttk.Button(
            actions,
            text="生成波形录屏",
            style="Secondary.TButton",
            command=self._rebuild_selected_session,
            state="disabled",
        )
        self.files_rebuild_button.grid(row=0, column=3, padx=6)
        self.files_delete_button = ttk.Button(
            actions,
            text="删除这次采集",
            style="Secondary.TButton",
            command=self._delete_selected_session,
            state="disabled",
        )
        self.files_delete_button.grid(row=0, column=4, padx=(18, 0))

        tk.Label(
            page,
            text="每次采集一个文件夹（CSV + 波形录屏），行车视频等也可以放进去；删除会把整次采集移到废纸篓。",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Helvetica Neue", 9),
            anchor="w",
        ).grid(row=4, column=0, sticky="ew", pady=(8, 0))

        self._refresh_files_list()

    def _selected_session(self) -> SessionFile | None:
        selection = self.files_tree.selection()
        if not selection:
            return None
        return self.files_sessions.get(selection[0])

    def _selected_sessions(self) -> list[SessionFile]:
        found = [self.files_sessions.get(item) for item in self.files_tree.selection()]
        return [session for session in found if session is not None]

    def _show_files_menu(self, event) -> str:
        """Right-click: act on the row under the cursor."""
        row = self.files_tree.identify_row(event.y)
        if not row:
            return "break"
        # Clicking outside the current selection moves to that row; clicking
        # inside it keeps the multi-selection intact.
        if row not in self.files_tree.selection():
            self.files_tree.selection_set(row)
        self.files_tree.focus(row)
        self._update_files_menu()
        try:
            self.files_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.files_menu.grab_release()
        return "break"

    # 菜单项下标：0 显示 / 1 CSV / 2 录屏 / 3 分隔线 / 4 删除。
    # 按下标而不是按标签配置——删除项的标签会随选中数量变，按旧标签查会找不到。
    (
        FILES_MENU_REVEAL,
        FILES_MENU_CSV,
        FILES_MENU_VIDEO,
        FILES_MENU_REBUILD,
        FILES_MENU_DELETE,
    ) = 0, 1, 2, 3, 5

    def _update_files_menu(self) -> None:
        sessions = self._selected_sessions()
        single = "normal" if len(sessions) == 1 else "disabled"
        has_video = len(sessions) == 1 and sessions[0].video_path is not None
        self.files_menu.entryconfigure(self.FILES_MENU_REVEAL, state=single)
        self.files_menu.entryconfigure(self.FILES_MENU_CSV, state=single)
        self.files_menu.entryconfigure(
            self.FILES_MENU_VIDEO, state="normal" if has_video else "disabled"
        )
        # 已经有录屏就不必再生成；正在跑的时候也不让再点。
        can_rebuild = (
            len(sessions) == 1
            and not has_video
            and self.rebuild_thread is None
            and self.ffmpeg_path is not None
        )
        self.files_menu.entryconfigure(
            self.FILES_MENU_REBUILD, state="normal" if can_rebuild else "disabled"
        )
        self.files_menu.entryconfigure(
            self.FILES_MENU_DELETE,
            state="normal" if sessions else "disabled",
            label=(
                "删除这次采集" if len(sessions) <= 1
                else f"删除选中的 {len(sessions)} 次采集"
            ),
        )

    def _update_files_buttons(self) -> None:
        sessions = self._selected_sessions()
        # 打开类操作对"多选"没有意义，只有删除支持批量。
        single = "normal" if len(sessions) == 1 else "disabled"
        self.files_reveal_button.configure(state=single)
        self.files_open_csv_button.configure(state=single)
        self.files_delete_button.configure(
            state="normal" if sessions else "disabled",
            text="删除这次采集" if len(sessions) <= 1 else f"删除选中的 {len(sessions)} 次",
        )
        has_video = len(sessions) == 1 and sessions[0].video_path is not None
        self.files_open_video_button.configure(state="normal" if has_video else "disabled")
        can_rebuild = (
            len(sessions) == 1
            and not has_video
            and self.rebuild_thread is None
            and self.ffmpeg_path is not None
        )
        self.files_rebuild_button.configure(state="normal" if can_rebuild else "disabled")

    def _refresh_files_list(self) -> None:
        directory = Path(self.output_var.get()).expanduser()
        self.files_folder_var.set(str(directory))
        for item in self.files_tree.get_children():
            self.files_tree.delete(item)
        self.files_sessions = {}

        if not directory.is_dir():
            self.files_summary_var.set("文件夹尚未创建")
            self._update_files_buttons()
            return

        sessions = scan_sessions(directory)
        total_bytes = 0
        for index, session in enumerate(sessions):
            item_id = f"session{index}"
            self.files_sessions[item_id] = session
            total_bytes += session.size_bytes + session.video_bytes
            self.files_tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    session.recorded_text,
                    session.subject_text,
                    f"{session.row_count:,}",
                    session.duration_text,
                    session.size_text,
                    str(len(session.columns)),
                    session.device_text,
                    "有" if session.video_path is not None else "—",
                ),
            )
        if sessions:
            from .library import human_size

            self.files_summary_var.set(
                f"共 {len(sessions)} 次采集 · 合计 {human_size(total_bytes)}"
            )
        else:
            self.files_summary_var.set("这个文件夹里还没有采集记录")
        self._update_files_buttons()

    def _open_output_folder(self) -> None:
        directory = Path(self.output_var.get()).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(directory)], check=False)

    def _reveal_selected_session(self) -> None:
        session = self._selected_session()
        if session is None:
            return
        # Newer sessions have their own folder — open it so the dashcam footage
        # and notes you drop in are right there. Older flat files just reveal.
        folder = session.folder
        if folder.name.startswith(session.csv_path.stem[:8]) and folder.is_dir():
            subprocess.run(["open", str(folder)], check=False)
        else:
            subprocess.run(["open", "-R", str(session.csv_path)], check=False)

    def _open_selected_session(self, kind: str) -> None:
        session = self._selected_session()
        if session is None:
            return
        target = session.csv_path if kind == "csv" else session.video_path
        if target is None or not target.exists():
            messagebox.showwarning("文件不存在", "这个文件已经不在磁盘上，请刷新列表。")
            self._refresh_files_list()
            return
        subprocess.run(["open", str(target)], check=False)

    def _rebuild_selected_session(self) -> None:
        """从 CSV 画出这次采集的波形录屏，在后台线程里跑。"""
        if self.rebuild_thread is not None:
            messagebox.showinfo("正在生成", "已经有一次录屏在生成，请等它结束。")
            return
        session = self._selected_session()
        if session is None:
            return
        if self.ffmpeg_path is None:
            messagebox.showerror(
                "缺少 ffmpeg",
                "生成录屏需要 ffmpeg。\n\n在终端执行：brew install ffmpeg",
            )
            return

        # 渲染大约要采集时长的三分之一，值得先说一声再开始。
        minutes = session.row_count * 0.048 / 60
        if not messagebox.askyesno(
            "生成波形录屏",
            f"{session.recorded_text}\n"
            f"{session.row_count:,} 条数据，约 {minutes:.1f} 分钟\n\n"
            f"预计需要 {minutes / 3:.0f}~{minutes / 2:.0f} 分钟渲染，期间可以继续采集。\n\n"
            "开始生成吗？",
        ):
            return

        csv_path = session.csv_path
        self.rebuild_cancel = False
        self.notebook.select(self.files_tab)
        self._log(f"开始生成波形录屏：{csv_path.name}")

        def report(done: int, total: int) -> None:
            # 线程里不能碰 Tk，交回主线程。
            self.root.after(0, lambda: self.footer_var.set(
                f"正在生成波形录屏 {done * 100 // max(total, 1)}%（{done:,}/{total:,} 帧）"
            ))

        def work() -> None:
            try:
                result = rebuild_session(
                    csv_path,
                    progress=report,
                    should_cancel=lambda: self.rebuild_cancel,
                )
            except Cancelled:
                self.root.after(0, lambda: self._rebuild_done(None, "已取消"))
            except Exception as exc:
                message = str(exc)
                self.root.after(0, lambda: self._rebuild_done(None, message))
            else:
                self.root.after(0, lambda: self._rebuild_done(result, None))

        self.rebuild_thread = threading.Thread(target=work, daemon=True, name="rebuild-video")
        self.rebuild_thread.start()
        self._update_files_buttons()

    def _rebuild_done(self, result, error: str | None) -> None:
        self.rebuild_thread = None
        if error is not None:
            self.footer_var.set(f"波形录屏未生成：{error}")
            self._log(f"波形录屏未生成：{error}", error=True)
            if error != "已取消":
                messagebox.showerror("生成失败", error)
        else:
            self.footer_var.set(
                f"波形录屏已生成：{result.frames:,} 帧 / {result.seconds} 秒"
            )
            self._log(
                f"波形录屏已生成（耗时 {result.elapsed / 60:.1f} 分）：{result.video_path}"
            )
        self._refresh_files_list()

    @staticmethod
    def _trash_targets(session: SessionFile) -> list[Path]:
        """What to move to Trash for one session."""
        folder = session.folder
        # A session that owns its own folder goes as a whole, so anything the
        # user dropped in there (dashcam footage, notes) goes with it.
        if folder.is_dir() and folder.name == session.csv_path.stem:
            return [folder]
        return [session.csv_path] + (
            [session.video_path] if session.video_path else []
        )

    def _delete_selected_session(self) -> None:
        sessions = self._selected_sessions()
        if not sessions:
            return

        if len(sessions) == 1:
            title = "删除这次采集"
            listing = [path.name for path in self._trash_targets(sessions[0])]
        else:
            title = f"删除选中的 {len(sessions)} 次采集"
            listing = [
                f"{session.recorded_text}  {session.subject_text}"
                for session in sessions
            ]
        # 删除是不可逆的方向，所以把行数一并摆出来——空采集和一小时的数据
        # 在列表里长得一样，确认框是最后一道防线。
        rows = sum(session.row_count for session in sessions)
        if not messagebox.askyesno(
            title,
            "以下内容将被移到废纸篓：\n\n"
            + "\n".join(listing)
            + f"\n\n共 {rows:,} 条数据。确定删除吗？",
        ):
            return

        targets = [path for session in sessions for path in self._trash_targets(session)]
        # Move to Trash rather than unlink, so a mis-click is recoverable.
        script = "".join(
            f'tell application "Finder" to delete POSIX file "{path}"\n' for path in targets
        )
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True
        )
        if result.returncode != 0:
            messagebox.showerror("删除失败", result.stderr.strip() or "无法移动到废纸篓。")
        else:
            self._log(f"已删除 {len(sessions)} 次采集，共 {rows:,} 条数据。")
        self._refresh_files_list()

    def _build_firmware_tab(self) -> None:
        page = self.firmware_tab
        page.columnconfigure(0, weight=1)
        page.rowconfigure(6, weight=1)

        header = tk.Frame(page, bg=COLORS["surface"])
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        tk.Label(
            header,
            text="ESP32 通用配置与自动烧录",
            bg=COLORS["surface"],
            fg=COLORS["navy"],
            font=("Helvetica Neue", 18, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self.firmware_environment_label = tk.Label(
            header,
            textvariable=self.firmware_environment_var,
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Helvetica Neue", 10, "bold"),
        )
        self.firmware_environment_label.grid(row=0, column=1, sticky="e")

        instruction = tk.Label(
            page,
            textvariable=self.firmware_instruction_var,
            bg="#EEF6FF",
            fg=COLORS["text"],
            justify="left",
            anchor="w",
            padx=14,
            pady=11,
            wraplength=1000,
        )
        instruction.grid(row=1, column=0, sticky="ew", pady=(10, 10))

        configuration = tk.Frame(page, bg=COLORS["surface"])
        configuration.grid(row=2, column=0, sticky="ew")
        configuration.columnconfigure(1, weight=1)
        configuration.columnconfigure(4, weight=1)

        tk.Label(
            configuration,
            text="开发板识别",
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=("Helvetica Neue", 10, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self.firmware_fqbn_combo = ttk.Combobox(
            configuration,
            textvariable=self.firmware_fqbn_var,
            values=(
                AUTO_BOARD_SELECTION,
                "esp32:esp32:esp32",
                "esp32:esp32:esp32s2",
                "esp32:esp32:esp32s3",
                "esp32:esp32:esp32c3",
                "esp32:esp32:esp32c6",
            ),
            width=31,
        )
        self.firmware_fqbn_combo.grid(row=0, column=1, sticky="ew", padx=(8, 18))

        tk.Label(
            configuration,
            text="当前 USB 串口",
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=("Helvetica Neue", 10, "bold"),
        ).grid(row=0, column=2, sticky="w")
        self.firmware_port_combo = ttk.Combobox(
            configuration,
            textvariable=self.firmware_port_var,
            state="readonly",
            width=27,
        )
        self.firmware_port_combo.grid(row=0, column=3, sticky="ew", padx=8)
        self.firmware_port_refresh_button = ttk.Button(
            configuration,
            text="刷新",
            style="Secondary.TButton",
            command=self._firmware_refresh_ports,
        )
        self.firmware_port_refresh_button.grid(row=0, column=4, sticky="w")

        tk.Label(
            configuration,
            text="检测到的 Master MAC",
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=("Helvetica Neue", 10, "bold"),
        ).grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.master_mac_entry = ttk.Entry(
            configuration,
            textvariable=self.master_mac_var,
            width=31,
        )
        self.master_mac_entry.grid(row=1, column=1, sticky="ew", padx=(8, 18), pady=(10, 0))
        self.mac_check_label = tk.Label(
            configuration,
            textvariable=self.mac_check_var,
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Menlo", 10),
            anchor="w",
        )
        self.mac_check_label.grid(row=1, column=2, columnspan=3, sticky="w", pady=(10, 0))
        self.master_mac_var.trace_add("write", lambda *_: self._refresh_mac_check())
        self._refresh_mac_check()

        columns = ("step", "device", "status")
        self.firmware_tree = ttk.Treeview(
            page,
            columns=columns,
            show="headings",
            height=5,
        )
        self.firmware_tree.heading("step", text="顺序")
        self.firmware_tree.heading("device", text="设备")
        self.firmware_tree.heading("status", text="状态")
        self.firmware_tree.column("step", width=70, anchor="center")
        self.firmware_tree.column("device", width=300, anchor="w")
        self.firmware_tree.column("status", width=260, anchor="center")
        self.firmware_tree.grid(row=3, column=0, sticky="ew", pady=(12, 8))
        # Colour the rows so the current step is obvious at a glance.
        self.firmware_tree.tag_configure("current", background="#FFF3CD")
        self.firmware_tree.tag_configure("done", background="#E9F8F2")
        self.firmware_tree.tag_configure("failed", background="#FDE8EC")
        for index, target in enumerate(FIRMWARE_TARGETS, start=1):
            self.firmware_tree.insert(
                "",
                "end",
                iid=target.target_id,
                values=(index, target.display_name, "等待"),
            )
        # Pick any board directly instead of always following the 1→5 order.
        self.firmware_tree.bind("<<TreeviewSelect>>", self._on_firmware_row_selected)

        actions = tk.Frame(page, bg=COLORS["surface"])
        actions.grid(row=4, column=0, sticky="ew")
        actions.columnconfigure(3, weight=1)
        self.firmware_action_button = ttk.Button(
            actions,
            text="检测并烧录当前设备",
            style="Primary.TButton",
            command=self._firmware_flash_current,
        )
        self.firmware_action_button.grid(row=0, column=0, padx=(0, 6))
        self.firmware_read_mac_button = ttk.Button(
            actions,
            text="只读取 Master MAC",
            style="Secondary.TButton",
            command=self._firmware_read_master_mac,
        )
        self.firmware_read_mac_button.grid(row=0, column=1, padx=6)
        self.firmware_reset_button = ttk.Button(
            actions,
            text="重新开始向导",
            style="Secondary.TButton",
            command=self._firmware_reset_wizard,
        )
        self.firmware_reset_button.grid(row=0, column=2, padx=6)
        ttk.Button(
            actions,
            text="打开固件源码",
            style="Secondary.TButton",
            command=self._open_firmware_folder,
        ).grid(row=0, column=3, padx=6)
        self.firmware_progress = ttk.Progressbar(actions, mode="indeterminate", length=250)
        self.firmware_progress.grid(row=0, column=4, sticky="e")

        tk.Label(
            page,
            text="编译与烧录日志",
            bg=COLORS["surface"],
            fg=COLORS["navy"],
            font=("Helvetica Neue", 12, "bold"),
        ).grid(row=5, column=0, sticky="w", pady=(12, 6))
        firmware_log_frame = tk.Frame(page, bg="#0B1220")
        firmware_log_frame.grid(row=6, column=0, sticky="nsew")
        firmware_log_frame.rowconfigure(0, weight=1)
        firmware_log_frame.columnconfigure(0, weight=1)
        self.firmware_log_text = tk.Text(
            firmware_log_frame,
            height=8,
            wrap="word",
            state="disabled",
            bg="#0B1220",
            fg="#D7E3F4",
            relief="flat",
            font=("Menlo", 9),
            padx=9,
            pady=8,
        )
        firmware_scroll = ttk.Scrollbar(
            firmware_log_frame,
            orient="vertical",
            command=self.firmware_log_text.yview,
        )
        self.firmware_log_text.configure(yscrollcommand=firmware_scroll.set)
        self.firmware_log_text.grid(row=0, column=0, sticky="nsew")
        firmware_scroll.grid(row=0, column=1, sticky="ns")

        environment_ok, environment_text = self.firmware_manager.environment_status()
        if environment_ok and self.firmware_fqbn_var.get() == AUTO_BOARD_SELECTION:
            environment_text = "Arduino 工具已就绪；将对每块板单独自动识别"
        self.firmware_environment_ok = environment_ok
        self.firmware_environment_var.set(environment_text)
        self.firmware_environment_label.configure(
            fg=COLORS["good"] if environment_ok else COLORS["error"]
        )
        if not environment_ok:
            self.firmware_action_button.configure(state="disabled")
        self._firmware_refresh_ports(silent=True)
        self._update_firmware_instruction()

    def _firmware_refresh_ports(self, silent: bool = False) -> None:
        ports = available_ports()
        values = [device for device, _description in ports]
        self.last_firmware_ports = tuple(values)
        display_values = values or ["（未检测到 USB ESP32）"]
        self.firmware_port_combo.configure(values=display_values)

        selected = self.firmware_port_var.get().strip()
        if selected not in display_values:
            preferred = self.port_var.get().strip()
            if preferred in values:
                selected = preferred
            else:
                usb_ports = [value for value in values if "usb" in value.lower()]
                selected = usb_ports[0] if usb_ports else display_values[0]
            self.firmware_port_var.set(selected)

        if not silent:
            if ports:
                self._append_firmware_log(
                    f"已发现 {len(ports)} 个 USB 串口，当前选择 {self.firmware_port_var.get()}。"
                )
            else:
                self._append_firmware_log("未发现 ESP32。请使用可传输数据的 USB 线连接当前设备。")

    def _update_firmware_instruction(self) -> None:
        # "Done" means every board is in the completed set — not that the cursor
        # ran off the end, which can happen when flashing out of order.
        remaining = self._remaining_targets()
        if remaining and self.firmware_step_index >= len(FIRMWARE_TARGETS):
            self._advance_to_next_unflashed()
        if not remaining:
            self.firmware_instruction_var.set(
                "全部 5 块 ESP32 已完成烧录。请把 Master 接回电脑，"
                "回到“采集”页连接串口并检查各设备状态。"
            )
            self.firmware_action_button.configure(text="烧录已完成", state="disabled")
            self.firmware_instruction_var.set(
                self.firmware_instruction_var.get()
                + "　｜　需要重烧某一块，点上方对应的行即可。"
            )
            return

        target = FIRMWARE_TARGETS[self.firmware_step_index]
        if target.target_id == "master":
            instruction = (
                "第 1 步：只连接 Master ESP32（其他板先不要接 USB）。"
                "点下方按钮后会烧录 Master，并自动读取记住它的 MAC。"
                "　｜　已经烧过的话，可以直接点上方任意一行，单独烧那一块。"
            )
            action_text = "烧录 Master 并读取 MAC"
        else:
            mac_text = self.master_mac_var.get().strip()
            if mac_text:
                instruction = (
                    f"第 {self.firmware_step_index + 1} 步：只连接 {target.display_name}（其他板先拔掉）。"
                    f"Master MAC = {mac_text}，烧录时自动写入。"
                    "　｜　也可以直接点上方任意一行，单独烧那一块。"
                )
            else:
                instruction = (
                    f"只连接 {target.display_name}（其他板先拔掉）。"
                    "但还没有 Master MAC —— 请先烧 Master，或在下方手动填入 MAC，否则从机不知道该发给谁。"
                )
            action_text = f"注入 MAC 并烧录 {target.display_name}"

        self.firmware_instruction_var.set(instruction)
        self.firmware_action_button.configure(text=action_text)
        if self.firmware_environment_ok and not self.firmware_busy:
            self.firmware_action_button.configure(state="normal")
        # Only one row may be the current step: clear any stale marker first.
        for other in FIRMWARE_TARGETS:
            if other.target_id == target.target_id:
                continue
            if self.firmware_tree.set(other.target_id, "status") == "当前步骤":
                self._set_firmware_tree_status(other.target_id, "等待")
        if target.target_id not in self.firmware_completed:
            current_status = self.firmware_tree.set(target.target_id, "status")
            if current_status in ("等待", "当前步骤"):
                self._set_firmware_tree_status(target.target_id, "当前步骤")
        self._firmware_row_sync = True
        try:
            self.firmware_tree.selection_set(target.target_id)
            self.firmware_tree.focus(target.target_id)
            self.firmware_tree.see(target.target_id)
        finally:
            self._firmware_row_sync = False

    def _open_firmware_folder(self) -> None:
        """Open the editable firmware sources in Finder.

        These .ino files are what the wizard compiles; edits take effect on the
        next flash with no restart needed.
        """
        folder = self.firmware_manager.firmware_root
        if not folder.is_dir():
            messagebox.showerror("找不到固件目录", str(folder))
            return
        subprocess.run(["open", str(folder)], check=False)
        self._append_firmware_log(
            f"已打开固件源码目录：{folder}　修改后直接再烧录即可，不需要重启程序。"
        )

    def _remaining_targets(self) -> list:
        """Targets still not flashed, in wizard order."""
        return [t for t in FIRMWARE_TARGETS if t.target_id not in self.firmware_completed]

    def _advance_to_next_unflashed(self) -> None:
        """Move the cursor to the next board that still needs flashing.

        Progress is tracked by the completed set, not by the cursor, because the
        user may flash boards in any order.
        """
        remaining = self._remaining_targets()
        if not remaining:
            self.firmware_step_index = len(FIRMWARE_TARGETS)
            return
        nxt = remaining[0]
        self.firmware_step_index = next(
            i for i, t in enumerate(FIRMWARE_TARGETS) if t.target_id == nxt.target_id
        )

    def _refresh_mac_check(self) -> None:
        """Show whether the MAC is usable and exactly what gets compiled in."""
        raw = self.master_mac_var.get().strip()
        if not raw:
            self.mac_check_var.set("尚未填写：烧 Master 会自动读取，也可手动输入")
            self.mac_check_label.configure(fg=COLORS["muted"])
            return
        try:
            initializer = mac_to_cpp_initializer(raw)
        except ValueError:
            self.mac_check_var.set("✗ 格式不对，应为 AA:BB:CC:DD:EE:FF")
            self.mac_check_label.configure(fg=COLORS["recording"])
            return
        self.mac_check_var.set(f"✓ 将写入从机： {{{initializer}}}")
        self.mac_check_label.configure(fg=COLORS["good"])

    def _on_firmware_row_selected(self, _event: Any = None) -> None:
        """Clicking a row makes that board the flash target."""
        if self._firmware_row_sync or self.firmware_busy:
            return
        selection = self.firmware_tree.selection()
        if not selection:
            return
        target_id = selection[0]
        index = next(
            (i for i, t in enumerate(FIRMWARE_TARGETS) if t.target_id == target_id),
            None,
        )
        if index is None or index == self.firmware_step_index:
            return
        self.firmware_step_index = index
        self._update_firmware_instruction()

    def _set_firmware_tree_status(self, target_id: str, status: str) -> None:
        self.firmware_tree.set(target_id, "status", status)
        if status.startswith("✓"):
            tag = "done"
        elif "失败" in status:
            tag = "failed"
        elif status == "当前步骤":
            tag = "current"
        else:
            tag = ""
        self.firmware_tree.item(target_id, tags=(tag,) if tag else ())

    def _append_firmware_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.firmware_log_text.configure(state="normal")
        self.firmware_log_text.insert("end", f"[{timestamp}] {message}\n")
        lines = int(self.firmware_log_text.index("end-1c").split(".")[0])
        if lines > 600:
            self.firmware_log_text.delete("1.0", "100.0")
        self.firmware_log_text.see("end")
        self.firmware_log_text.configure(state="disabled")

    def _firmware_set_busy(self, busy: bool) -> None:
        self.firmware_busy = busy
        if busy:
            self.firmware_progress.start(12)
            state = "disabled"
            self.firmware_action_button.configure(state=state)
            self.firmware_read_mac_button.configure(state=state)
            self.firmware_reset_button.configure(state=state)
            self.firmware_port_refresh_button.configure(state=state)
            self.firmware_port_combo.configure(state=state)
            self.firmware_fqbn_combo.configure(state=state)
            self.master_mac_entry.configure(state=state)
            return

        self.firmware_progress.stop()
        self.firmware_read_mac_button.configure(state="normal")
        self.firmware_reset_button.configure(state="normal")
        self.firmware_port_refresh_button.configure(state="normal")
        self.firmware_port_combo.configure(state="readonly")
        self.firmware_fqbn_combo.configure(state="normal")
        self.master_mac_entry.configure(state="normal")
        if self.firmware_environment_ok and self.firmware_step_index < len(FIRMWARE_TARGETS):
            self.firmware_action_button.configure(state="normal")
        else:
            self.firmware_action_button.configure(state="disabled")

    def _firmware_selected_port(self) -> str | None:
        port = self.firmware_port_var.get().strip()
        if not port or port.startswith("（"):
            messagebox.showwarning(
                "未检测到 ESP32",
                "请先用 USB 数据线连接当前要烧录的 ESP32，然后点击“刷新”。",
            )
            return None
        return port

    def _release_serial_for_firmware(self, port: str) -> None:
        """Hand the port to arduino-cli: it needs exclusive access to flash."""
        if self.worker is None:
            return
        self._disconnect()
        self._log(f"为烧录释放串口 {port}。")

    def _firmware_flash_current(self) -> None:
        if self.firmware_busy or (
            self.firmware_thread is not None and self.firmware_thread.is_alive()
        ):
            return
        if self.recorder.active:
            messagebox.showwarning("正在采集", "请先停止并保存当前采集，再进行固件烧录。")
            return
        if self.firmware_step_index >= len(FIRMWARE_TARGETS):
            return
        port = self._firmware_selected_port()
        if port is None:
            return
        self._release_serial_for_firmware(port)

        target = FIRMWARE_TARGETS[self.firmware_step_index]
        master_mac: str | None = None
        if target.requires_master_mac:
            try:
                master_mac = normalize_mac(self.master_mac_var.get())
            except ValueError as exc:
                messagebox.showerror("Master MAC 无效", str(exc))
                return
            self.master_mac_var.set(master_mac)

        board_selection = self.firmware_fqbn_var.get().strip() or AUTO_BOARD_SELECTION
        self.firmware_fqbn_var.set(board_selection)
        auto_detect_board = board_selection == AUTO_BOARD_SELECTION
        fqbn = DEFAULT_FQBN if auto_detect_board else board_selection
        self.firmware_manager.fqbn = fqbn
        environment_ok, environment_text = self.firmware_manager.environment_status()
        if environment_ok and auto_detect_board:
            environment_text = "Arduino 工具已就绪；本次将自动识别芯片"
        self.firmware_environment_ok = environment_ok
        self.firmware_environment_var.set(environment_text)
        self.firmware_environment_label.configure(
            fg=COLORS["good"] if environment_ok else COLORS["error"]
        )
        if not environment_ok:
            messagebox.showerror("烧录环境未就绪", environment_text)
            self._firmware_set_busy(False)
            return

        confirmed = messagebox.askokcancel(
            f"烧录 {target.display_name}",
            f"请确认现在只连接了 {target.display_name}。\n\n"
            f"串口：{port}\n"
            f"开发板：{board_selection}\n"
            "稳定参数：115200 上传速度、DIO Flash 模式\n\n"
            "烧录期间请不要拔线或关闭程序。",
        )
        if not confirmed:
            return

        if self.worker is not None:
            self._disconnect()
        self._set_firmware_tree_status(target.target_id, "正在编译 / 烧录…")
        self._append_firmware_log(
            f"开始处理 {target.display_name}（{port}）。"
        )
        if master_mac is not None:
            self._append_firmware_log(f"本次自动注入 Master MAC：{master_mac}")
        self._firmware_set_busy(True)

        def work() -> None:
            try:
                result = self.firmware_manager.flash(
                    target.target_id,
                    port,
                    master_mac=master_mac,
                    log=lambda line: self.firmware_events.put(("log", line)),
                    auto_detect_board=auto_detect_board,
                    fqbn=fqbn,
                )
            except Exception as exc:
                self.firmware_events.put(("error", (target.target_id, str(exc))))
            else:
                self.firmware_events.put(("flash_success", result))

        self.firmware_thread = threading.Thread(
            target=work,
            daemon=True,
            name=f"firmware-{target.target_id}",
        )
        self.firmware_thread.start()

    def _firmware_read_master_mac(self) -> None:
        if self.firmware_busy or (
            self.firmware_thread is not None and self.firmware_thread.is_alive()
        ):
            return
        if self.recorder.active:
            messagebox.showwarning("正在采集", "请先停止并保存当前采集。")
            return
        port = self._firmware_selected_port()
        if port is None:
            return
        if self.worker is not None:
            self._disconnect()

        self._append_firmware_log(f"正在从 {port} 读取 Master MAC…")
        self._firmware_set_busy(True)

        def work() -> None:
            try:
                mac = self.firmware_manager.read_master_mac(
                    port,
                    timeout_seconds=12.0,
                    log=lambda line: self.firmware_events.put(("log", line)),
                )
            except Exception as exc:
                self.firmware_events.put(("error", ("master", str(exc))))
            else:
                self.firmware_events.put(("mac_success", mac))

        self.firmware_thread = threading.Thread(
            target=work,
            daemon=True,
            name="firmware-read-master-mac",
        )
        self.firmware_thread.start()

    def _poll_firmware_events(self) -> None:
        while True:
            try:
                kind, payload = self.firmware_events.get_nowait()
            except queue.Empty:
                return

            if kind == "log":
                self._append_firmware_log(str(payload))
                continue
            if kind == "flash_success":
                result = payload
                if not isinstance(result, FlashResult):
                    continue
                self.firmware_thread = None
                self.firmware_completed.add(result.target.target_id)
                profile_text = (
                    f" · {result.board_profile.display_name}"
                    if result.board_profile is not None
                    else ""
                )
                self._set_firmware_tree_status(
                    result.target.target_id,
                    f"✓ 烧录成功{profile_text}",
                )
                if result.master_mac:
                    self.master_mac_var.set(result.master_mac)
                    self._append_firmware_log(f"Master MAC 已记住：{result.master_mac}")
                remaining = self._remaining_targets()
                self._advance_to_next_unflashed()
                self._persist_firmware_settings()
                self._firmware_set_busy(False)
                self._update_firmware_instruction()
                if remaining:
                    names = "、".join(t.display_name for t in remaining)
                    messagebox.showinfo(
                        "烧录成功",
                        f"{result.target.display_name} 已完成。\n\n"
                        f"还剩 {len(remaining)} 块没烧：{names}\n\n"
                        f"请拔下这块，接上 {remaining[0].display_name} 继续；"
                        "也可以在列表里点其它行改烧别的。",
                    )
                else:
                    messagebox.showinfo(
                        "全部完成",
                        "Master 和 4 块 Slave 都已烧录完成。\n\n"
                        "请将 Master 接回电脑，然后到采集页连接它。",
                    )
                continue
            if kind == "mac_success":
                mac = normalize_mac(str(payload))
                self.firmware_thread = None
                self.master_mac_var.set(mac)
                self._append_firmware_log(f"已读取并记住 Master MAC：{mac}")
                if self.firmware_step_index == 0:
                    self.firmware_completed.add("master")
                    self._set_firmware_tree_status("master", "✓ MAC 已读取")
                    self.firmware_step_index = 1
                self._persist_firmware_settings()
                self._firmware_set_busy(False)
                self._update_firmware_instruction()
                messagebox.showinfo("MAC 读取成功", f"Master MAC：{mac}\n\n现在可以继续烧录 Slave。")
                continue
            if kind == "error":
                target_id, error_text = payload
                self.firmware_thread = None
                if target_id in {target.target_id for target in FIRMWARE_TARGETS}:
                    self._set_firmware_tree_status(target_id, "失败，可重试")
                self._append_firmware_log(f"失败：{error_text}")
                self._firmware_set_busy(False)
                messagebox.showerror(
                    "烧录或读取失败",
                    f"{error_text}\n\n"
                    "请检查 USB 数据线和串口。如果卡在连接阶段，"
                    "可按住 ESP32 的 BOOT 键后重试，开始写入后再松开。",
                )

    def _persist_firmware_settings(self) -> None:
        self.settings.driver_name = self.driver_var.get().strip()
        self.settings.other_name = self.other_person_var.get().strip()
        self.settings.master_mac = self.master_mac_var.get().strip()
        self.settings.firmware_fqbn = (
            self.firmware_fqbn_var.get().strip() or AUTO_BOARD_SELECTION
        )
        try:
            save_settings(self.settings)
        except OSError as exc:
            self._append_firmware_log(f"无法保存固件设置：{exc}")

    def _firmware_reset_wizard(self) -> None:
        if self.firmware_busy:
            return
        self.firmware_step_index = 0
        self.firmware_completed.clear()
        for target in FIRMWARE_TARGETS:
            self._set_firmware_tree_status(target.target_id, "等待")
        self._append_firmware_log("烧录向导已重置。请从 Master 开始。")
        self._update_firmware_instruction()

    def _build_settings_tab(self) -> None:
        page = self.settings_tab
        page.columnconfigure(1, weight=1)

        tk.Label(
            page,
            text="文件保存",
            bg=COLORS["surface"],
            fg=COLORS["navy"],
            font=("Helvetica Neue", 18, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        self._settings_label(page, "保存文件夹", 1)
        self.output_entry = ttk.Entry(page, textvariable=self.output_var)
        self.output_entry.grid(row=1, column=1, sticky="ew", padx=10, pady=5)
        ttk.Button(page, text="选择…", style="Secondary.TButton", command=self._choose_output).grid(row=1, column=2, pady=5)

        self._settings_label(page, "CSV 文件名前缀", 2)
        self.prefix_entry = ttk.Entry(page, textvariable=self.prefix_var)
        self.prefix_entry.grid(row=2, column=1, sticky="ew", padx=10, pady=5)
        tk.Label(
            page,
            text="自动附加日期时间",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Helvetica Neue", 9),
        ).grid(row=2, column=2, sticky="w")

        self._settings_label(page, "Plot 录像", 3)
        video_row = tk.Frame(page, bg=COLORS["surface"])
        video_row.grid(row=3, column=1, sticky="w", padx=10, pady=5)
        ttk.Checkbutton(
            video_row,
            text="同步保存波形录屏 MP4",
            variable=self.video_var,
        ).pack(side="left")
        # 有没有 ffmpeg 必须看得见：没有它录屏会失败，而失败是事后才发现的。
        tk.Label(
            video_row,
            text=(f"✓ ffmpeg：{self.ffmpeg_path}" if self.ffmpeg_path
                  else "⚠ 找不到 ffmpeg，录屏无法保存（brew install ffmpeg）"),
            bg=COLORS["surface"],
            fg=COLORS["muted"] if self.ffmpeg_path else COLORS["error"],
            font=("Helvetica Neue", 9),
        ).pack(side="left", padx=(10, 0))

        ttk.Separator(page, orient="horizontal").grid(row=4, column=0, columnspan=3, sticky="ew", pady=14)

        tk.Label(
            page,
            text="断线 / 数据卡住判定",
            bg=COLORS["surface"],
            fg=COLORS["navy"],
            font=("Helvetica Neue", 18, "bold"),
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 8))

        self._settings_label(page, "Slave 离线", 6)
        offline_row = tk.Frame(page, bg=COLORS["surface"])
        offline_row.grid(row=6, column=1, sticky="w", padx=10, pady=3)
        ttk.Entry(offline_row, textvariable=self.offline_var, width=10).pack(side="left")
        tk.Label(offline_row, text="毫秒无数据", bg=COLORS["surface"], fg=COLORS["muted"]).pack(side="left", padx=8)

        self._settings_label(page, "延迟警告", 7)
        delayed_row = tk.Frame(page, bg=COLORS["surface"])
        delayed_row.grid(row=7, column=1, sticky="w", padx=10, pady=3)
        ttk.Entry(delayed_row, textvariable=self.delayed_var, width=10).pack(side="left")
        tk.Label(delayed_row, text="毫秒无新包", bg=COLORS["surface"], fg=COLORS["muted"]).pack(side="left", padx=8)

        self._settings_label(page, "数据卡住", 8)
        flat_row = tk.Frame(page, bg=COLORS["surface"])
        flat_row.grid(row=8, column=1, sticky="w", padx=10, pady=3)
        ttk.Entry(flat_row, textvariable=self.flat_var, width=10).pack(side="left")
        tk.Label(flat_row, text="秒内数值完全不变", bg=COLORS["surface"], fg=COLORS["muted"]).pack(side="left", padx=8)

        self._settings_label(page, "故障提示音", 9)
        alert_row = tk.Frame(page, bg=COLORS["surface"])
        alert_row.grid(row=9, column=1, sticky="w", padx=10, pady=3)
        ttk.Checkbutton(
            alert_row,
            text="设备异常时播放提示音",
            variable=self.alert_sound_var,
            command=lambda: None if self.alert_sound_var.get() else self._stop_siren(),
        ).pack(side="left")
        ttk.Button(
            alert_row,
            text="试听",
            style="Secondary.TButton",
            command=lambda: self._play_alert_sound("Ping.aiff"),
        ).pack(side="left", padx=10)

        self._settings_label(page, "报警确认", 10)
        confirm_row = tk.Frame(page, bg=COLORS["surface"])
        confirm_row.grid(row=10, column=1, sticky="w", padx=10, pady=3)
        ttk.Entry(confirm_row, textvariable=self.alert_confirm_var, width=10).pack(side="left")
        tk.Label(
            confirm_row,
            text="秒内持续异常才报警（避免偶发丢包误报）",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
        ).pack(side="left", padx=8)

        ttk.Button(
            page,
            text="保存设置",
            style="Primary.TButton",
            command=self._save_settings_from_ui,
        ).grid(row=11, column=1, sticky="w", padx=10, pady=(12, 5))

        info = tk.Label(
            page,
            textvariable=self.file_var,
            bg="#EEF6FF",
            fg=COLORS["text"],
            justify="left",
            anchor="w",
            padx=14,
            pady=12,
            wraplength=760,
        )
        info.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self._update_device_selection_summary()

    def _selected_devices(self) -> tuple[str, ...]:
        return tuple(
            device_id
            for device_id in DEVICE_IDS
            if self.device_selection_vars[device_id].get()
        )

    def _update_device_selection_summary(self) -> None:
        selected = self._selected_devices()
        if not selected:
            self.device_selection_summary_var.set("尚未选择设备，无法开始采集。")
            self.overview_csv_selection_var.set("未选择")
            return
        columns = csv_columns_for_devices(selected)
        labels = "、".join(DEVICE_LABELS[device_id] for device_id in selected)
        self.device_selection_summary_var.set(
            f"已选择：{labels}；CSV 共 {len(columns)} 列（含时间戳和电脑时间）。"
        )
        self.overview_csv_selection_var.set(
            f"{len(selected)} 台 / {len(columns)} 列"
        )

    def _select_all_devices(self) -> None:
        for variable in self.device_selection_vars.values():
            variable.set(True)
        self._update_device_selection_summary()

    def _clear_device_selection(self) -> None:
        for variable in self.device_selection_vars.values():
            variable.set(False)
        self._update_device_selection_summary()

    def _set_device_selection_state(self, state: str) -> None:
        for widget in self.device_selection_controls:
            widget.configure(state=state)

    def _settings_label(self, parent: tk.Misc, text: str, row: int) -> None:
        tk.Label(
            parent,
            text=text,
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=("Helvetica Neue", 11, "bold"),
        ).grid(row=row, column=0, sticky="w", pady=6)

    def _build_footer(self) -> None:
        footer = tk.Frame(self.root, bg=COLORS["background"], padx=18, pady=8)
        footer.grid(row=3, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        tk.Label(
            footer,
            textvariable=self.footer_var,
            bg=COLORS["background"],
            fg=COLORS["muted"],
            font=("Helvetica Neue", 9),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        tk.Label(
            footer,
            text="v0.1",
            bg=COLORS["background"],
            fg=COLORS["muted"],
            font=("Helvetica Neue", 9),
        ).grid(row=0, column=1, sticky="e")

    def _refresh_ports(self, first_run: bool = False, silent: bool = False) -> None:
        ports = available_ports()
        values = [device for device, _description in ports]
        self.last_available_ports = tuple(values)
        if not values:
            values = ["（未检测到 Master USB 串口）"]
        self.port_combo.configure(values=values)

        preferred = self.port_var.get()
        if preferred not in values:
            usb_ports = [value for value in values if "usb" in value.lower()]
            self.port_var.set(usb_ports[0] if usb_ports else values[0])

        if not ports and self.worker is None:
            self.footer_var.set("尚未检测到 Master ESP32。请连接 USB 数据线，程序会自动刷新串口。")
        elif ports and self.worker is None and self.footer_var.get().startswith("尚未检测"):
            self.footer_var.set(f"已发现串口 {self.port_var.get()}，可以点击“连接设备”。")

        if not first_run and not silent:
            self._log(f"已刷新串口，共发现 {len(ports)} 个设备。")

    def _auto_refresh_ports(self) -> None:
        if self.worker is None:
            current = tuple(device for device, _description in available_ports())
            if current != self.last_available_ports:
                self._refresh_ports(silent=True)
        if not self.firmware_busy:
            firmware_current = tuple(device for device, _description in available_ports())
            if firmware_current != self.last_firmware_ports:
                self._firmware_refresh_ports(silent=True)
        self.root.after(1000, self._auto_refresh_ports)

    def _toggle_connection(self) -> None:
        if self.worker is not None:
            self._disconnect()
        else:
            self._connect_serial()

    def _connect_serial(self) -> None:
        if self.firmware_busy:
            messagebox.showwarning("正在烧录", "请等待固件烧录或 MAC 读取完成。")
            return
        port = self.port_var.get().strip()
        if not port or port.startswith("（"):
            messagebox.showwarning("未选择串口", "没有发现可用串口。请连接主 ESP32 后刷新。")
            return
        try:
            baud = int(self.baud_var.get())
        except ValueError:
            messagebox.showerror("波特率错误", "波特率必须是整数。")
            return

        self._prepare_new_connection()
        self.worker = SerialWorker(port, baud, self.events)
        self.connect_started_at = time.monotonic()
        self.connection_var.set(f"正在连接 {port}…")
        self.connect_button.configure(text="取消连接")
        self.worker.start()
        self.settings.preferred_port = port
        self._log(f"正在连接 {port}，波特率 {baud}。")

    def _start_replay(self, csv_path: str, speed: float = 1.0) -> None:
        """Self-check hook: replay a recorded session. Not exposed in the UI."""
        if self.worker is not None:
            self._disconnect()
        self._prepare_new_connection()
        self.worker = ReplayWorker(csv_path, self.events, speed=speed)
        self.connect_started_at = time.monotonic()
        self.connection_var.set("正在回放历史数据…")
        self.worker.start()
        self._log(f"回放历史采集：{csv_path}")

    def _prepare_new_connection(self) -> None:
        self.monitor = HealthMonitor(
            offline_ms=self._safe_int(self.offline_var.get(), 1500),
            delayed_ms=self._safe_int(self.delayed_var.get(), 500),
            flat_seconds=self._safe_float(self.flat_var.get(), 2.0),

        )
        self.total_samples = 0
        self.parse_errors = 0
        self.sample_times.clear()
        self.plot_timestamps.clear()
        for values in self.plot_data.values():
            values.clear()
        self.last_line_monotonic = None
        self.last_sample_monotonic = None
        self.last_sample = None
        self.last_recorded_timestamp = None
        self.duplicate_timestamps = 0
        self.corr_fw_var.set("N/A")
        self.corr_fo_var.set("N/A")
        self.corr_wo_var.set("N/A")
        self._clear_serial_console()

    def _disconnect(self) -> None:
        if self.recorder.active:
            self._stop_recording(disconnected=True)
        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.stop()
        self._set_disconnected("已断开")
        self._log("设备连接已断开。")

    def _set_connected(self, name: str) -> None:
        self.connected = True
        self.connected_name = name
        self.connection_var.set(f"● 已连接：{name}")
        self.header_status.configure(fg="#64D9B0")
        self.connect_button.configure(text="断开连接")
        self.start_button.configure(state="normal")
        self.footer_var.set("连接成功。确认设备状态后即可开始采集。")
        self.notebook.select(self.capture_tab)

    def _set_disconnected(self, text: str) -> None:
        self.connected = False
        self.connected_name = ""
        self.connection_var.set(text)
        self.header_status.configure(fg="#B9CDE1")
        self.connect_button.configure(text="连接设备")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")

    def _poll_events(self) -> None:
        self._poll_firmware_events()
        processed = 0
        while processed < 300:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            processed += 1
            self._handle_event(event)

        self._flush_serial_console()

        now = time.monotonic()
        if now - self.last_ui_update >= self.UI_INTERVAL_MS / 1000:
            self._refresh_status_ui(now)
            self.last_ui_update = now
        if now - self.last_plot_update >= self.PLOT_INTERVAL_MS / 1000:
            self._refresh_plot(now)
            self.last_plot_update = now

        self.root.after(self.EVENT_INTERVAL_MS, self._poll_events)

    def _handle_event(self, event: SerialEvent) -> None:
        if event.kind == "connected":
            self._set_connected(event.payload)
            self._log(f"连接成功：{event.payload}")
            self._queue_console_line(f"--- 已连接：{event.payload} ---")
            return
        if event.kind == "disconnected":
            self._queue_console_line(f"--- 连接已断开：{event.payload} ---")
            if self.worker is not None and not self.worker.running:
                self.worker = None
                if self.recorder.active:
                    self._stop_recording(disconnected=True)
                self._set_disconnected("连接已断开")
            return
        if event.kind == "error":
            self._log(event.payload, error=True)
            self._queue_console_line(f"--- 错误：{event.payload} ---")
            self.footer_var.set(event.payload)
            if self.worker is not None and not self.worker.running:
                self.worker = None
                self._set_disconnected("连接失败")
            return
        if event.kind != "line":
            return

        now = time.monotonic()
        self.last_line_monotonic = now
        self._queue_console_line(event.payload)
        try:
            parsed = parse_serial_line(event.payload)
        except ValueError as exc:
            self.parse_errors += 1
            if self.parse_errors <= 5 or self.parse_errors % 100 == 0:
                self._log(f"跳过无法解析的数据：{exc}", error=True)
            return

        if parsed.kind == LineKind.DATA:
            sample = parsed.payload
            assert isinstance(sample, DataSample)
            self._handle_sample(sample, now)
        elif parsed.kind == LineKind.STATUS:
            packet = parsed.payload
            assert isinstance(packet, StatusPacket)
            self.monitor.update_status(packet, now)
        else:
            text = str(parsed.payload)
            if text:
                self.info_lines += 1
                if text != self.last_log_info and self.info_lines <= 20:
                    self.last_log_info = text
                    self._log(f"设备：{text}")

    def _queue_console_line(self, line: str) -> None:
        local_time = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.console_pending.append(f"[{local_time}]  {line}\n")
        self.console_received_lines += 1
        suffix = "（显示已暂停）" if self.console_paused else ""
        self.console_count_var.set(f"已接收 {self.console_received_lines:,} 行{suffix}")

    def _flush_serial_console(self) -> None:
        if self.console_paused or not self.console_pending:
            return

        batch: list[str] = []
        while self.console_pending and len(batch) < 500:
            batch.append(self.console_pending.popleft())
        if not batch:
            return

        self.console_text.configure(state="normal")
        self.console_text.insert("end", "".join(batch))
        self.console_displayed_lines += len(batch)

        # Keep the live monitor responsive during long experiments.
        if self.console_displayed_lines > 3000:
            self.console_text.delete("1.0", "501.0")
            self.console_displayed_lines -= 500

        if self.console_autoscroll_var.get():
            self.console_text.see("end")
        self.console_text.configure(state="disabled")

    def _toggle_console_pause(self) -> None:
        self.console_paused = not self.console_paused
        self.console_pause_button.configure(
            text="继续显示" if self.console_paused else "暂停显示"
        )
        suffix = "（显示已暂停）" if self.console_paused else ""
        self.console_count_var.set(f"已接收 {self.console_received_lines:,} 行{suffix}")
        if not self.console_paused:
            self._flush_serial_console()

    def _clear_serial_console(self) -> None:
        self.console_pending.clear()
        self.console_received_lines = 0
        self.console_displayed_lines = 0
        self.console_count_var.set("已接收 0 行")
        if hasattr(self, "console_text"):
            self.console_text.configure(state="normal")
            self.console_text.delete("1.0", "end")
            self.console_text.configure(state="disabled")

    def _handle_sample(self, sample: DataSample, now: float) -> None:
        self.last_sample = sample
        self.last_sample_monotonic = now
        self.total_samples += 1
        self.sample_times.append(now)
        self.monitor.update_sample(sample, now)
        self._append_plot_sample(sample)
        if self.recorder.active:
            if sample.timestamp_ms != self.last_recorded_timestamp:
                self.recorder.write_sample(sample)
                self.last_recorded_timestamp = sample.timestamp_ms
            else:
                self.duplicate_timestamps += 1

    def _append_plot_sample(self, sample: DataSample) -> None:
        self.plot_timestamps.append(sample.timestamp_ms)
        self.plot_data["finger"].append(sample.finger)
        self.plot_data["wrist"].append(sample.wrist)
        self.plot_data["other"].append(sample.other)

    def _refresh_plot(self, now: float) -> None:
        self.plot_time_text.set_text(
            f"System time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._refresh_correlations()
        for key, line in self.plot_lines.items():
            values = list(self.plot_data[key])
            if len(values) < self.ORIGINAL_PLOT_WINDOW:
                values = [0] * (self.ORIGINAL_PLOT_WINDOW - len(values)) + values
            line.set_ydata(values)
        self.plot_canvas.draw_idle()

        if self.recorder.video_active and now - self.last_video_frame_at >= 0.05:
            try:
                self.recorder.grab_video_frame()
            except Exception as exc:
                self._log(f"波形录像写入失败：{exc}", error=True)
            self.last_video_frame_at = now

    def _refresh_status_ui(self, now: float) -> None:
        self.monitor.refresh(now)
        self._refresh_master_card(now)
        for node_id, node in self.monitor.snapshot().items():
            self._update_node_card(node_id, node)
        self._check_device_alerts(now)

        rate = self._sample_rate(now)
        self.rate_var.set(f"{rate:.1f} Hz")
        self.samples_var.set(f"{self.total_samples:,}")
        self.protocol_var.set(
            "增强状态协议" if self.monitor.enhanced_protocol_seen else "16 列兼容模式"
        )
        if self.recorder.active and self.recording_started_at is not None:
            seconds = int(now - self.recording_started_at)
            self.duration_var.set(self._format_duration(seconds))
            self.recording_badge.configure(
                text=f"● 采集中  {self.duration_var.get()}",
                bg="#FDE8EC",
                fg=COLORS["recording"],
            )
        elif self.connected:
            self.recording_badge.configure(text="已连接 · 待机", bg="#E9F8F2", fg=COLORS["good"])
        else:
            self.recording_badge.configure(text="待机", bg="#EDF2F7", fg=COLORS["muted"])

    # Escalating alerts: confidence grows with how long a fault persists, so the
    # prompt goes from a quiet blip to a real alarm instead of shouting at once.
    # Stage thresholds are multiples of the user's "报警确认" seconds.
    ALERT_STAGES = (
        (1.0, "Tink.aiff", "可能异常"),      # just a blip — might be a dropped packet
        (3.0, "Ping.aiff", "持续异常"),      # looking real now
        (10.0, "Sosumi.aiff", "确认故障"),   # alarm, and repeats until resolved
    )
    ALERT_REPEAT_SECONDS = 20.0
    # Spoken names: short and unambiguous when heard rather than read.
    SPOKEN_NAMES = {
        "finger": "指端",
        "wrist": "手腕",
        "other": "对照",
        "wheel": "方向盘",
        "master": "主控",
    }
    VOICE = "Tingting"          # zh_CN 普通话
    VOICE_RATE = "175"
    # An offline module alarms continuously until it comes back. The siren is a
    # looping child process (afplay has no repeat flag), and the spoken name is
    # repeated now and then so you know which module without watching the screen.
    OFFLINE_RESPEAK_SECONDS = 30.0
    SOUND_DIRECTORY = "/System/Library/Sounds"
    # Continuous two-tone siren (generated, loops seamlessly); system
    # sounds are one-shots and leave an audible gap between repeats.
    SIREN_FILE = Path(__file__).resolve().parent.parent / "sounds" / "alarm_siren.wav"

    def _confirm_seconds(self) -> float:
        return max(0.5, self._safe_float(self.alert_confirm_var.get(), 1.5))

    def _alert_stage_for(self, elapsed: float) -> int:
        """0 = nothing yet, otherwise the 1-based stage index."""
        unit = self._confirm_seconds()
        stage = 0
        for index, (multiplier, _sound, _label) in enumerate(self.ALERT_STAGES, start=1):
            if elapsed >= unit * multiplier:
                stage = index
        return stage

    def _speak(self, text: str) -> None:
        """Say it out loud — which module, and what happened."""
        if not self.alert_sound_var.get():
            return
        try:
            subprocess.Popen(
                ["say", "-v", self.VOICE, "-r", self.VOICE_RATE, text],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            self._play_alert_sound("Sosumi.aiff")

    def _spoken_name(self, node_id: str) -> str:
        return self.SPOKEN_NAMES.get(node_id, node_id)

    def _spoken_fault(self, node) -> str:
        """Turn a sensor状态 into something that makes sense heard aloud."""
        who = self._spoken_name(node.node_id)
        text = node.sensor_text
        if "IMU 读取失败" in text:
            return f"{who}的 I M U 读取失败"
        if "IMU" in text and "未更新" in text:
            return f"{who}的 I M U 数据不再更新"
        if "PPG" in text and "未更新" in text:
            return f"{who}的 P P G 数据不再更新"
        if "量程边界" in text or "PPG 异常" in text:
            return f"{who}的 P P G 异常，请检查探头"
        if "PPG、IMU 异常" in text:
            return f"{who}的 P P G 和 I M U 都异常"
        return f"{who}模块异常"

    def _announce_offline(self, nodes: list) -> None:
        """Offline is serious: announce it immediately, by name."""
        names = "、".join(self._spoken_name(n.node_id) for n in nodes)
        self._speak(f"{names}模块离线")
        for n in nodes:
            self._log(f"{n.name} 离线", error=True)
        self.footer_var.set(f"⛔ {names}模块离线")

    def _announce_back_online(self, nodes: list) -> None:
        names = "、".join(self._spoken_name(n.node_id) for n in nodes)
        self._speak(f"{names}模块已上线")
        for n in nodes:
            self._log(f"{n.name} 已恢复上线")
        self.footer_var.set(f"✓ {names}模块已上线")

    def _start_siren(self) -> None:
        """Loop the alarm sound until stopped (afplay cannot repeat on its own)."""
        if self._siren_process is not None and self._siren_process.poll() is None:
            return
        if not self.alert_sound_var.get():
            return
        sound = str(self.SIREN_FILE if self.SIREN_FILE.is_file()
                    else Path(self.SOUND_DIRECTORY) / "Sosumi.aiff")
        try:
            self._siren_process = subprocess.Popen(
                ["/bin/sh", "-c", f'while :; do afplay "{sound}"; done'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            self._siren_process = None

    def _stop_siren(self) -> None:
        process = self._siren_process
        self._siren_process = None
        if process is None or process.poll() is not None:
            return
        try:
            # Kill the whole group: the shell loop may have an afplay child.
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                process.terminate()
            except OSError:
                pass

    def _sustain_offline_alarm(self, now: float) -> None:
        """Keep alarming while any enabled module stays offline."""
        offline = [
            self.monitor.nodes[node_id]
            for node_id in sorted(self.offline_announced)
            if node_id in self.monitor.nodes
        ]
        if not offline:
            self._stop_siren()
            return
        self._start_siren()
        if now - self.offline_last_speech >= self.OFFLINE_RESPEAK_SECONDS:
            self.offline_last_speech = now
            names = "、".join(self._spoken_name(n.node_id) for n in offline)
            longest = max(now - self.offline_since.get(n.node_id, now) for n in offline)
            self._speak(f"{names}模块仍然离线，已持续{int(longest)}秒")

    def _check_device_alerts(self, now: float) -> None:
        if not self.connected:
            self.alert_bad_since.clear()
            self.alert_stage.clear()
            self.alert_last_sound.clear()
            self.alert_active.clear()
            self.offline_announced.clear()
            self.offline_since.clear()
            self._stop_siren()
            return

        triggered: list[tuple[int, NodeHealth, float]] = []
        went_offline: list[NodeHealth] = []
        came_online: list[NodeHealth] = []
        for node_id, node in self.monitor.nodes.items():
            variable = self.device_selection_vars.get(node_id)
            if variable is not None and not variable.get():
                self._clear_alert(node_id, node, quiet=True)
                self.offline_announced.discard(node_id)
                continue

            # Losing the link is serious enough to announce right away, by name,
            # instead of starting with a quiet blip.
            is_offline = node.link_text == "离线"
            if is_offline and node_id not in self.offline_announced:
                self.offline_announced.add(node_id)
                self.offline_since[node_id] = now
                went_offline.append(node)
            elif not is_offline and node_id in self.offline_announced:
                self.offline_announced.discard(node_id)
                self.offline_since.pop(node_id, None)
                came_online.append(node)

            if node.level is not HealthLevel.ERROR:
                self._clear_alert(node_id, node)
                continue

            started = self.alert_bad_since.setdefault(node_id, now)
            stage = self._alert_stage_for(now - started)
            if stage == 0:
                continue

            previous = self.alert_stage.get(node_id, 0)
            top_stage = len(self.ALERT_STAGES)
            due_repeat = (
                stage == top_stage
                and now - self.alert_last_sound.get(node_id, 0.0)
                >= self.ALERT_REPEAT_SECONDS
            )
            if stage > previous or due_repeat:
                self.alert_stage[node_id] = stage
                self.alert_last_sound[node_id] = now
                self.alert_active.add(node_id)
                triggered.append((stage, node, now - started))

        if went_offline:
            self._announce_offline(went_offline)
            self.offline_last_speech = now
        if came_online:
            self._announce_back_online(came_online)
        self._sustain_offline_alarm(now)
        # Tiered tones still cover the non-offline faults (flat signal, I2C
        # failure...). Offline already spoke, so don't double up on it.
        remaining = [
            item for item in triggered
            if item[1].node_id not in self.offline_announced
        ]
        if remaining:
            self._announce_alerts(remaining)

    def _clear_alert(self, node_id: str, node: NodeHealth, quiet: bool = False) -> None:
        if node_id in self.alert_active and not quiet:
            self._log(f"{node.name} 已恢复：{node.link_text}")
            self.footer_var.set(f"{node.name} 已恢复正常。")
        self.alert_bad_since.pop(node_id, None)
        self.alert_stage.pop(node_id, None)
        self.alert_last_sound.pop(node_id, None)
        self.alert_active.discard(node_id)

    def _announce_alerts(self, triggered: list) -> None:
        """Log every faulty node, but sound only one tone at the worst stage."""
        top_stage = max(stage for stage, _node, _elapsed in triggered)
        _multiplier, sound, label = self.ALERT_STAGES[top_stage - 1]
        for stage, node, elapsed in triggered:
            _m, _s, stage_label = self.ALERT_STAGES[stage - 1]
            self._log(
                f"{node.name} {stage_label}（已持续 {elapsed:.0f} 秒）："
                f"{node.link_text} · {node.sensor_text}",
                error=stage >= len(self.ALERT_STAGES),
            )
        prefix = "·" if top_stage == 1 else ("⚠" if top_stage == 2 else "⛔")
        if len(triggered) == 1:
            _s2, node, elapsed = triggered[0]
            summary = f"{node.name} {label}（已持续 {elapsed:.0f} 秒）：{node.sensor_text}"
        else:
            names = "、".join(node.name for _s2, node, _e in triggered)
            summary = f"{len(triggered)} 个设备{label}：{names}"
        self.footer_var.set(f"{prefix} {summary}")
        if top_stage == 1:
            # Stage 1 may just be a finger shifting — a quiet blip is enough.
            self._play_alert_sound(sound)
            return
        # From stage 2 on, say which sensor on which module, so you do not have
        # to look at the screen to know what to check.
        spoken = [self._spoken_fault(node) for _s3, node, _e in triggered]
        self._speak("；".join(dict.fromkeys(spoken)))

    def _play_alert_sound(self, sound: str = "Ping.aiff") -> None:
        if not self.alert_sound_var.get():
            return
        path = f"{self.SOUND_DIRECTORY}/{sound}"
        try:
            # Fire and forget; blocking here would stall the UI loop.
            subprocess.Popen(
                ["afplay", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            try:
                self.root.bell()
            except tk.TclError:
                pass

    def _refresh_master_card(self, now: float) -> None:
        card = self.device_cards["master"]
        if not self.connected:
            card.update_state(
                HealthLevel.ERROR,
                "离线",
                "串口未连接",
                "USB 供电",
                "请选择主控串口",
            )
            return
        if self.last_line_monotonic is None:
            card.update_state(
                HealthLevel.WARNING,
                "已连接",
                "等待串口数据",
                "USB 供电",
                "尚未收到数据",
            )
            return
        age_ms = int((now - self.last_line_monotonic) * 1000)
        if age_ms >= self.monitor.offline_ms:
            level = HealthLevel.ERROR
            link = "无数据"
        elif age_ms >= self.monitor.delayed_ms:
            level = HealthLevel.WARNING
            link = "数据延迟"
        else:
            level = HealthLevel.GOOD
            link = "在线"
        card.update_state(
            level,
            link,
            "USB 串口正常" if level == HealthLevel.GOOD else "检查主控与数据线",
            "USB 供电",
            f"最近数据：{age_ms} ms 前",
        )

    def _update_node_card(self, node_id: str, node: NodeHealth) -> None:
        # A device the user unchecked is not part of this rig, so report it as
        # disabled rather than flagging it offline forever. That keeps a red
        # card meaning a genuine fault.
        variable = self.device_selection_vars.get(node_id)
        if variable is not None and not variable.get():
            self.device_cards[node_id].update_state(
                HealthLevel.UNKNOWN,
                "未启用",
                "本次不使用",
                "",
                "勾选后开始监测",
            )
            return

        battery = self._battery_text(node)
        age = (
            "等待第一次数据变化"
            if node.change_age_ms is None
            else f"距上次数据变化：{self._format_age_ms(node.change_age_ms)}"
        )
        self.device_cards[node_id].update_state(
            node.level,
            node.link_text,
            node.sensor_text,
            battery,
            age,
        )


    def _start_recording(self) -> None:
        if self.firmware_busy:
            messagebox.showwarning("正在烧录", "请等待固件烧录或 MAC 读取完成。")
            return
        if not self.connected:
            messagebox.showwarning("设备未连接", "请先连接主 ESP32。")
            return
        # Confirm who is being recorded. The names carry over from the previous
        # session, so the common mistake is recording a new person under the
        # last one's name — which is unfixable afterwards.
        driver = self.driver_var.get().strip()
        other = self.other_person_var.get().strip()
        note = self.session_note_var.get().strip()
        if not driver:
            if not messagebox.askyesno(
                "没有填驾驶者",
                "这次采集没有填写驾驶者姓名。\n\n"
                "不同人的驾驶风格本身可能就是要区分的特征，事后很难补记。\n\n"
                "仍要继续吗？",
            ):
                self.notebook.select(self.capture_tab)
                self.driver_entry.focus_set()
                return
        else:
            lines = [f"驾驶者：{driver}", f"对照者：{other or '（未填）'}"]
            if note:
                lines.append(f"备注：{note}")
            if not messagebox.askyesno(
                "确认本次采集对象",
                "\n".join(lines) + "\n\n姓名会写进 CSV，采集开始后不能修改。\n确认无误，开始采集？",
            ):
                self.notebook.select(self.capture_tab)
                self.driver_entry.focus_set()
                self.driver_entry.selection_range(0, "end")
                return
        selected_devices = self._selected_devices()
        if not selected_devices:
            self.notebook.select(self.capture_tab)
            messagebox.showwarning("没有选择设备", "请在设备卡上至少启用一个设备。")
            return
        # 录屏要是起不来，必须现在就说。原来只往日志里记一行，等采完一趟车
        # 才发现没有录上——那时候已经补不回来了。
        if self.video_var.get() and self.ffmpeg_path is None:
            if not messagebox.askyesno(
                "录屏无法保存",
                "勾选了「同步保存波形录屏」，但这台电脑上找不到 ffmpeg，"
                "这次的 MP4 不会生成。\n\n"
                "CSV 数据不受影响，照常完整保存。\n\n"
                "装 ffmpeg：在终端执行 brew install ffmpeg\n\n"
                "要就这样开始采集吗？",
            ):
                return
        output = Path(self.output_var.get()).expanduser()
        prefix = self.prefix_var.get()
        try:
            self.recorder.start(
                output,
                prefix,
                self.video_var.get(),
                self.figure,
                selected_devices=selected_devices,
                metadata={
                    "driver": self.driver_var.get().strip(),
                    "other": self.other_person_var.get().strip(),
                    "note": self.session_note_var.get().strip(),
                },
            )
        except OSError as exc:
            messagebox.showerror("无法开始采集", f"无法创建保存文件：\n{exc}")
            return
        except RuntimeError as exc:
            messagebox.showwarning("采集已开始", str(exc))
            return

        self.recording_started_at = time.monotonic()
        # 停止后不清空：常常要事后回填笔记、对行车视频的时间轴。
        self.start_time_var.set(datetime.now().strftime("%H:%M:%S"))
        self.notebook.select(self.capture_tab)
        self.active_recording_devices = selected_devices
        self.last_recorded_timestamp = None
        self.duplicate_timestamps = 0
        self.last_video_frame_at = 0.0
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.output_entry.configure(state="disabled")
        self.prefix_entry.configure(state="disabled")
        self._set_device_selection_state("disabled")
        for widget in (self.driver_entry, self.other_entry, self.note_entry):
            widget.configure(state="disabled")
        self.plot_status_text.set_text("Status: RECORDING")
        selected_labels = "、".join(DEVICE_LABELS[device_id] for device_id in selected_devices)
        self.file_var.set(
            f"正在保存：{self.recorder.csv_path}\n"
            f"本段设备：{selected_labels}\n"
            f"CSV 列数：{len(self.recorder.csv_columns)}"
        )
        self.footer_var.set("正在采集。断开设备或关闭程序前请先停止并保存。")
        self._log(f"开始采集：{self.recorder.csv_path}")
        if self.recorder.video_error:
            self._log(f"CSV 已开始，但波形录屏未启动：{self.recorder.video_error}", error=True)
            # 日志里那一行太容易错过，这是不可逆的损失，用弹窗挡一下。
            messagebox.showwarning(
                "波形录屏未启动",
                f"CSV 正常记录中，但这次不会有 MP4：\n\n{self.recorder.video_error}",
            )

    def _stop_recording(self, disconnected: bool = False) -> None:
        if not self.recorder.active:
            return
        result = self.recorder.stop()
        self.recording_started_at = None
        self.stop_button.configure(state="disabled")
        self.start_button.configure(state="normal" if self.connected and not disconnected else "disabled")
        self.output_entry.configure(state="normal")
        self.prefix_entry.configure(state="normal")
        self._set_device_selection_state("normal")
        for widget in (self.driver_entry, self.other_entry, self.note_entry):
            widget.configure(state="normal")
        self.plot_status_text.set_text("Status: PAUSED")

        if result.row_count:
            file_lines = [f"CSV：{result.csv_path}", f"样本数：{result.row_count:,}"]
        else:
            file_lines = ["本段没有收到数据，空 CSV 已删除。", "样本数：0"]
        selected_labels = "、".join(
            DEVICE_LABELS[device_id] for device_id in self.active_recording_devices
        )
        file_lines.append(f"本段设备：{selected_labels}")
        file_lines.append(f"CSV 列数：{len(result.csv_columns)}")
        if self.duplicate_timestamps:
            file_lines.append(f"跳过重复时间戳：{self.duplicate_timestamps:,} 条")
        if result.video_path and result.video_path.exists():
            file_lines.append(f"波形录像：{result.video_path}")
        elif self.recorder.video_error:
            file_lines.append(f"录像未保存：{self.recorder.video_error}")
        self.file_var.set("\n".join(file_lines))
        self.footer_var.set(f"采集完成，共保存 {result.row_count:,} 条数据。")
        self._log(f"采集已保存，共 {result.row_count:,} 条：{result.csv_path}")
        self._refresh_files_list()
        self.active_recording_devices = ()

    def _choose_output(self) -> None:
        chosen = filedialog.askdirectory(
            title="选择采集数据保存文件夹",
            initialdir=str(Path(self.output_var.get()).expanduser()),
        )
        if chosen:
            self.output_var.set(chosen)
        self._refresh_files_list()


    def _save_settings_from_ui(self) -> None:
        try:
            offline = int(self.offline_var.get())
            delayed = int(self.delayed_var.get())
            flat = float(self.flat_var.get())
            if offline <= 250 or delayed <= 0 or delayed >= offline or flat < 0.5:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "设置不正确",
                "请确保：离线时间大于 250 ms，延迟时间小于离线时间，卡死窗口至少 0.5 秒。",
            )
            return

        self.monitor.update_thresholds(offline, delayed, flat)
        # The monitor enforces floors tied to the 500 ms @STATUS cadence. Write
        # back whatever it actually adopted, so the number on screen is the
        # number in use — otherwise the settings page quietly lies.
        adopted = (
            self.monitor.offline_ms,
            self.monitor.delayed_ms,
            self.monitor.flat_seconds,
        )
        if adopted != (offline, delayed, flat):
            changes = [
                f"{label} {was} → {now}"
                for label, was, now in zip(
                    ("离线", "延迟", "卡住"), (offline, delayed, flat), adopted
                )
                if was != now
            ]
            self._log(
                "阈值已调整到可用范围：" + "，".join(changes)
                + f"。Master 每 {STATUS_PERIOD_MS} ms 才发一条 @STATUS，"
                "阈值比这个节奏还紧的话，正常的设备也会一直报警。"
            )
        offline, delayed, flat = adopted
        self.offline_var.set(str(offline))
        self.delayed_var.set(str(delayed))
        self.flat_var.set(f"{flat:g}")

        self.settings.output_directory = self.output_var.get()
        self.settings.filename_prefix = self.prefix_var.get()
        self.settings.record_video = self.video_var.get()
        self.settings.offline_ms = offline
        self.settings.delayed_ms = delayed
        self.settings.flat_seconds = flat
        self.settings.baud_rate = self._safe_int(self.baud_var.get(), 115200)
        self.settings.selected_devices = list(self._selected_devices())
        self.settings.master_mac = self.master_mac_var.get().strip()
        self.settings.firmware_fqbn = (
            self.firmware_fqbn_var.get().strip() or AUTO_BOARD_SELECTION
        )
        try:
            save_settings(self.settings)
        except OSError as exc:
            messagebox.showerror("无法保存设置", str(exc))
            return
        self.footer_var.set("设置已保存，下次启动会自动恢复。")
        self._log("保存路径和诊断阈值已更新。")

    def _log(self, message: str, error: bool = False) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        prefix = "错误" if error else "信息"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {prefix}  {message}\n")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 400:
            self.log_text.delete("1.0", "80.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _sample_rate(self, now: float) -> float:
        while self.sample_times and now - self.sample_times[0] > 5.0:
            self.sample_times.popleft()
        if len(self.sample_times) < 2:
            return 0.0
        duration = self.sample_times[-1] - self.sample_times[0]
        return (len(self.sample_times) - 1) / duration if duration > 0 else 0.0

    def _refresh_correlations(self) -> None:
        if len(self.plot_timestamps) < 10:
            self.corr_fw_var.set("N/A")
            self.corr_fo_var.set("N/A")
            self.corr_wo_var.set("N/A")
            self.plot_corr_fw_text.set_text("Finger ↔ Wrist: N/A")
            self.plot_corr_fo_text.set_text("Finger ↔ Other: N/A")
            self.plot_corr_wo_text.set_text("Wrist ↔ Other: N/A")
            return

        timestamps = np.asarray(self.plot_timestamps, dtype=float)
        mask = timestamps >= timestamps[-1] - self.ORIGINAL_SIM_WINDOW_MS
        finger = np.asarray(self.plot_data["finger"], dtype=float)[mask]
        wrist = np.asarray(self.plot_data["wrist"], dtype=float)[mask]
        other = np.asarray(self.plot_data["other"], dtype=float)[mask]

        corr_fw = self._format_correlation(self._correlation(finger, wrist))
        corr_fo = self._format_correlation(self._correlation(finger, other))
        corr_wo = self._format_correlation(self._correlation(wrist, other))
        self.corr_fw_var.set(corr_fw)
        self.corr_fo_var.set(corr_fo)
        self.corr_wo_var.set(corr_wo)
        self.plot_corr_fw_text.set_text(f"Finger ↔ Wrist: {corr_fw}")
        self.plot_corr_fo_text.set_text(f"Finger ↔ Other: {corr_fo}")
        self.plot_corr_wo_text.set_text(f"Wrist ↔ Other: {corr_wo}")

    # 相关性算法也在 plot.py，重建录屏时算出来的数要和界面上显示的一致。
    _correlation = staticmethod(correlation)
    _format_correlation = staticmethod(format_correlation)

    @staticmethod
    def _battery_text(node: NodeHealth) -> str:
        if node.battery_mv is None or node.battery_percent is None:
            # Firmware ships with BATTERY_MONITOR_ENABLED 0 — this is normal,
            # not a fault, so say what it refers to instead of a bare "未配置".
            return "电量未接"
        return f"{node.battery_percent}%  ({node.battery_mv / 1000:.2f} V)"

    @staticmethod
    def _format_duration(seconds: int) -> str:
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    @staticmethod
    def _format_age_ms(age_ms: int) -> str:
        if age_ms < 1000:
            return f"{age_ms} ms"
        return f"{age_ms / 1000:.1f} 秒"

    @staticmethod
    def _safe_int(value: str, default: int) -> int:
        try:
            return int(value)
        except ValueError:
            return default

    @staticmethod
    def _safe_float(value: str, default: float) -> float:
        try:
            return float(value)
        except ValueError:
            return default

    def _on_close(self) -> None:
        if self.firmware_busy or (
            self.firmware_thread is not None and self.firmware_thread.is_alive()
        ):
            messagebox.showwarning(
                "正在烧录",
                "固件编译、烧录或 MAC 读取尚未完成。为防止 ESP32 固件损坏，请等待完成后再关闭。",
            )
            return
        if self.recorder.active:
            should_close = messagebox.askyesno(
                "采集正在进行",
                "关闭前会停止采集并保存现有数据。确定关闭吗？",
            )
            if not should_close:
                return
            self._stop_recording()
        self._stop_siren()
        if self.worker is not None:
            worker = self.worker
            self.worker = None
            worker.stop()
        try:
            self.settings.output_directory = self.output_var.get()
            self.settings.filename_prefix = self.prefix_var.get()
            self.settings.record_video = self.video_var.get()
            self.settings.selected_devices = list(self._selected_devices())
            self.settings.driver_name = self.driver_var.get().strip()
            self.settings.other_name = self.other_person_var.get().strip()
            self.settings.master_mac = self.master_mac_var.get().strip()
            self.settings.firmware_fqbn = (
                self.firmware_fqbn_var.get().strip() or AUTO_BOARD_SELECTION
            )
            save_settings(self.settings)
        except OSError:
            pass
        self.root.destroy()


def find_recorded_session() -> Path | None:
    """Locate a previously recorded CSV for the offline self-check."""
    root = Path(__file__).resolve().parent.parent.parent
    for folder in ("采集到成品数据", "采集数据", "data"):
        directory = root / folder
        if not directory.is_dir():
            continue
        files = sorted(directory.glob("*.csv"))
        if files:
            return files[0]
    return None


def run(smoke_test: bool = False) -> None:
    root = tk.Tk()
    app = PPGCollectorApp(root)

    if smoke_test:
        # Self-check against a real recorded session (the 16-column path that
        # the current dual-imu master firmware actually produces).
        sample = find_recorded_session()
        if sample is None:
            raise RuntimeError("找不到用于自检的历史采集 CSV")
        root.after(250, lambda: app._start_replay(str(sample), speed=25.0))

        def finish_smoke_test() -> None:
            if app.worker is not None:
                app.worker.stop()
                app.worker = None
            root.destroy()

        root.after(2500, finish_smoke_test)

    root.mainloop()

    if smoke_test:
        if not app.connected or app.total_samples < 15:
            raise RuntimeError("自检未能从历史 CSV 读到数据")
