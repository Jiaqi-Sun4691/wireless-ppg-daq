from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path


@dataclass(slots=True)
class AppSettings:
    output_directory: str = str(Path.home() / "Documents" / "PPG Data")
    filename_prefix: str = "ppg_imu_data"
    driver_name: str = ""
    other_name: str = ""
    baud_rate: int = 115200
    # 默认不在采集时编码视频：那会和串口读取抢主线程，而且视频是派生物，
    # 事后在「数据文件」页按需生成即可，时间轴还更准。
    record_video: bool = False
    offline_ms: int = 2000
    delayed_ms: int = 800
    flat_seconds: float = 2.0
    alert_sound: bool = True
    alert_confirm_seconds: float = 1.5
    preferred_port: str = ""
    selected_devices: list[str] = field(
        default_factory=lambda: ["finger", "wrist", "other", "wheel"]
    )
    master_mac: str = ""
    firmware_fqbn: str = "自动识别（稳定优先）"


def settings_path() -> Path:
    return Path.home() / ".ppg_collector_gui" / "settings.json"


def load_settings(path: Path | None = None) -> AppSettings:
    target = path or settings_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return AppSettings()

    defaults = asdict(AppSettings())
    values = {key: raw.get(key, default) for key, default in defaults.items()}
    try:
        return AppSettings(**values)
    except TypeError:
        return AppSettings()


def save_settings(settings: AppSettings, path: Path | None = None) -> None:
    target = path or settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(target)
