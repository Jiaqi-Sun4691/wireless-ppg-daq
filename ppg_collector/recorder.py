from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import TextIO

from matplotlib.animation import FFMpegWriter
from matplotlib.figure import Figure

from .protocol import DEVICE_IDS, CSV_COLUMNS, DataSample, csv_columns_for_devices


@dataclass(frozen=True, slots=True)
class RecordingResult:
    csv_path: Path | None
    video_path: Path | None
    row_count: int
    csv_columns: tuple[str, ...]


def safe_prefix(prefix: str) -> str:
    cleaned = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", prefix.strip(), flags=re.UNICODE)
    return cleaned.strip("_") or "ppg_imu_data"


class SessionRecorder:
    def __init__(self) -> None:
        self.csv_path: Path | None = None
        self.session_directory: Path | None = None
        self.metadata: dict = {}
        self.driver_name: str = ""
        self.other_name: str = ""
        self.video_path: Path | None = None
        self._file: TextIO | None = None
        self._writer: csv.writer | None = None
        self._video_writer: FFMpegWriter | None = None
        self.row_count = 0
        self.video_error: str | None = None
        self.selected_devices: tuple[str, ...] = DEVICE_IDS
        self.csv_columns: tuple[str, ...] = CSV_COLUMNS

    @property
    def active(self) -> bool:
        return self._file is not None

    @property
    def video_active(self) -> bool:
        return self._video_writer is not None

    def start(
        self,
        output_directory: Path,
        prefix: str,
        record_video: bool,
        figure: Figure | None = None,
        selected_devices: tuple[str, ...] | list[str] | None = None,
        metadata: dict | None = None,
    ) -> None:
        if self.active:
            raise RuntimeError("已有采集任务正在进行")

        output_directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        base_name = f"{safe_prefix(prefix)}_{stamp}"
        # One folder per session, so the CSV, the plot video and anything you
        # add later (dashcam footage, notes) stay together instead of piling up
        # in a single flat directory.
        session_directory = output_directory / base_name
        session_directory.mkdir(parents=True, exist_ok=True)
        self.session_directory = session_directory
        self.csv_path = session_directory / f"{base_name}.csv"
        # Same stem as the CSV so a session's files sort next to each other.
        self.video_path = (
            session_directory / f"{base_name}.mp4"
            if record_video
            else None
        )
        self.row_count = 0
        self.video_error = None
        self.selected_devices = tuple(
            DEVICE_IDS if selected_devices is None else selected_devices
        )
        self.csv_columns = csv_columns_for_devices(self.selected_devices)

        # Who took part and any note. Kept both in session.json and as CSV
        # columns on every row, so a CSV shared on its own still says whose
        # drive it was.
        meta = metadata or {}
        self.driver_name = str(meta.get("driver", "") or "")
        self.other_name = str(meta.get("other", "") or "")
        self.metadata = {
            "session": base_name,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "devices": list(self.selected_devices),
            "csv_columns": list(self.csv_columns),
            **(metadata or {}),
        }
        self._write_metadata()

        self._file = self.csv_path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(self.csv_columns)
        self._file.flush()

        if record_video and figure is not None and self.video_path is not None:
            try:
                writer = FFMpegWriter(
                    fps=20,
                    metadata={"title": "PPG IMU Plot Recording"},
                )
                writer.setup(figure, str(self.video_path), dpi=120)
                self._video_writer = writer
            except Exception as exc:  # FFmpeg availability varies by machine.
                self._video_writer = None
                self.video_error = str(exc)


    def _write_metadata(self) -> None:
        if self.session_directory is None:
            return
        try:
            (self.session_directory / "session.json").write_text(
                json.dumps(self.metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def write_sample(self, sample: DataSample) -> None:
        if self._writer is None or self._file is None:
            return
        self._writer.writerow(
            sample.csv_row_for_devices(
                self.selected_devices, self.driver_name, self.other_name
            )
        )
        self.row_count += 1
        # The original collector flushes every row. At about 20 Hz this is
        # inexpensive and minimizes data loss after a cable disconnect.
        self._file.flush()

    def grab_video_frame(self) -> None:
        if self._video_writer is not None:
            self._video_writer.grab_frame()

    def stop(self) -> RecordingResult:
        video_writer = self._video_writer
        self._video_writer = None
        if video_writer is not None:
            try:
                video_writer.finish()
            except Exception as exc:
                self.video_error = str(exc)

        if self._file is not None:
            self._file.flush()
            self._file.close()
        if self.row_count == 0 and self.csv_path is not None:
            try:
                self.csv_path.unlink()
            except OSError:
                pass
            # Drop the session folder too, but only if nothing else landed in it.
            directory = getattr(self, "session_directory", None)
            if directory is not None:
                # The metadata was written at start, so remove it as well or the
                # folder would never be empty.
                try:
                    (directory / "session.json").unlink()
                except OSError:
                    pass
                try:
                    next(directory.iterdir())
                except StopIteration:
                    directory.rmdir()
                except OSError:
                    pass
        self._file = None
        self._writer = None
        if self.row_count and self.session_directory is not None:
            self.metadata["ended_at"] = datetime.now().isoformat(timespec="seconds")
            self.metadata["rows"] = self.row_count
            self.metadata["duration_seconds"] = round(self.row_count * 0.048, 1)
            self._write_metadata()

        result = RecordingResult(
            self.csv_path,
            self.video_path,
            self.row_count,
            self.csv_columns,
        )
        return result
