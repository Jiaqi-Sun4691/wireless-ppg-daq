from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Union


DATA_COLUMNS = (
    "timestamp(ms)",
    "finger",
    "wrist",
    "other",
    "wrist_ax",
    "wrist_ay",
    "wrist_az",
    "wrist_gx",
    "wrist_gy",
    "wrist_gz",
    "wheel_ax",
    "wheel_ay",
    "wheel_az",
    "wheel_gx",
    "wheel_gy",
    "wheel_gz",
)

SUBJECT_COLUMNS = ("driver_name", "other_name")
CSV_COLUMNS = DATA_COLUMNS + ("system_time",) + SUBJECT_COLUMNS

DEVICE_IDS = ("finger", "wrist", "other", "wheel")
DEVICE_LABELS = {
    "finger": "Finger PPG",
    "wrist": "Wrist PPG + IMU",
    "other": "Other PPG",
    "wheel": "Wheel IMU",
}
FIELD_DEVICE = {
    "finger": "finger",
    "wrist": "wrist",
    "other": "other",
    "wrist_ax": "wrist",
    "wrist_ay": "wrist",
    "wrist_az": "wrist",
    "wrist_gx": "wrist",
    "wrist_gy": "wrist",
    "wrist_gz": "wrist",
    "wheel_ax": "wheel",
    "wheel_ay": "wheel",
    "wheel_az": "wheel",
    "wheel_gx": "wheel",
    "wheel_gy": "wheel",
    "wheel_gz": "wheel",
}


def csv_columns_for_devices(selected_devices: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    selected = set(selected_devices)
    unknown = selected.difference(DEVICE_IDS)
    if unknown:
        raise ValueError(f"未知设备：{', '.join(sorted(unknown))}")
    if not selected:
        raise ValueError("至少选择一个采集设备")
    sensor_columns = tuple(
        column
        for column in DATA_COLUMNS
        if column == "timestamp(ms)" or FIELD_DEVICE[column] in selected
    )
    return sensor_columns + ("system_time",) + SUBJECT_COLUMNS


FLAG_PPG_OK = 0x01
FLAG_IMU_OK = 0x02
FLAG_BATTERY_VALID = 0x04
UNKNOWN_AGE_MS = 0xFFFFFFFF


class LineKind(str, Enum):
    DATA = "data"
    STATUS = "status"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class DataSample:
    timestamp_ms: int
    finger: int
    wrist: int
    other: int
    wrist_ax: int
    wrist_ay: int
    wrist_az: int
    wrist_gx: int
    wrist_gy: int
    wrist_gz: int
    wheel_ax: int
    wheel_ay: int
    wheel_az: int
    wheel_gx: int
    wheel_gy: int
    wheel_gz: int
    received_at: datetime

    @property
    def values(self) -> tuple[int, ...]:
        return (
            self.timestamp_ms,
            self.finger,
            self.wrist,
            self.other,
            self.wrist_ax,
            self.wrist_ay,
            self.wrist_az,
            self.wrist_gx,
            self.wrist_gy,
            self.wrist_gz,
            self.wheel_ax,
            self.wheel_ay,
            self.wheel_az,
            self.wheel_gx,
            self.wheel_gy,
            self.wheel_gz,
        )

    @property
    def csv_row(self) -> tuple[object, ...]:
        # Match the system_time format used by the existing CSV collector.
        return self.values + (
            self.received_at.strftime("%Y-%m-%d %H:%M:%S"),
            "",
            "",
        )

    def csv_row_for_devices(
        self,
        selected_devices: tuple[str, ...] | list[str],
        driver_name: str = "",
        other_name: str = "",
    ) -> tuple[object, ...]:
        columns = csv_columns_for_devices(selected_devices)
        value_by_column = dict(zip(DATA_COLUMNS, self.values))
        extras = {
            "system_time": self.received_at.strftime("%Y-%m-%d %H:%M:%S"),
            "driver_name": driver_name,
            "other_name": other_name,
        }
        return tuple(
            extras[column] if column in extras else value_by_column[column]
            for column in columns
        )

    @property
    def wrist_accel(self) -> tuple[int, int, int]:
        return (self.wrist_ax, self.wrist_ay, self.wrist_az)

    @property
    def wrist_gyro(self) -> tuple[int, int, int]:
        return (self.wrist_gx, self.wrist_gy, self.wrist_gz)

    @property
    def wheel_accel(self) -> tuple[int, int, int]:
        return (self.wheel_ax, self.wheel_ay, self.wheel_az)

    @property
    def wheel_gyro(self) -> tuple[int, int, int]:
        return (self.wheel_gx, self.wheel_gy, self.wheel_gz)


@dataclass(frozen=True, slots=True)
class NodeTelemetry:
    age_ms: int | None
    flags: int | None
    battery_mv: int | None
    sequence: int | None


@dataclass(frozen=True, slots=True)
class StatusPacket:
    master_timestamp_ms: int
    nodes: dict[str, NodeTelemetry]
    received_at: datetime


@dataclass(frozen=True, slots=True)
class ParsedLine:
    kind: LineKind
    payload: Union[DataSample, StatusPacket, str]


def _parse_age(raw: str) -> int | None:
    value = int(raw)
    return None if value == UNKNOWN_AGE_MS else value


def _parse_optional_nonnegative(raw: str) -> int | None:
    value = int(raw)
    return None if value < 0 else value


def parse_serial_line(line: str, received_at: datetime | None = None) -> ParsedLine:
    """Parse a master serial line.

    Supported formats:
      * the existing 16-column sample line;
      * enhanced ``@STATUS`` lines emitted by the bundled monitoring firmware;
      * any other text, retained as an informational line.
    """

    text = line.strip()
    now = received_at or datetime.now()
    if not text:
        return ParsedLine(LineKind.INFO, "")

    parts = [part.strip() for part in text.split(",")]

    if parts[0] == "@STATUS":
        if len(parts) != 18:
            raise ValueError(f"状态行应有 18 项，实际为 {len(parts)} 项")

        master_timestamp_ms = int(parts[1])
        node_names = ("finger", "wrist", "other", "wheel")
        ages = parts[2:6]
        flags = parts[6:10]
        batteries = parts[10:14]
        sequences = parts[14:18]
        nodes = {
            name: NodeTelemetry(
                age_ms=_parse_age(ages[index]),
                flags=_parse_optional_nonnegative(flags[index]),
                battery_mv=_parse_optional_nonnegative(batteries[index]),
                sequence=_parse_optional_nonnegative(sequences[index]),
            )
            for index, name in enumerate(node_names)
        }
        return ParsedLine(
            LineKind.STATUS,
            StatusPacket(master_timestamp_ms, nodes, now),
        )

    if len(parts) == len(DATA_COLUMNS):
        try:
            values = tuple(int(value) for value in parts)
        except ValueError as exc:
            raise ValueError("16 列数据中包含非整数") from exc
        return ParsedLine(LineKind.DATA, DataSample(*values, received_at=now))

    return ParsedLine(LineKind.INFO, text)
