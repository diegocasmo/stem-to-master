import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

RATE = 44100


def kicks(seconds, every_s=0.5, freq=55.0, level=0.5):
    """Mono decaying low sine bursts, one per beat."""
    x = np.zeros(int(seconds * RATE))
    t = np.arange(int(0.3 * RATE)) / RATE
    hit = level * np.sin(2 * np.pi * freq * t) * np.exp(-t / 0.06)
    for start in range(0, len(x) - len(hit), int(every_s * RATE)):
        x[start : start + len(hit)] += hit
    return x


def noise(seconds, channels=2, level=0.05, seed=0):
    return level * np.random.default_rng(seed).standard_normal((channels, int(seconds * RATE)))
