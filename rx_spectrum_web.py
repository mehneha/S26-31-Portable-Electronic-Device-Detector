#!/usr/bin/env python3
#
# Copyright 2025 Ettus Research, a National Instruments Brand
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Dash web UI for spectrum display and detection.

Run:
  python rx_spectrum_web.py

This keeps rx_spectrum_to_pyplot.py intact and provides a browser-based
settings UI, live plot, and log output.
"""

import argparse
import threading
import time
import sys
from collections import deque
from datetime import datetime
from typing import Optional, Tuple

try:
    import dash
    from dash import dcc, html, dash_table
    from dash.dependencies import Input, Output, State
except Exception:
    print("Error: dash is required. Install with: pip install dash plotly")
    sys.exit(1)

import numpy as np
import uhd

# Fixed Welch defaults.
WELCH_NPERSEG_DEFAULT = 4096
WELCH_NOVERLAP_DEFAULT = 2048

# Optional Excel writer (pandas + openpyxl). Falls back to CSV if not present.
try:
    import pandas as pd
except Exception:
    pd = None

# Optional scipy for peak finding and Welch's method. Falls back to simple method if not present.
try:
    from scipy.signal import find_peaks, welch
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False
    welch = None


SWEEP_START_HZ = 650e6
SWEEP_END_HZ = 5.5e9
SWEEP_STEP_HZ = 50e6
EXCLUDED_RANGES_HZ = [
    (1.0e9, 2.0e9),
    (3.0e9, 4.0e9),
    (4.0e9, 5.0e9),
]


def build_sweep_plan() -> list[float]:
    freqs = []
    f = SWEEP_START_HZ
    while f <= SWEEP_END_HZ + 1.0:
        skip = False
        for low, high in EXCLUDED_RANGES_HZ:
            if low <= f <= high:
                skip = True
                break
        if not skip:
            freqs.append(f)
        f += SWEEP_STEP_HZ
    return freqs


def psd(nfft: int, samples: np.ndarray) -> np.ndarray:
    """Return the power spectral density of `samples`."""
    window = np.hamming(nfft)
    fft = np.fft.fft(samples * window)
    window_power = sum(window * window) / nfft
    logfft = (
        20 * np.log10(np.abs(np.fft.fftshift(fft)))
        - 10 * np.log10(window_power)
        - 20 * np.log10(nfft)
        + 3
    )
    return logfft


def estimate_peak_bandwidth_hz(
    xdata: np.ndarray, ydata: np.ndarray, peak_idx: int, drop_db: float = 6.0
) -> float:
    """Rough occupied bandwidth estimate using -drop_db points around the peak."""
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
    """
    Classify into one of:
      - 'Wi-Fi 2.4 GHz'
      - 'Bluetooth (2.4 GHz)'
      - 'Wi-Fi 5 GHz'
      - 'Cellular'
      - 'Other'
    """
    f = float(freq_hz)

    if 2.400e9 <= f <= 2.4835e9:
        if bw_hz is not None and bw_hz < 5e6:
            return "Bluetooth (2.4 GHz)"
        return "Wi-Fi 2.4 GHz"

    if 5.150e9 <= f <= 5.925e9:
        return "Wi-Fi 5 GHz"

    if (700e6 <= f <= 960e6) or (1.710e9 <= f <= 2.170e9) or (2.300e9 <= f <= 2.700e9):
        return "Cellular"

    return "Other"


def compute_welch_psd(
    samples: np.ndarray,
    sample_rate: float,
    nfft: Optional[int] = None,
    nperseg: Optional[int] = None,
    noverlap: Optional[int] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Compute the Power Spectral Density using Welch's method.

    Uses scipy.signal.welch when available; otherwise performs a manual Welch
    estimate matching the MATLAB style (Hann window, 50% overlap default).
    """
    if nfft is None:
        nfft = len(samples)

    if len(samples) == 0:
        return np.array([]), np.array([])

    if nperseg is None:
        nperseg = min(len(samples), max(1024, nfft // 4))
    else:
        nperseg = min(nperseg, len(samples))

    if nperseg < 2:
        nperseg = min(len(samples), 2)

    if noverlap is None:
        noverlap = nperseg // 2
    else:
        noverlap = min(noverlap, nperseg - 1)

    is_complex = np.iscomplexobj(samples)

    if HAS_SCIPY and welch is not None:
        freqs, psd_linear = welch(
            samples,
            fs=sample_rate,
            nperseg=nperseg,
            noverlap=noverlap,
            nfft=nfft,
            return_onesided=not is_complex,
            scaling="density",
        )
        order = np.argsort(freqs)
        return freqs[order], psd_linear[order]

    window = np.hanning(nperseg)
    step = max(1, nperseg - noverlap)
    scale = np.sum(window ** 2)

    segments = []
    for start in range(0, len(samples) - nperseg + 1, step):
        segment = samples[start : start + nperseg]
        windowed = segment * window
        fft_vals = np.fft.fft(windowed, n=nfft)
        segments.append(np.abs(fft_vals) ** 2)

    if not segments:
        fft_vals = np.fft.fft(samples[:nperseg] * window, n=nfft)
        segments = [np.abs(fft_vals) ** 2]

    psd_linear = np.mean(segments, axis=0) / (scale * sample_rate)
    freqs = np.fft.fftfreq(nfft, d=1.0 / sample_rate)

    if not is_complex:
        half_idx = nfft // 2 + 1
        psd_linear = psd_linear[:half_idx]
        freqs = freqs[:half_idx]

    order = np.argsort(freqs)
    return freqs[order], psd_linear[order]


def calculate_noise_floor_welch(
    samples: np.ndarray,
    sample_rate: float,
    nfft: int,
    nperseg: Optional[int] = None,
    noverlap: Optional[int] = None,
) -> float:
    """
    Calculate noise floor using Welch's method for better noise estimation.
    """
    if len(samples) == 0:
        return -100.0

    try:
        _, psd_linear = compute_welch_psd(
            samples,
            sample_rate=sample_rate,
            nfft=nfft,
            nperseg=nperseg,
            noverlap=noverlap,
        )
    except Exception as exc:
        print(f"Warning: Welch's method failed ({exc}), falling back to percentile method")
        logfft = psd(nfft, samples)
        return float(np.percentile(logfft, 50.0))

    psd_db = 10 * np.log10(psd_linear + 1e-20)
    return float(np.median(psd_db))


def check_frequency_range_above_threshold(
    xdata: np.ndarray,
    ydata: np.ndarray,
    center_freq_hz: float,
    bandwidth_hz: float,
    threshold_db: float,
) -> Tuple[bool, float, int]:
    """
    Check if a frequency range has power above threshold.
    """
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
    """
    Simple peak finding algorithm (fallback when scipy is not available).
    """
    peaks = []
    n = len(ydata)

    for i in range(1, n - 1):
        if ydata[i] > ydata[i - 1] and ydata[i] > ydata[i + 1] and ydata[i] > min_height:
            if not peaks or (i - peaks[-1]) >= min_distance:
                peaks.append(i)

    return np.array(peaks, dtype=int)


class DetectionLogger:
    """Collects detections and writes them to Excel (or CSV fallback) on exit."""
    def __init__(self, out_path: str = "detected_devices.xlsx"):
        self.out_path = out_path
        self.rows = []

    def add(self, *, ts: datetime, peak_freq_hz: float, peak_power_db: float,
            band: str, center_freq_hz: float, sample_rate_sps: float, gain_db: float, channel: int):
        self.rows.append({
            "timestamp": ts.isoformat(timespec="seconds"),
            "peak_freq_hz": float(peak_freq_hz),
            "peak_power_db": round(float(peak_power_db), 2),
            "detection": band,
            "center_freq_hz": float(center_freq_hz),
            "sample_rate_sps": float(sample_rate_sps),
            "rx_gain_db": float(gain_db),
            "channel": int(channel),
        })

    def save(self):
        if not self.rows:
            return
        if pd is not None:
            try:
                pd.DataFrame(self.rows).to_excel(self.out_path, index=False)
                print(f"Saved detections to {self.out_path}")
                return
            except Exception as exc:
                print(f"Excel save failed ({exc}); falling back to CSV.")
        csv_path = self.out_path.rsplit(".", 1)[0] + ".csv"
        import csv
        with open(csv_path, "w", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "timestamp", "peak_freq_hz", "peak_power_db", "detection",
                    "center_freq_hz", "sample_rate_sps", "rx_gain_db", "channel"
                ],
            )
            writer.writeheader()
            writer.writerows(self.rows)
        print(f"Saved detections to {csv_path}")


class SpectrumWorker(threading.Thread):
    def __init__(
        self,
        settings: dict,
        log_buffer: deque[str],
        log_lock: threading.Lock,
        detect_buffer: deque[dict],
        detect_lock: threading.Lock,
    ):
        super().__init__(daemon=True)
        self.settings = settings
        self.log_buffer = log_buffer
        self.log_lock = log_lock
        self.detect_buffer = detect_buffer
        self.detect_lock = detect_lock
        self.data_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.latest = {"x": np.array([]), "y": np.array([]), "y_range": None, "title": ""}
        self.noise_floor = None
        self.rescale_pending = True
        self.dlog = DetectionLogger(out_path="detected_devices.xlsx")

    def log(self, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {message}"
        with self.log_lock:
            self.log_buffer.append(line)
        print(line)

    def log_detection(self, row: dict) -> None:
        with self.detect_lock:
            self.detect_buffer.append(row)

    def stop(self) -> None:
        self.stop_event.set()

    def get_latest(self):
        with self.data_lock:
            return {
                "x": self.latest["x"].copy(),
                "y": self.latest["y"].copy(),
                "y_range": self.latest["y_range"],
                "title": self.latest["title"],
            }

    def run(self) -> None:
        args = self.settings
        sim_offsets: list[float] = []
        sim_freqs: list[float] = []

        if args["sim_enabled"]:
            usrp = None
            raw_sim_values = args["sim_values"] if args["sim_values"] else [args["freq"]]
            offset_threshold = max(args["rate"], 5e6)
            for val in raw_sim_values:
                try:
                    fval = float(val)
                except (TypeError, ValueError):
                    continue
                if abs(fval) <= offset_threshold:
                    sim_offsets.append(fval)
                else:
                    sim_freqs.append(fval)
            self.log("Running in simulation mode - no USRP device will be used.")
            if sim_offsets:
                self.log("Simulation baseband offsets (Hz): " + ", ".join(f"{off:+.0f}" for off in sim_offsets))
            if sim_freqs:
                self.log("Simulation absolute tones (Hz): " + ", ".join(f"{freq:.3f}" for freq in sim_freqs))
        else:
            try:
                usrp = uhd.usrp.MultiUSRP(args["usrp_args"])
            except Exception as exc:
                self.log("No USRP found. Enable simulation or check device args.")
                raise exc

        if args["sim_enabled"]:
            rx_rate = args["rate"]
            rx_freq = args["freq"]
            current_gain = args["gain"]
            self.log("Simulation mode active: using dummy signal source.")
        else:
            usrp.set_rx_antenna(args["ant"], args["channel"])
            self.log(f"Available RX antennas: {usrp.get_rx_antennas(args['channel'])}")
            self.log(f"Using RX antenna: {usrp.get_rx_antenna(args['channel'])}")

            usrp.set_rx_rate(args["rate"], args["channel"])
            usrp.set_rx_freq(uhd.types.TuneRequest(args["freq"]), args["channel"])
            usrp.set_rx_gain(args["gain"], args["channel"])
            rx_rate = usrp.get_rx_rate()
            rx_freq = usrp.get_rx_freq(0)
            try:
                current_gain = float(usrp.get_rx_gain(args["channel"]))
            except Exception:
                current_gain = float(args["gain"])

        num_samps = max(args["nsamps"], 1024)
        samples = np.empty((1, num_samps), dtype=np.complex64)

        st_args = uhd.usrp.StreamArgs("fc32", "sc16")
        st_args.channels = [args["channel"]]

        if args["sim_enabled"]:
            streamer = None
            metadata = None
            buffer_samps = num_samps
            recv_buffer = None
            self.log("Simulation mode: skipping USRP streamer setup.")
        else:
            metadata = uhd.types.RXMetadata()
            streamer = usrp.get_rx_stream(st_args)
            buffer_samps = streamer.get_max_num_samps()
            self.log(f"Recv Buffer size set to: {buffer_samps} samples.")
            recv_buffer = np.zeros((1, buffer_samps), dtype=np.complex64)

            stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.num_done)
            stream_cmd.stream_now = True
            stream_cmd.num_samps = buffer_samps

        self.log("Beginning streaming...")

        def refresh_title():
            return (
                f"Channel:{args['channel']} operating at "
                f"{round(rx_rate / 1e6, 2)} MSps tuned to "
                f"{round(rx_freq / 1e9, 2)} GHz."
            )

        def set_freq(new_freq):
            nonlocal rx_freq
            rx_freq = new_freq
            if not args["sim_enabled"]:
                usrp.set_rx_freq(uhd.types.TuneRequest(rx_freq), args["channel"])
            self.rescale_pending = True

        current_range_idx = 0
        last_range_switch_time = time.time()
        range_switch_interval = args["scan_interval"]
        logging_timer = 0

        try:
            while not self.stop_event.is_set():
                sweep_plan = args["sweep_plan"]

                current_freq_idx = None
                for idx, freq in enumerate(sweep_plan):
                    if abs(rx_freq - freq) < 1e6:
                        current_freq_idx = idx
                        break

                if sweep_plan:
                    per_step_interval = range_switch_interval / max(1, len(sweep_plan))
                    if current_freq_idx is None:
                        current_range_idx = 0
                        freq = sweep_plan[current_range_idx]
                        set_freq(freq)
                        self.log(f"Switched to {freq / 1e9:.3f} GHz")
                        last_range_switch_time = time.time()
                    else:
                        current_range_idx = current_freq_idx
                        if len(sweep_plan) > 1:
                            current_time = time.time()
                            if current_time - last_range_switch_time >= per_step_interval:
                                current_range_idx = (current_range_idx + 1) % len(sweep_plan)
                                freq = sweep_plan[current_range_idx]
                                if current_range_idx != current_freq_idx:
                                    set_freq(freq)
                                    self.log(f"Switched to {freq / 1e9:.3f} GHz")
                                last_range_switch_time = current_time
                else:
                    last_range_switch_time = time.time()

                if args["sim_enabled"]:
                    t = np.arange(num_samps) / rx_rate
                    vec = np.zeros(num_samps, dtype=np.complex64)

                    for offset in set(sim_offsets):
                        if abs(offset) <= rx_rate / 2:
                            vec += 2.5 * np.exp(2j * np.pi * offset * t)

                    for f in sim_freqs:
                        df = f - rx_freq
                        if abs(df) <= rx_rate / 2:
                            vec += 2.0 * np.exp(2j * np.pi * df * t)
                    vec += 0.1 * (np.random.randn(num_samps) + 1j * np.random.randn(num_samps))
                    samples = np.expand_dims(vec, axis=0)
                else:
                    recv_samps = 0
                    while recv_samps < num_samps:
                        streamer.issue_stream_cmd(stream_cmd)
                        samps = streamer.recv(recv_buffer, metadata)
                        if metadata.error_code != uhd.types.RXMetadataErrorCode.none:
                            self.log(metadata.strerror())
                        if samps:
                            real_samps = min(num_samps - recv_samps, samps)
                            samples[:, recv_samps : recv_samps + real_samps] = recv_buffer[:, 0:real_samps]
                            recv_samps += real_samps

                len_samples = len(samples[args["channel"]])
                freqs, psd_welch = compute_welch_psd(
                    samples[args["channel"]],
                    sample_rate=rx_rate,
                    nfft=len_samples,
                    nperseg=WELCH_NPERSEG_DEFAULT,
                    noverlap=WELCH_NOVERLAP_DEFAULT,
                )

                if freqs.size == 0 or psd_welch.size == 0:
                    continue

                ydata = 10 * np.log10(psd_welch + 1e-20)
                xdata = freqs + rx_freq

                if self.noise_floor is None:
                    self.noise_floor = float(np.percentile(ydata, 30.0))

                y_range = None
                if self.rescale_pending:
                    y_max = float(np.max(ydata))
                    y_min = float(np.min(ydata))
                    if np.isfinite(y_max) and np.isfinite(y_min):
                        upper = y_max + 3.0
                        margin_db = 5.0
                        lower = self.noise_floor - margin_db
                        y_range = [lower, upper]
                    self.rescale_pending = False

                with self.data_lock:
                    self.latest["x"] = xdata
                    self.latest["y"] = ydata
                    if y_range is not None:
                        self.latest["y_range"] = y_range
                    self.latest["title"] = refresh_title()

                if time.time() - logging_timer > args["detect_interval"]:
                    self.noise_floor = calculate_noise_floor_welch(
                        samples[args["channel"]],
                        sample_rate=rx_rate,
                        nfft=len_samples,
                        nperseg=WELCH_NPERSEG_DEFAULT,
                        noverlap=WELCH_NOVERLAP_DEFAULT,
                    )
                    dynamic_threshold = self.noise_floor + args["thresh_offset"]

                    min_peak_height = dynamic_threshold
                    min_distance_samples = max(1, int(args["detect_bw"] / rx_rate * len_samples))

                    if HAS_SCIPY:
                        peak_indices, _ = find_peaks(ydata, height=min_peak_height, distance=min_distance_samples)
                    else:
                        peak_indices = find_peaks_simple(
                            ydata, min_height=min_peak_height, min_distance=min_distance_samples
                        )

                    detections_found = False
                    for peak_idx in peak_indices:
                        peak_freq_hz = float(xdata[peak_idx])

                        is_above, max_power_in_range, _ = check_frequency_range_above_threshold(
                            xdata, ydata, peak_freq_hz, args["detect_bw"], dynamic_threshold
                        )

                        if is_above:
                            detections_found = True
                            bw_6db = estimate_peak_bandwidth_hz(xdata, ydata, peak_idx, drop_db=6.0)
                            band = classify_band(peak_freq_hz, bw_hz=bw_6db)

                            self.log(
                                f"Detected {peak_freq_hz / 1e6:.3f} MHz "
                                f"({max_power_in_range:.1f} dB, noise floor: {self.noise_floor:.1f} dB) - {band}"
                            )

                            ts = datetime.now()
                            row = {
                                "timestamp": ts.isoformat(timespec="seconds"),
                                "peak_freq_hz": float(peak_freq_hz),
                                "peak_power_db": round(float(max_power_in_range), 2),
                                "detection": band,
                                "center_freq_hz": float(rx_freq),
                                "sample_rate_sps": float(rx_rate),
                                "rx_gain_db": float(current_gain),
                                "channel": int(args["channel"]),
                            }
                            self.dlog.add(
                                ts=ts,
                                peak_freq_hz=peak_freq_hz,
                                peak_power_db=max_power_in_range,
                                band=band,
                                center_freq_hz=rx_freq,
                                sample_rate_sps=rx_rate,
                                gain_db=current_gain,
                                channel=args["channel"],
                            )
                            self.log_detection(row)

                    if not detections_found:
                        self.log(
                            f"No detections (noise floor: {self.noise_floor:.1f} dB, "
                            f"threshold: {dynamic_threshold:.1f} dB)"
                        )

                    logging_timer = time.time()

                time.sleep(0.02)
        except Exception as exc:
            self.log(f"Worker error: {exc}")
        finally:
            try:
                self.dlog.save()
            except Exception as exc:
                self.log(f"Warning: could not save detections: {exc}")


app = dash.Dash(__name__)

log_buffer: deque[str] = deque(maxlen=500)
log_lock = threading.Lock()
detect_buffer: deque[dict] = deque(maxlen=500)
detect_lock = threading.Lock()
worker_lock = threading.Lock()
worker: Optional[SpectrumWorker] = None


def parse_float(value: str, name: str) -> float:
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"{name} must be a number.")


def parse_int(value: str, name: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{name} must be an integer.")


def parse_sim_values(text: str) -> list[float]:
    items = []
    for chunk in text.replace(",", " ").split():
        if chunk.strip():
            items.append(float(chunk))
    return items


app.layout = html.Div(
    [
        html.H2("RX Spectrum Web UI"),
        html.Div(
            [
                html.Div(
                    [
                        html.H4("Settings"),
                        html.Label("USRP args"),
                        dcc.Input(id="usrp-args", type="text", value="", style={"width": "100%"}),
                        html.Label("Antenna"),
                        dcc.Dropdown(id="ant", options=[{"label": "TX/RX", "value": "TX/RX"}, {"label": "RX2", "value": "RX2"}], value="TX/RX"),
                        html.Label("Center freq (Hz)"),
                        dcc.Input(id="freq", type="text", value="2.42e9", style={"width": "100%"}),
                        html.Label("Sample rate (S/s)"),
                        dcc.Input(id="rate", type="text", value="50e6", style={"width": "100%"}),
                        html.Label("Gain (dB)"),
                        dcc.Input(id="gain", type="text", value="10", style={"width": "100%"}),
                        html.Label("Channel"),
                        dcc.Input(id="channel", type="text", value="0", style={"width": "100%"}),
                        html.Label("Samples per update"),
                        dcc.Input(id="nsamps", type="text", value="100000", style={"width": "100%"}),
                        html.Hr(),
                        html.Label("Threshold offset (dB)"),
                        dcc.Input(id="thresh-offset", type="text", value="10.0", style={"width": "100%"}),
                        html.Label("Detect bandwidth (Hz)"),
                        dcc.Input(id="detect-bw", type="text", value="2000000", style={"width": "100%"}),
                        html.Label("Scan interval (s)"),
                        dcc.Input(id="scan-interval", type="text", value="3", style={"width": "100%"}),
                        html.Label("Detection interval (s)"),
                        dcc.Input(id="detect-interval", type="text", value="10", style={"width": "100%"}),
                        html.Hr(),
                        dcc.Checklist(
                            id="sim-enabled",
                            options=[{"label": "Enable simulation mode", "value": "sim"}],
                            value=[],
                        ),
                        html.Label("Sim tones (Hz, space or comma separated)"),
                        dcc.Textarea(id="sim-tones", value="", style={"width": "100%", "height": "80px"}),
                        html.Div(
                            [
                                html.Button("Start", id="start-btn", n_clicks=0),
                                html.Button("Stop", id="stop-btn", n_clicks=0, style={"marginLeft": "10px"}),
                            ],
                            style={"marginTop": "10px"},
                        ),
                        html.Div(id="status", style={"marginTop": "10px", "fontWeight": "bold"}),
                        html.Div(id="error", style={"color": "#a00", "marginTop": "6px"}),
                    ],
                    style={"width": "28%", "display": "inline-block", "verticalAlign": "top", "padding": "10px"},
                ),
                html.Div(
                    [
                        dcc.Graph(id="spectrum-graph"),
                        html.H4("Detections"),
                        dash_table.DataTable(
                            id="detections-table",
                            columns=[
                                {"name": "Timestamp", "id": "timestamp"},
                                {"name": "Peak freq (Hz)", "id": "peak_freq_hz"},
                                {"name": "Peak power (dB)", "id": "peak_power_db"},
                                {"name": "Detection", "id": "detection"},
                                {"name": "Center freq (Hz)", "id": "center_freq_hz"},
                                {"name": "Sample rate (S/s)", "id": "sample_rate_sps"},
                                {"name": "RX gain (dB)", "id": "rx_gain_db"},
                                {"name": "Channel", "id": "channel"},
                            ],
                            data=[],
                            page_size=10,
                            style_table={"height": "260px", "overflowY": "auto"},
                            style_cell={"fontFamily": "monospace", "fontSize": "12px", "padding": "6px"},
                            style_header={"fontWeight": "bold"},
                        ),
                    ],
                    style={"width": "70%", "display": "inline-block", "verticalAlign": "top"},
                ),
            ]
        ),
        dcc.Interval(id="update-interval", interval=250, n_intervals=0),
    ]
)


@app.callback(
    Output("status", "children"),
    Output("error", "children"),
    Input("start-btn", "n_clicks"),
    Input("stop-btn", "n_clicks"),
    State("usrp-args", "value"),
    State("ant", "value"),
    State("freq", "value"),
    State("rate", "value"),
    State("gain", "value"),
    State("channel", "value"),
    State("nsamps", "value"),
    State("thresh-offset", "value"),
    State("detect-bw", "value"),
    State("scan-interval", "value"),
    State("detect-interval", "value"),
    State("sim-enabled", "value"),
    State("sim-tones", "value"),
    prevent_initial_call=True,
)
def on_control(
    start_clicks,
    stop_clicks,
    usrp_args,
    ant,
    freq,
    rate,
    gain,
    channel,
    nsamps,
    thresh_offset,
    detect_bw,
    scan_interval,
    detect_interval,
    sim_enabled_values,
    sim_tones,
):
    global worker
    triggered = dash.callback_context.triggered
    if not triggered:
        return "Idle", ""

    trigger_id = triggered[0]["prop_id"].split(".")[0]

    if trigger_id == "stop-btn":
        with worker_lock:
            if worker is not None:
                worker.stop()
                worker = None
        return "Stopped", ""

    try:
        settings = {
            "usrp_args": usrp_args or "",
            "ant": ant or "TX/RX",
            "freq": parse_float(freq, "Center freq"),
            "rate": parse_float(rate, "Sample rate"),
            "gain": parse_int(gain, "Gain"),
            "channel": parse_int(channel, "Channel"),
            "nsamps": parse_int(nsamps, "Samples per update"),
            "thresh_offset": parse_float(thresh_offset, "Threshold offset"),
            "detect_bw": parse_float(detect_bw, "Detect bandwidth"),
            "scan_interval": parse_float(scan_interval, "Scan interval"),
            "detect_interval": parse_float(detect_interval, "Detection interval"),
            "sweep_plan": build_sweep_plan(),
            "sim_enabled": "sim" in (sim_enabled_values or []),
            "sim_values": parse_sim_values(sim_tones or ""),
        }
    except ValueError as exc:
        return "Idle", str(exc)

    with worker_lock:
        if worker is not None:
            worker.stop()
        with detect_lock:
            detect_buffer.clear()
        worker = SpectrumWorker(settings, log_buffer, log_lock, detect_buffer, detect_lock)
        worker.start()

    return "Running", ""


@app.callback(
    Output("spectrum-graph", "figure"),
    Output("detections-table", "data"),
    Input("update-interval", "n_intervals"),
)
def update_display(_tick):
    with worker_lock:
        active_worker = worker

    if active_worker is None:
        fig = {
            "data": [],
            "layout": {
                "title": "No data",
                "xaxis": {"title": "Frequency (Hz)"},
                "yaxis": {"title": "Power Spectral Density (dB)"},
            },
        }
    else:
        latest = active_worker.get_latest()
        fig = {
            "data": [
                {
                    "x": latest["x"],
                    "y": latest["y"],
                    "type": "scatter",
                    "mode": "lines",
                    "name": "Power Spectral Density",
                }
            ],
            "layout": {
                "title": latest["title"] or "Spectrum",
                "xaxis": {"title": "Frequency (Hz)"},
                "yaxis": {"title": "Power Spectral Density (dB)"},
                "margin": {"l": 60, "r": 10, "t": 40, "b": 40},
            },
        }
        if latest["y_range"] is not None:
            fig["layout"]["yaxis"]["range"] = latest["y_range"]

    with detect_lock:
        table_rows = list(detect_buffer)

    return fig, table_rows


def main():
    parser = argparse.ArgumentParser(description="RX Spectrum Dash Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind [default: 127.0.0.1]")
    parser.add_argument("--port", type=int, default=8050, help="Port to bind [default: 8050]")
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
