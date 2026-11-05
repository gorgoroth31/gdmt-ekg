"""Pan-Tompkins-QRS-Detektion und Herzfrequenz-Zeitreihen.

Pan J, Tompkins WJ. A real-time QRS detection algorithm.
IEEE Trans Biomed Eng. 1985;32(3):220-236.

Pipeline: Bandpass (ca. 5-15 Hz) -> Ableitung -> Quadrierung ->
Moving-Window-Integration (ca. 150 ms) -> adaptive Schwellenwert-
Suche mit Refraktärzeit und Searchback. Die R-Position wird abschließend
auf das Maximum des bandgefilterten Signals zurückverfeinert.
"""

from __future__ import annotations

import numpy as np

from .scp_reader import Measurement

HR_MIN = 30.0   # bpm
HR_MAX = 220.0  # bpm
RR_MIN_S = 60.0 / HR_MAX
RR_MAX_S = 60.0 / HR_MIN


def _biquad(x: np.ndarray, b0, b1, b2, a1, a2) -> np.ndarray:
    y = np.zeros_like(x)
    for n in range(len(x)):
        xn = x[n]
        xn1 = x[n - 1] if n >= 1 else 0.0
        xn2 = x[n - 2] if n >= 2 else 0.0
        yn1 = y[n - 1] if n >= 1 else 0.0
        yn2 = y[n - 2] if n >= 2 else 0.0
        y[n] = b0 * xn + b1 * xn1 + b2 * xn2 - a1 * yn1 - a2 * yn2
    return y


def _butter_lowpass(x: np.ndarray, f0: float, fs: float) -> np.ndarray:
    """Butterworth 2. Ordnung (RBJ-Cookbook)."""
    w0 = 2.0 * np.pi * f0 / fs
    alpha = np.sin(w0) / (2.0 * np.sqrt(2.0) / 2.0)
    cosw = np.cos(w0)
    a0 = 1.0 + alpha
    b0 = (1.0 - cosw) / 2.0 / a0
    b1 = (1.0 - cosw) / a0
    b2 = b0
    a1 = -2.0 * cosw / a0
    a2 = (1.0 - alpha) / a0
    return _biquad(x, b0, b1, b2, a1, a2)


def _butter_highpass(x: np.ndarray, f0: float, fs: float) -> np.ndarray:
    w0 = 2.0 * np.pi * f0 / fs
    alpha = np.sin(w0) / (2.0 * np.sqrt(2.0) / 2.0)
    cosw = np.cos(w0)
    a0 = 1.0 + alpha
    b0 = (1.0 + cosw) / 2.0 / a0
    b1 = -(1.0 + cosw) / a0
    b2 = b0
    a1 = -2.0 * cosw / a0
    a2 = (1.0 - alpha) / a0
    return _biquad(x, b0, b1, b2, a1, a2)


def bandpass(x: np.ndarray, fs: float) -> np.ndarray:
    return _butter_highpass(_butter_lowpass(x, 15.0, fs), 5.0, fs)


def moving_average(x: np.ndarray, w: int) -> np.ndarray:
    if w <= 1:
        return x.copy()
    c = np.cumsum(np.insert(x, 0, 0.0))
    out = (c[w:] - c[:-w]) / w
    return np.concatenate([np.full(w - 1, out[0]), out])


def _local_maxima(x: np.ndarray, min_dist: int) -> list[int]:
    """Lokale Maxima mit Mindestabstand, absteigend nach Höhe."""
    n = len(x)
    if n < 3:
        return []
    rising = x[1:-1] >= x[:-2]
    falling = x[1:-1] > x[2:]
    peaks = np.flatnonzero(rising & falling) + 1
    order = np.argsort(x[peaks])[::-1]
    chosen: list[int] = []
    last = np.full(0, 0, dtype=int)
    for idx in peaks[order]:
        if len(last) == 0 or np.min(np.abs(idx - last)) >= min_dist:
            chosen.append(int(idx))
            last = np.append(last, idx)
    return sorted(chosen)


def pan_tompkins(signal: np.ndarray, fs: float) -> np.ndarray:
    """R-Zacken-Positionen (Sample-Indizes) im Rohsignal."""
    x = np.asarray(signal, dtype=np.float64)
    x = x - np.median(x)
    if len(x) < int(2 * fs):
        return np.empty(0, dtype=int)

    filtered = bandpass(x, fs)
    deriv = np.zeros_like(filtered)
    deriv[3:] = (
        2 * filtered[3:] + filtered[2:-1] - filtered[1:-2] - 2 * filtered[:-3]
    ) / 8.0
    squared = deriv**2
    w_mwi = max(int(round(0.150 * fs)), 2)
    mwi = moving_average(squared, w_mwi)

    refractory = int(round(0.2 * fs))
    candidates = _local_maxima(mwi, refractory)
    if not candidates:
        return np.empty(0, dtype=int)

    # adaptive Schwellwerte (Signal-/Rauschpegel wie im Original):
    # Initialisierung mit dem Maximum der ersten 2 Sekunden
    init_win = mwi[: max(int(2 * fs), 1)]
    spki = float(np.max(init_win))
    nkpi = 0.25 * spki
    beats: list[int] = []
    for pk in candidates:
        val = mwi[pk]
        threshold = nkpi + 0.25 * (spki - nkpi)

        # Searchback: übersprungenes Intervall?
        if beats:
            last_rrs = np.diff(np.asarray(beats)) / fs
            avg_rr = float(np.mean(last_rrs[-8:])) if len(last_rrs) else 0.0
            if avg_rr > 0 and (pk - beats[-1]) / fs > 1.66 * avg_rr:
                seg = mwi[beats[-1] + refractory : pk - refractory]
                if len(seg):
                    miss = int(np.argmax(seg)) + beats[-1] + refractory
                    if mwi[miss] > threshold:
                        beats.append(miss)
                        spki = 0.125 * mwi[miss] + 0.875 * spki

        threshold = nkpi + 0.25 * (spki - nkpi)
        if val >= threshold:
            if not beats or pk - beats[-1] >= refractory:
                beats.append(pk)
            spki = 0.125 * val + 0.875 * spki
        else:
            nkpi = 0.125 * val + 0.875 * nkpi

    # Verfeinerung: Maximum des gefilterten Signals im Umfeld der
    # MWI-Peaks (MWI enthält die Gruppenlaufzeit der Pipeline).
    lo_w = 2 * w_mwi
    hi_w = w_mwi
    group_delay = int(round(0.05 * fs))  # Bandpass ~50 ms Gruppenlaufzeit
    refined = []
    for b in beats:
        lo = max(0, b - lo_w)
        hi = min(len(filtered), b + hi_w)
        seg = np.abs(filtered[lo:hi])
        if len(seg):
            refined.append(int(lo + np.argmax(seg)) - group_delay)
    refined = [r for r in refined if r >= 0]
    refined = sorted(set(refined))

    # Plausibilität: RR-Intervall in Sekundengrenzen
    keep: list[int] = []
    for i, r in enumerate(refined):
        if not keep:
            keep.append(r)
            continue
        rr = (r - keep[-1]) / fs
        if RR_MIN_S <= rr <= RR_MAX_S:
            keep.append(r)
    return np.array(keep, dtype=int)


def heart_rate_from_beats(beats: np.ndarray, fs: float):
    """Instantane Herzfrequenz je Herzschlag.

    Rückgabe: (t_mitte [s], rr [s], hr [bpm]) – Länge n_beats-1.
    """
    if len(beats) < 2:
        return np.empty(0), np.empty(0), np.empty(0)
    t = np.asarray(beats) / fs
    rr = np.diff(t)
    ok = (rr >= RR_MIN_S) & (rr <= RR_MAX_S)
    rr = rr[ok]
    t_mid = 0.5 * (t[:-1] + t[1:])[ok]
    hr = 60.0 / rr
    return t_mid, rr, hr


def sample_smooth(t: np.ndarray, y: np.ndarray, t_out: np.ndarray,
                  window_s: float) -> np.ndarray:
    """Gleitender Mittelwert über window_s zu Ausgabezeiten t_out."""
    if len(t) == 0 or window_s <= 0:
        return np.full(len(t_out), np.nan)
    t = np.asarray(t)
    y = np.asarray(y)
    order = np.argsort(t)
    t, y = t[order], y[order]
    half = window_s / 2.0
    left = np.searchsorted(t, t_out - half, side="left")
    right = np.searchsorted(t, t_out + half, side="right")
    # kumulative Summen mitte-links
    csum = np.concatenate([[0.0], np.cumsum(y)])
    out = np.full(len(t_out), np.nan)
    for i in range(len(t_out)):
        a, b = left[i], right[i]
        if b > a:
            out[i] = (csum[b] - csum[a]) / (b - a)
    return out


def series_payload(measurement: Measurement, beats: np.ndarray,
                   avg_window: float = 30.0, grid: float = 0.5):
    """Alle Zeitreihen für das Frontend berechnen."""
    fs = measurement.fs
    duration = measurement.duration
    t_mid, rr, hr = heart_rate_from_beats(beats, fs)
    t_grid = np.arange(0.0, duration + grid, grid)
    hr_step_t = np.concatenate([[0.0], t_mid, [duration]])
    hr_step_v = np.concatenate(
        [[hr[0] if len(hr) else np.nan], hr, [hr[-1] if len(hr) else np.nan]]
    )
    hr_grid = sample_smooth(t_mid, hr, t_grid, avg_window)
    # Ableitung der geglätteten HR (bpm pro Minute)
    dhr = np.full(len(t_grid), np.nan)
    if len(t_grid) > 3:
        valid = ~np.isnan(hr_grid)
        if valid.sum() > 3:
            filled = np.interp(t_grid, t_grid[valid], hr_grid[valid])
            window = min(11, len(filled))
            kernel = np.ones(window) / window
            sm = np.convolve(filled, kernel, mode="same")
            dy = np.gradient(sm, grid) * 60.0  # bpm/s -> bpm/min
            dhr = np.convolve(dy, kernel, mode="same")
            dhr[:2] = np.nan
            dhr[-2:] = np.nan
    return {
        "beats_t": (np.asarray(beats) / fs).round(4).tolist(),
        "hr_t": t_mid.round(4).tolist(),
        "hr": hr.round(2).tolist(),
        "rr": rr.round(4).tolist(),
        "grid_t": t_grid.round(3).tolist(),
        "hr_avg": np.where(np.isnan(hr_grid), None, hr_grid.round(2)).tolist(),
        "hr_avg_window": avg_window,
        "dhr": np.where(np.isnan(dhr), None, dhr.round(3)).tolist(),
    }


def analyze(measurement: Measurement, avg_window: float = 30.0) -> dict:
    """Gesamte Analyse einer Messung (Pan-Tompkins + Zeitreihen)."""
    beats = pan_tompkins(measurement.signal, measurement.fs)
    payload = series_payload(measurement, beats, avg_window=avg_window)
    payload["beats_idx"] = np.asarray(beats).tolist()
    payload["device_beats_idx"] = list(measurement.device_beats)
    return payload
