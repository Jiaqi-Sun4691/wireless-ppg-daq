from __future__ import annotations

import csv
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from ppg_collector.firmware import (
    AUTO_BOARD_SELECTION,
    BOARD_PROFILES,
    FIRMWARE_TARGETS,
    FirmwareManager,
    board_profile_from_output,
    inject_master_mac,
    mac_to_cpp_initializer,
    normalize_mac,
    stable_options_for_fqbn,
)
from ppg_collector.monitor import (
    STATUS_PERIOD_MS,
    HealthLevel,
    HealthMonitor,
    battery_percentage,
)
from ppg_collector.protocol import LineKind, csv_columns_for_devices, parse_serial_line
from ppg_collector.library import scan_sessions
from ppg_collector.plot import MAPPING_SUFFIX, REBUILT_SUFFIX
from ppg_collector.rebuild import FPS as REBUILD_FPS, load_session
from ppg_collector.recorder import SessionRecorder, safe_prefix
from ppg_collector.settings import (
    AppSettings,
    load_settings,
    save_settings,
    settings_path,
)


SAMPLE_LINE = "385214,1843,2097,1984,-24,1606,-1825,-1024,145,296,-506,-1933,437,0,0,0"
STATUS_LINE = "@STATUS,1000,12,18,16,22,5,7,5,6,4060,3920,3750,3640,10,11,12,13"


class FirmwareTests(unittest.TestCase):
    def test_mac_normalization_and_initializer(self) -> None:
        self.assertEqual(normalize_mac("aa:0b:cc:1d:ee:2f"), "AA:0B:CC:1D:EE:2F")
        self.assertEqual(
            mac_to_cpp_initializer("AA:0B:CC:1D:EE:2F"),
            "0xAA, 0x0B, 0xCC, 0x1D, 0xEE, 0x2F",
        )
        with self.assertRaises(ValueError):
            normalize_mac("not-a-mac")

    def test_receiver_mac_is_replaced_once(self) -> None:
        source = "uint8_t receiverMac[] = {0, 0, 0, 0, 0, 0};\nvoid setup() {}"
        updated = inject_master_mac(source, "AA:BB:CC:DD:EE:FF")
        self.assertIn(
            "uint8_t receiverMac[] = {0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF};",
            updated,
        )

    def test_all_slave_sources_accept_dynamic_master_mac(self) -> None:
        manager = FirmwareManager()
        for target in FIRMWARE_TARGETS:
            source = manager.prepared_source(
                target.target_id,
                "12:34:56:78:9A:BC" if target.requires_master_mac else None,
            )
            if target.requires_master_mac:
                self.assertIn(
                    "uint8_t receiverMac[] = {0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC};",
                    source,
                    target.display_name,
                )
            else:
                self.assertIn("@MASTER_MAC,", source)

    def test_wizard_target_order(self) -> None:
        self.assertEqual(
            [target.target_id for target in FIRMWARE_TARGETS],
            ["master", "finger", "wrist", "other", "wheel"],
        )

    def test_esptool_output_selects_chip_specific_profile(self) -> None:
        classic = board_profile_from_output(
            "Chip is ESP32-D0WD-V3 (revision v3.1)\nMAC: AA:BB:CC:DD:EE:FF"
        )
        s3 = board_profile_from_output("Chip type: ESP32-S3 (QFN56)\n")
        c3 = board_profile_from_output("Chip is ESP32-C3 (revision v0.4)\n")
        self.assertEqual(classic, BOARD_PROFILES["esp32"])
        self.assertEqual(s3, BOARD_PROFILES["esp32s3"])
        self.assertEqual(c3, BOARD_PROFILES["esp32c3"])

    def test_stable_profile_matches_verified_ide_settings(self) -> None:
        # Mirrors the Arduino IDE combination confirmed working on the rig:
        # slow upload, QIO flash at 40 MHz, 4 MB, PSRAM off.
        options = stable_options_for_fqbn("esp32:esp32:esp32")
        self.assertIn("UploadSpeed=115200", options)
        self.assertIn("FlashMode=qio", options)
        self.assertIn("FlashFreq=40", options)
        self.assertIn("FlashSize=4M", options)
        self.assertIn("PSRAM=disabled", options)

    def test_settings_default_to_automatic_board_detection(self) -> None:
        self.assertEqual(AppSettings().firmware_fqbn, AUTO_BOARD_SELECTION)


class ProtocolTests(unittest.TestCase):
    def test_csv_columns_follow_device_selection(self) -> None:
        self.assertEqual(
            csv_columns_for_devices(("finger",)),
            ("timestamp(ms)", "finger", "system_time", "driver_name", "other_name"),
        )
        wrist_and_wheel = csv_columns_for_devices(("wrist", "wheel"))
        self.assertIn("wrist", wrist_and_wheel)
        self.assertIn("wrist_ax", wrist_and_wheel)
        self.assertIn("wheel_gz", wrist_and_wheel)
        self.assertNotIn("finger", wrist_and_wheel)
        self.assertNotIn("other", wrist_and_wheel)
        self.assertEqual(len(wrist_and_wheel), 17)
        with self.assertRaises(ValueError):
            csv_columns_for_devices(())

    def test_parse_existing_16_column_sample(self) -> None:
        parsed = parse_serial_line(SAMPLE_LINE, datetime(2026, 8, 10, 12, 0, 0))
        self.assertEqual(parsed.kind, LineKind.DATA)
        self.assertEqual(parsed.payload.wrist_ax, -24)
        self.assertEqual(parsed.payload.wheel_az, 437)
        self.assertEqual(len(parsed.payload.csv_row), 19)

    def test_parse_enhanced_status(self) -> None:
        parsed = parse_serial_line(STATUS_LINE)
        self.assertEqual(parsed.kind, LineKind.STATUS)
        self.assertEqual(parsed.payload.nodes["wrist"].battery_mv, 3920)
        self.assertEqual(parsed.payload.nodes["wheel"].sequence, 13)

    def test_non_data_line_is_info(self) -> None:
        parsed = parse_serial_line("Master monitor firmware ready")
        self.assertEqual(parsed.kind, LineKind.INFO)

    def test_invalid_numeric_data_raises(self) -> None:
        values = SAMPLE_LINE.split(",")
        values[4] = "bad"
        with self.assertRaises(ValueError):
            parse_serial_line(",".join(values))

    def test_incomplete_row_is_rejected_not_zero_filled(self) -> None:
        """Several historical recordings have an empty wheel IMU block.

        Those rows must be rejected outright. Quietly turning missing data
        into zeros would look like a stationary steering wheel.
        """
        values = SAMPLE_LINE.split(",")
        values[10:16] = [""] * 6                  # wheel IMU block missing
        with self.assertRaises(ValueError):
            parse_serial_line(",".join(values))

    def test_existing_collected_csv_files_are_compatible(self) -> None:
        """Parse whatever real recordings this machine has, if any.

        The data folder deliberately lives outside the repository, so this
        check is a no-op on a fresh clone rather than a failure.
        """
        project_root = Path(__file__).resolve().parents[2]
        csv_files = sorted((project_root / "采集到成品数据").glob("*.csv"))
        if not csv_files:
            self.skipTest("本机没有历史采集数据，跳过真实文件兼容性检查")
        checked_rows = 0
        rejected_incomplete_rows = 0
        for csv_path in csv_files:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.reader(handle)
                next(reader)
                for index, row in enumerate(reader):
                    if index >= 200:
                        break
                    if not row:
                        continue
                    try:
                        parsed = parse_serial_line(",".join(row[:16]))
                    except ValueError:
                        rejected_incomplete_rows += 1
                        continue
                    self.assertEqual(parsed.kind, LineKind.DATA)
                    checked_rows += 1
        self.assertGreater(checked_rows, 0, f"{len(csv_files)} 个历史文件一行都没解析成功")


class MonitorTests(unittest.TestCase):
    def test_enhanced_status_distinguishes_link_and_sensor(self) -> None:
        monitor = HealthMonitor(offline_ms=1500, delayed_ms=500)
        packet = parse_serial_line(STATUS_LINE).payload
        monitor.update_status(packet, monotonic_now=10.0)
        self.assertEqual(monitor.nodes["wrist"].link_text, "在线")
        self.assertEqual(monitor.nodes["wrist"].sensor_text, "PPG、IMU 正常")
        self.assertEqual(monitor.nodes["wrist"].level, HealthLevel.GOOD)

    def test_stale_node_becomes_offline(self) -> None:
        monitor = HealthMonitor(offline_ms=1500, delayed_ms=500)
        packet = parse_serial_line(STATUS_LINE).payload
        monitor.update_status(packet, monotonic_now=10.0)
        monitor.refresh(monotonic_now=12.0)
        self.assertEqual(monitor.nodes["finger"].link_text, "离线")
        self.assertEqual(monitor.nodes["finger"].level, HealthLevel.ERROR)

    def test_enhanced_mode_still_detects_flat_ppg(self) -> None:
        monitor = HealthMonitor(flat_seconds=2.0)
        sample = parse_serial_line(SAMPLE_LINE).payload
        for index in range(40):
            monitor.update_sample(sample, monotonic_now=7.0 + index * 0.075)
        packet = parse_serial_line(STATUS_LINE).payload
        monitor.update_status(packet, monotonic_now=10.0)
        self.assertEqual(monitor.nodes["finger"].link_text, "在线")
        self.assertIn("未更新", monitor.nodes["finger"].sensor_text)
        self.assertGreaterEqual(monitor.nodes["finger"].change_age_ms, 2900)
        self.assertEqual(monitor.nodes["finger"].level, HealthLevel.ERROR)

        changed = replace(sample, finger=sample.finger + 1)
        monitor.update_sample(changed, monotonic_now=10.1)
        monitor.refresh(monotonic_now=10.1)
        self.assertEqual(monitor.nodes["finger"].sensor_text, "PPG 正常")
        self.assertEqual(monitor.nodes["finger"].change_age_ms, 0)
        # Recovery is held back until it proves stable, so a node hovering at a
        # threshold cannot flicker between states on every refresh.
        self.assertEqual(monitor.nodes["finger"].level, HealthLevel.ERROR)
        # The master keeps emitting @STATUS every 500 ms; feed one so the
        # effective age stays fresh while the recovery hold elapses.
        monitor.update_status(packet, monotonic_now=10.5)
        monitor.update_sample(changed, monotonic_now=10.8)
        monitor.refresh(monotonic_now=10.8)
        self.assertEqual(monitor.nodes["finger"].level, HealthLevel.GOOD)

    def test_legacy_slave_stuck_uses_time_since_last_change(self) -> None:
        monitor = HealthMonitor(flat_seconds=1.0)
        sample = parse_serial_line(SAMPLE_LINE).payload
        for index in range(25):
            monitor.update_sample(sample, monotonic_now=1.0 + index * 0.06)
        monitor.refresh(monotonic_now=2.5)
        finger = monitor.nodes["finger"]
        self.assertEqual(finger.link_text, "数据未更新")
        self.assertIn("1.5 秒", finger.sensor_text)
        self.assertEqual(finger.level, HealthLevel.ERROR)

        changed = replace(sample, finger=sample.finger + 3)
        monitor.update_sample(changed, monotonic_now=2.6)
        self.assertEqual(monitor.nodes["finger"].link_text, "数据更新中")
        self.assertEqual(monitor.nodes["finger"].change_age_ms, 0)

    def test_wrist_ppg_and_imu_have_independent_update_timers(self) -> None:
        monitor = HealthMonitor(flat_seconds=1.0)
        sample = parse_serial_line(SAMPLE_LINE).payload
        for index in range(25):
            changing_ppg = replace(sample, wrist=sample.wrist + index)
            monitor.update_sample(changing_ppg, monotonic_now=1.0 + index * 0.06)
        monitor.refresh(monotonic_now=2.5)
        wrist = monitor.nodes["wrist"]
        self.assertIn("IMU", wrist.sensor_text)
        self.assertIn("未更新", wrist.sensor_text)
        self.assertNotIn("PPG 已", wrist.sensor_text)
        self.assertEqual(wrist.level, HealthLevel.ERROR)

    def test_battery_curve(self) -> None:
        self.assertEqual(battery_percentage(3300), 0)
        self.assertEqual(battery_percentage(4200), 100)
        self.assertEqual(battery_percentage(3800), 60)
        self.assertIsNone(battery_percentage(None))


class RecordingTests(unittest.TestCase):
    def test_csv_recording(self) -> None:
        sample = parse_serial_line(SAMPLE_LINE).payload
        with tempfile.TemporaryDirectory() as temporary:
            recorder = SessionRecorder()
            recorder.start(Path(temporary), "测试 对象/01", False)
            recorder.write_sample(sample)
            result = recorder.stop()
            self.assertEqual(result.row_count, 1)
            self.assertTrue(result.csv_path.exists())
            with result.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(rows[0]), 19)
            self.assertEqual(rows[1][1], "1843")

    def test_safe_prefix(self) -> None:
        self.assertEqual(safe_prefix("  PPG / 受试者 01  "), "PPG_受试者_01")

    def test_selected_devices_control_csv_header_and_row(self) -> None:
        sample = parse_serial_line(SAMPLE_LINE).payload
        with tempfile.TemporaryDirectory() as temporary:
            recorder = SessionRecorder()
            recorder.start(
                Path(temporary),
                "selected",
                False,
                selected_devices=("finger", "wheel"),
            )
            recorder.write_sample(sample)
            result = recorder.stop()
            with result.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(
                rows[0],
                [
                    "timestamp(ms)",
                    "finger",
                    "wheel_ax",
                    "wheel_ay",
                    "wheel_az",
                    "wheel_gx",
                    "wheel_gy",
                    "wheel_gz",
                    "system_time",
                    "driver_name",
                    "other_name",
                ],
            )
            self.assertEqual(len(rows[1]), 11)


    def test_subject_names_are_written_into_every_row(self) -> None:
        sample = parse_serial_line(SAMPLE_LINE).payload
        with tempfile.TemporaryDirectory() as temporary:
            recorder = SessionRecorder()
            recorder.start(
                Path(temporary), "subject", False,
                metadata={"driver": "张三", "other": "李四", "note": "市区"},
            )
            recorder.write_sample(sample)
            recorder.write_sample(sample)
            result = recorder.stop()
            with result.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle))
            header = rows[0]
            # "other" is the reference PPG channel, so the subject column must
            # not reuse that name.
            self.assertIn("other", header)
            self.assertIn("other_name", header)
            driver_at = header.index("driver_name")
            other_at = header.index("other_name")
            for row in rows[1:]:
                self.assertEqual(row[driver_at], "张三")
                self.assertEqual(row[other_at], "李四")

    def test_no_selected_device_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            recorder = SessionRecorder()
            with self.assertRaises(ValueError):
                recorder.start(Path(temporary), "none", False, selected_devices=())

    def test_segment_names_and_empty_csv_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            recorder = SessionRecorder()
            recorder.start(Path(temporary), "ppg_imu_data", True, figure=None)
            csv_path = recorder.csv_path
            video_path = recorder.video_path
            self.assertIsNotNone(csv_path)
            self.assertIsNotNone(video_path)
            # CSV and video share one stem so a session's files sort together.
            self.assertEqual(video_path.stem, csv_path.stem)
            self.assertTrue(csv_path.name.startswith("ppg_imu_data_"))
            recorder.stop()
            self.assertFalse(csv_path.exists())


class RebuildTests(unittest.TestCase):
    """录屏重建：CSV 进去，帧序列出来。不碰 ffmpeg，只验数据到帧的映射。"""

    def _write(self, folder: Path, rows: int) -> Path:
        path = folder / "ppg_imu_data_2026-09-19_10-00-00.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["timestamp(ms)", "finger", "wrist", "other", "system_time"]
            )
            for index in range(rows):
                writer.writerow([index * 48, 2000, 1900, 2100, "2026-09-19 10:00:00"])
        return path

    def test_loads_the_three_ppg_channels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write(Path(temporary), 50)
            timestamps, values, wall = load_session(path)
            self.assertEqual(timestamps.size, 50)
            self.assertEqual(set(values), {"finger", "wrist", "other"})
            self.assertEqual(timestamps[1] - timestamps[0], 48)
            self.assertEqual(wall[0], "2026-09-19 10:00:00")

    def test_video_length_matches_real_elapsed_time(self) -> None:
        """重建版是真实时长——原来的实时录屏因为掉帧会快约 1.6 倍。"""
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write(Path(temporary), 1000)
            timestamps, _values, _wall = load_session(path)
            span_seconds = (timestamps[-1] - timestamps[0]) / 1000
            frames = int(span_seconds * REBUILD_FPS) + 1
            self.assertAlmostEqual(frames / REBUILD_FPS, span_seconds, delta=0.1)

    def test_missing_ppg_column_is_rejected_with_a_readable_reason(self) -> None:
        # 只勾了部分设备的采集画不出三路波形，要说清楚而不是抛 KeyError。
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "partial.csv"
            path.write_text("timestamp(ms),finger,system_time\n0,2000,x\n", encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                load_session(path)
            self.assertIn("wrist", str(caught.exception))


class LibraryTests(unittest.TestCase):
    """扫描历史采集：区分实时录屏和事后重建。"""

    def _session(self, root: Path, stamp: str, live: bool, rebuilt: bool) -> Path:
        folder = root / f"ppg_imu_data_{stamp}"
        folder.mkdir()
        stem = f"ppg_imu_data_{stamp}"
        (folder / f"{stem}.csv").write_text(
            "timestamp(ms),finger,system_time\n0,2000,x\n48,2001,x\n", encoding="utf-8"
        )
        if live:
            (folder / f"{stem}.mp4").write_bytes(b"x" * 64)
        if rebuilt:
            (folder / f"{stem}{REBUILT_SUFFIX}").write_bytes(b"x" * 128)
            (folder / f"{stem}{REBUILT_SUFFIX[:-4]}{MAPPING_SUFFIX}").write_text(
                "frame,video_seconds,timestamp(ms),system_time\n0,0.0,0,x\n",
                encoding="utf-8",
            )
        return folder

    def test_live_and_rebuilt_recordings_coexist(self) -> None:
        """两份是不同的文件，重建不覆盖实时录的那份。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._session(root, "2026-09-19_10-00-00", live=True, rebuilt=True)
            self._session(root, "2026-09-19_11-00-00", live=True, rebuilt=False)
            self._session(root, "2026-09-19_12-00-00", live=False, rebuilt=True)
            self._session(root, "2026-09-19_13-00-00", live=False, rebuilt=False)
            found = {s.csv_path.parent.name: s for s in scan_sessions(root)}
            self.assertEqual(len(found), 4)

            both = found["ppg_imu_data_2026-09-19_10-00-00"]
            self.assertIsNotNone(both.video_path)
            self.assertIsNotNone(both.rebuilt_path)
            self.assertNotEqual(both.video_path, both.rebuilt_path)
            self.assertTrue(both.is_rebuilt)
            self.assertEqual(both.rebuilt_text, "是")
            # 两份都要算进占用空间。
            self.assertEqual(both.video_bytes, 64 + 128)

            live_only = found["ppg_imu_data_2026-09-19_11-00-00"]
            self.assertIsNotNone(live_only.video_path)
            self.assertIsNone(live_only.rebuilt_path)
            self.assertEqual(live_only.rebuilt_text, "—")

            rebuilt_only = found["ppg_imu_data_2026-09-19_12-00-00"]
            self.assertIsNone(rebuilt_only.video_path)
            self.assertIsNotNone(rebuilt_only.rebuilt_path)
            self.assertEqual(rebuilt_only.rebuilt_text, "是")

            self.assertEqual(found["ppg_imu_data_2026-09-19_13-00-00"].rebuilt_text, "—")

    def test_rebuilt_video_is_not_mistaken_for_the_live_one(self) -> None:
        # _重建.mp4 的时间戳和实时录的一样，按戳匹配会认错。
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._session(root, "2026-09-19_12-00-00", live=False, rebuilt=True)
            session = scan_sessions(root)[0]
            self.assertIsNone(session.video_path, "重建的那份被当成实时录屏了")

    def test_deleting_the_rebuilt_video_counts_as_never_rebuilt(self) -> None:
        # 用户明确要求：重建过又把文件删了，就该显示成没重建过。
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = self._session(root, "2026-09-19_10-00-00", live=True, rebuilt=True)
            next(folder.glob(f"*{REBUILT_SUFFIX}")).unlink()
            session = scan_sessions(root)[0]
            self.assertFalse(session.is_rebuilt)
            self.assertEqual(session.rebuilt_text, "—")
            # 实时录的那份不受影响。
            self.assertIsNotNone(session.video_path)

    def test_mapping_table_is_not_listed_as_its_own_session(self) -> None:
        # 对照表也是 .csv，早先它让每一次重建过的采集在列表里出现两次。
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._session(root, "2026-09-19_10-00-00", live=True, rebuilt=True)
            self.assertEqual(len(scan_sessions(root)), 1)


class RebuildAvailabilityTests(unittest.TestCase):
    """什么时候允许生成波形录屏。不建界面，只验判断逻辑。"""

    class _Fake:
        """够 _can_rebuild 用的最小 SessionFile 替身。"""

        def __init__(self, columns, video=False, rebuilt=False):
            self.columns = columns
            self.video_path = Path("v.mp4") if video else None
            self.rebuilt_path = Path("v_重建.mp4") if rebuilt else None

        @property
        def is_rebuilt(self):
            return self.rebuilt_path is not None

    FULL = ("timestamp(ms)", "finger", "wrist", "other", "system_time")
    PARTIAL = ("timestamp(ms)", "finger", "system_time")

    def _can(self, sessions, busy=False, ffmpeg="/usr/bin/ffmpeg"):
        from ppg_collector.app import PPGCollectorApp

        app = PPGCollectorApp.__new__(PPGCollectorApp)   # 不起界面
        app.rebuild_thread = object() if busy else None
        app.ffmpeg_path = ffmpeg
        return PPGCollectorApp._can_rebuild(app, sessions)

    def test_existing_video_does_not_block_generating(self) -> None:
        # 实时录的那些恰恰最该重建——掉帧导致播放比真实快，时间轴对不上。
        self.assertTrue(self._can([self._Fake(self.FULL, video=True)]))

    def test_already_rebuilt_can_be_rebuilt_again(self) -> None:
        # 允许，但界面会在确认框里问"已经重建过，要覆盖吗"。
        self.assertTrue(self._can([self._Fake(self.FULL, video=True, rebuilt=True)]))

    def test_partial_device_selection_cannot_draw_three_channels(self) -> None:
        self.assertFalse(self._can([self._Fake(self.PARTIAL)]))

    def test_blocked_while_another_rebuild_runs(self) -> None:
        self.assertFalse(self._can([self._Fake(self.FULL)], busy=True))

    def test_blocked_without_ffmpeg(self) -> None:
        self.assertFalse(self._can([self._Fake(self.FULL)], ffmpeg=None))

    def test_multi_selection_is_not_supported(self) -> None:
        self.assertFalse(self._can([self._Fake(self.FULL), self._Fake(self.FULL)]))
        self.assertFalse(self._can([]))


class SettingsTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            expected = AppSettings(
                output_directory="/tmp/data",
                record_video=False,
                master_mac="AA:BB:CC:DD:EE:FF",
                firmware_fqbn="esp32:esp32:esp32doit-devkit-v1",
            )
            save_settings(expected, path)
            actual = load_settings(path)
            self.assertEqual(actual.output_directory, "/tmp/data")
            self.assertFalse(actual.record_video)
            self.assertEqual(actual.master_mac, "AA:BB:CC:DD:EE:FF")
            self.assertEqual(actual.firmware_fqbn, "esp32:esp32:esp32doit-devkit-v1")

    def test_defaults_carry_nothing_machine_specific(self) -> None:
        """A fresh install must not inherit whoever's machine built it.

        Settings live in the user's home, never in the repository, so these
        defaults are what a teammate actually starts from.
        """
        defaults = AppSettings()
        home = str(Path.home())
        self.assertTrue(
            defaults.output_directory.startswith(home),
            f"默认保存目录跑到了 home 外面：{defaults.output_directory}",
        )
        self.assertTrue(str(settings_path()).startswith(home))
        # No leftover port, MAC or subject name from the developer's bench.
        self.assertEqual(defaults.preferred_port, "")
        self.assertEqual(defaults.master_mac, "")
        self.assertEqual(defaults.driver_name, "")
        self.assertEqual(defaults.other_name, "")

    def test_default_output_directory_follows_whoever_runs_it(self) -> None:
        import importlib

        import ppg_collector.settings as settings_module

        original = Path.home
        try:
            Path.home = staticmethod(lambda: Path("/Users/someone-else"))
            importlib.reload(settings_module)
            self.assertEqual(
                settings_module.AppSettings().output_directory,
                "/Users/someone-else/Documents/PPG Data",
            )
        finally:
            Path.home = original
            importlib.reload(settings_module)


class ThresholdTests(unittest.TestCase):
    """The master only speaks every 500 ms, so thresholds have a hard floor."""

    def test_delay_threshold_cannot_go_below_the_status_cadence(self) -> None:
        monitor = HealthMonitor(offline_ms=1500, delayed_ms=300, flat_seconds=2.0)
        self.assertGreater(monitor.delayed_ms, STATUS_PERIOD_MS)
        self.assertEqual(monitor.delayed_ms, STATUS_PERIOD_MS + 200)

    def test_floor_applies_to_stored_settings_not_just_edits(self) -> None:
        # A too-tight value saved by an older build must be clamped on load,
        # not only when the user opens the settings page again.
        monitor = HealthMonitor(offline_ms=1500, delayed_ms=100, flat_seconds=0.1)
        self.assertEqual(monitor.delayed_ms, STATUS_PERIOD_MS + 200)
        self.assertEqual(monitor.flat_seconds, 0.5)

    def test_offline_must_stay_above_delayed(self) -> None:
        monitor = HealthMonitor(offline_ms=600, delayed_ms=400, flat_seconds=2.0)
        self.assertGreater(monitor.offline_ms, monitor.delayed_ms)

    def test_sane_values_pass_through_untouched(self) -> None:
        monitor = HealthMonitor(offline_ms=2000, delayed_ms=800, flat_seconds=2.0)
        self.assertEqual(
            (monitor.offline_ms, monitor.delayed_ms, monitor.flat_seconds),
            (2000, 800, 2.0),
        )


if __name__ == "__main__":
    unittest.main()
