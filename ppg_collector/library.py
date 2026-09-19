from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import json
import re

from .plot import MAPPING_SUFFIX
from .protocol import DEVICE_LABELS, FIELD_DEVICE

# ppg_imu_data_2026-08-18_15-30-00.csv / plot_recording_2026-08-18_15-30-00.mp4
STAMP_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})")


@dataclass(frozen=True, slots=True)
class SessionFile:
    """One recorded session: its CSV plus the matching plot video, if any."""

    csv_path: Path
    video_path: Path | None
    recorded_at: datetime | None
    size_bytes: int
    video_bytes: int
    row_count: int
    columns: tuple[str, ...]
    meta: dict | None = None
    mapping_path: Path | None = None


    @property
    def is_rebuilt(self) -> bool:
        """这次的录屏是不是事后从 CSV 重建的。

        看的是帧时间对照表在不在——只有重建会生成它，实时录屏不会。
        对照表和视频必须都还在才算数：视频被删掉之后，剩一张对照表没有
        意义，应该显示成"没重建过"。
        """
        return self.video_path is not None and self.mapping_path is not None

    @property
    def rebuilt_text(self) -> str:
        """这次的录屏有没有用 CSV 重建过。

        重建完又把视频删掉的，算没重建过——列表要反映现在盘上有什么，
        而不是"曾经做过什么"。
        """
        return "是" if self.is_rebuilt else "—"

    @property
    def folder(self) -> Path:
        """The session's own folder, or the root for older flat recordings."""
        return self.csv_path.parent

    @property
    def subject_text(self) -> str:
        """Driver / reference recorded at collection time, if any."""
        meta = self.meta or {}
        driver, other = meta.get("driver", ""), meta.get("other", "")
        if driver and other:
            return f"{driver} / {other}"
        return driver or other or "—"

    @property
    def note_text(self) -> str:
        return (self.meta or {}).get("note", "") or "—"

    @property
    def devices(self) -> tuple[str, ...]:
        found: list[str] = []
        for column in self.columns:
            device = FIELD_DEVICE.get(column)
            if device is not None and device not in found:
                found.append(device)
        return tuple(found)

    @property
    def device_text(self) -> str:
        names = [DEVICE_LABELS.get(d, d) for d in self.devices]
        return "、".join(names) if names else "—"

    @property
    def duration_text(self) -> str:
        if self.row_count <= 1:
            return "—"
        seconds = self.row_count * 0.048        # master outputs every 48 ms
        if seconds < 60:
            return f"{seconds:.0f} 秒"
        return f"{seconds / 60:.1f} 分"

    @property
    def size_text(self) -> str:
        return human_size(self.size_bytes + self.video_bytes)

    @property
    def recorded_text(self) -> str:
        if self.recorded_at is None:
            return "—"
        return self.recorded_at.strftime("%Y-%m-%d %H:%M:%S")


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def stamp_of(path: Path) -> str | None:
    match = STAMP_PATTERN.search(path.name)
    return match.group(1) if match else None


def parse_stamp(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.strptime(stamp, "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None


def count_data_rows(path: Path) -> int:
    """Rows excluding the header, counted without loading the file."""
    try:
        with path.open("rb") as handle:
            newlines = 0
            trailing_newline = True
            while chunk := handle.read(1 << 20):
                newlines += chunk.count(b"\n")
                trailing_newline = chunk.endswith(b"\n")
            if not trailing_newline:
                newlines += 1        # last line has no terminator
    except OSError:
        return 0
    return max(0, newlines - 1)


def read_columns(path: Path) -> tuple[str, ...]:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            header = handle.readline().strip()
    except OSError:
        return ()
    if not header:
        return ()
    return tuple(part.strip() for part in header.split(",") if part.strip())


def read_metadata(folder: Path) -> dict | None:
    """session.json written alongside the data, if this is a session folder."""
    path = folder / "session.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def scan_sessions(directory: Path) -> list[SessionFile]:
    """List recorded sessions in a folder, newest first."""
    if not directory.is_dir():
        return []

    # Recordings now live in one folder per session; older ones sit flat in the
    # root. Scan both so existing data keeps showing up.
    videos: dict[str, Path] = {}
    for pattern in ("*.mp4", "*/*.mp4"):
        for video in directory.glob(pattern):
            stamp = stamp_of(video)
            if stamp:
                videos.setdefault(stamp, video)

    # 帧时间对照表也是 .csv，但它不是一次采集——不排除掉，重建过的采集会
    # 在列表里出现两次。
    csv_files = [
        path for path in list(directory.glob("*.csv")) + list(directory.glob("*/*.csv"))
        if not path.name.endswith(MAPPING_SUFFIX)
    ]
    sessions: list[SessionFile] = []
    for csv_file in csv_files:
        stamp = stamp_of(csv_file)
        video = None
        if stamp:
            # Prefer a video sitting beside the CSV in its own session folder.
            same_folder = [
                v for v in csv_file.parent.glob("*.mp4") if stamp_of(v) == stamp
            ]
            video = same_folder[0] if same_folder else videos.get(stamp)
        try:
            size = csv_file.stat().st_size
        except OSError:
            continue
        video_size = 0
        if video is not None:
            try:
                video_size = video.stat().st_size
            except OSError:
                video = None
        # 帧时间对照表：只有事后重建会生成，实时录屏不会。
        mapping = None
        if video is not None:
            candidate = video.with_name(video.stem + MAPPING_SUFFIX)
            if candidate.is_file():
                mapping = candidate

        recorded = parse_stamp(stamp)
        if recorded is None:
            try:
                recorded = datetime.fromtimestamp(csv_file.stat().st_mtime)
            except OSError:
                recorded = None
        sessions.append(
            SessionFile(
                csv_path=csv_file,
                video_path=video,
                recorded_at=recorded,
                size_bytes=size,
                video_bytes=video_size,
                row_count=count_data_rows(csv_file),
                columns=read_columns(csv_file),
                meta=read_metadata(csv_file.parent),
                mapping_path=mapping,
            )
        )

    sessions.sort(
        key=lambda item: (item.recorded_at or datetime.min),
        reverse=True,
    )
    return sessions
