# SDR worker and detection loop extracted from rx_spectrum_web.py
import os
import threading
import time
from collections import deque
from datetime import datetime

import numpy as np
import uhd

from helper_utils import (
    estimate_peak_bandwidth_hz,
    classify_band,
    check_frequency_range_above_threshold,
    find_peaks_simple,
    open_arduino,
    write_arduino,
    close_arduino,
)
from sim_utils import generate_sim_samples, split_sim_values
from welch_utils import (
    WELCH_NPERSEG_DEFAULT,
    WELCH_NOVERLAP_DEFAULT,
    calculate_noise_floor_welch,
    compute_welch_psd,
    HAS_SCIPY,
    find_peaks,
)

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except Exception:
    HAS_MATPLOTLIB = False


def _format_freq(freq_hz: float) -> str:
    if freq_hz >= 1e9:
        val = freq_hz / 1e9
        text = f"{val:.3f}".rstrip("0").rstrip(".")
        return f"{text} GHz"
    val = freq_hz / 1e6
    text = f"{val:.3f}".rstrip("0").rstrip(".")
    return f"{text} MHz"


def _apply_notch_band(
    xdata: np.ndarray,
    ydata: np.ndarray,
    center_hz: float,
    inner_hz: float,
    outer_hz: float,
) -> np.ndarray:
    center_mask = (xdata >= center_hz - inner_hz) & (xdata <= center_hz + inner_hz)
    ring_mask = (
        (xdata >= center_hz - outer_hz) & (xdata <= center_hz - inner_hz)
    ) | (
        (xdata >= center_hz + inner_hz) & (xdata <= center_hz + outer_hz)
    )
    if np.any(center_mask) and np.any(ring_mask):
        fill_value = float(np.mean(ydata[ring_mask]))
        ydata = ydata.copy()
        ydata[center_mask] = fill_value
    return ydata


def _save_detection_snapshot(
    xdata_hz: np.ndarray,
    ydata_db: np.ndarray,
    row: dict,
    threshold_db: float | None,
    y_range: list[float] | None,
    out_dir: str = "detection_images",
) -> None:
    if not HAS_MATPLOTLIB:
        return

    try:
        os.makedirs(out_dir, exist_ok=True)

        ts = str(row.get("timestamp", "")).replace(":", "-")
        peak_power_db = float(row.get("peak_power_db", 0.0))
        peak_freq_ghz = float(row.get("peak_freq_ghz", 0.0))
        filename = f"{ts}_{peak_power_db:.2f}dB_{peak_freq_ghz:.6f}GHz.png"
        path = os.path.join(out_dir, filename)

        x_plot = xdata_hz / 1e9
        fig, ax = plt.subplots(figsize=(10, 4), dpi=120)
        ax.plot(x_plot, ydata_db, linewidth=1.0, label="Power Spectral Density")
        if threshold_db is not None:
            ax.axhline(float(threshold_db), color="red", linestyle="--", linewidth=1.2, label="Threshold")
        if y_range is not None and len(y_range) == 2:
            ax.set_ylim(float(y_range[0]), float(y_range[1]))

        ax.set_xlabel("Frequency (GHz)")
        ax.set_ylabel("Power Spectral Density (dB)")
        ax.grid(True, alpha=0.35)
        ax.legend(loc="upper right")
        fig.tight_layout()
        fig.savefig(path)
        plt.close(fig)
    except Exception:
        return


class SpectrumWorker(threading.Thread):
    def __init__(
        self,
        settings: dict,
        detect_buffer: deque[dict],
        detect_lock: threading.Lock,
    ):
        super().__init__(daemon=True)
        self.settings = settings
        self.detect_buffer = detect_buffer
        self.detect_lock = detect_lock
        self.data_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.latest = {
            "x": np.array([]),
            "y": np.array([]),
            "y_range": None,
            "title": "",
            "noise_floor_db": None,
            "threshold_db": None,
            "thresh_offset_db": float(settings.get("thresh_offset", 0.0)),
        }
        self.noise_floor = None
        self.rescale_pending = True
        self.last_logged_step_token = None
        self.manual_y_enabled = bool(settings.get("manual_y_enabled", False))
        self.manual_y_min = settings.get("y_min")
        self.manual_y_max = settings.get("y_max")
        self.status = {
            "arduino": "Not initialized",
            "last_signal": "",
        }

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
                "noise_floor_db": self.latest["noise_floor_db"],
                "threshold_db": self.latest["threshold_db"],
                "thresh_offset_db": self.latest["thresh_offset_db"],
            }

    def get_status(self):
        with self.data_lock:
            return dict(self.status)

    def _compute_threshold_db(
        self,
        channel_samples: np.ndarray,
        sample_rate: float,
        nfft: int,
        thresh_offset_db: float,
    ) -> tuple[float, float]:
        noise_floor_db = calculate_noise_floor_welch(
            channel_samples,
            sample_rate=sample_rate,
            nfft=nfft,
            nperseg=WELCH_NPERSEG_DEFAULT,
            noverlap=WELCH_NOVERLAP_DEFAULT,
        )
        threshold_db = noise_floor_db + float(thresh_offset_db)
        return float(noise_floor_db), float(threshold_db)

    def run(self) -> None:
        args = self.settings
        sim_offsets: list[float] = []
        sim_freqs: list[float] = []

        if args["sim_enabled"]:
            sim_offsets, sim_freqs = split_sim_values(args["sim_values"], args["rate"])
            usrp = None
        else:
            try:
                usrp = uhd.usrp.MultiUSRP(args["usrp_args"])
            except Exception as exc:
                raise exc

        if args["sim_enabled"]:
            rx_rate = args["rate"]
            rx_freq = args["freq"]
            gain_factor = float(args["gain"])
            current_gain = gain_factor * (rx_freq / 1e9)
        else:
            usrp.set_rx_antenna(args["ant"], args["channel"])

            usrp.set_rx_rate(args["rate"], args["channel"])
            usrp.set_rx_freq(uhd.types.TuneRequest(args["freq"]), args["channel"])
            rx_freq = usrp.get_rx_freq(0)
            gain_factor = float(args["gain"])
            current_gain = gain_factor * (rx_freq / 1e9)
            usrp.set_rx_gain(current_gain, args["channel"])
            rx_rate = usrp.get_rx_rate()
            try:
                current_gain = float(usrp.get_rx_gain(args["channel"]))
            except Exception:
                current_gain = gain_factor * (rx_freq / 1e9)

        num_samps = max(args["nsamps"], 1024)
        samples = np.empty((1, num_samps), dtype=np.complex64)

        st_args = uhd.usrp.StreamArgs("fc32", "sc16")
        st_args.channels = [args["channel"]]

        if args["sim_enabled"]:
            streamer = None
            metadata = None
            buffer_samps = num_samps
            recv_buffer = None
        else:
            metadata = uhd.types.RXMetadata()
            streamer = usrp.get_rx_stream(st_args)
            buffer_samps = streamer.get_max_num_samps()
            recv_buffer = np.zeros((1, buffer_samps), dtype=np.complex64)

            stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.num_done)
            stream_cmd.stream_now = True
            stream_cmd.num_samps = buffer_samps

        if args.get("arduino_enabled", True):
            arduino, arduino_status = open_arduino()
        else:
            arduino, arduino_status = None, "Disabled"
        with self.data_lock:
            self.status["arduino"] = arduino_status

        def refresh_title():
            return (
                f"Channel:{args['channel']} operating at "
                f"{round(rx_rate / 1e6, 2)} MSps tuned to "
                f"{round(rx_freq / 1e9, 2)} GHz."
            )

        def set_freq(new_freq):
            nonlocal rx_freq, current_gain
            rx_freq = new_freq
            if not args["sim_enabled"]:
                current_gain = gain_factor * (rx_freq / 1e9)
                usrp.set_rx_freq(uhd.types.TuneRequest(rx_freq), args["channel"])
                usrp.set_rx_gain(current_gain, args["channel"])
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
                                last_range_switch_time = current_time
                else:
                    last_range_switch_time = time.time()

                if args["sim_enabled"]:
                    samples = generate_sim_samples(
                        rx_rate,
                        rx_freq,
                        num_samps,
                        sim_offsets,
                        sim_freqs,
                    )
                else:
                    recv_samps = 0
                    while recv_samps < num_samps:
                        streamer.issue_stream_cmd(stream_cmd)
                        samps = streamer.recv(recv_buffer, metadata)
                        if metadata.error_code != uhd.types.RXMetadataErrorCode.none:
                            pass
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
                center_hz = rx_freq
                inner_hz = 200e3
                outer_hz = 300e3
                ydata = _apply_notch_band(xdata, ydata, center_hz, inner_hz, outer_hz)

                spur_offset_hz = 10e6
                spur_inner_hz = 20e3
                spur_outer_hz = 30e3
                ydata = _apply_notch_band(
                    xdata, ydata, center_hz - spur_offset_hz, spur_inner_hz, spur_outer_hz
                )
                ydata = _apply_notch_band(
                    xdata, ydata, center_hz + spur_offset_hz, spur_inner_hz, spur_outer_hz
                )

                noise_floor_db, threshold_db = self._compute_threshold_db(
                    samples[args["channel"]],
                    sample_rate=rx_rate,
                    nfft=len_samples,
                    thresh_offset_db=args["thresh_offset"],
                )
                self.noise_floor = noise_floor_db

                y_range = None
                if self.manual_y_enabled and self.manual_y_min is not None and self.manual_y_max is not None:
                    y_range = [float(self.manual_y_min), float(self.manual_y_max)]
                    self.rescale_pending = False
                elif self.rescale_pending:
                    y_max = float(np.max(ydata))
                    y_min = float(np.min(ydata))
                    if np.isfinite(y_max) and np.isfinite(y_min):
                        upper = y_max + 3.0
                        margin_db = 5.0
                        lower = noise_floor_db - margin_db
                        y_range = [lower, upper]
                    self.rescale_pending = False

                with self.data_lock:
                    self.latest["x"] = xdata
                    self.latest["y"] = ydata
                    if y_range is not None:
                        self.latest["y_range"] = y_range
                    self.latest["title"] = refresh_title()
                    self.latest["noise_floor_db"] = noise_floor_db
                    self.latest["threshold_db"] = threshold_db
                    self.latest["thresh_offset_db"] = float(args["thresh_offset"])

                if time.time() - logging_timer > args["detect_interval"]:
                    step_token = (current_range_idx, last_range_switch_time)
                    if step_token == self.last_logged_step_token:
                        logging_timer = time.time()
                        continue
                    min_peak_height = threshold_db
                    # Temporarily disable minimum peak spacing so close peaks are not suppressed.
                    min_distance_samples = 1

                    if HAS_SCIPY:
                        peak_indices, _ = find_peaks(ydata, height=min_peak_height, distance=min_distance_samples)
                    else:
                        peak_indices = find_peaks_simple(
                            ydata, min_height=min_peak_height, min_distance=min_distance_samples
                        )

                    detections_found = False
                    detections = []
                    for peak_idx in peak_indices:
                        peak_freq_hz = float(xdata[peak_idx])

                        is_above, max_power_in_range, _ = check_frequency_range_above_threshold(
                            xdata, ydata, peak_freq_hz, args["detect_bw"], threshold_db
                        )

                        if is_above:
                            detections_found = True
                            bw_6db = estimate_peak_bandwidth_hz(xdata, ydata, peak_idx, drop_db=6.0)
                            band = classify_band(peak_freq_hz, bw_hz=bw_6db)

                            ts = datetime.now()
                            row = {
                                "id": f"{ts.isoformat(timespec='seconds')}_{int(round(peak_freq_hz))}",
                                "timestamp": ts.isoformat(timespec="seconds"),
                                "peak_freq_label": _format_freq(peak_freq_hz),
                                "peak_freq_ghz": round(float(peak_freq_hz) / 1e9, 5),
                                "peak_power_db": round(float(max_power_in_range), 2),
                                "detection": band,
                                "center_freq_label": _format_freq(rx_freq),
                                "center_freq_ghz": round(float(rx_freq) / 1e9, 5),
                                "rx_gain_db": float(current_gain),
                                "false_positive": "No",
                            }
                            detections.append((max_power_in_range, row))

                    if detections:
                        detections.sort(key=lambda item: item[1]["peak_freq_ghz"])
                        selected = [detections[0]]
                        if len(detections) > 1:
                            selected.append(detections[-1])

                        for power, row in selected:
                            self.log_detection(row)
                            _save_detection_snapshot(
                                xdata_hz=xdata,
                                ydata_db=ydata,
                                row=row,
                                threshold_db=threshold_db,
                                y_range=y_range if y_range is not None else self.latest.get("y_range"),
                            )

                    write_arduino(arduino, detections_found)
                    with self.data_lock:
                        self.status["last_signal"] = "RED" if detections_found else "GREEN"

                    self.last_logged_step_token = step_token
                    logging_timer = time.time()

                time.sleep(0.02)
        except Exception:
            pass
        finally:
            write_arduino(arduino, None)
            with self.data_lock:
                self.status["last_signal"] = "YELLOW"
            close_arduino(arduino)
