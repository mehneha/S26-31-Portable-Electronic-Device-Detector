# Welch/PSD utilities extracted from rx_spectrum_web.py
from typing import Optional, Tuple

import numpy as np

# Optional scipy for peak finding and Welch's method. Falls back to simple method if not present.
try:
    from scipy.signal import find_peaks, welch
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False
    welch = None
    find_peaks = None

# Fixed Welch defaults.
WELCH_NPERSEG_DEFAULT = 4096
WELCH_NOVERLAP_DEFAULT = 2048


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
    except Exception:
        return -100.0

    psd_db = 10 * np.log10(psd_linear + 1e-20)
    return float(np.median(psd_db))
