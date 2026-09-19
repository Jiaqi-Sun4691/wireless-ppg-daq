"""端到端自检：不接硬件，把一段 16 列数据当作 Master 输出回放进 GUI。

需要有显示器（会真的建一个 Tk 窗口）。跑法：

    PYTHONPATH=. python3 tests/gui_smoke.py

覆盖 test_core.py 覆盖不到的那一半：串口事件 → 解析 → 曲线 → 录制这条
完整链路，以及开始采集前的姓名确认弹窗。

各步骤是用 after() 排进 mainloop 的，不能改成 root.update() 循环：界面每
35 ms 就重新排一次事件轮询，一轮如果跑满 35 ms，update() 里永远有到期的
定时器，就再也退不出来了。
"""

from __future__ import annotations

import csv
import math
import os
import re
from pathlib import Path
import shutil
import tempfile
import tkinter as tk


PROJECT_DIRECTORY = Path(__file__).resolve().parents[1]
os.environ.setdefault("MPLCONFIGDIR", str(PROJECT_DIRECTORY / ".matplotlib_cache"))

from ppg_collector import app as app_module  # noqa: E402
from ppg_collector.app import PPGCollectorApp  # noqa: E402
from ppg_collector.firmware import AUTO_BOARD_SELECTION  # noqa: E402
from ppg_collector.protocol import DATA_COLUMNS  # noqa: E402


def system_has_ffmpeg() -> bool:
    """这台机器上到底有没有 ffmpeg —— 故意不调用被测的 find_ffmpeg()。

    守卫和被测对象是同一个函数的话，find_ffmpeg() 一旦退化，测试只会安静
    地跳过而不是报错，正好放过它该抓的那个 bug。
    """
    import shutil

    if shutil.which("ffmpeg"):
        return True
    return any(
        Path(candidate).is_file()
        for candidate in (
            "/opt/homebrew/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "/opt/local/bin/ffmpeg",
            "/usr/bin/ffmpeg",
        )
    )


EXPECTED_TABS = ["采集", "串口与日志", "数据文件", "固件烧录", "设置"]
EXPECTED_HEADER = [
    "timestamp(ms)",
    "finger",
    "wheel_ax", "wheel_ay", "wheel_az",
    "wheel_gx", "wheel_gy", "wheel_gz",
    "system_time",
    "driver_name", "other_name",
]


def write_replay_source(directory: Path) -> Path:
    """一段 16 列数据，格式与 Master 实际输出一致，只是波形是算出来的。

    它只负责把行喂进串口事件队列，本身不参与断言；被检查的是 GUI 自己
    写出来的那份 CSV。长度要够跑完整个自检——回放一放完就会发出断开事件，
    后面的录制步骤就无从谈起了。
    """
    path = directory / "replay_source.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(DATA_COLUMNS)
        for index in range(2400):
            phase = index / 12.0
            ppg = 2048 + int(600 * math.sin(phase))
            imu = [int(1000 * math.sin(phase + axis)) for axis in range(6)]
            writer.writerow([index * 48, ppg, ppg - 40, ppg + 55, *imu, *imu])
    return path


def check_layout(app: PPGCollectorApp) -> None:
    tabs = [app.notebook.tab(tab_id, "text") for tab_id in app.notebook.tabs()]
    assert tabs == EXPECTED_TABS, f"标签页变了：{tabs}"
    assert set(app.plot_lines) == {"finger", "wrist", "other"}
    assert len(app.plot_lines["finger"].get_ydata()) == app.ORIGINAL_PLOT_WINDOW
    assert tuple(round(v) for v in app.ppg_axis.get_ylim()) == (0, 4095)
    assert app.plot_status_text.get_text() == "Status: PAUSED"
    assert len(app.firmware_tree.get_children()) == 5
    assert app.firmware_fqbn_var.get() == AUTO_BOARD_SELECTION
    assert not hasattr(app, "demo_button"), "演示模式应该已经彻底移除"


def check_firmware_wizard(app: PPGCollectorApp) -> None:
    """读到 Master MAC 之后，后续每一步的提示里都要带上它。"""
    app.master_mac_var.set("AA:BB:CC:DD:EE:FF")
    app.firmware_step_index = 1
    app._update_firmware_instruction()
    assert "AA:BB:CC:DD:EE:FF" in app.firmware_instruction_var.get()
    app._firmware_reset_wizard()


def check_stream(app: PPGCollectorApp) -> None:
    assert app.connected, "回放没有连上"
    assert app.total_samples >= 15, f"只收到 {app.total_samples} 个样本"
    assert len(app.plot_timestamps) == app.total_samples
    assert app.corr_fw_var.get() != "N/A", "没有算出实时相关性"
    assert "," in app.console_text.get("1.0", "end"), "原始数据行没有进串口页"


def start_recording(app: PPGCollectorApp, workspace: Path) -> None:
    app.output_var.set(str(workspace / "out"))
    app.prefix_var.set("gui_smoke")
    # 装了 ffmpeg 就真录一段。这条路专门防的是 PATH 问题：双击启动的 App
    # 只有 /usr/bin:/bin:/usr/sbin:/sbin，Homebrew 的 ffmpeg 不在里面，
    # 录屏会静默失败，等采完一趟车才发现没录上。
    app.video_var.set(system_has_ffmpeg())
    app.driver_var.set("张三")
    app.other_person_var.set("李四")
    for device_id, variable in app.device_selection_vars.items():
        variable.set(device_id in ("finger", "wheel"))
    app._update_device_selection_summary()

    asked: list[str] = []
    original = app_module.messagebox.askyesno
    app_module.messagebox.askyesno = lambda title, message, **kw: (
        asked.append(f"{title}\n{message}") or True
    )
    try:
        app._start_recording()
    finally:
        app_module.messagebox.askyesno = original

    assert asked, "开始采集前没有弹确认框"
    assert "张三" in asked[0] and "李四" in asked[0], f"确认框没显示姓名：{asked[0]}"
    assert app.plot_status_text.get_text() == "Status: RECORDING"
    assert str(app.device_cards["finger"].selection_check.cget("state")) == "disabled"
    # 开始时刻要是墙上时钟，不是时长——用来对行车视频和现场笔记。
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", app.start_time_var.get()), \
        f"开始时刻没填上：{app.start_time_var.get()!r}"


def finish_recording(app: PPGCollectorApp, summary: dict) -> None:
    app._stop_recording()
    assert app.plot_status_text.get_text() == "Status: PAUSED"
    assert str(app.device_cards["finger"].selection_check.cget("state")) == "normal"

    csv_path = app.recorder.csv_path
    assert csv_path is not None and csv_path.exists(), "CSV 没有保存"
    # 一次采集 = 一个同名文件夹，行车视频和笔记都能丢进去。
    session_directory = csv_path.parent
    assert session_directory.name == csv_path.stem, "CSV 没有放进同名的会话文件夹"
    assert (session_directory / "session.json").is_file(), "session.json 没有写出来"

    if system_has_ffmpeg():
        assert not app.recorder.video_error, f"录屏报错：{app.recorder.video_error}"
        video = app.recorder.video_path
        assert video is not None and video.exists(), "装了 ffmpeg 却没生成 MP4"
        assert video.stat().st_size > 1000, f"MP4 是空的（{video.stat().st_size} 字节）"
        assert video.parent == session_directory, "MP4 没和 CSV 放在同一个会话文件夹"
        summary["video"] = f"{video.stat().st_size / 1024:.0f} KB"
    else:
        summary["video"] = "跳过（本机没有 ffmpeg）"

    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert len(rows) >= 10, f"只写了 {len(rows) - 1} 行"
    assert rows[0] == EXPECTED_HEADER, f"表头没有跟随设备勾选：{rows[0]}"
    # 姓名要每行都有——单个 CSV 分享出去必须自带身份信息。
    for row in rows[1:]:
        assert row[-2:] == ["张三", "李四"], f"某行缺姓名：{row}"

    # 停止后不清空：事后回填笔记时还要看。
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", app.start_time_var.get()), \
        "停止采集后开始时刻被清空了"

    summary["samples"] = app.total_samples
    summary["rows"] = len(rows) - 1


def check_files_list(app: PPGCollectorApp, workspace: Path) -> None:
    """数据文件页：多选、右键菜单、批量删除的确认文案。"""
    app.output_var.set(str(workspace / "out"))
    app._refresh_files_list()
    items = app.files_tree.get_children()
    assert items, "刚采完的这一次没有出现在列表里"
    assert str(app.files_tree.cget("selectmode")) == "extended", "列表不能多选"

    menu_labels = [
        str(app.files_menu.entrycget(index, "label"))
        for index in (app.FILES_MENU_REVEAL, app.FILES_MENU_CSV,
                      app.FILES_MENU_VIDEO, app.FILES_MENU_DELETE)
    ]
    assert menu_labels[:3] == ["在访达中显示", "打开 CSV", "播放录屏"], menu_labels
    assert "删除" in menu_labels[3], menu_labels

    app.files_tree.selection_set(items[0])
    app._update_files_buttons()
    app._update_files_menu()
    assert str(app.files_reveal_button.cget("state")) == "normal"
    # "播放录屏"跟着这次有没有 MP4 走：没有录屏时点了也没反应，必须是灰的。
    expected = "normal" if system_has_ffmpeg() else "disabled"
    assert str(app.files_menu.entrycget(app.FILES_MENU_VIDEO, "state")) == expected

    # 删除要真的弹确认，而且不能在没确认时就动手。
    asked: list[str] = []
    original = app_module.messagebox.askyesno
    app_module.messagebox.askyesno = lambda title, message, **kw: (
        asked.append(f"{title}\n{message}") or False
    )
    try:
        app._delete_selected_session()
    finally:
        app_module.messagebox.askyesno = original
    assert asked, "删除没有弹确认框"
    assert "废纸篓" in asked[0], asked[0]
    assert app.recorder.csv_path is not None and app.recorder.csv_path.exists(), \
        "用户没点确认，文件却已经被删了"


def main() -> None:
    root = tk.Tk()
    app = PPGCollectorApp(root)
    workspace = Path(tempfile.mkdtemp(prefix="ppg_smoke_"))
    failures: list[BaseException] = []
    summary: dict = {}

    # (开始前等待毫秒, 这一步做什么)
    steps = [
        (200, lambda: check_layout(app)),
        (0, lambda: check_firmware_wizard(app)),
        (0, lambda: app._start_replay(str(write_replay_source(workspace)), speed=10.0)),
        (2000, lambda: check_stream(app)),
        (0, lambda: start_recording(app, workspace)),
        (1500, lambda: finish_recording(app, summary)),
        (0, lambda: check_files_list(app, workspace)),
    ]

    def shutdown() -> None:
        if app.worker is not None:
            app.worker.stop()
            app.worker = None
        root.quit()

    def advance(index: int) -> None:
        if index >= len(steps):
            shutdown()
            return
        delay, action = steps[index]

        def fire() -> None:
            try:
                action()
            except BaseException as exc:      # 断言失败也要先把窗口收掉
                failures.append(exc)
                shutdown()
                return
            advance(index + 1)

        root.after(delay, fire)

    advance(0)
    # 卡住的话要自己失败，不能挂在那儿等人来关窗口。
    root.after(60_000, lambda: (failures.append(TimeoutError("自检超时")), shutdown()))
    root.mainloop()
    root.destroy()
    shutil.rmtree(workspace, ignore_errors=True)

    if failures:
        raise failures[0]
    print(
        f"GUI 自检通过：收到 {summary['samples']} 个样本，"
        f"写入 {summary['rows']} 行 CSV，录屏 {summary['video']}"
    )


if __name__ == "__main__":
    main()
