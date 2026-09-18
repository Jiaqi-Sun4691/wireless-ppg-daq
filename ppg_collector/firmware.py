from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Callable

import serial


DEFAULT_ARDUINO_CLI = Path(
    "/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli"
)
DEFAULT_FQBN = "esp32:esp32:esp32"
DEFAULT_UPLOAD_SPEED = "115200"
AUTO_BOARD_SELECTION = "自动识别（稳定优先）"

MAC_PATTERN = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
RECEIVER_MAC_PATTERN = re.compile(
    r"uint8_t\s+receiverMac\s*\[\s*\]\s*=\s*\{[^}]*\}\s*;",
    re.MULTILINE,
)
CHIP_LINE_PATTERN = re.compile(
    r"(?:Chip is|Chip type:)\s*([^\r\n]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class BoardProfile:
    chip_id: str
    display_name: str
    fqbn: str
    board_options: tuple[str, ...]


# Most-compatible settings, chosen for cheap/unknown boards:
#   UploadSpeed=115200  slowest, survives poor USB cables and clone bridge chips
#   FlashMode=qio       matches the Arduino IDE default for ESP32 Dev Module,
#                       which is the combination verified working on this rig
#   FlashFreq=40        80 MHz is unreliable on long traces / low-grade flash
#   FlashSize=4M        smallest size found on virtually every module; declaring
#                       more than the board has makes it fail to boot
#   PSRAM=disabled      many modules have no PSRAM; enabling it crashes at boot
#   PartitionScheme=default  matches the 4 MB assumption above
# Options differ per chip: S3 folds the frequency into FlashMode, and C3/C6
# expose no PSRAM option at all — passing an unsupported option aborts the build.
_COMMON_SAFE = (
    "UploadSpeed=115200",
    "FlashMode=qio",
    "FlashSize=4M",
    "PartitionScheme=default",
    "DebugLevel=none",
)

BOARD_PROFILES = {
    "esp32": BoardProfile(
        "esp32",
        "ESP32（经典款）",
        "esp32:esp32:esp32",
        _COMMON_SAFE + ("FlashFreq=40", "PSRAM=disabled", "EraseFlash=none"),
    ),
    "esp32s2": BoardProfile(
        "esp32s2",
        "ESP32-S2",
        "esp32:esp32:esp32s2",
        _COMMON_SAFE + ("FlashFreq=40", "PSRAM=disabled"),
    ),
    "esp32s3": BoardProfile(
        "esp32s3",
        "ESP32-S3",
        "esp32:esp32:esp32s3",
        _COMMON_SAFE + ("PSRAM=disabled",),      # S3 has no separate FlashFreq
    ),
    "esp32c3": BoardProfile(
        "esp32c3",
        "ESP32-C3",
        "esp32:esp32:esp32c3",
        _COMMON_SAFE + ("FlashFreq=40",),        # C3 has no PSRAM option
    ),
    "esp32c6": BoardProfile(
        "esp32c6",
        "ESP32-C6",
        "esp32:esp32:esp32c6",
        _COMMON_SAFE + ("FlashFreq=40",),        # C6 has no PSRAM option
    ),
}


@dataclass(frozen=True, slots=True)
class FirmwareTarget:
    target_id: str
    display_name: str
    folder_name: str
    requires_master_mac: bool


FIRMWARE_TARGETS = (
    FirmwareTarget("master", "Master ESP32", "01-master-monitor", False),
    FirmwareTarget("finger", "Finger ESP32", "02-finger-monitor", True),
    FirmwareTarget("wrist", "Wrist PPG + IMU ESP32", "03-wrist-monitor", True),
    FirmwareTarget("other", "Other PPG ESP32", "04-other-monitor", True),
    FirmwareTarget("wheel", "Wheel IMU ESP32", "05-wheel-monitor", True),
)
TARGET_BY_ID = {target.target_id: target for target in FIRMWARE_TARGETS}


@dataclass(frozen=True, slots=True)
class FlashResult:
    target: FirmwareTarget
    port: str
    master_mac: str | None = None
    board_profile: BoardProfile | None = None


class FirmwareError(RuntimeError):
    pass


def normalize_mac(value: str) -> str:
    match = MAC_PATTERN.search(value.strip())
    if match is None:
        raise ValueError("MAC 地址格式应为 AA:BB:CC:DD:EE:FF")
    return match.group(0).upper()


def mac_to_cpp_initializer(mac: str) -> str:
    normalized = normalize_mac(mac)
    return ", ".join(f"0x{part}" for part in normalized.split(":"))


def inject_master_mac(source: str, master_mac: str) -> str:
    initializer = mac_to_cpp_initializer(master_mac)
    replacement = f"uint8_t receiverMac[] = {{{initializer}}};"
    updated, count = RECEIVER_MAC_PATTERN.subn(replacement, source, count=1)
    if count != 1:
        raise FirmwareError("Slave 固件中没有找到 receiverMac 定义")
    return updated


def find_arduino_cli() -> Path | None:
    if DEFAULT_ARDUINO_CLI.is_file():
        return DEFAULT_ARDUINO_CLI
    discovered = shutil.which("arduino-cli")
    return Path(discovered) if discovered else None


def find_esptool() -> Path | None:
    tools_root = (
        Path.home()
        / "Library"
        / "Arduino15"
        / "packages"
        / "esp32"
        / "tools"
        / "esptool_py"
    )
    candidates = sorted(tools_root.glob("*/esptool"), reverse=True)
    if candidates:
        return candidates[0]
    discovered = shutil.which("esptool") or shutil.which("esptool.py")
    return Path(discovered) if discovered else None


def board_profile_from_output(output: str) -> BoardProfile:
    match = CHIP_LINE_PATTERN.search(output)
    if match is None:
        raise FirmwareError("未能识别 ESP32 芯片型号")
    chip_text = match.group(1).upper().replace("_", "-")
    checks = (
        ("ESP32-S3", "esp32s3"),
        ("ESP32-S2", "esp32s2"),
        ("ESP32-C3", "esp32c3"),
        ("ESP32-C6", "esp32c6"),
    )
    for marker, profile_id in checks:
        if marker in chip_text:
            return BOARD_PROFILES[profile_id]
    if "ESP32" in chip_text and not any(
        marker in chip_text for marker in ("ESP32-H", "ESP32-P", "ESP32-C2", "ESP32-C5")
    ):
        return BOARD_PROFILES["esp32"]
    raise FirmwareError(
        f"检测到尚未验证的芯片：{match.group(1).strip()}。"
        "为避免使用错误针脚或烧录参数，已停止烧录。"
    )


def stable_options_for_fqbn(fqbn: str) -> tuple[str, ...]:
    for profile in BOARD_PROFILES.values():
        if profile.fqbn == fqbn:
            return profile.board_options
    return (f"UploadSpeed={DEFAULT_UPLOAD_SPEED}",)


# Preferred value for each option, most conservative first. Third-party board
# definitions expose wildly different option sets (some have no FlashMode at
# all), so an unsupported option must never be sent — it aborts the build.
SAFE_OPTION_PREFERENCES = (
    ("UploadSpeed", DEFAULT_UPLOAD_SPEED),
    ("FlashMode", "qio"),
    ("FlashFreq", "40"),
    ("FlashSize", "4M"),
    ("PSRAM", "disabled"),
    ("PartitionScheme", "default"),
    ("DebugLevel", "none"),
)


def supported_option_keys(details_output: str) -> set[str]:
    """Option identifiers advertised by ``arduino-cli board details``."""
    return set(re.findall(r"\b([A-Za-z]+)=\S+", details_output))


def safe_options_from_details(details_output: str) -> tuple[str, ...]:
    """Pick the conservative value of every option this board actually offers."""
    available = supported_option_keys(details_output)
    return tuple(
        f"{key}={value}"
        for key, value in SAFE_OPTION_PREFERENCES
        if key in available
    )


class FirmwareManager:
    def __init__(
        self,
        firmware_root: Path | None = None,
        cli_path: Path | None = None,
        fqbn: str = DEFAULT_FQBN,
    ) -> None:
        self.firmware_root = firmware_root or Path(__file__).resolve().parents[1] / "ESP32状态固件"
        self.cli_path = cli_path or find_arduino_cli()
        self.esptool_path = find_esptool()
        self.fqbn = fqbn

    def environment_status(self) -> tuple[bool, str]:
        if self.cli_path is None or not self.cli_path.is_file():
            return False, "未找到 Arduino CLI；请先安装 Arduino IDE 2。"
        core_root = Path.home() / "Library" / "Arduino15" / "packages" / "esp32" / "hardware" / "esp32"
        if not core_root.is_dir() or not any(core_root.iterdir()):
            return False, "未安装 Espressif ESP32 开发板核心。"
        missing = [
            target.display_name
            for target in FIRMWARE_TARGETS
            if not self.sketch_path(target.target_id).is_file()
        ]
        if missing:
            return False, f"缺少固件：{'、'.join(missing)}"
        return True, f"Arduino CLI 与 ESP32 core 已就绪（{self.fqbn}）"

    def detect_board_profile(
        self,
        port: str,
        log: Callable[[str], None] | None = None,
    ) -> BoardProfile:
        if self.esptool_path is None or not self.esptool_path.is_file():
            raise FirmwareError("未找到 ESP32 芯片识别工具 esptool")
        emit = log or (lambda _line: None)
        emit("正在以 115200 速度识别当前 ESP32 芯片…")
        command = [
            str(self.esptool_path),
            "--chip",
            "auto",
            "--port",
            port,
            "--baud",
            DEFAULT_UPLOAD_SPEED,
            "--connect-attempts",
            "7",
            "--after",
            "hard-reset",
            "chip-id",
        ]
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise FirmwareError(
                "ESP32 芯片识别超时。请检查 USB 线，必要时按住 BOOT 键重试。"
            ) from exc
        output = completed.stdout or ""
        for line in output.splitlines():
            cleaned = line.strip()
            if cleaned:
                emit(cleaned)
        if completed.returncode != 0:
            raise FirmwareError(
                "无法连接当前 ESP32 来识别芯片。"
                "请检查串口和 USB 线，或按住 BOOT 键后重试。"
            )
        profile = board_profile_from_output(output)
        emit(
            f"识别结果：{profile.display_name}；使用稳定配置 "
            f"{profile.fqbn}，上传速度 115200。"
        )
        return profile

    def safe_options_for_manual_fqbn(
        self,
        fqbn: str,
        log: Callable[[str], None] | None = None,
    ) -> tuple[str, ...]:
        """Ask the CLI which options this board defines, then take the safe value.

        Board definitions vary a lot (nodemcu-32s has no FlashMode, wrover has
        no FlashSize...). Querying avoids sending an option the board rejects.
        """
        emit = log or (lambda _line: None)
        known = stable_options_for_fqbn(fqbn)
        cli = self.cli_path or find_arduino_cli()
        if cli is None:
            return known
        try:
            completed = subprocess.run(
                [str(cli), "board", "details", "-b", fqbn],
                capture_output=True,
                text=True,
                timeout=25,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            emit(f"无法查询开发板选项（{exc}），改用默认稳妥参数。")
            return known
        if completed.returncode != 0:
            return known
        options = safe_options_from_details(completed.stdout)
        return options or known

    def sketch_path(self, target_id: str) -> Path:
        target = TARGET_BY_ID[target_id]
        return self.firmware_root / target.folder_name / f"{target.folder_name}.ino"

    def prepared_source(self, target_id: str, master_mac: str | None = None) -> str:
        target = TARGET_BY_ID[target_id]
        source = self.sketch_path(target_id).read_text(encoding="utf-8")
        if target.requires_master_mac:
            if not master_mac:
                raise FirmwareError("烧录 Slave 前必须先读取 Master MAC")
            source = inject_master_mac(source, master_mac)
        return source

    def flash(
        self,
        target_id: str,
        port: str,
        master_mac: str | None = None,
        log: Callable[[str], None] | None = None,
        auto_detect_board: bool = False,
        fqbn: str | None = None,
    ) -> FlashResult:
        target = TARGET_BY_ID[target_id]
        if self.cli_path is None:
            raise FirmwareError("未找到 Arduino CLI")
        if not port:
            raise FirmwareError("没有选择烧录串口")

        emit = log or (lambda _line: None)
        source = self.prepared_source(target_id, master_mac)
        board_profile: BoardProfile | None = None
        selected_fqbn = fqbn or self.fqbn
        if auto_detect_board:
            board_profile = self.detect_board_profile(port, emit)
            selected_fqbn = board_profile.fqbn
            board_options = board_profile.board_options
        else:
            board_options = self.safe_options_for_manual_fqbn(selected_fqbn, emit)
            emit(
                f"手动选择开发板：{selected_fqbn}；已套用该板型支持的最稳妥参数："
                + "、".join(board_options)
            )
        with tempfile.TemporaryDirectory(prefix=f"ppg_firmware_{target_id}_") as temporary:
            temporary_root = Path(temporary)
            sketch_directory = temporary_root / target.folder_name
            sketch_directory.mkdir()
            sketch_file = sketch_directory / f"{target.folder_name}.ino"
            sketch_file.write_text(source, encoding="utf-8")
            build_path = temporary_root / "build"
            build_path.mkdir()

            command = [
                str(self.cli_path),
                "compile",
                "--upload",
                "--no-color",
                "--fqbn",
                selected_fqbn,
                "--board-options",
                ",".join(board_options),
                "--build-path",
                str(build_path),
                "--port",
                port,
                str(sketch_directory),
            ]
            emit(f"开始编译 {target.display_name}…")
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                cleaned = line.rstrip()
                if cleaned:
                    emit(cleaned)
            exit_code = process.wait()
            if exit_code != 0:
                raise FirmwareError(
                    f"{target.display_name} 编译或烧录失败（代码 {exit_code}）"
                )
            emit(f"{target.display_name} 烧录成功。")

        detected_mac: str | None = None
        if target_id == "master":
            emit("正在等待 Master 重启并读取 MAC…")
            detected_mac = self.read_master_mac(port, timeout_seconds=15.0, log=emit)
            emit(f"已读取 Master MAC：{detected_mac}")
        return FlashResult(target, port, detected_mac, board_profile)

    def read_master_mac(
        self,
        port: str,
        timeout_seconds: float = 12.0,
        log: Callable[[str], None] | None = None,
    ) -> str:
        emit = log or (lambda _line: None)
        deadline = time.monotonic() + timeout_seconds
        last_error: Exception | None = None

        while time.monotonic() < deadline:
            try:
                with serial.Serial(port, 115200, timeout=0.35) as serial_port:
                    while time.monotonic() < deadline:
                        raw = serial_port.readline()
                        if not raw:
                            continue
                        line = raw.decode("utf-8", errors="ignore").strip()
                        if line.startswith("@MASTER_MAC,"):
                            return normalize_mac(line)
            except (serial.SerialException, OSError) as exc:
                last_error = exc
                time.sleep(0.4)

        suffix = f"：{last_error}" if last_error else ""
        raise FirmwareError(f"烧录成功，但没有从串口读到 Master MAC{suffix}")
