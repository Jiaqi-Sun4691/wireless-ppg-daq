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
from ppg_collector.monitor import HealthLevel, HealthMonitor, battery_percentage
from ppg_collector.protocol import LineKind, csv_columns_for_devices, parse_serial_line
from ppg_collector.recorder import SessionRecorder, safe_prefix
from ppg_collector.settings import AppSettings, load_settings, save_settings


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

    def test_existing_collected_csv_files_are_compatible(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        csv_files = sorted((project_root / "采集到成品数据").glob("*.csv"))
        self.assertTrue(csv_files, "No historical acquisition CSV files found")
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
                        # Several historical recordings contain an empty wheel
                        # IMU block. The live collector must reject those rows
                        # instead of silently turning missing data into zeros.
                        rejected_incomplete_rows += 1
                        continue
                    self.assertEqual(parsed.kind, LineKind.DATA)
                    checked_rows += 1
        self.assertGreater(checked_rows, 500)
        self.assertGreater(rejected_incomplete_rows, 0)


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


if __name__ == "__main__":
    unittest.main()
