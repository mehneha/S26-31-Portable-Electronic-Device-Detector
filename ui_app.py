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
SWEEP_END_HZ = 6.0e9
SWEEP_STEP_HZ = 40e6
EXCLUDED_RANGES_HZ = [
    (1.0e9, 1.85e9),
    (3.0e9, 5.2e9),
    (5.8e9, 6.0e9),
]
DEFAULT_USRP_ARGS = ""
DEFAULT_CENTER_FREQ = SWEEP_START_HZ
DEFAULT_SAMPLE_RATE = 40e6
DEFAULT_CHANNEL = 0
DEFAULT_NSAMPS = 100000
DEFAULT_DETECT_BW = 2e6
PLOT_MAX_POINTS = 3000

CELLULAR_SWEEP_FREQS = [
    700e6,
    850e6,
    1900e6,
    2109e6,
    2600e6,
]

WIFI_SWEEP_FREQS = [
    5.730e9,
    5.740e9,
    5.750e9,
]

BLUETOOTH_SWEEP_FREQS = [2.4e9]


def build_sweep_plan(modes: list[str]) -> list[float]:
    if "all" in modes or not modes:
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

    freqs: list[float] = []
    if "cellular" in modes:
        freqs.extend(CELLULAR_SWEEP_FREQS)
    if "wifi" in modes:
        freqs.extend(WIFI_SWEEP_FREQS)
    if "bluetooth" in modes:
        freqs.extend(BLUETOOTH_SWEEP_FREQS)

    return freqs
    freqs = []
    f = SWEEP_START_HZ
    while f <= SWEEP_END_HZ + 1.0:
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
                        html.Label("Gain factor"),
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
                            id="manual-y-enabled",
                            options=[{"label": "Manual Y range", "value": "manual"}],
                            value=[],
                        ),
                        html.Div(
                            id="manual-y-inputs",
                            children=[
                                html.Label("Y min (dB)"),
                                dcc.Input(id="y-min", type="text", value="-90", style={"width": "100%"}),
                                html.Label("Y max (dB)"),
                                dcc.Input(id="y-max", type="text", value="0", style={"width": "100%"}),
                            ],
                            style={"display": "none", "marginTop": "6px"},
                        ),
                        dcc.Checklist(
                            id="sim-enabled",
                            options=[{"label": "Enable simulation mode", "value": "sim"}],
                            value=[],
                        ),
                        html.Label("Sim tones (Hz, space or comma separated)"),
                        dcc.Textarea(id="sim-tones", value="", style={"width": "100%", "height": "80px"}),
                        html.Div(
                            [
                                html.Button("Cellular", id="scan-cellular-btn", n_clicks=0),
                                html.Button("Bluetooth", id="scan-bt-btn", n_clicks=0, style={"marginLeft": "6px"}),
                                html.Button("WiFi", id="scan-wifi-btn", n_clicks=0, style={"marginLeft": "6px"}),
                                html.Button("All", id="scan-all-btn", n_clicks=0, style={"marginLeft": "6px"}),
                            ],
                            style={"marginTop": "10px"},
                        ),
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
                                html.Button("Clear Table", id="clear-table-btn", n_clicks=0, style={"marginLeft": "10px"}),
                            ],
                            style={"marginTop": "10px"},
                        ),
                        html.Div(
                            [
                                html.Button("Hide Graph", id="toggle-graph-btn", n_clicks=0),
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
                        html.Div(
                            dcc.Graph(id="spectrum-graph"),
                            id="graph-container",
                        ),
                        html.H4("Detections"),
                        dash_table.DataTable(
                            id="detections-table",
                            columns=[
                                {"name": "Timestamp", "id": "timestamp"},
                                {"name": "Peak freq", "id": "peak_freq_label"},
                                {"name": "Peak power (dB)", "id": "peak_power_db"},
                                {"name": "Detection", "id": "detection"},
                                {"name": "Center freq", "id": "center_freq_label"},
                                {"name": "RX gain (dB)", "id": "rx_gain_db"},
                                {"name": "Notes", "id": "notes", "editable": True},
                                {"name": "False Detection", "id": "false_positive", "presentation": "dropdown", "editable": True},
                            ],
                            dropdown={
                                "false_positive": {
                                    "options": [
                                        {"label": "False", "value": False},
                                        {"label": "True", "value": True},
                                    ],
                                    "clearable": False,
                                }
                            },
                            data=[],
                            page_size=10,
                            style_table={"height": "260px", "overflowY": "auto"},
                            style_cell={"fontFamily": "monospace", "fontSize": "12px", "padding": "6px"},
                            style_header={"fontWeight": "bold"},
                            editable=True,
                        ),
                    ],
                    style={"width": "70%", "display": "inline-block", "verticalAlign": "top"},
                ),
            ]
        ),
        dcc.Interval(id="update-interval", interval=250, n_intervals=0),
        dcc.Download(id="download-data"),
        dcc.Store(id="scan-modes", data=["all"]),
        dcc.Store(id="notes-store"),
        dcc.Store(id="table-count"),
        dcc.Store(id="run-state", data="stopped"),
        dcc.Store(id="graph-visible", data=True),
    ]
)


@app.callback(
    Output("scan-cellular-btn", "style"),
    Output("scan-bt-btn", "style"),
    Output("scan-wifi-btn", "style"),
    Output("scan-all-btn", "style"),
    Input("scan-modes", "data"),
)
def update_scan_button_styles(scan_modes):
    modes = set(scan_modes or [])
    base = {"marginLeft": "6px"}
    selected = {"marginLeft": "6px", "backgroundColor": "#0b5", "color": "white"}

    def style_for(mode):
        return selected if mode in modes else base

    return (
        style_for("cellular"),
        style_for("bluetooth"),
        style_for("wifi"),
        style_for("all"),
    )


@app.callback(
    Output("manual-y-inputs", "style"),
    Input("manual-y-enabled", "value"),
)
def toggle_manual_y_inputs(enabled_values):
    if "manual" in (enabled_values or []):
        return {"display": "block", "marginTop": "6px"}
    return {"display": "none", "marginTop": "6px"}


@app.callback(
    Output("graph-visible", "data"),
    Output("toggle-graph-btn", "children"),
    Input("toggle-graph-btn", "n_clicks"),
    State("graph-visible", "data"),
    prevent_initial_call=True,
)
def toggle_graph_visibility(_clicks, visible):
    new_visible = not bool(visible)
    return new_visible, ("Hide Graph" if new_visible else "Show Graph")


@app.callback(
    Output("graph-container", "style"),
    Input("graph-visible", "data"),
)
def apply_graph_visibility(visible):
    if visible:
        return {"display": "block"}
    return {"display": "none"}


@app.callback(
    Output("status", "children"),
    Output("error", "children"),
    Output("run-state", "data"),
    Input("start-btn", "n_clicks"),
    Input("stop-btn", "n_clicks"),
    State("ant", "value"),
    State("gain", "value"),
    State("thresh-offset", "value"),
    State("scan-interval", "value"),
    State("detect-interval", "value"),
    State("manual-y-enabled", "value"),
    State("y-min", "value"),
    State("y-max", "value"),
    State("sim-enabled", "value"),
    State("sim-tones", "value"),
    State("scan-modes", "data"),
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
    manual_y_enabled,
    y_min,
    y_max,
    sim_enabled_values,
    sim_tones,
    scan_modes,
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
        return "Stopped", "", "stopped"

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
            "manual_y_enabled": "manual" in (manual_y_enabled or []),
            "y_min": parse_float(y_min, "Y min"),
            "y_max": parse_float(y_max, "Y max"),
            "sweep_plan": build_sweep_plan(scan_modes or ["all"]),
            "sim_enabled": "sim" in (sim_enabled_values or []),
            "sim_values": parse_sim_values(sim_tones or ""),
        }
    except ValueError as exc:
        return "Idle", str(exc)

    if settings["manual_y_enabled"] and settings["y_min"] >= settings["y_max"]:
        return "Idle", "Y min must be less than Y max."

    with worker_lock:
        if worker is not None:
            worker.stop()
        worker = SpectrumWorker(settings, detect_buffer, detect_lock)
        worker.start()

    return "Running", "", "running"


@app.callback(
    Output("scan-modes", "data"),
    Input("scan-cellular-btn", "n_clicks"),
    Input("scan-bt-btn", "n_clicks"),
    Input("scan-wifi-btn", "n_clicks"),
    Input("scan-all-btn", "n_clicks"),
    State("scan-modes", "data"),
    prevent_initial_call=True,
)
def set_scan_mode(cell_clicks, bt_clicks, wifi_clicks, all_clicks, scan_modes):
    triggered = dash.callback_context.triggered
    if not triggered:
        return dash.no_update
    trigger_id = triggered[0]["prop_id"].split(".")[0]
    modes = set(scan_modes or [])

    def toggle_mode(mode: str):
        if mode in modes:
            modes.remove(mode)
        else:
            modes.add(mode)

    if trigger_id == "scan-all-btn":
        modes = {"all"}
    else:
        modes.discard("all")
        if trigger_id == "scan-cellular-btn":
            toggle_mode("cellular")
        elif trigger_id == "scan-bt-btn":
            toggle_mode("bluetooth")
        elif trigger_id == "scan-wifi-btn":
            toggle_mode("wifi")

        if not modes:
            modes.add("all")

    return sorted(modes)


@app.callback(
    Output("update-interval", "disabled"),
    Output("detections-table", "editable"),
    Input("run-state", "data"),
)
def toggle_run_state(run_state):
    if run_state == "stopped":
        return True, True
    return False, False


@app.callback(
    Output("spectrum-graph", "figure"),
    Output("detections-table", "data"),
    Input("update-interval", "n_intervals"),
    Input("notes-store", "data"),
    Input("manual-y-enabled", "value"),
    Input("y-min", "value"),
    Input("y-max", "value"),
    State("run-state", "data"),
    State("table-count", "data"),
)
def update_display(_tick, notes_store, manual_y_enabled, y_min, y_max, run_state, table_count):
    with worker_lock:
        active_worker = worker

    manual_enabled = "manual" in (manual_y_enabled or [])
    manual_range = None
    if manual_enabled:
        try:
            manual_min = float(y_min)
            manual_max = float(y_max)
            if manual_min < manual_max:
                manual_range = [manual_min, manual_max]
        except (TypeError, ValueError):
            manual_range = None

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

        if manual_range is not None:
            fig["layout"]["yaxis"]["range"] = manual_range
        if latest["y_range"] is not None:
            fig["layout"]["yaxis"]["range"] = latest["y_range"]
        if manual_range is not None:
            fig["layout"]["yaxis"]["range"] = manual_range

    if run_state == "stopped":
        return fig, dash.no_update

    with detect_lock:
        table_rows = list(detect_buffer)

    notes_map = notes_store or {}
    for row in table_rows:
        key = row.get("id")
        if key in notes_map:
            row["notes"] = notes_map[key].get("notes", "")
            row["false_positive"] = notes_map[key].get("false_positive", False)

    current_count = len(table_rows)
    if table_count is not None and current_count == table_count:
        return fig, dash.no_update

    return fig, table_rows


@app.callback(
    Output("download-data", "data"),
    Input("export-csv-btn", "n_clicks"),
    Input("export-xlsx-btn", "n_clicks"),
    Input("clear-table-btn", "n_clicks"),
    State("notes-store", "data"),
    prevent_initial_call=True,
)
def export_table(_csv_clicks, _xlsx_clicks, clear_clicks, notes_store):
    triggered = dash.callback_context.triggered
    if not triggered:
        return None
    trigger_id = triggered[0]["prop_id"].split(".")[0]

    if trigger_id == "clear-table-btn":
        with detect_lock:
            detect_buffer.clear()
        return None

    with detect_lock:
        rows = list(detect_buffer)

    notes_map = notes_store or {}
    if notes_map:
        for row in rows:
            key = row.get("id")
            if key in notes_map:
                row["notes"] = notes_map[key].get("notes", "")
                row["false_positive"] = notes_map[key].get("false_positive", False)

    if not rows:
        return None

    filename_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    if trigger_id == "export-xlsx-btn":
        if pd is None:
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


@app.callback(
    Output("notes-store", "data"),
    Output("table-count", "data"),
    Input("detections-table", "data"),
    prevent_initial_call=True,
)
def save_notes(rows):
    if rows is None:
        return None, None
    notes_map = {}
    for row in rows:
        key = row.get("id")
        if key:
            notes_map[key] = {
                "notes": row.get("notes", ""),
                "false_positive": bool(row.get("false_positive", False)),
            }
    with detect_lock:
        for idx, row in enumerate(detect_buffer):
            key = row.get("id")
            if key in notes_map:
                row["notes"] = notes_map[key]["notes"]
                row["false_positive"] = notes_map[key]["false_positive"]
                detect_buffer[idx] = row
    return notes_map, len(rows)


def main():
    parser = argparse.ArgumentParser(description="RX Spectrum Dash Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind [default: 127.0.0.1]")
    parser.add_argument("--port", type=int, default=8050, help="Port to bind [default: 8050]")
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
