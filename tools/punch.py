"""Punch, tail and width measures shared by the engineer and the grader."""
import math

import numpy as np
from scipy import signal

ENV_S = 220 / 44100


def db(x):
    return 10 * math.log10(max(float(x), 1e-25))


def bandpass(x, rate, low, high):
    high = min(high, rate / 2 * 0.98)
    sos = signal.butter(4, [low, high], "bandpass", fs=rate, output="sos")
    return signal.sosfiltfilt(sos, x, axis=-1)


def env(x, rate):
    """Band power in 5 ms frames, summed over channels."""
    hop = round(ENV_S * rate)
    power = (x**2).sum(axis=0)
    count = len(power) // hop
    return power[: count * hop].reshape(count, hop).mean(axis=1)


def prominence(e, times_s, attack=(-0.030, 0.040), around=((-0.130, -0.050), (0.070, 0.150))):
    """Per hit: the band's smoothed peak in the attack window over the median of the surrounding windows (dB)."""
    t = (np.arange(len(e)) + 0.5) * ENV_S
    out = []
    for h in times_s:
        a = (t >= h + attack[0]) & (t < h + attack[1])
        s = ((t >= h + around[0][0]) & (t < h + around[0][1])) | ((t >= h + around[1][0]) & (t < h + around[1][1]))
        if a.sum() < 3 or s.sum() < 3:
            out.append(np.nan)
            continue
        peak = np.convolve(e[a], np.ones(2) / 2, mode="valid").max()
        out.append(db(peak) - db(np.median(e[s])))
    return np.array(out)


def tail(e, times_s):
    """Per hit: band level 80-180 ms after the hit relative to its attack peak (dB); higher is a longer tail."""
    t = (np.arange(len(e)) + 0.5) * ENV_S
    out = []
    for h in times_s:
        a = (t >= h - 0.01) & (t < h + 0.04)
        late = (t >= h + 0.08) & (t < h + 0.18)
        out.append(db(e[late].mean()) - db(e[a].max()) if a.any() and late.any() else np.nan)
    return np.array(out)


def side_mid(x):
    mid, side = (x[0] + x[1]) / 2, (x[0] - x[1]) / 2
    return db((side**2).sum()) - db((mid**2).sum())


HIT_BANDS = {"kick": (30, 150), "snare": (1000, 8000)}


def drum_hits(audio, rate, drum):
    """Hit times (s) found in the drum's own band, so bleed of the other drum in its mics is not counted as a hit."""
    import hits as hitlib

    x = np.atleast_2d(np.asarray(audio, dtype=np.float64))
    low, high = HIT_BANDS[drum]
    return hitlib.detect((bandpass(x, rate, low, high) ** 2).sum(axis=0), rate) / rate
