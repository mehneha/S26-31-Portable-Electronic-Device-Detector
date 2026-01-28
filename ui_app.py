#!/usr/bin/env python3
#
# Copyright 2025 Ettus Research, a National Instruments Brand
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Dash web UI for spectrum display and detection (split modules).

Run:
  python ui_app.py
"""

import argparse
import csv
import io
import threading
from collections import deque
from datetime import datetime

try:
    import dash
    from dash import dcc, html, dash_table
    from dash.dependencies import Input, Output, State
except Exception:
    print("Error: dash is required. Install with: pip install dash plotly")
    raise SystemExit(1)

from sdr_worker import SpectrumWorker

try:
    import pandas as pd
except Exception:
    pd = None

# Fixed config.
SWEEP_START_HZ = 650e6
SWEEP_END_HZ = 5.5e9
SWEEP_STEP_HZ = 40e6
EXCLUDED_RANGES_HZ = [
    (1.0e9, 2.0e9),
    (3.0e9, 4.0e9),
    (4.0e9, 5.0e9),
]
DEFAULT_USRP_ARGS = ""
DEFAULT_CENTER_FREQ = SWEEP_START_HZ
DEFAULT_SAMPLE_RATE = 40e6
DEFAULT_CHANNEL = 0
DEFAULT_NSAMPS = 100000
DEFAULT_DETECT_BW = 2e6
PLOT_MAX_POINTS = 3000


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


app = dash.Dash(__name__)

detect_buffer: deque[dict] = deque(maxlen=500)
detect_lock = threading.Lock()
worker_lock = threading.Lock()
worker: SpectrumWorker | None = None

app.layout = html.Div(
    [
        html.H2("RX Spectrum Web UI"),
        html.Div(
            [
                html.Div(
                    [
                        html.H4("Settings"),
                        html.Label("Antenna"),
                        dcc.Dropdown(id="ant", options=[{"label": "TX/RX", "value": "TX/RX"}, {"label": "RX2", "value": "RX2"}], value="TX/RX"),
                        html.Label("Gain (dB)"),
                        dcc.Input(id="gain", type="text", value="10", style={"width": "100%"}),
                        html.Hr(),
                        html.Label("Threshold offset (dB)"),
                        dcc.Input(id="thresh-offset", type="text", value="10.0", style={"width": "100%"}),
                        html.Label("Scan interval (s)"),
                        dcc.Input(id="scan-interval", type="text", value="20", style={"width": "100%"}),
                        html.Label("Detection interval (s)"),
                        dcc.Input(id="detect-interval", type="text", value="0.2", style={"width": "100%"}),
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
                        html.Div(
                            [
                                html.Button("Export CSV", id="export-csv-btn", n_clicks=0),
                                html.Button("Export Excel", id="export-xlsx-btn", n_clicks=0, style={"marginLeft": "10px"}),
                            ],
                            style={"marginTop": "10px"},
                        ),
                        html.Div(id="status", style={"marginTop": "10px", "fontWeight": "bold"}),
                        html.Div(id="error", style={"color": "#a00", "marginTop": "6px"}),
                        html.Hr(),
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
                                {"name": "Peak freq (GHz)", "id": "peak_freq_ghz"},
                                {"name": "Peak power (dB)", "id": "peak_power_db"},
                                {"name": "Detection", "id": "detection"},
                                {"name": "Center freq (GHz)", "id": "center_freq_ghz"},
                                {"name": "RX gain (dB)", "id": "rx_gain_db"},
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
        dcc.Download(id="download-data"),
    ]
)


@app.callback(
    Output("status", "children"),
    Output("error", "children"),
    Input("start-btn", "n_clicks"),
    Input("stop-btn", "n_clicks"),
    State("ant", "value"),
    State("gain", "value"),
    State("thresh-offset", "value"),
    State("scan-interval", "value"),
    State("detect-interval", "value"),
    State("sim-enabled", "value"),
    State("sim-tones", "value"),
    prevent_initial_call=True,
)
def on_control(
    start_clicks,
    stop_clicks,
    ant,
    gain,
    thresh_offset,
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
            "usrp_args": DEFAULT_USRP_ARGS,
            "ant": ant or "TX/RX",
            "freq": DEFAULT_CENTER_FREQ,
            "rate": DEFAULT_SAMPLE_RATE,
            "gain": parse_int(gain, "Gain"),
            "channel": DEFAULT_CHANNEL,
            "nsamps": DEFAULT_NSAMPS,
            "thresh_offset": parse_float(thresh_offset, "Threshold offset"),
            "detect_bw": DEFAULT_DETECT_BW,
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
        worker = SpectrumWorker(settings, detect_buffer, detect_lock)
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
        arduino_status = ""
        arduino_signal = ""
    else:
        latest = active_worker.get_latest()
        status = active_worker.get_status()
        x_plot = latest["x"]
        y_plot = latest["y"]
        if len(x_plot) > PLOT_MAX_POINTS:
            step = max(1, len(x_plot) // PLOT_MAX_POINTS)
            x_plot = x_plot[::step]
            y_plot = y_plot[::step]

        x_plot = x_plot / 1e9

        fig = {
            "data": [
                {
                    "x": x_plot,
                    "y": y_plot,
                    "type": "scatter",
                    "mode": "lines",
                    "name": "Power Spectral Density",
                }
            ],
            "layout": {
                "title": latest["title"] or "Spectrum",
                "xaxis": {
                    "title": {"text": "Frequency (GHz)", "standoff": 20},
                    "tickformat": ".3f",
                    "automargin": True,
                },
                "yaxis": {
                    "title": {"text": "Power Spectral Density (dB)", "standoff": 15},
                    "automargin": True,
                },
                "margin": {"l": 40, "r": 20, "t": 50, "b": 40},
            }
        }

        if latest["y_range"] is not None:
            fig["layout"]["yaxis"]["range"] = latest["y_range"]

        arduino_status = ""
        arduino_signal = ""

    with detect_lock:
        table_rows = list(detect_buffer)

    return fig, table_rows


@app.callback(
    Output("download-data", "data"),
    Input("export-csv-btn", "n_clicks"),
    Input("export-xlsx-btn", "n_clicks"),
    prevent_initial_call=True,
)
def export_table(_csv_clicks, _xlsx_clicks):
    triggered = dash.callback_context.triggered
    if not triggered:
        return None
    trigger_id = triggered[0]["prop_id"].split(".")[0]

    with detect_lock:
        rows = list(detect_buffer)

    if not rows:
        return None

    filename_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    if trigger_id == "export-xlsx-btn":
        if pd is None:
            # Fall back to CSV when pandas/openpyxl are not available.
            trigger_id = "export-csv-btn"
        else:
            output = io.BytesIO()
            df = pd.DataFrame(rows)
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name="detections")
            output.seek(0)
            return dcc.send_bytes(output.getvalue(), f"detections_{filename_ts}.xlsx")

    if trigger_id == "export-csv-btn":
        output = io.StringIO()
        fieldnames = list(rows[0].keys())
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        return dcc.send_string(output.getvalue(), f"detections_{filename_ts}.csv")

    return None


def main():
    parser = argparse.ArgumentParser(description="RX Spectrum Dash Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind [default: 127.0.0.1]")
    parser.add_argument("--port", type=int, default=8050, help="Port to bind [default: 8050]")
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
