# Simulation helpers extracted from sdr_worker.py
from __future__ import annotations

import numpy as np


def split_sim_values(sim_values: list[float], rate_hz: float) -> tuple[list[float], list[float]]:
    sim_offsets: list[float] = []
    sim_freqs: list[float] = []
    offset_threshold = max(rate_hz, 5e6)
    for val in sim_values:
        try:
            fval = float(val)
        except (TypeError, ValueError):
            continue
        if abs(fval) <= offset_threshold:
            sim_offsets.append(fval)
        else:
            sim_freqs.append(fval)
    return sim_offsets, sim_freqs


def generate_sim_samples(
    rx_rate: float,
    rx_freq: float,
    num_samps: int,
    sim_offsets: list[float],
    sim_freqs: list[float],
    noise_scale: float = 0.1,
) -> np.ndarray:
    t = np.arange(num_samps) / rx_rate
    vec = np.zeros(num_samps, dtype=np.complex64)

    for offset in set(sim_offsets):
        if abs(offset) <= rx_rate / 2:
            vec += 2.5 * np.exp(2j * np.pi * offset * t)

    for f in sim_freqs:
        df = f - rx_freq
        if abs(df) <= rx_rate / 2:
            vec += 2.0 * np.exp(2j * np.pi * df * t)

    vec += noise_scale * (np.random.randn(num_samps) + 1j * np.random.randn(num_samps))
    return np.expand_dims(vec, axis=0)
