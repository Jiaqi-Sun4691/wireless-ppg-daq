from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
import queue
import random
import threading
import time

import serial
from serial.tools import list_ports

from .protocol import (
    FLAG_BATTERY_VALID,
    FLAG_IMU_OK,
    FLAG_PPG_OK,
)


@dataclass(frozen=True, slots=True)
class SerialEvent:
    kind: str
    payload: str


def available_ports() -> list[tuple[str, str]]:
    ports = []
    for port in list_ports.comports():
        description = port.description or "串口设备"
        identity = f"{port.device} {description}".lower()
        if "bluetooth" in identity or "debug-console" in identity:
            continue
        ports.append((port.device, description))
    return sorted(ports, key=lambda item: item[0])


class SerialWorker:
    def __init__(
        self,
        port: str,
        baud_rate: int,
        events: queue.Queue[SerialEvent],
    ) -> None:
        self.port = port
        self.baud_rate = baud_rate
        self.events = events
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._serial: serial.Serial | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="serial-reader")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        serial_port = self._serial
        if serial_port is not None:
            try:
                serial_port.close()
            except serial.SerialException:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)

    def _run(self) -> None:
        try:
            with serial.Serial(self.port, self.baud_rate, timeout=0.25) as serial_port:
                self._serial = serial_port
                serial_port.reset_input_buffer()
                self.events.put(SerialEvent("connected", self.port))
                while not self._stop_event.is_set():
                    try:
                        raw = serial_port.readline()
                    except serial.SerialException as exc:
                        if not self._stop_event.is_set():
                            self.events.put(SerialEvent("error", f"串口读取失败：{exc}"))
                        break
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if line:
                        self.events.put(SerialEvent("line", line))
        except (serial.SerialException, OSError) as exc:
            self.events.put(SerialEvent("error", f"无法打开串口 {self.port}：{exc}"))
        finally:
            self._serial = None
            self.events.put(SerialEvent("disconnected", self.port))


class ReplayWorker:
    """Replay a previously recorded CSV as if it arrived from the master.

    Used for self-checks without hardware. It emits the real 16 columns from
    an existing session file, so the parsing, plotting and CSV paths are
    exercised with genuine data rather than synthetic waveforms.
    """

    def __init__(
        self,
        csv_path: str,
        events: queue.Queue[SerialEvent],
        speed: float = 1.0,
        max_rows: int | None = None,
    ) -> None:
        self.csv_path = csv_path
        self.events = events
        self.speed = max(0.1, speed)
        self.max_rows = max_rows
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="csv-replay")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        import csv as _csv

        self.events.put(SerialEvent("connected", f"回放 {self.csv_path}"))
        try:
            with open(self.csv_path, newline="", encoding="utf-8") as handle:
                reader = _csv.reader(handle)
                header = next(reader, None)
                if header is None:
                    return
                previous_ts: int | None = None
                emitted = 0
                for row in reader:
                    if self._stop_event.is_set():
                        break
                    if len(row) < 16:
                        continue
                    values = [cell.strip() for cell in row[:16]]
                    try:
                        timestamp = int(values[0])
                    except ValueError:
                        continue
                    if previous_ts is not None:
                        gap = (timestamp - previous_ts) / 1000.0 / self.speed
                        if 0 < gap < 1.0:
                            time.sleep(gap)
                    previous_ts = timestamp
                    self.events.put(SerialEvent("line", ",".join(values)))
                    emitted += 1
                    if self.max_rows is not None and emitted >= self.max_rows:
                        break
        except OSError as exc:
            self.events.put(SerialEvent("error", f"回放失败：{exc}"))
        finally:
            self.events.put(SerialEvent("disconnected", "回放结束"))
