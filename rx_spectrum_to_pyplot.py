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
from typing import Optional

try:
    import matplotlib.pyplot as plt
    import matplotlib.style as mplstyle
    from matplotlib.widgets import Button
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


class DetectionLogger:
    """Collects detections and writes them to Excel (or CSV fallback) on exit."""
    def __init__(self, out_path: str = "detected_devices.xlsx"):
        self.out_path = out_path
        self.rows = []  # list of dicts

    def add(self, *, ts_utc: datetime, peak_freq_hz: float, peak_power_db: float,
            band: str, center_freq_hz: float, sample_rate_sps: float, gain_db: float, channel: int):
        self.rows.append({
            "timestamp_utc": ts_utc.isoformat(timespec="seconds"),
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
                    "timestamp_utc", "peak_freq_hz", "peak_power_db", "detection",
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
    if args.sim:
        usrp = None
        sim_freqs = args.sim if len(args.sim) > 0 else [args.freq]
        print("Running in simulation mode — no USRP device will be used.")
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

    def set_freq(new_freq):
        nonlocal rx_freq
        rx_freq = new_freq
        if not args.sim:
            usrp.set_rx_freq(uhd.types.TuneRequest(rx_freq), args.channel)
        refresh_title()
        fig.canvas.draw_idle()

    # Buttons to hop bands (kept as-is)
    ax_cell = plt.axes([0.15, 0.9, 0.1, 0.05])
    ax_wifi = plt.axes([0.27, 0.9, 0.1, 0.05])
    ax_bt   = plt.axes([0.39, 0.9, 0.1, 0.05])
    btn_cell = Button(ax_cell, 'Cellular')
    btn_wifi = Button(ax_wifi, 'WiFi')
    btn_bt   = Button(ax_bt, 'Bluetooth')

    btn_cell.on_clicked(lambda event: set_freq(875e6))
    btn_wifi.on_clicked(lambda event: set_freq(2.42e9))
    btn_bt.on_clicked(lambda event: set_freq(2.4e9))

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
        threshold = -30  # keeps your original default threshold
        try:
            arduino = serial.Serial('COM5', 9600)
            time.sleep(2)
        except Exception:
            arduino = None
            print("No Arduino detected — running without serial output.")

        while True:
            if args.sim is not None:
                t = np.arange(num_samps) / args.rate
                vec = np.zeros(num_samps, dtype=np.complex64)
                for f in sim_freqs:
                    df = f - rx_freq
                    if abs(df) <= rx_rate / 2:
                        vec += np.exp(2j * np.pi * df * t)
                vec += 0.3 * (np.random.randn(num_samps) + 1j * np.random.randn(num_samps))
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
            logfft = psd(len_samples, samples[args.channel])

            # Create xdata and ydata for the plot
            xdata = (
                np.arange(-len_samples / 2, len_samples / 2, 1) / len_samples * rx_rate + rx_freq
            )
            ydata = logfft

            # Reset the data in the plot
            ln_signal_plot.set_xdata(xdata)
            ln_signal_plot.set_ydata(ydata)

            # Rescale axes.
            ax_signal_plot.set_xlim(xdata[0], xdata[-1])
            ax_signal_plot.relim()
            ax_signal_plot.autoscale_view(scalex=False, scaley=True)
            fig.canvas.draw_idle()

            # Update the window
            fig.canvas.flush_events()

            # check if plot window has been closed by user
            if not plt.fignum_exists(fig.number):
                break
               
            # --- existing periodic logger, now also logs to Excel buffer
            if time.time() - logging_timer > 10:
                peak_idx = int(np.argmax(ydata))
                peak_freq_hz = float(xdata[peak_idx])
                peak_pow_db = float(ydata[peak_idx])

                if peak_pow_db > threshold:
                    if arduino:
                        arduino.write(b"RED\n")

                    # estimate -6 dB occupied bandwidth for 2.4 GHz Wi-Fi vs Bluetooth split
                    bw_6db = estimate_peak_bandwidth_hz(xdata, ydata, peak_idx, drop_db=6.0)
                    band = classify_band(peak_freq_hz, bw_hz=bw_6db)

                    # print to console (kept original style)
                    print(
                        f"[{time.strftime('%H:%M:%S')}] Detected {peak_freq_hz/1e6:.3f} MHz "
                        f"({peak_pow_db:.1f} dB) — {band}"
                    )

                    # log to memory for Excel/CSV
                    ts = datetime.utcnow()
                    dlog.add(
                        ts_utc=ts,
                        peak_freq_hz=peak_freq_hz,
                        peak_power_db=peak_pow_db,
                        band=band,
                        center_freq_hz=rx_freq,
                        sample_rate_sps=rx_rate,
                        gain_db=current_gain,
                        channel=args.channel
                    )
                else:
                    if arduino:
                        arduino.write(b"GREEN\n")
                    print(f"[{time.strftime('%H:%M:%S')}]")
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
