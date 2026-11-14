"""Which stem is what: the file name is a hint, the audio decides when the name says nothing.

Vocal and DI stems are left out by name (the project mixes instrumentals and ignores DIs). Groups map to buses.
"""
import math
import re

import numpy as np
import soundfile
from scipy import signal

import hits as hitlib

# Order matters: the first hint found wins ("snaresample" is a snare, "bassdrum" is a kick).
HINTS = [
    ("vocal", ("vox", "vocal", "voice")),
    ("kick", ("kick", "bassdrum", "bdrum")),
    ("snare", ("snare",)),
    ("toms", ("tom",)),
    ("hat", ("hihat", "hat")),
    ("ride", ("ride",)),
    ("cymbal", ("cymbal", "crash", "china", "splash")),
    ("overheads", ("overhead",)),
    ("room", ("room", "ambience", "amb")),
    ("bass", ("bass",)),
    ("guitar", ("gtr", "guitar", "git")),
    ("synth", ("synth", "keys", "piano", "pad", "organ", "sfx", "fx", "strings")),
]
GROUP = {"kick": "kick", "snare": "snare", "toms": "toms", "hat": "cymbals", "ride": "cymbals", "cymbal": "cymbals",
         "overheads": "cymbals", "room": "room", "bass": "bass", "guitar": "gtr", "synth": "synths"}
DRUM_GROUPS = ("kick", "snare", "toms", "cymbals", "room")
SILENT_DB = -60.0


def words(stem):
    return re.sub(r"[^a-z]", "", stem.lower())


def hint(stem):
    name = words(stem)
    tokens = re.findall(r"[a-z]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", stem).lower())
    if name.endswith("di") or "di" in tokens:
        return "di"
    if "oh" in tokens:
        return "overheads"
    for cls, keys in HINTS:
        if any(k in name for k in keys):
            return cls
    return None


def by_content(audio, rate):
    """For a stem whose name gives no hint: hits or sustain, and where its energy sits."""
    mono = audio.mean(axis=0).astype(np.float64)
    f, p = signal.welch(mono, rate, nperseg=8192)
    total = p.sum() + 1e-20
    low = p[f < 150].sum() / total
    centroid = float((f * p).sum() / total)
    energy = mono**2
    times = hitlib.detect(energy, rate)
    seconds = len(mono) / rate
    hit_rate = len(times) / seconds
    smooth = np.convolve(energy, np.ones(441) / 441, mode="same")
    crest = 10 * math.log10((np.percentile(smooth, 99.9) + 1e-20) / (smooth.mean() + 1e-20))
    if hit_rate > 0.5 and crest > 12:
        if low > 0.5:
            return "kick"
        return "toms" if centroid < 1500 else "snare"
    if low > 0.6:
        return "bass"
    return "synth"


def classify(folder):
    """One entry per usable stem: name, class, group, sample, channels. Silent and left-out stems are listed apart."""
    found, left_out = [], []
    for path in sorted(folder.glob("*.wav")):
        audio, rate = soundfile.read(path, always_2d=True, dtype="float32")
        audio = audio.T
        peak = float(np.abs(audio).max()) if audio.size else 0.0
        cls = hint(path.stem)
        if cls in ("vocal", "di"):
            left_out.append({"stem": path.name, "why": cls})
            continue
        if peak < 10 ** (SILENT_DB / 20):
            left_out.append({"stem": path.name, "why": "silent"})
            continue
        how = "name"
        if cls is None:
            cls, how = by_content(audio, rate), "content"
        found.append({"stem": path.name, "name": path.stem, "class": cls, "group": GROUP[cls], "by": how,
                      "sample": "sample" in words(path.stem), "double": bool(re.search(r"dt\d*$", words(path.stem))),
                      "channels": audio.shape[0]})
    return found, left_out
