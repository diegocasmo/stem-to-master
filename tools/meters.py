"""Fast measurements on float32 (channels, samples) audio, for judging inserts and buses."""
import math

import numpy as np
import pyloudnorm
from scipy import signal

import hits as hitlib

TINY = 1e-20


def db(power):
    return 10 * math.log10(power) if power > TINY else -200.0


def spectrum(audio, rate):
    """Mean power per frequency (Welch, 8192 points, summed over channels)."""
    freqs, power = signal.welch(audio.astype(np.float64), rate, nperseg=8192, axis=-1)
    return freqs, power.sum(axis=0)


def band_db(spec, low, high):
    freqs, power = spec
    return db(float(power[(freqs >= low) & (freqs < high)].sum()))


def filtered(audio, rate, low, high):
    if low and high:
        sos = signal.butter(4, [low, high], "bandpass", fs=rate, output="sos")
    elif high:
        sos = signal.butter(4, high, "lowpass", fs=rate, output="sos")
    else:
        sos = signal.butter(4, low, "highpass", fs=rate, output="sos")
    return signal.sosfiltfilt(sos, audio.astype(np.float64), axis=-1)


def windows(length, times, rate, span_ms):
    inside = np.zeros(length, dtype=bool)
    first, last = round(span_ms[0] / 1000 * rate), round(span_ms[1] / 1000 * rate)
    for t in times:
        inside[max(0, t + first) : max(0, min(length, t + last))] = True
    return inside


def masked_band_db(audio, rate, low, high, inside):
    x = filtered(audio, rate, low, high)
    return db(float((x[:, inside] ** 2).sum(axis=0).mean())) if inside.any() else -200.0


def detect_hits(audio, rate, floor_db=hitlib.FLOOR_DB):
    return hitlib.detect((audio.astype(np.float64) ** 2).sum(axis=0), rate, floor_db)


def hit_stats(audio, rate, times):
    """Per-hit level and attack, and off-hit bleed, as hits.py measures them."""
    if len(times) == 0:
        return {"hits": 0}
    found = hitlib.measure(audio.T.astype(np.float64), rate, times, include_off_hit=False)
    return {"hits": len(times), "level_db": found["level_db"].tolist(), "attack_db": found["attack_db"].tolist(),
            "tail_db": found["tail_db"].tolist(), "bleed_db": found["bleed_db"]}


def pct(values, p):
    return float(np.percentile(values, p)) if len(values) else None


def lufs(audio, rate):
    try:
        value = pyloudnorm.Meter(rate).integrated_loudness(audio.T.astype(np.float64))
    except ValueError:
        return -200.0
    return value if math.isfinite(value) else -200.0


def span_lufs(audio, rate, spans):
    """Integrated loudness over the joined spans (seconds)."""
    parts = [audio[:, round(a * rate) : round(b * rate)] for a, b in spans]
    return lufs(np.concatenate(parts, axis=1), rate)


def true_peak_db(audio):
    peak = max(float(np.max(np.abs(signal.resample_poly(ch.astype(np.float64), 4, 1)))) for ch in audio)
    return db(peak**2)


def short_term(audio, rate, hop_s=0.1, window_s=3.0):
    meter = pyloudnorm.Meter(rate)
    weighted = audio.T.astype(np.float64).copy()
    for stage in meter._filters.values():
        for ch in range(weighted.shape[1]):
            weighted[:, ch] = stage.apply_filter(weighted[:, ch])
    energy = np.concatenate([[0.0], np.cumsum(weighted.T.__pow__(2).sum(axis=0))])
    window, hop = int(window_s * rate), int(hop_s * rate)
    starts = np.arange(0, weighted.shape[0] - window + 1, hop)
    with np.errstate(divide="ignore"):
        return starts / rate, -0.691 + 10 * np.log10((energy[starts + window] - energy[starts]) / window)


def side_mid_db(audio):
    if audio.shape[0] < 2:
        return -200.0
    mid, side = (audio[0] + audio[1]) / 2, (audio[0] - audio[1]) / 2
    return db(float((side.astype(np.float64) ** 2).sum())) - db(float((mid.astype(np.float64) ** 2).sum()))
