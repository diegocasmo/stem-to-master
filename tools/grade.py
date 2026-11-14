"""Grader: our master against the official excerpt, aligned and at matched loudness, on the graded spans only.

uv run tools/grade.py songs/<song> [more songs] [--master renders/master.wav] [--label text]

Gaps are ours minus official. The official is used only here, never by the pipeline.
"""
import argparse
import json
import math
import time
from pathlib import Path

import numpy as np
import pyloudnorm
import soundfile
from scipy import signal

import hits as hitlib

OCTAVES = [31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
THIRDS = [10000, 12500]
WIDTH_BANDS = [(120, 500), (500, 2000), (2000, 8000)]
ENV_S = 220 / 44100
TOL = {"band": 2.0, "punch": 1.5, "width": 1.5}
HISTORY = Path.home() / "stemmaster-scratch" / "night" / "grades.jsonl"


def db(x):
    return 10 * math.log10(max(float(x), 1e-25))


def r2(x):
    return None if x is None else round(float(x), 2)


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


def stem_hits(song, chain, rate, length):
    """Hit times (s) from the close-mic stems, found by name: the grader is test equipment, not the pipeline."""
    total = np.zeros(length)
    for path in sorted((song / "stems").glob("*.wav")):
        name = path.stem.lower()
        if chain in name and "sample" not in name:
            x = soundfile.read(path, always_2d=True, dtype="float64")[0]
            total[: len(x)] += (x**2).sum(axis=1)[:length]
    return hitlib.detect(total, rate) / rate


def align(ours, official, mask, rate, reach_s=0.05):
    a = np.where(mask, ours.mean(axis=0), 0)
    b = np.where(mask, official.mean(axis=0), 0)
    reach = int(reach_s * rate)
    corr = signal.correlate(b[reach:-reach], a, mode="valid", method="fft")
    return int(np.argmax(np.abs(corr))) - reach


def grade(song, master_path=None):
    song = Path(song)
    notes = json.loads((song / "notes.json").read_text())
    ours, rate = soundfile.read(master_path or song / "renders" / "master.wav", always_2d=True, dtype="float64")
    official, orate = soundfile.read(song / "official.wav", always_2d=True, dtype="float64")
    assert rate == orate
    length = min(len(ours), len(official))
    ours, official = ours[:length].T, official[:length].T
    mask = np.zeros(length, bool)
    spans = notes["graded_spans_s"]
    for a, b in spans:
        mask[round(a * rate) : round(b * rate)] = True
    lag = align(ours, official, mask, rate)
    # official[t + lag] matches ours[t]: shift the official onto our clock.
    official = np.roll(official, -lag, axis=1)

    meter = pyloudnorm.Meter(rate)
    joined = lambda x: np.concatenate([x[:, round(a * rate) : round(b * rate)] for a, b in spans], axis=1)
    l_ours, l_off = meter.integrated_loudness(joined(ours).T), meter.integrated_loudness(joined(official).T)
    ours = ours * 10 ** ((l_off - l_ours) / 20)
    jo, jf = joined(ours), joined(official)

    def spec(x):
        f, p = signal.welch(x, rate, nperseg=16384, axis=-1)
        return f, p.mean(axis=0)

    fo, po = spec(jo)
    _, pf = spec(jf)

    def band(p, lo, hi):
        return db(p[(fo >= lo) & (fo < hi)].sum())

    bands = {}
    for c in OCTAVES:
        lo, hi = c / 2**0.5, min(c * 2**0.5, rate / 2)
        bands[str(c)] = r2(band(po, lo, hi) - band(pf, lo, hi))
    thirds = {str(c): r2(band(po, c / 2 ** (1 / 6), c * 2 ** (1 / 6)) - band(pf, c / 2 ** (1 / 6), c * 2 ** (1 / 6))) for c in THIRDS}
    low_thirds = {str(c): r2(band(po, c / 2 ** (1 / 6), c * 2 ** (1 / 6)) - band(pf, c / 2 ** (1 / 6), c * 2 ** (1 / 6)))
                  for c in (40, 50, 63, 80, 100, 125, 160, 200, 250)}

    width = {"all": r2(side_mid(jo) - side_mid(jf))}
    width_abs = {"all": [r2(side_mid(jo)), r2(side_mid(jf))]}
    for lo, hi in WIDTH_BANDS:
        so, sf = side_mid(bandpass(jo, rate, lo, hi)), side_mid(bandpass(jf, rate, lo, hi))
        width[f"{lo}-{hi}"] = r2(so - sf)
        width_abs[f"{lo}-{hi}"] = [r2(so), r2(sf)]

    def in_spans(times, edge=0.2):
        return np.array([t for t in times if any(a + edge <= t <= b - edge for a, b in spans)])

    punch, raw = {}, {}
    for drum, bands_hz in (("kick", {"low": (40, 120), "click": (2000, 6000)}), ("snare", {"body": (150, 350), "crack": (2000, 8000)})):
        times = in_spans(stem_hits(song, drum, rate, length))
        for name, (lo, hi) in bands_hz.items():
            eo, ef = env(bandpass(ours, rate, lo, hi), rate), env(bandpass(official, rate, lo, hi), rate)
            po_, pf_ = prominence(eo, times), prominence(ef, times)
            d = po_ - pf_
            punch[f"{drum}_{name}"] = r2(np.nanmedian(d)) if np.isfinite(d).any() else None
            raw[f"{drum}_{name}"] = [r2(np.nanmedian(po_)), r2(np.nanmedian(pf_)), int(np.isfinite(d).sum())]
            if drum == "kick" and name == "low":
                to, tf = tail(eo, times), tail(ef, times)
                punch["kick_low_tail"] = r2(np.nanmedian(to - tf))
                raw["kick_low_tail"] = [r2(np.nanmedian(to)), r2(np.nanmedian(tf))]

    # Guitar low-mid: 125-250 Hz relative to the whole, away from kick and snare hits, where guitars and bass dominate.
    hits_all = np.concatenate([stem_hits(song, "kick", rate, length), stem_hits(song, "snare", rate, length)])
    off = mask.copy()
    for h in hits_all:
        off[max(0, round((h - 0.01) * rate)) : round((h + 0.15) * rate)] = False

    def rel_lowmid(x):
        seg = x[:, off]
        return db((bandpass(seg, rate, 125, 250) ** 2).sum()) - db((seg**2).sum())

    lowmid = r2(rel_lowmid(ours) - rel_lowmid(official)) if off.sum() > rate else None

    def crest(x):
        frames = env(x, rate)
        return db(np.percentile(frames, 99.5)) - db(frames.mean())

    crest_gap = r2(crest(jo) - crest(jf))
    gates = {"bands": max(abs(v) for v in bands.values()) <= TOL["band"],
             "kick": all(abs(punch[k]) <= TOL["punch"] for k in ("kick_low", "kick_click") if punch[k] is not None),
             "snare": all(abs(punch[k]) <= TOL["punch"] for k in ("snare_body", "snare_crack") if punch[k] is not None),
             "width": all(abs(v) <= TOL["width"] for v in width.values())}
    excess = (sum(max(0, abs(v) - TOL["band"]) for v in bands.values())
              + sum(max(0, abs(punch[k]) - TOL["punch"]) for k in ("kick_low", "kick_click", "snare_body", "snare_crack") if punch[k] is not None)
              + sum(max(0, abs(v) - TOL["width"]) for v in width.values()))
    total = (sum(abs(v) for v in bands.values()) / TOL["band"]
             + sum(abs(punch[k]) for k in ("kick_low", "kick_click", "snare_body", "snare_crack") if punch[k] is not None) / TOL["punch"]
             + sum(abs(v) for v in width.values()) / TOL["width"])
    return {"song": song.name, "graded_s": round(float(mask.sum()) / rate, 1), "align_lag_samples": lag,
            "lufs_ours_official": [r2(l_ours), r2(l_off)], "bands": bands, "thirds": thirds, "low_thirds": low_thirds,
            "punch": punch, "punch_raw_ours_official_n": raw, "width": width, "width_abs_ours_official": width_abs,
            "guitar_lowmid_125_250": lowmid, "crest": crest_gap, "gates": gates, "pass": all(gates.values()),
            "excess": r2(excess), "total": r2(total)}


def line(g):
    b = " ".join(f"{k}:{v:+.1f}" for k, v in g["bands"].items())
    p = " ".join(f"{k}:{v:+.1f}" for k, v in g["punch"].items() if v is not None)
    w = " ".join(f"{k}:{v:+.1f}" for k, v in g["width"].items())
    return (f"{g['song'][:24]:24s} pass={g['pass']!s:5s} excess={g['excess']:5.1f} total={g['total']:5.1f}\n"
            f"  bands {b}\n  hi-thirds {g['thirds']}  lowmid {g['guitar_lowmid_125_250']}  crest {g['crest']}\n"
            f"  punch {p}\n  width {w}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("songs", nargs="+")
    parser.add_argument("--master")
    parser.add_argument("--label", default="")
    parser.add_argument("--no-log", action="store_true")
    args = parser.parse_args()
    results = [grade(s, args.master) for s in args.songs]
    for g in results:
        print(line(g))
    summary = {"time": time.strftime("%Y-%m-%d %H:%M"), "label": args.label,
               "excess_sum": r2(sum(g["excess"] for g in results)), "total_sum": r2(sum(g["total"] for g in results)),
               "results": results}
    print(f"SUM excess={summary['excess_sum']} total={summary['total_sum']} all_pass={all(g['pass'] for g in results)}")
    if not args.no_log:
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY.open("a") as f:
            f.write(json.dumps(summary) + "\n")


if __name__ == "__main__":
    main()
