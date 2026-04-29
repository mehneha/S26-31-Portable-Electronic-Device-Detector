from typing import Optional
import time

import numpy as np

try:
    import serial
except Exception:
    serial = None


def estimate_peak_bandwidth_hz(
    xdata: np.ndarray, ydata: np.ndarray, peak_idx: int, drop_db: float = 6.0
) -> float:
    peak = float(ydata[peak_idx])
    floor = peak - float(drop_db)

    i = peak_idx
    while i > 0 and ydata[i] > floor:
        i -= 1
    f_left = float(xdata[max(i, 0)])

    j = peak_idx
    n = len(ydata)
    while j < n - 1 and ydata[j] > floor:
        j += 1
    f_right = float(xdata[min(j, n - 1)])

    return max(0.0, f_right - f_left)


def classify_band(freq_hz: float, bw_hz: Optional[float] = None) -> str:
    f = float(freq_hz)

    if 2.200e9 < f <= 2.4835e9:
        if bw_hz is not None and bw_hz < 5e6:
            return "Bluetooth"
        return "Wi-Fi"

    if 5.150e9 <= f <= 5.925e9:
        return "Wi-Fi"

    if (700e6 <= f <= 960e6) or (1.710e9 <= f <= 2.200e9) or (2.300e9 <= f <= 2.700e9):
        return "Cellular"

    return "Other"


def check_frequency_range_above_threshold(
    xdata: np.ndarray,
    ydata: np.ndarray,
    center_freq_hz: float,
    bandwidth_hz: float,
    threshold_db: float,
) -> tuple[bool, float, int]:
    if len(xdata) == 0 or len(ydata) == 0:
        return False, -100.0, -1

    half_bw = bandwidth_hz / 2.0
    f_min = center_freq_hz - half_bw
    f_max = center_freq_hz + half_bw

    mask = (xdata >= f_min) & (xdata <= f_max)
    if not np.any(mask):
        return False, -100.0, -1

    range_power = ydata[mask]
    max_power = float(np.max(range_power))

    range_indices = np.where(mask)[0]
    peak_idx_in_range = range_indices[np.argmax(range_power)]

    is_above = max_power > threshold_db
    return is_above, max_power, peak_idx_in_range


def find_peaks_simple(ydata: np.ndarray, min_height: float, min_distance: int = 10) -> np.ndarray:
    peaks = []
    n = len(ydata)

    for i in range(1, n - 1):
        if ydata[i] > ydata[i - 1] and ydata[i] > ydata[i + 1] and ydata[i] > min_height:
            if not peaks or (i - peaks[-1]) >= min_distance:
                peaks.append(i)

    return np.array(peaks, dtype=int)


def open_arduino(port: str = "COM5", baud: int = 9600):
    if serial is None:
        return None, "pyserial not installed"
    try:
        arduino = serial.Serial(port, baud, timeout=0.2, write_timeout=0.2)
        time.sleep(2.0)
        try:
            arduino.reset_input_buffer()
            arduino.reset_output_buffer()
        except Exception:
            pass
        return arduino, f"Connected ({port} @ {baud})"
    except Exception as exc:
        return None, f"Failed: {exc}"


def write_arduino(arduino, detected: bool | None, cfg: Optional[dict] = None) -> None:
    if arduino is None:
        return

    if cfg is None:
        cfg = {
            "red_enabled": True,
            "green_enabled": True,
            "yellow_enabled": True,
            "speaker_enabled": True,
            "red_duration_ms": 300,
            "green_duration_ms": 300,
            "speaker_duration_ms": 300,
        }

    red_enabled = 1 if cfg.get("red_enabled", True) else 0
    green_enabled = 1 if cfg.get("green_enabled", True) else 0
    yellow_enabled = 1 if cfg.get("yellow_enabled", True) else 0
    speaker_enabled = 1 if cfg.get("speaker_enabled", True) else 0
    red_duration_ms = max(0, int(cfg.get("red_duration_ms", 300)))
    green_duration_ms = max(0, int(cfg.get("green_duration_ms", 300)))
    speaker_duration_ms = max(0, int(cfg.get("speaker_duration_ms", 300)))

    try:
        if detected:
            state = "RED"
        elif detected is None:
            state = "YELLOW"
        else:
            state = "GREEN"
        cmd = (
            f"CFG,{state},{red_enabled},{green_enabled},{yellow_enabled},"
            f"{speaker_enabled},{red_duration_ms},{green_duration_ms},{speaker_duration_ms}\n"
        )
        arduino.write(cmd.encode("ascii", errors="ignore"))
    except Exception:
        return


def close_arduino(arduino) -> None:
    if arduino is None:
        return
    try:
        arduino.close()
    except Exception:
        return
