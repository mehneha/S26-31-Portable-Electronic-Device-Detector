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
import base64
import csv
import io
import os
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
    (2.0e9, 3.0e9),
    (3.0e9, 4.8e9),
    #(5.8e9, 6.0e9),
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
    740e6,
    780e6,
    820e6,
    860e6,
    900e6,
    940e6,
    980e6,
]

WIFI_SWEEP_FREQS = [
    5.70e9,
    5.740e9,
    5.780e9,
    5.820e9,
    5.860e9,
]

BLUETOOTH_SWEEP_FREQS = [
    2.4e9,
    2.44e9,
    2.48e9,
    2.52e9,
    2.56e9,
]


def build_sweep_plan(
    modes: list[str],
    select_mode: str = "exact",
    selected_freq_ghz: float | list[float] | None = None,
    selected_min_ghz: float | list[float] | None = None,
    selected_max_ghz: float | list[float] | None = None,
) -> list[float]:
    if "select" in modes:
        freqs: list[float] = []
        if select_mode == "range" and selected_min_ghz is not None and selected_max_ghz is not None:
            min_values = selected_min_ghz if isinstance(selected_min_ghz, list) else [selected_min_ghz]
            max_values = selected_max_ghz if isinstance(selected_max_ghz, list) else [selected_max_ghz]
            for start_ghz, end_ghz in zip(min_values, max_values):
                start_hz = start_ghz * 1e9
                end_hz = end_ghz * 1e9
                f = start_hz
                while f <= end_hz + 1.0:
                    freqs.append(f)
                    f += SWEEP_STEP_HZ
            if freqs:
                return freqs
            if min_values:
                return [min_values[0] * 1e9]
        if isinstance(selected_freq_ghz, list) and selected_freq_ghz:
            return [freq * 1e9 for freq in selected_freq_ghz]
        if selected_freq_ghz is not None:
            return [selected_freq_ghz * 1e9]

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


def parse_freq_values_ghz(text: str, name: str) -> list[float]:
    values = []
    for chunk in str(text).replace(",", " ").split():
        if chunk.strip():
            try:
                values.append(float(chunk))
            except ValueError:
                raise ValueError(f"{name} must contain only numbers.")
    if not values:
        raise ValueError(f"{name} must contain at least one number.")
    return values


app = dash.Dash(__name__)

detect_buffer: deque[dict] = deque(maxlen=500)
detect_lock = threading.Lock()
worker_lock = threading.Lock()
worker: SpectrumWorker | None = None


def normalize_false_positive(value) -> str:
    if isinstance(value, str):
        return "Yes" if value.strip().lower() in {"yes", "true", "1"} else "No"
    return "Yes" if bool(value) else "No"


def load_logo_data_uri(image_path: str) -> str | None:
    if not os.path.exists(image_path):
        return None
    try:
        with open(image_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        ext = image_path.rsplit(".", 1)[-1].lower()
        mime = "image/jpeg" if ext in {"jpg", "jpeg"} else "image/png"
        return f"data:{mime};base64,{encoded}"
    except Exception:
        return None


LOGO_DATA_URI = load_logo_data_uri("ZetaLogo.jpeg")


def restart_worker(settings: dict | None) -> None:
    global worker
    if worker is not None:
        worker.stop()
        worker.join(timeout=1.0)
        worker = None
    if settings is not None:
        worker = SpectrumWorker(settings, detect_buffer, detect_lock)
        worker.start()


def build_worker_settings(
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
    arduino_enabled_values,
    arduino_red_values,
    arduino_green_values,
    arduino_yellow_values,
    arduino_speaker_values,
    arduino_red_duration_ms,
    arduino_green_duration_ms,
    arduino_speaker_duration_ms,
    scan_modes,
    select_range_mode,
    selected_freq_ghz,
    selected_min_ghz,
    selected_max_ghz,
):
    modes = scan_modes or ["all"]
    select_mode = "range" if "range" in (select_range_mode or []) else "exact"

    selected_freq = None
    selected_min = None
    selected_max = None
    if "select" in modes:
        if select_mode == "range":
            selected_min = parse_freq_values_ghz(selected_min_ghz, "Selected min freq (GHz)")
            selected_max = parse_freq_values_ghz(selected_max_ghz, "Selected max freq (GHz)")
        else:
            selected_freq = parse_freq_values_ghz(selected_freq_ghz, "Selected freq (GHz)")

    red_duration_ms = parse_int(arduino_red_duration_ms, "Red duration (ms)")
    green_duration_ms = parse_int(arduino_green_duration_ms, "Green duration (ms)")
    speaker_duration_ms = parse_int(arduino_speaker_duration_ms, "Speaker duration (ms)")
    if speaker_duration_ms > red_duration_ms:
        speaker_duration_ms = red_duration_ms

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
        "sweep_plan": build_sweep_plan(
            modes,
            select_mode=select_mode,
            selected_freq_ghz=selected_freq,
            selected_min_ghz=selected_min,
            selected_max_ghz=selected_max,
        ),
        "sim_enabled": "sim" in (sim_enabled_values or []),
        "sim_values": parse_sim_values(sim_tones or ""),
        "arduino_enabled": "ardu" in (arduino_enabled_values or []),
        "arduino_red_enabled": "red" in (arduino_red_values or []),
        "arduino_green_enabled": "green" in (arduino_green_values or []),
        "arduino_yellow_enabled": "yellow" in (arduino_yellow_values or []),
        "arduino_speaker_enabled": "speaker" in (arduino_speaker_values or []),
        "arduino_red_duration_ms": red_duration_ms,
        "arduino_green_duration_ms": green_duration_ms,
        "arduino_speaker_duration_ms": speaker_duration_ms,
    }

    if settings["manual_y_enabled"] and settings["y_min"] >= settings["y_max"]:
        raise ValueError("Y min must be less than Y max.")
    if "select" in modes and select_mode == "range" and selected_min is not None and selected_max is not None:
        paired_ranges = list(zip(selected_min, selected_max))
        if not paired_ranges:
            raise ValueError("Selected range must contain at least one min/max pair.")
        for min_freq, max_freq in paired_ranges:
            if min_freq >= max_freq:
                raise ValueError("Each selected min freq must be less than its corresponding max freq.")
    if (
        settings["arduino_red_duration_ms"] < 0
        or settings["arduino_green_duration_ms"] < 0
        or settings["arduino_speaker_duration_ms"] < 0
    ):
        raise ValueError("Arduino durations must be >= 0.")

    return settings


app.layout = html.Div(
    [
        html.Div(
            [
                html.Div(
                    [
                        html.Div(
                            [
                                html.Button("Cellular", id="scan-cellular-btn", n_clicks=0),
                                html.Button("Bluetooth", id="scan-bt-btn", n_clicks=0, style={"marginLeft": "6px"}),
                                html.Button("WiFi", id="scan-wifi-btn", n_clicks=0, style={"marginLeft": "6px"}),
                                html.Button("Select", id="scan-select-btn", n_clicks=0, style={"marginLeft": "6px"}),
                                html.Button("All", id="scan-all-btn", n_clicks=0, style={"marginLeft": "6px"}),
                            ],
                            style={"marginTop": "10px"},
                        ),
                        html.Div(
                            id="selected-freq-container",
                            children=[
                                html.Div(
                                    [
                                        dcc.Checklist(
                                            id="select-exact-mode",
                                            options=[{"label": "Exact", "value": "exact"}],
                                            value=["exact"],
                                        ),
                                        dcc.Checklist(
                                            id="select-range-mode",
                                            options=[{"label": "Range", "value": "range"}],
                                            value=[],
                                            style={"marginLeft": "16px"},
                                        ),
                                    ],
                                    style={"display": "flex", "alignItems": "center", "marginTop": "6px"},
                                ),
                                        html.Div(
                                            id="select-exact-input-container",
                                            children=[
                                                html.Label("Selected freq (GHz)"),
                                                dcc.Input(id="selected-freq-ghz", type="text", value="2.42", debounce=True, style={"width": "100%"}),
                                            ],
                                            style={"display": "block"},
                                        ),
                                        html.Div(
                                            id="select-range-input-container",
                                            children=[
                                                html.Label("Selected min freq (GHz)"),
                                                dcc.Input(id="selected-min-ghz", type="text", value="0.65", debounce=True, style={"width": "100%"}),
                                                html.Label("Selected max freq (GHz)"),
                                                dcc.Input(id="selected-max-ghz", type="text", value="6.0", debounce=True, style={"width": "100%"}),
                                            ],
                                            style={"display": "none"},
                                        ),
                            ],
                            style={"display": "none"},
                        ),
                        html.Div(
                            [
                                html.Button("Start", id="start-btn", n_clicks=0),
                                html.Button("Stop", id="stop-btn", n_clicks=0),
                            ],
                            style={"marginTop": "10px", "display": "flex", "gap": "10px", "width": "100%"},
                        ),
                        html.Details(
                            [
                                html.Summary("Settings"),
                                html.Div(
                                    [
                                        html.Label("Antenna"),
                                        dcc.Dropdown(
                                            id="ant",
                                            options=[
                                                {"label": "TX/RX", "value": "TX/RX"},
                                                {"label": "RX2", "value": "RX2"},
                                            ],
                                            value="TX/RX",
                                        ),
                                        html.Label("Gain factor"),
                                        dcc.Input(id="gain", type="text", value="10", debounce=True, style={"width": "100%"}),
                                        html.Label("Threshold offset (dB)"),
                                        dcc.Input(id="thresh-offset", type="text", value="11.0", debounce=True, style={"width": "100%"}),
                                        html.Label("Scan interval (s)"),
                                        dcc.Input(id="scan-interval", type="text", value="80", debounce=True, style={"width": "100%"}),
                                        html.Label("Detection interval (s)"),
                                        dcc.Input(id="detect-interval", type="text", value="0.2", debounce=True, style={"width": "100%"}),
                                        dcc.Checklist(
                                            id="manual-y-enabled",
                                            options=[{"label": "Manual Y range", "value": "manual"}],
                                            value=[],
                                        ),
                                        html.Div(
                                            id="manual-y-inputs",
                                            children=[
                                                html.Label("Y max (dB)"),
                                                dcc.Input(id="y-max", type="text", value="-110", debounce=True, style={"width": "100%"}),
                                                html.Label("Y min (dB)"),
                                                dcc.Input(id="y-min", type="text", value="-140", debounce=True, style={"width": "100%"}),
                                            ],
                                            style={"display": "none", "marginTop": "6px"},
                                        ),
                                        dcc.Checklist(
                                            id="sim-enabled",
                                            options=[{"label": "Enable simulation mode", "value": "sim"}],
                                            value=[],
                                        ),
                                        html.Div(
                                            id="sim-tones-container",
                                            children=[
                                                html.Label("Sim tones (Hz, space or comma separated)"),
                                                dcc.Textarea(id="sim-tones", value="", style={"width": "100%", "height": "80px"}),
                                            ],
                                            style={"display": "none"},
                                        ),
                                        dcc.Checklist(
                                            id="arduino-enabled",
                                            options=[{"label": "Enable Arduino", "value": "ardu"}],
                                            value=["ardu"],
                                        ),
                                        html.Div(
                                            id="arduino-options-container",
                                            children=[
                                                dcc.Checklist(
                                                    id="arduino-red-enabled",
                                                    options=[{"label": "Red LED", "value": "red"}],
                                                    value=["red"],
                                                ),
                                                html.Div(
                                                    id="arduino-red-duration-container",
                                                    children=[
                                                        html.Label("Red duration (ms)"),
                                                        dcc.Input(id="arduino-red-duration-ms", type="text", value="1200", debounce=True, style={"width": "100%"}),
                                                    ],
                                                    style={"display": "block"},
                                                ),
                                                dcc.Checklist(
                                                    id="arduino-green-enabled",
                                                    options=[{"label": "Green LED", "value": "green"}],
                                                    value=["green"],
                                                ),
                                                html.Div(
                                                    id="arduino-green-duration-container",
                                                    children=[
                                                        html.Label("Green duration (ms)"),
                                                        dcc.Input(id="arduino-green-duration-ms", type="text", value="1200", debounce=True, style={"width": "100%"}),
                                                    ],
                                                    style={"display": "block"},
                                                ),
                                                dcc.Checklist(
                                                    id="arduino-yellow-enabled",
                                                    options=[{"label": "Yellow LED", "value": "yellow"}],
                                                    value=["yellow"],
                                                ),
                                                dcc.Checklist(
                                                    id="arduino-speaker-enabled",
                                                    options=[{"label": "Speaker", "value": "speaker"}],
                                                    value=["speaker"],
                                                ),
                                                html.Div(
                                                    id="arduino-speaker-duration-container",
                                                    children=[
                                                        html.Label("Speaker duration (ms)"),
                                                        dcc.Input(id="arduino-speaker-duration-ms", type="text", value="300", debounce=True, style={"width": "100%"}),
                                                    ],
                                                    style={"display": "block"},
                                                ),
                                            ],
                                            style={"display": "block"},
                                        ),
                                        html.Div(
                                            [
                                                html.Button("Export Excel", id="export-xlsx-btn", n_clicks=0),
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
                                    ],
                                    style={"marginTop": "8px"},
                                ),
                            ],
                            open=False,
                            style={"marginTop": "10px"},
                        ),
                        html.Div(id="error", style={"color": "#a00", "marginTop": "6px"}),
                        html.Div(id="export-status", style={"color": "#0a0", "marginTop": "6px"}),
                        html.Hr(),
                    ],
                    style={"width": "22%", "display": "inline-block", "verticalAlign": "top", "padding": "10px"},
                ),
                html.Div(
                    [
                        html.Div(
                            dcc.Graph(id="spectrum-graph"),
                            id="graph-container",
                        ),
                        dash_table.DataTable(
                            id="detections-table",
                            columns=[
                                {"name": "Timestamp", "id": "timestamp"},
                                {"name": "Peak freq", "id": "peak_freq_label"},
                                {"name": "Peak power (dB)", "id": "peak_power_db"},
                                {"name": "Detection", "id": "detection"},
                                {"name": "Center freq", "id": "center_freq_label"},
                                {"name": "RX gain (dB)", "id": "rx_gain_db"},
                                {"name": "False Detection", "id": "false_positive", "presentation": "dropdown", "editable": True},
                                {"name": "Notes", "id": "notes", "editable": True},
                            ],
                            dropdown={
                                "false_positive": {
                                    "options": [
                                        {"label": "No", "value": "No"},
                                        {"label": "Yes", "value": "Yes"},
                                    ],
                                    "clearable": False,
                                }
                            },
                            data=[],
                            page_size=10,
                            style_table={
                                "height": "260px",
                                "overflowY": "auto",
                                "marginLeft": "10px",
                                "width": "calc(100% - 10px)",
                                "boxSizing": "border-box",
                            },
                            style_cell={"fontFamily": "monospace", "fontSize": "12px", "padding": "6px"},
                            style_header={"fontWeight": "bold"},
                            style_cell_conditional=[
                                {"if": {"column_id": "false_positive"}, "width": "120px", "minWidth": "120px", "maxWidth": "120px"},
                                {"if": {"column_id": "notes"}, "width": "260px", "minWidth": "260px"},
                            ],
                            editable=True,
                        ),
                    ],
                    style={"width": "75%", "display": "inline-block", "verticalAlign": "top", "paddingLeft": "20px"},
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
    Output("scan-select-btn", "style"),
    Output("scan-all-btn", "style"),
    Input("scan-modes", "data"),
)
def update_scan_button_styles(scan_modes):
    modes = set(scan_modes or [])
    base = {"marginLeft": "6px"}
    selected = {"marginLeft": "6px", "backgroundColor": "#06c", "color": "white"}

    def style_for(mode):
        return selected if mode in modes else base

    return (
        style_for("cellular"),
        style_for("bluetooth"),
        style_for("wifi"),
        style_for("select"),
        style_for("all"),
    )


@app.callback(
    Output("start-btn", "style"),
    Output("stop-btn", "style"),
    Input("run-state", "data"),
)
def update_run_button_styles(run_state):
    base = {"fontSize": "22px", "padding": "12px 18px", "flex": "1", "width": "100%"}
    if run_state == "running":
        return (
            {**base, "backgroundColor": "#0b5", "color": "white", "border": "1px solid #0b5"},
            base,
        )
    return (
        base,
        {**base, "backgroundColor": "#c22", "color": "white", "border": "1px solid #c22"},
    )


@app.callback(
    Output("selected-freq-container", "style"),
    Input("scan-modes", "data"),
)
def toggle_selected_freq_input(scan_modes):
    modes = set(scan_modes or [])
    if "select" in modes:
        return {"display": "block"}
    return {"display": "none"}


@app.callback(
    Output("select-exact-mode", "value"),
    Output("select-range-mode", "value"),
    Input("select-exact-mode", "value"),
    Input("select-range-mode", "value"),
    prevent_initial_call=True,
)
def enforce_select_mode_mutual_exclusive(exact_values, range_values):
    triggered = dash.callback_context.triggered
    trigger_id = triggered[0]["prop_id"].split(".")[0] if triggered else ""
    exact_on = "exact" in (exact_values or [])
    range_on = "range" in (range_values or [])

    if trigger_id == "select-exact-mode" and exact_on:
        return ["exact"], []
    if trigger_id == "select-range-mode" and range_on:
        return [], ["range"]
    if exact_on and range_on:
        return ["exact"], []
    return exact_values or [], range_values or []


@app.callback(
    Output("select-exact-input-container", "style"),
    Output("select-range-input-container", "style"),
    Input("scan-modes", "data"),
    Input("select-exact-mode", "value"),
    Input("select-range-mode", "value"),
)
def toggle_select_input_mode(scan_modes, exact_values, range_values):
    modes = set(scan_modes or [])
    if "select" not in modes:
        return {"display": "none"}, {"display": "none"}

    if "range" in (range_values or []):
        return {"display": "none"}, {"display": "block"}
    return {"display": "block"}, {"display": "none"}


@app.callback(
    Output("manual-y-inputs", "style"),
    Input("manual-y-enabled", "value"),
)
def toggle_manual_y_inputs(enabled_values):
    if "manual" in (enabled_values or []):
        return {"display": "block", "marginTop": "6px"}
    return {"display": "none", "marginTop": "6px"}


@app.callback(
    Output("sim-tones-container", "style"),
    Input("sim-enabled", "value"),
)
def toggle_sim_tones_inputs(enabled_values):
    if "sim" in (enabled_values or []):
        return {"display": "block"}
    return {"display": "none"}


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
    Output("arduino-options-container", "style"),
    Input("arduino-enabled", "value"),
)
def toggle_arduino_options(enabled_values):
    if "ardu" in (enabled_values or []):
        return {"display": "block"}
    return {"display": "none"}


@app.callback(
    Output("arduino-red-duration-container", "style"),
    Input("arduino-red-enabled", "value"),
)
def toggle_arduino_red_duration(values):
    if "red" in (values or []):
        return {"display": "block"}
    return {"display": "none"}


@app.callback(
    Output("arduino-green-duration-container", "style"),
    Input("arduino-green-enabled", "value"),
)
def toggle_arduino_green_duration(values):
    if "green" in (values or []):
        return {"display": "block"}
    return {"display": "none"}


@app.callback(
    Output("arduino-speaker-duration-container", "style"),
    Input("arduino-speaker-enabled", "value"),
)
def toggle_arduino_speaker_duration(values):
    if "speaker" in (values or []):
        return {"display": "block"}
    return {"display": "none"}


@app.callback(
    Output("arduino-speaker-duration-ms", "value"),
    Input("arduino-red-duration-ms", "value"),
    Input("arduino-speaker-duration-ms", "value"),
)
def clamp_speaker_duration_display(red_value, speaker_value):
    try:
        red_ms = int(red_value)
        speaker_ms = int(speaker_value)
    except (TypeError, ValueError):
        return speaker_value

    if red_ms < 0 or speaker_ms < 0:
        return speaker_value
    if speaker_ms > red_ms:
        return str(red_ms)
    return str(speaker_ms)


@app.callback(
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
    State("arduino-enabled", "value"),
    State("arduino-red-enabled", "value"),
    State("arduino-green-enabled", "value"),
    State("arduino-yellow-enabled", "value"),
    State("arduino-speaker-enabled", "value"),
    State("arduino-red-duration-ms", "value"),
    State("arduino-green-duration-ms", "value"),
    State("arduino-speaker-duration-ms", "value"),
    State("scan-modes", "data"),
    State("select-exact-mode", "value"),
    State("select-range-mode", "value"),
    State("selected-freq-ghz", "value"),
    State("selected-min-ghz", "value"),
    State("selected-max-ghz", "value"),
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
    arduino_enabled_values,
    arduino_red_values,
    arduino_green_values,
    arduino_yellow_values,
    arduino_speaker_values,
    arduino_red_duration_ms,
    arduino_green_duration_ms,
    arduino_speaker_duration_ms,
    scan_modes,
    select_exact_mode,
    select_range_mode,
    selected_freq_ghz,
    selected_min_ghz,
    selected_max_ghz,
):
    global worker
    triggered = dash.callback_context.triggered
    if not triggered:
        return "", dash.no_update

    trigger_id = triggered[0]["prop_id"].split(".")[0]

    if trigger_id == "stop-btn":
        with worker_lock:
            restart_worker(None)
        return "", "stopped"

    try:
        settings = build_worker_settings(
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
            arduino_enabled_values,
            arduino_red_values,
            arduino_green_values,
            arduino_yellow_values,
            arduino_speaker_values,
            arduino_red_duration_ms,
            arduino_green_duration_ms,
            arduino_speaker_duration_ms,
            scan_modes,
            select_range_mode,
            selected_freq_ghz,
            selected_min_ghz,
            selected_max_ghz,
        )
    except ValueError as exc:
        return str(exc), dash.no_update

    with worker_lock:
        restart_worker(settings)

    return "", "running"


@app.callback(
    Output("error", "children", allow_duplicate=True),
    Input("ant", "value"),
    Input("gain", "value"),
    Input("thresh-offset", "value"),
    Input("scan-interval", "value"),
    Input("detect-interval", "value"),
    Input("manual-y-enabled", "value"),
    Input("y-min", "value"),
    Input("y-max", "value"),
    Input("sim-enabled", "value"),
    Input("sim-tones", "value"),
    Input("arduino-enabled", "value"),
    Input("arduino-red-enabled", "value"),
    Input("arduino-green-enabled", "value"),
    Input("arduino-yellow-enabled", "value"),
    Input("arduino-speaker-enabled", "value"),
    Input("arduino-red-duration-ms", "value"),
    Input("arduino-green-duration-ms", "value"),
    Input("arduino-speaker-duration-ms", "value"),
    Input("scan-modes", "data"),
    Input("select-range-mode", "value"),
    Input("selected-freq-ghz", "value"),
    Input("selected-min-ghz", "value"),
    Input("selected-max-ghz", "value"),
    State("run-state", "data"),
    prevent_initial_call=True,
)
def apply_live_settings(
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
    arduino_enabled_values,
    arduino_red_values,
    arduino_green_values,
    arduino_yellow_values,
    arduino_speaker_values,
    arduino_red_duration_ms,
    arduino_green_duration_ms,
    arduino_speaker_duration_ms,
    scan_modes,
    select_range_mode,
    selected_freq_ghz,
    selected_min_ghz,
    selected_max_ghz,
    run_state,
):
    if run_state != "running":
        return dash.no_update

    with worker_lock:
        active_worker = worker

    if active_worker is None:
        return dash.no_update

    try:
        settings = build_worker_settings(
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
            arduino_enabled_values,
            arduino_red_values,
            arduino_green_values,
            arduino_yellow_values,
            arduino_speaker_values,
            arduino_red_duration_ms,
            arduino_green_duration_ms,
            arduino_speaker_duration_ms,
            scan_modes,
            select_range_mode,
            selected_freq_ghz,
            selected_min_ghz,
            selected_max_ghz,
        )
    except ValueError as exc:
        return str(exc)

    current_settings = active_worker.get_runtime_settings()
    if bool(current_settings.get("sim_enabled", False)) != bool(settings.get("sim_enabled", False)):
        return "Changing simulation mode while running still requires Stop then Start."

    active_worker.update_runtime_settings(settings)
    return ""


@app.callback(
    Output("scan-modes", "data"),
    Input("scan-cellular-btn", "n_clicks"),
    Input("scan-bt-btn", "n_clicks"),
    Input("scan-wifi-btn", "n_clicks"),
    Input("scan-select-btn", "n_clicks"),
    Input("scan-all-btn", "n_clicks"),
    State("scan-modes", "data"),
    prevent_initial_call=True,
)
def set_scan_mode(cell_clicks, bt_clicks, wifi_clicks, select_clicks, all_clicks, scan_modes):
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
    elif trigger_id == "scan-select-btn":
        modes = {"select"}
    else:
        modes.discard("all")
        modes.discard("select")
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
    Input("clear-table-btn", "n_clicks"),
    Input("manual-y-enabled", "value"),
    Input("y-min", "value"),
    Input("y-max", "value"),
    State("run-state", "data"),
    State("table-count", "data"),
)
def update_display(_tick, notes_store, clear_clicks, manual_y_enabled, y_min, y_max, run_state, table_count):
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
        if LOGO_DATA_URI is not None:
            fig = {
                "data": [],
                "layout": {
                    "title": "Spectrum",
                    "uirevision": "spectrum-fixed",
                    "xaxis": {"visible": False, "showgrid": False, "zeroline": False},
                    "yaxis": {"visible": False, "showgrid": False, "zeroline": False},
                    "images": [
                        {
                            "source": LOGO_DATA_URI,
                            "xref": "paper",
                            "yref": "paper",
                            "x": 0,
                            "y": 1,
                            "sizex": 1,
                            "sizey": 1,
                            "sizing": "contain",
                            "layer": "below",
                            "opacity": 1.0,
                        }
                    ],
                    "margin": {"l": 40, "r": 20, "t": 50, "b": 40},
                },
            }
        else:
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
                "uirevision": "spectrum-fixed",
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

        threshold_db = latest.get("threshold_db")
        if threshold_db is not None and len(x_plot) > 1:
            fig["layout"]["shapes"] = [
                {
                    "type": "line",
                    "xref": "paper",
                    "x0": 0,
                    "x1": 1,
                    "yref": "y",
                    "y0": threshold_db,
                    "y1": threshold_db,
                    "line": {"color": "red", "width": 2, "dash": "dash"},
                }
            ]
            fig["layout"]["annotations"] = [
                {
                    "xref": "paper",
                    "x": 1,
                    "yref": "y",
                    "y": threshold_db,
                    "text": f"Threshold {threshold_db:.1f} dB",
                    "showarrow": False,
                    "xanchor": "right",
                    "yanchor": "bottom",
                    "font": {"color": "red"},
                    "bgcolor": "rgba(255,255,255,0.75)",
                }
            ]

        if manual_range is not None:
            fig["layout"]["yaxis"]["range"] = manual_range
        if latest["y_range"] is not None:
            fig["layout"]["yaxis"]["range"] = latest["y_range"]
        if manual_range is not None:
            fig["layout"]["yaxis"]["range"] = manual_range

    triggered = dash.callback_context.triggered
    trigger_id = triggered[0]["prop_id"].split(".")[0] if triggered else ""

    if run_state == "stopped":
        if trigger_id == "clear-table-btn":
            return dash.no_update, []
        if active_worker is None:
            return fig, dash.no_update
        return dash.no_update, dash.no_update

    with detect_lock:
        table_rows = list(detect_buffer)

    notes_map = notes_store or {}
    for row in table_rows:
        key = row.get("id")
        if key in notes_map:
            row["notes"] = notes_map[key].get("notes", "")
            row["false_positive"] = normalize_false_positive(notes_map[key].get("false_positive", "No"))
        else:
            row["false_positive"] = normalize_false_positive(row.get("false_positive", "No"))

    current_count = len(table_rows)
    if table_count is not None and current_count == table_count:
        return fig, dash.no_update

    return fig, table_rows


@app.callback(
    Output("download-data", "data"),
    Output("export-status", "children"),
    Input("export-xlsx-btn", "n_clicks"),
    Input("clear-table-btn", "n_clicks"),
    State("notes-store", "data"),
    prevent_initial_call=True,
)
def export_table(_xlsx_clicks, clear_clicks, notes_store):
    triggered = dash.callback_context.triggered
    if not triggered:
        return None, ""
    trigger_id = triggered[0]["prop_id"].split(".")[0]

    if trigger_id == "clear-table-btn":
        with detect_lock:
            detect_buffer.clear()
        return None, ""

    with detect_lock:
        rows = list(detect_buffer)

    notes_map = notes_store or {}
    if notes_map:
        for row in rows:
            key = row.get("id")
            if key in notes_map:
                row["notes"] = notes_map[key].get("notes", "")
                row["false_positive"] = normalize_false_positive(notes_map[key].get("false_positive", "No"))
            else:
                row["false_positive"] = normalize_false_positive(row.get("false_positive", "No"))
    else:
        for row in rows:
            row["false_positive"] = normalize_false_positive(row.get("false_positive", "No"))

    if not rows:
        return None, "No detections to export."

    if trigger_id == "export-xlsx-btn":
        if pd is None:
            return None, "Excel export unavailable: install pandas + openpyxl."

        log_path = "detections_log.xlsx"
        df = pd.DataFrame(rows)

        if os.path.exists(log_path):
            try:
                existing = pd.read_excel(log_path)
                df = pd.concat([existing, df], ignore_index=True)
            except Exception:
                pass

        if "false_positive" in df.columns:
            df["false_positive"] = df["false_positive"].apply(normalize_false_positive)

        df.to_excel(log_path, index=False, sheet_name="detections")
        return None, f"Saved {len(rows)} rows to {log_path}."

    return None, ""


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
                "false_positive": normalize_false_positive(row.get("false_positive", "No")),
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
