#!/usr/bin/env python3
#
# Copyright 2025 Ettus Research, a National Instruments Brand
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Spectrum display example using Python API and Matplotlib.

This example demonstrates how to use the UHD Python API to create a simple
FFT (Fast Fourier Transform) display using the matplotlib library. The script
configures the USRP device to receive number of samples configured by the user.
The example then computes the instantaneous estimate of the power spectral
density of the received signal and is displayed on the terminal window. The
display is updated once new set of samples are received. The user can
adjust parameters such as frequency, gain, and sample rate through command-line
arguments. This example is useful for visualizing the frequency spectrum of
signals received by the USRP device, allowing users to monitor and analyze the
signal characteristics in a user-friendly way.

Example Usage:
                                 
TESTING: python rx_spectrum_to_pyplot.py --args "type=b200" --freq 2.42e9 --rate 45e6 --gain 40 --ant TX/RX

SIMULATION MODE: python rx_spectrum_to_pyplot.py --sim 99e6 101e6 2.4201e9 --freq 100e6 --rate 12e6
"""

import argparse
import time
import serial
import csv
from datetime import datetime
from typing import Optional, Tuple

try:
    import matplotlib.pyplot as plt
    import matplotlib.style as mplstyle
    from matplotlib.widgets import CheckButtons, Button
    from matplotlib.ticker import EngFormatter
except ImportError as e:
    print("Error: matplotlib is required to run this example.")
    print("Install it with: pip install matplotlib")
    raise e

import numpy as np
import uhd

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


def parse_args():
    """Parse the command line arguments."""
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
    )
    parser.add_argument(
        "-a",
        "--args",
        default="",
        type=str,
        help="""specifies the USRP device arguments, which holds
        multiple key-value pairs separated by commas
        (e.g., addr=192.168.40.2,type=x300) [default = ""].""",
    )
    parser.add_argument(
        "-f",
        "--freq",
        type=float,
        required=True,
        help="specifies the center frequency in Hz [input is required].",
    )
    parser.add_argument(
        "-r",
        "--rate",
        default=1e6,
        type=float,
        help="specifies the sample rate in samples/sec [default = 1e6].",
    )
    parser.add_argument(
        "-g", "--gain", type=int, default=10, help="specifies the gain in dB [default = 10]."
    )
    parser.add_argument(
        "-c",
        "--channel",
        type=int,
        default=0,
        help='specifies the channel to use (e.g., "0", "1", etc) [Default = 0].',
    )
    parser.add_argument(
        "-n",
        "--nsamps",
        type=int,
        default=100000,
        help="specifies the total number of samples to be received for FFT calculation "
        "and display on the screen before refreshing [default = 100000].",
    )
    parser.add_argument(
        "--dyn",
        type=int,
        default=60,
        help="specifies the dynamic range in dB. This defines the range of power levels "
        "relative to the reference level argument (--ref). Adjusting dynamic range "
        "influences the resolution displayed on y-axis of the plot [default = 60].",
    )
    parser.add_argument(
        "--ref",
        type=int,
        default=0,
        help="specifies the reference level in dB. This defines the maximum power "
        "level displayed on the plot. All power levels are relative to reference level. "
        "Adjusting the reference level allows to focus on specific power ranges in the "
        "signal [default = 0].",
    )
    parser.add_argument(
        "--ant",
        "--antenna",
        type=str,
        default="TX/RX",
        help="Select RX antenna (TX/RX or RX2) [default = TX/RX].",
    )
    parser.add_argument(
        "--sim",
        nargs="*",
        type=float,
        help="Run without USRP, optionally specify signal frequencies in Hz."
    )
    parser.add_argument(
        "--thresh-offset",
        type=float,
        default=10.0,
        help="Threshold offset above noise floor in dB [default = 10.0]."
    )
    parser.add_argument(
        "--detect-bw",
        type=float,
        default=2e6,
        help="Detection bandwidth (diameter) in Hz to check around peaks [default = 2e6 (2 MHz)]."
    )
    parser.add_argument(
        "--welch-nperseg",
        type=int,
        default=None,
        help="Number of samples per segment for Welch's method. If None, uses nfft/4 [default = None]."
    )
    parser.add_argument(
        "--welch-noverlap",
        type=int,
        default=None,
        help="Number of overlapping samples for Welch's method. If None, uses 50%% overlap [default = None]."
    )
    
    return parser.parse_args()


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


# ---------- Helpers for detection labeling & logging ----------

def estimate_peak_bandwidth_hz(
    xdata: np.ndarray, ydata: np.ndarray, peak_idx: int, drop_db: float = 6.0
) -> float:
    """Rough occupied bandwidth estimate using -drop_db points around the peak."""
    peak = float(ydata[peak_idx])
    floor = peak - float(drop_db)

    # scan left
    i = peak_idx
    while i > 0 and ydata[i] > floor:
        i -= 1
    f_left = float(xdata[max(i, 0)])

    # scan right
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
    Uses a simple width heuristic in 2.4 GHz to separate Wi-Fi vs Bluetooth.
    """
    f = float(freq_hz)

    # 2.4 GHz ISM: Wi-Fi/BLE share 2400–2483.5 MHz
    if 2.400e9 <= f <= 2.4835e9:
        # BLE ~1–2 MHz wide; Wi-Fi 20/40 MHz. Adjust as needed.
        if bw_hz is not None and bw_hz < 5e6:
            return "Bluetooth (2.4 GHz)"
        return "Wi-Fi 2.4 GHz"

    # 5 GHz Wi-Fi UNII bands (coarse)
    if 5.150e9 <= f <= 5.925e9:
        return "Wi-Fi 5 GHz"

    # Broad cellular buckets (coarse, covers many LTE/NR bands)
    if (700e6 <= f <= 960e6) or (1.710e9 <= f <= 2.170e9) or (2.300e9 <= f <= 2.700e9):
        return "Cellular"

    return "Other"


def calculate_noise_floor_welch(
    samples: np.ndarray,
    sample_rate: float,
    nfft: int,
    nperseg: Optional[int] = None,
    noverlap: Optional[int] = None
) -> float:
    """
    Calculate noise floor using Welch's method for better noise estimation.
    
    Welch's method averages periodograms from overlapping segments, providing
    a more robust noise floor estimate than single FFT methods.
    
    Args:
        samples: Time-domain signal samples (complex or real)
        sample_rate: Sample rate in Hz
        nfft: FFT size used for the main PSD computation
        nperseg: Number of samples per segment. If None, uses nfft/4
        noverlap: Number of overlapping samples. If None, uses 50% overlap
    
    Returns:
        Noise floor in dB
    """
    if len(samples) == 0:
        return -100.0  # Default low value if no data

    try:
        _, psd_linear = compute_welch_psd(
            samples,
            sample_rate=sample_rate,
            nfft=nfft,
            nperseg=nperseg,
            noverlap=noverlap,
        )
    except Exception as e:
        print(f"Warning: Welch's method failed ({e}), falling back to percentile method")
        logfft = psd(nfft, samples)
        return float(np.percentile(logfft, 50.0))

    psd_db = 10 * np.log10(psd_linear + 1e-20)
    return float(np.median(psd_db))


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

    # Manual Welch implementation (Hann window, density scaling)
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
        # Fallback to single FFT if not enough data for Welch segmentation
        fft_vals = np.fft.fft(samples[:nperseg] * window, n=nfft)
        segments = [np.abs(fft_vals) ** 2]

    psd_linear = np.mean(segments, axis=0) / (scale * sample_rate)
    freqs = np.fft.fftfreq(nfft, d=1.0 / sample_rate)

    # For real signals, mirror to one-sided spectrum aligning with scipy behavior
    if not is_complex:
        half_idx = nfft // 2 + 1
        psd_linear = psd_linear[:half_idx]
        freqs = freqs[:half_idx]

    order = np.argsort(freqs)
    return freqs[order], psd_linear[order]


def check_frequency_range_above_threshold(
    xdata: np.ndarray,
    ydata: np.ndarray,
    center_freq_hz: float,
    bandwidth_hz: float,
    threshold_db: float
) -> Tuple[bool, float, int]:
    """
    Check if a frequency range has power above threshold.
    
    Args:
        xdata: Frequency array in Hz
        ydata: Power spectral density in dB
        center_freq_hz: Center frequency of the range to check
        bandwidth_hz: Total bandwidth to check (diameter)
        threshold_db: Threshold in dB
    
    Returns:
        Tuple of (is_above_threshold, max_power_in_range_db, peak_index_in_range)
    """
    if len(xdata) == 0 or len(ydata) == 0:
        return False, -100.0, -1
    
    half_bw = bandwidth_hz / 2.0
    f_min = center_freq_hz - half_bw
    f_max = center_freq_hz + half_bw
    
    # Find indices within the frequency range
    mask = (xdata >= f_min) & (xdata <= f_max)
    if not np.any(mask):
        return False, -100.0, -1
    
    # Get power values in the range
    range_power = ydata[mask]
    max_power = float(np.max(range_power))
    
    # Find the index of the peak within the range
    range_indices = np.where(mask)[0]
    peak_idx_in_range = range_indices[np.argmax(range_power)]
    
    is_above = max_power > threshold_db
    return is_above, max_power, peak_idx_in_range


def find_peaks_simple(ydata: np.ndarray, min_height: float, min_distance: int = 10) -> np.ndarray:
    """
    Simple peak finding algorithm (fallback when scipy is not available).
    Finds local maxima that are above min_height and separated by at least min_distance.
    
    Args:
        ydata: Power spectral density in dB
        min_height: Minimum peak height in dB
        min_distance: Minimum distance between peaks in samples
    
    Returns:
        Array of peak indices
    """
    peaks = []
    n = len(ydata)
    
    for i in range(1, n - 1):
        # Check if it's a local maximum
        if ydata[i] > ydata[i-1] and ydata[i] > ydata[i+1] and ydata[i] > min_height:
            # Check distance from previous peak
            if not peaks or (i - peaks[-1]) >= min_distance:
                peaks.append(i)
    
    return np.array(peaks, dtype=int)


class DetectionLogger:
    """Collects detections and writes them to Excel (or CSV fallback) on exit."""
    def __init__(self, out_path: str = "detected_devices.xlsx"):
        self.out_path = out_path
        self.rows = []  # list of dicts

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
            except Exception as e:
                print(f"Excel save failed ({e}); falling back to CSV.")
        # CSV fallback
        csv_path = self.out_path.rsplit(".", 1)[0] + ".csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "timestamp", "peak_freq_hz", "peak_power_db", "detection",
                    "center_freq_hz", "sample_rate_sps", "rx_gain_db", "channel"
                ],
            )
            writer.writeheader()
            writer.writerows(self.rows)
        print(f"Saved detections to {csv_path}")


def main():
    """Create matplotlib display of power spectral density.

    ######## Receive Data and Display ########

    # 1. Setup USRP rate, frequency, and gain.
    # 2. Create a matplotlib figure and axis for plotting.
    # 2. Configure radio to stream fixed number of samples.
    # 3. Receive user configured samples from the USRP device.
    # 4. Plot Spectrum using FFT
    # 5. Repeat Steps 3-4 until user interrupts the program.
    """
    args = parse_args()
    sim_offsets: list[float] = []
    sim_freqs: list[float] = []

    if args.sim is not None:
        usrp = None
        raw_sim_values = args.sim if len(args.sim) > 0 else [args.freq]
        offset_threshold = max(args.rate, 5e6)
        for val in raw_sim_values:
            try:
                fval = float(val)
            except (TypeError, ValueError):
                continue
            if abs(fval) <= offset_threshold:
                sim_offsets.append(fval)
            else:
                sim_freqs.append(fval)
        print("Running in simulation mode — no USRP device will be used.")
        if sim_offsets:
            print(
                "Simulation baseband offsets (Hz): "
                + ", ".join(f"{off:+.0f}" for off in sim_offsets)
            )
        if sim_freqs:
            print(
                "Simulation absolute tones (Hz): "
                + ", ".join(f"{freq:.3f}" for freq in sim_freqs)
            )
    else:
        try:
            usrp = uhd.usrp.MultiUSRP(args.args)
        except Exception as e:
            print("No USRP found. Use --sim to run without hardware.")
            raise e

    # Set antenna
    if args.sim:
        rx_rate = args.rate
        rx_freq = args.freq
        current_gain = args.gain
        print("Simulation mode active: using dummy signal source.")
    else:
        # Set antenna
        usrp.set_rx_antenna(args.ant, args.channel)
        print("Available RX antennas:", usrp.get_rx_antennas(args.channel))
        print("Using RX antenna:", usrp.get_rx_antenna(args.channel))

        # Set the USRP rate, freq, and gain
        usrp.set_rx_rate(args.rate, args.channel)
        usrp.set_rx_freq(uhd.types.TuneRequest(args.freq), args.channel)
        usrp.set_rx_gain(args.gain, args.channel)
        rx_rate = usrp.get_rx_rate()
        rx_freq = usrp.get_rx_freq(0)
        try:
            current_gain = float(usrp.get_rx_gain(args.channel))
        except Exception:
            current_gain = float(args.gain)

    # Initialize logger
    dlog = DetectionLogger(out_path="detected_devices.xlsx")

    # Plotting initialization.
    mplstyle.use("fast")  # Use a fast style for matplotlib
    plt.ion()  # Stop matplotlib windows from blocking execution

    fig, axes = plt.subplots(
        nrows=1, ncols=1, figsize=(8, 4), gridspec_kw={"wspace": 0, "hspace": 0}
    )
    ax_signal_plot = axes
    formatter = EngFormatter()
    # Setup figure, axis and initiate plot
    (ln_signal_plot,) = ax_signal_plot.plot(
        [],
        [],
        label="Power Spectral Density",
    )
    ax_signal_plot.title.set_text(
        f"Channel:{args.channel} operating at "
        f"{round(rx_rate/1e6, 2)} MSps tuned to "
        f"{round(rx_freq/1e9, 2)} GHz."
    )
    ax_signal_plot.set_ylim(args.ref - args.dyn, args.ref)
    ax_signal_plot.grid()
    ax_signal_plot.margins(x=0)
    ax_signal_plot.legend()
    ax_signal_plot.set_xlabel("Frequency (Hz)")
    ax_signal_plot.set_ylabel("Power Spectral Density (dB)")
    ax_signal_plot.xaxis.set_major_formatter(formatter)

    def refresh_title():
        ax_signal_plot.title.set_text(
            f"Channel:{args.channel} operating at "
            f"{round(rx_rate/1e6, 2)} MSps tuned to "
            f"{round(rx_freq/1e9, 2)} GHz."
        )

    def set_freq(new_freq, band_label=None):
        nonlocal rx_freq, current_band_label, rescale_pending
        rx_freq = new_freq
        if band_label is not None:
            current_band_label = band_label
        if not args.sim:
            usrp.set_rx_freq(uhd.types.TuneRequest(rx_freq), args.channel)
        # Trigger a one-time y-axis autoscale for the new band.
        rescale_pending = True
        refresh_title()
        fig.canvas.draw_idle()

    # Define frequency ranges for monitoring
    frequency_ranges = [
        ("Cellular", 875e6),
        ("WiFi", 2.42e9),
        ("Bluetooth", 2.4e9),
    ]

    # default_band_offsets = {
    #     "Cellular": [0.0, 150e3, -320e3],
    #     "WiFi": [0.0, 1.5e6, -3.2e6],
    #     "Bluetooth": [0.0, 300e3, -600e3],
    # }
    
    # Create checkboxes for frequency range selection
    ax_checkboxes = plt.axes([0.15, 0.90, 0.25, 0.08])
    range_labels = [label for label, _ in frequency_ranges]
    # All ranges checked by default
    range_states = [True] * len(frequency_ranges)
    checkboxes = CheckButtons(ax_checkboxes, range_labels, range_states)
    
    # Create Start/Stop button for scanning
    ax_start_stop = plt.axes([0.42, 0.90, 0.12, 0.05])
    scanning_active = True  # Start with scanning active
    btn_start_stop = Button(ax_start_stop, 'Stop Scan')
    
    def toggle_scanning(event):
        nonlocal scanning_active
        scanning_active = not scanning_active
        if scanning_active:
            btn_start_stop.label.set_text('Stop Scan')
            print("Scanning resumed")
        else:
            btn_start_stop.label.set_text('Start Scan')
            print("Scanning paused")
        fig.canvas.draw_idle()
    
    btn_start_stop.on_clicked(toggle_scanning)
    
    # Track current range index for cycling
    current_range_idx = 0
    current_band_label = None
    last_range_switch_time = time.time()
    range_switch_interval = 3.0  # Switch every 3 seconds
    rescale_pending = True
    
    # Initialize to first checked range if available
    checked_indices = [i for i, checked in enumerate(checkboxes.get_status()) if checked]
    if checked_indices:
        initial_idx = checked_indices[0]
        label, freq = frequency_ranges[initial_idx]
        set_freq(freq, label)
        print(f"Initialized to {label} band: {freq/1e9:.3f} GHz")

    fig.tight_layout()
    height, width = plt.get_current_fig_manager().canvas.get_width_height()

    # Update every update_interval seconds.
    update_interval = 0.02

    # Create the buffer to receive samples
    num_samps = max(args.nsamps, width)
    print(f"Receiving {num_samps} samples per channel.")
    samples = np.empty((1, num_samps), dtype=np.complex64)

    st_args = uhd.usrp.StreamArgs("fc32", "sc16")
    st_args.channels = [args.channel]

    # Create Rx streamer to receive samples
    if args.sim:
        streamer = None
        metadata = None
        buffer_samps = num_samps
        recv_buffer = None
        print("Simulation mode: skipping USRP streamer setup.")
    else:
        metadata = uhd.types.RXMetadata()
        streamer = usrp.get_rx_stream(st_args)
        buffer_samps = streamer.get_max_num_samps()
        print(f"Recv Buffer size set to: {buffer_samps} samples.")
        recv_buffer = np.zeros((1, buffer_samps), dtype=np.complex64)

        stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.num_done)
        stream_cmd.stream_now = True
        stream_cmd.num_samps = buffer_samps

    print(
        "Beginning Streaming...\n"
        "Using matplotlib for display. Press Ctrl+C or close the plot to exit."
    )

    try:
        logging_timer = 0
        try:
            arduino = serial.Serial('COM5', 9600)
            time.sleep(2)
        except Exception:
            arduino = None
            print("No Arduino detected — running without serial output.")

        while True:
            # Only perform frequency cycling if scanning is active
            if scanning_active:
                # Get currently checked ranges
                checked_indices = [i for i, checked in enumerate(checkboxes.get_status()) if checked]
                
                # Check if current frequency is still in checked ranges
                current_freq_idx = None
                for idx, (_, freq) in enumerate(frequency_ranges):
                    if abs(rx_freq - freq) < 1e6:  # Within 1 MHz tolerance
                        current_freq_idx = idx
                        break
                
                # If current frequency is not checked, switch immediately
                if checked_indices and (current_freq_idx is None or current_freq_idx not in checked_indices):
                    current_range_idx = 0
                    selected_idx = checked_indices[0]
                    label, freq = frequency_ranges[selected_idx]
                    set_freq(freq, label)
                    print(f"Switched to {label} band: {freq/1e9:.3f} GHz (current range unchecked)")
                    last_range_switch_time = time.time()
                
                # Cycle through checked frequency ranges every 3 seconds
                current_time = time.time()
                if current_time - last_range_switch_time >= range_switch_interval:
                    if checked_indices:
                        # Ensure current_range_idx is valid for the current checked ranges
                        if current_range_idx >= len(checked_indices):
                            current_range_idx = 0
                        
                        # Cycle to next checked range
                        current_range_idx = (current_range_idx + 1) % len(checked_indices)
                        selected_idx = checked_indices[current_range_idx]
                        label, freq = frequency_ranges[selected_idx]
                        set_freq(freq, label)
                        print(f"Switched to {label} band: {freq/1e9:.3f} GHz")
                        last_range_switch_time = current_time
                    else:
                        # No ranges checked, keep current frequency
                        current_range_idx = 0  # Reset for when ranges are checked again
                        last_range_switch_time = current_time
            
            if args.sim is not None:
                t = np.arange(num_samps) / args.rate
                vec = np.zeros(num_samps, dtype=np.complex64)
                baseband_offsets = sim_offsets[:]


                for offset in set(baseband_offsets):
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
                        print(metadata.strerror())
                    if samps:
                        real_samps = min(num_samps - recv_samps, samps)
                        samples[:, recv_samps : recv_samps + real_samps] = recv_buffer[:, 0:real_samps]
                        recv_samps += real_samps

            # Get power spectral density
            len_samples = len(samples[args.channel])

            freqs, psd_welch = compute_welch_psd(
                samples[args.channel],
                sample_rate=rx_rate,
                nfft=len_samples,
                nperseg=args.welch_nperseg,
                noverlap=args.welch_noverlap,
            )

            if freqs.size == 0 or psd_welch.size == 0:
                continue

            ydata = 10*np.log10(psd_welch + 1e-20)
            xdata = freqs + rx_freq
            # Initial/fallback noise floor estimate
            try:
                noise_floor
            except NameError:
                noise_floor = float(np.percentile(ydata, 30.0))


            # Reset the data in the plot
            ln_signal_plot.set_xdata(xdata)
            ln_signal_plot.set_ydata(ydata)

            # Update y-axis limits only when switching to a new band.
            if rescale_pending:
                y_max = float(np.max(ydata))
                y_min = float(np.min(ydata))
                if np.isfinite(y_max) and np.isfinite(y_min):
                    upper = y_max + 3.0
                    margin_db = 5.0
                    try:
                        lower = noise_floor - margin_db
                    except NameError:
                        lower = y_min - 3.0
                    ax_signal_plot.set_ylim(lower, upper)
                rescale_pending = False

            # Keep x-axis aligned with the current center frequency.
            ax_signal_plot.set_xlim(xdata[0], xdata[-1])
            fig.canvas.draw_idle()

            # Update the window
            fig.canvas.flush_events()

            # check if plot window has been closed by user
            if not plt.fignum_exists(fig.number):
                break
               
            # --- Dynamic threshold detection with frequency range checking
            if time.time() - logging_timer > 10:
                # Calculate noise floor using Welch's method
                noise_floor = calculate_noise_floor_welch(
                    samples[args.channel],
                    sample_rate=rx_rate,
                    nfft=len_samples,
                    nperseg=args.welch_nperseg,
                    noverlap=args.welch_noverlap
                )
                
                # Calculate dynamic threshold (noise floor + offset)
                dynamic_threshold = noise_floor + args.thresh_offset
                
                # Find peaks in the spectrum
                # Use scipy if available, otherwise use simple method
                min_peak_height = dynamic_threshold
                min_distance_samples = max(1, int(args.detect_bw / rx_rate * len_samples))
                
                if HAS_SCIPY:
                    peak_indices, _ = find_peaks(ydata, height=min_peak_height, distance=min_distance_samples)
                else:
                    peak_indices = find_peaks_simple(ydata, min_height=min_peak_height, min_distance=min_distance_samples)
                
                # Check each peak to see if its frequency range exceeds threshold
                detections_found = False
                for peak_idx in peak_indices:
                    peak_freq_hz = float(xdata[peak_idx])
                    
                    # Check if frequency range around peak exceeds threshold
                    is_above, max_power_in_range, _ = check_frequency_range_above_threshold(
                        xdata, ydata, peak_freq_hz, args.detect_bw, dynamic_threshold
                    )
                    
                    if is_above:
                        detections_found = True
                        
                        # estimate -6 dB occupied bandwidth for 2.4 GHz Wi-Fi vs Bluetooth split
                        bw_6db = estimate_peak_bandwidth_hz(xdata, ydata, peak_idx, drop_db=6.0)
                        band = classify_band(peak_freq_hz, bw_hz=bw_6db)

                        # print to console
                        print(
                            f"[{time.strftime('%H:%M:%S')}] Detected {peak_freq_hz/1e6:.3f} MHz "
                            f"({max_power_in_range:.1f} dB, noise floor: {noise_floor:.1f} dB) — {band}"
                        )

                        # log to memory for Excel/CSV
                        ts = datetime.now()
                        dlog.add(
                            ts=ts,
                            peak_freq_hz=peak_freq_hz,
                            peak_power_db=max_power_in_range,
                            band=band,
                            center_freq_hz=rx_freq,
                            sample_rate_sps=rx_rate,
                            gain_db=current_gain,
                            channel=args.channel
                        )
                
                # Update Arduino status
                if arduino:
                    if detections_found:
                        arduino.write(b"RED\n")
                    else:
                        arduino.write(b"GREEN\n")
                
                if not detections_found:
                    print(f"[{time.strftime('%H:%M:%S')}] No detections (noise floor: {noise_floor:.1f} dB, threshold: {dynamic_threshold:.1f} dB)")
                
                logging_timer = time.time()

            time.sleep(update_interval)
    except KeyboardInterrupt:
        pass

    if arduino:
        arduino.close()
    plt.close(fig)

    # Save detections to Excel/CSV on exit
    try:
        dlog.save()
    except Exception as e:
        print(f"Warning: could not save detections: {e}")


if __name__ == "__main__":
    main()
