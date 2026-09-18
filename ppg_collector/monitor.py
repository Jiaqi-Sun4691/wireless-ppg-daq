from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
import time

from .protocol import (
    DataSample,
    FLAG_BATTERY_VALID,
    FLAG_IMU_OK,
    FLAG_PPG_OK,
    StatusPacket,
)


# Master firmware's @STATUS cadence (01-master-monitor.ino).
STATUS_PERIOD_MS = 500
# A node sitting near a threshold would otherwise toggle every refresh:
# degrade immediately, but require this long of steady improvement to recover.
RECOVER_HOLD_MS = 600


class HealthLevel(str, Enum):
    GOOD = "good"
    WARNING = "warning"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class NodeHealth:
    node_id: str
    name: str
    sensor_name: str
    level: HealthLevel = HealthLevel.UNKNOWN
    link_text: str = "等待数据"
    sensor_text: str = "未检测"
    battery_mv: int | None = None
    battery_percent: int | None = None
    age_ms: int | None = None
    sequence: int | None = None
    flags: int | None = None
    enhanced: bool = False
    change_age_ms: int | None = None
    component_change_age_ms: dict[str, int | None] = field(default_factory=dict)


@dataclass(slots=True)
class SignalWindow:
    values: deque[tuple[float, tuple[int, ...]]] = field(
        default_factory=lambda: deque(maxlen=300)
    )

    def add(self, values: tuple[int, ...], now: float) -> None:
        self.values.append((now, values))

    def recent(self, seconds: float, now: float) -> list[tuple[int, ...]]:
        return [values for timestamp, values in self.values if now - timestamp <= seconds]


NODE_INFO = {
    "finger": ("Finger ESP32", "Finger PPG"),
    "wrist": ("Wrist ESP32", "Wrist PPG + IMU"),
    "other": ("Other ESP32", "Other PPG"),
    "wheel": ("Wheel ESP32", "Wheel IMU"),
}

NODE_COMPONENTS = {
    "finger": ("PPG",),
    "wrist": ("PPG", "IMU"),
    "other": ("PPG",),
    "wheel": ("IMU",),
}


def battery_percentage(millivolts: int | None) -> int | None:
    if millivolts is None or millivolts <= 0:
        return None

    curve = (
        (3300, 0),
        (3500, 10),
        (3600, 20),
        (3700, 40),
        (3800, 60),
        (3900, 75),
        (4000, 85),
        (4100, 95),
        (4200, 100),
    )
    if millivolts <= curve[0][0]:
        return 0
    if millivolts >= curve[-1][0]:
        return 100
    for (low_mv, low_pct), (high_mv, high_pct) in zip(curve, curve[1:]):
        if low_mv <= millivolts <= high_mv:
            fraction = (millivolts - low_mv) / (high_mv - low_mv)
            return round(low_pct + fraction * (high_pct - low_pct))
    return None


class HealthMonitor:
    def __init__(
        self,
        offline_ms: int = 2000,
        delayed_ms: int = 800,
        flat_seconds: float = 2.0,
    ) -> None:
        self.flat_seconds = flat_seconds
        self.nodes = {
            node_id: NodeHealth(node_id, *NODE_INFO[node_id])
            for node_id in NODE_INFO
        }
        self.windows = {node_id: SignalWindow() for node_id in NODE_INFO}
        self.last_status_monotonic: float | None = None
        self.last_sample_monotonic: float | None = None
        self.enhanced_protocol_seen = False
        self.last_component_values: dict[tuple[str, str], tuple[int, ...]] = {}
        self.last_component_change: dict[tuple[str, str], float] = {}
        self._improve_since: dict[str, float] = {}
        # Run the saved values through the same clamping as the settings page,
        # otherwise a stale settings.json (e.g. delayed_ms=200) makes healthy
        # nodes report "数据延迟" forever.
        self.update_thresholds(offline_ms, delayed_ms, flat_seconds)


    _SEVERITY = {
        HealthLevel.GOOD: 0,
        HealthLevel.UNKNOWN: 1,
        HealthLevel.WARNING: 2,
        HealthLevel.ERROR: 3,
    }

    def _apply_hysteresis(
        self,
        node: "NodeHealth",
        new_level: HealthLevel,
        new_link: str,
        now: float,
    ) -> tuple[HealthLevel, str]:
        """Degrade at once, recover only after the improvement holds."""
        old_level = node.level
        # First determination after connecting is not a "recovery" — show it now.
        if old_level is HealthLevel.UNKNOWN:
            self._improve_since.pop(node.node_id, None)
            return new_level, new_link
        if self._SEVERITY[new_level] >= self._SEVERITY[old_level]:
            self._improve_since.pop(node.node_id, None)
            return new_level, new_link
        started = self._improve_since.setdefault(node.node_id, now)
        if (now - started) * 1000 >= RECOVER_HOLD_MS:
            self._improve_since.pop(node.node_id, None)
            return new_level, new_link
        return old_level, node.link_text

    def update_thresholds(
        self,
        offline_ms: int,
        delayed_ms: int,
        flat_seconds: float,
    ) -> None:
        # The master emits @STATUS every 500 ms, so between two status lines the
        # effective age always climbs by up to 500 ms. A delay threshold at or
        # below that makes a perfectly healthy node flicker, hence the floor.
        self.offline_ms = max(STATUS_PERIOD_MS * 2, int(offline_ms))
        self.delayed_ms = max(
            STATUS_PERIOD_MS + 200,
            min(int(delayed_ms), self.offline_ms - 1),
        )
        self.flat_seconds = max(0.5, float(flat_seconds))

    def update_sample(self, sample: DataSample, monotonic_now: float | None = None) -> None:
        now = monotonic_now if monotonic_now is not None else time.monotonic()
        self.last_sample_monotonic = now
        node_values = {
            "finger": (sample.finger,),
            "wrist": (sample.wrist, *sample.wrist_accel, *sample.wrist_gyro),
            "other": (sample.other,),
            "wheel": (*sample.wheel_accel, *sample.wheel_gyro),
        }
        component_values = {
            ("finger", "PPG"): (sample.finger,),
            ("wrist", "PPG"): (sample.wrist,),
            ("wrist", "IMU"): (*sample.wrist_accel, *sample.wrist_gyro),
            ("other", "PPG"): (sample.other,),
            ("wheel", "IMU"): (*sample.wheel_accel, *sample.wheel_gyro),
        }
        for node_id, values in node_values.items():
            self.windows[node_id].add(values, now)
        for component, values in component_values.items():
            if self.last_component_values.get(component) != values:
                self.last_component_values[component] = values
                self.last_component_change[component] = now

        self._refresh_change_ages(now)

        if not self.enhanced_protocol_seen:
            for node in self.nodes.values():
                node.enhanced = False
                node.link_text = "兼容模式"
                node.age_ms = None
            self._apply_signal_checks(now)

    def update_status(self, packet: StatusPacket, monotonic_now: float | None = None) -> None:
        now = monotonic_now if monotonic_now is not None else time.monotonic()
        self.last_status_monotonic = now
        # Switching from compatibility mode to the enhanced protocol changes what
        # link_text means, so it must not be treated as a "recovery" by the
        # hysteresis — let the first enhanced verdict apply immediately.
        if not self.enhanced_protocol_seen:
            for node in self.nodes.values():
                node.level = HealthLevel.UNKNOWN
            self._improve_since.clear()
        self.enhanced_protocol_seen = True

        for node_id, telemetry in packet.nodes.items():
            if node_id not in self.nodes:
                continue
            node = self.nodes[node_id]
            node.enhanced = True
            node.age_ms = telemetry.age_ms
            node.flags = telemetry.flags
            node.sequence = telemetry.sequence

            battery_valid = bool(
                telemetry.flags is not None
                and telemetry.flags & FLAG_BATTERY_VALID
            )
            node.battery_mv = telemetry.battery_mv if battery_valid else None
            node.battery_percent = battery_percentage(node.battery_mv)

        self.refresh(now)

    def refresh(self, monotonic_now: float | None = None) -> None:
        now = monotonic_now if monotonic_now is not None else time.monotonic()
        self._refresh_change_ages(now)

        if self.enhanced_protocol_seen:
            status_transport_age = (
                float("inf")
                if self.last_status_monotonic is None
                else (now - self.last_status_monotonic) * 1000
            )
            for node in self.nodes.values():
                effective_age = (
                    None
                    if node.age_ms is None
                    else node.age_ms + int(status_transport_age)
                )
                if effective_age is None or effective_age >= self.offline_ms:
                    raw_level, raw_link = HealthLevel.ERROR, "离线"
                elif effective_age >= self.delayed_ms:
                    raw_level, raw_link = HealthLevel.WARNING, "数据延迟"
                else:
                    raw_level, raw_link = HealthLevel.GOOD, "在线"
                node.level, node.link_text = self._apply_hysteresis(
                    node, raw_level, raw_link, now
                )

                if node.link_text == "离线":
                    node.sensor_text = "无法检测"
                    continue

                if node.flags is None:
                    node.sensor_text = "状态未知"
                    node.level = HealthLevel.WARNING
                    continue

                if node.node_id in ("finger", "other"):
                    sensor_ok = bool(node.flags & FLAG_PPG_OK)
                    node.sensor_text = "PPG 正常" if sensor_ok else "PPG 异常"
                elif node.node_id == "wheel":
                    sensor_ok = bool(node.flags & FLAG_IMU_OK)
                    node.sensor_text = "IMU 正常" if sensor_ok else "IMU 读取失败"
                else:
                    ppg_ok = bool(node.flags & FLAG_PPG_OK)
                    imu_ok = bool(node.flags & FLAG_IMU_OK)
                    if ppg_ok and imu_ok:
                        node.sensor_text = "PPG、IMU 正常"
                        sensor_ok = True
                    elif not ppg_ok and not imu_ok:
                        node.sensor_text = "PPG、IMU 异常"
                        sensor_ok = False
                    elif not ppg_ok:
                        node.sensor_text = "PPG 异常"
                        sensor_ok = False
                    else:
                        node.sensor_text = "IMU 读取失败"
                        sensor_ok = False

                if not sensor_ok:
                    node.level = HealthLevel.ERROR

                # A successful ADC/I2C call does not guarantee that a sensor is
                # producing fresh values. Keep the host-side flat-line check in
                # enhanced mode so a wedged sensor is still visible.
                if sensor_ok:
                    signal_issue = self._detect_signal_issue(node.node_id, now)
                    if signal_issue is not None:
                        issue_level, issue_text = signal_issue
                        node.sensor_text = issue_text
                        node.level = issue_level

                if node.battery_percent is not None and node.battery_percent <= 15:
                    if node.level == HealthLevel.GOOD:
                        node.level = HealthLevel.WARNING
        else:
            self._apply_signal_checks(now)

    def _apply_signal_checks(self, now: float) -> None:
        for node_id, node in self.nodes.items():
            values = self.windows[node_id].recent(self.flat_seconds, now)
            if len(values) < 8:
                node.level = HealthLevel.UNKNOWN
                node.sensor_text = "正在分析"
                node.link_text = "正在检测"
                continue

            signal_issue = self._detect_signal_issue(node_id, now)
            if signal_issue is None:
                node.level = HealthLevel.GOOD
                node.sensor_text = "数据持续更新"
                node.link_text = "数据更新中"
            else:
                node.level, node.sensor_text = signal_issue
                node.link_text = (
                    "数据未更新" if "未更新" in node.sensor_text else "数据异常"
                )

    def _detect_signal_issue(
        self,
        node_id: str,
        now: float,
    ) -> tuple[HealthLevel, str] | None:
        values = self.windows[node_id].recent(self.flat_seconds, now)
        if len(values) < 8:
            return None

        ppg = [value[0] for value in values] if node_id in ("finger", "wrist", "other") else []
        if ppg and (all(value <= 5 for value in ppg) or all(value >= 4090 for value in ppg)):
            return (HealthLevel.ERROR, "PPG 达到量程边界")

        stale_components: list[str] = []
        threshold_ms = int(self.flat_seconds * 1000)
        for component_name in NODE_COMPONENTS[node_id]:
            age_ms = self.nodes[node_id].component_change_age_ms.get(component_name)
            if age_ms is not None and age_ms >= threshold_ms:
                stale_components.append(
                    f"{component_name} 已 {self._format_age(age_ms)}未更新"
                )
        if stale_components:
            return (HealthLevel.ERROR, "；".join(stale_components))

        if node_id == "wheel" and all(all(axis == 0 for axis in row) for row in values):
            return (HealthLevel.WARNING, "IMU 全零，需检查")
        if node_id == "wrist" and all(all(axis == 0 for axis in row[1:]) for row in values):
            return (HealthLevel.WARNING, "PPG 有数据，IMU 全零")
        return None

    def _refresh_change_ages(self, now: float) -> None:
        for node_id, node in self.nodes.items():
            component_ages: dict[str, int | None] = {}
            for component_name in NODE_COMPONENTS[node_id]:
                changed_at = self.last_component_change.get((node_id, component_name))
                component_ages[component_name] = (
                    None if changed_at is None else max(0, int((now - changed_at) * 1000))
                )
            node.component_change_age_ms = component_ages
            known_ages = [age for age in component_ages.values() if age is not None]
            node.change_age_ms = max(known_ages) if known_ages else None

    @staticmethod
    def _format_age(age_ms: int) -> str:
        if age_ms < 1000:
            return f"{age_ms} ms "
        return f"{age_ms / 1000:.1f} 秒 "

    def snapshot(self) -> dict[str, NodeHealth]:
        self.refresh()
        return self.nodes
