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
from punch import bandpass, db, env, prominence, side_mid, tail

OCTAVES = [31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
THIRDS = [10000, 12500]
WIDTH_BANDS = [(120, 500), (500, 2000), (2000, 8000)]
TOL = {"band": 2.0, "punch": 1.5, "width": 1.5}
HISTORY = Path.home() / "stemmaster-scratch" / "night" / "grades.jsonl"


def r2(x):
    return None if x is None else round(float(x), 2)


def stem_hits(song, chain, rate, length):
    """Hit times (s) from the close-mic stems, found by name: the grader is test equipment, not the pipeline."""
    total = np.zeros(length)
    for path in sorted((song / "stems").glob("*.wav")):
        name = path.stem.lower()
        if chain in name and "sample" not in name:
            x = soundfile.read(path, always_2d=True, dtype="float64")[0]
            total[: len(x)] += (x**2).sum(axis=1)[:length]
    return hitlib.detect(total, rate) / rate


def align(ours, official, rate, reach_s=0.05):
    a, b = ours.mean(axis=0), official.mean(axis=0)
    reach = int(reach_s * rate)
    corr = signal.correlate(b[reach:-reach], a, mode="valid", method="fft")
    return int(np.argmax(np.abs(corr))) - reach


THIRD_CENTERS = [31.5 * 2 ** (k / 3) for k in range(-2, 28)]  # 25 Hz to 16 kHz
HOP_S = 0.1
NFFT = 8192


def cells(x, rate):
    """Power per (third-octave band, 0.1 s frame), summed over channels."""
    hop = int(HOP_S * rate)
    f, _, z = signal.stft(x, rate, nperseg=NFFT, noverlap=NFFT - hop, boundary=None, padded=False, axis=-1)
    power = (np.abs(z) ** 2).sum(axis=0) if z.ndim == 3 else np.abs(z) ** 2
    rows = [power[(f >= c / 2 ** (1 / 6)) & (f < c * 2 ** (1 / 6))].sum(axis=0) for c in THIRD_CENTERS]
    return np.array(rows), f, z


def k_weights(freqs, rate):
    meter = pyloudnorm.Meter(rate)
    w = np.ones(len(freqs))
    for stage in meter._filters.values():
        _, h = signal.freqz(stage.b, stage.a, worN=freqs, fs=rate)
        w *= np.abs(h) ** 2
    return w


def vocal_mask(song, notes, rate, length, frames):
    """True where a cell is scored: the raw vocal stems are >= 15 dB under the raw instrument stems there."""
    folder = song / "vocals"
    vocal_files = sorted(folder.glob("*.wav")) if folder.exists() else []
    if not vocal_files:
        mask = np.zeros((len(THIRD_CENTERS), frames), bool)
        t = (np.arange(frames) * HOP_S) + NFFT / rate / 2
        for a, b in notes["graded_spans_s"]:
            mask[:, (t >= a) & (t <= b)] = True
        return mask

    def summed(files):
        total = np.zeros(length)
        for path in files:
            x = soundfile.read(path, always_2d=True, dtype="float64")[0]
            total[: len(x)] += x.mean(axis=1)[:length]
        return total

    voice, _, _ = cells(summed(vocal_files), rate)
    band, _, _ = cells(summed(sorted((song / "stems").glob("*.wav"))), rate)
    return 10 * np.log10(voice + 1e-20) <= 10 * np.log10(band + 1e-20) - 15


def grade(song, master_path=None):
    song = Path(song)
    notes = json.loads((song / "notes.json").read_text())
    path = master_path or song / "renders" / "master.wav"
    if not Path(path).exists():
        path = song / "renders" / "master-failed-gates.wav"
    ours, rate = soundfile.read(path, always_2d=True, dtype="float64")
    official, orate = soundfile.read(song / "official.wav", always_2d=True, dtype="float64")
    assert rate == orate
    length = min(len(ours), len(official))
    ours, official = ours[:length].T, official[:length].T
    # Aligned to the raw stems, not to our master, so the official's numbers never move with our mix.
    stems_sum = np.zeros(length)
    for stem in sorted((song / "stems").glob("*.wav")):
        x = soundfile.read(stem, always_2d=True, dtype="float64")[0]
        stems_sum[: len(x)] += x.mean(axis=1)[:length]
    lag = align(stems_sum[None], official, rate, reach_s=0.02)
    # official[t + lag] matches ours[t]: shift the official onto our clock.
    official = np.roll(official, -lag, axis=1)
    co, freqs, zo = cells(ours, rate)
    cf, _, zf = cells(official, rate)
    frames = co.shape[1]
    mask = vocal_mask(song, notes, rate, length, frames)[:, :frames]
    # Loudness match on the scored cells only, K-weighted per bin.
    kw = k_weights(freqs, rate)
    def k_cells(z):
        p = ((np.abs(z) ** 2).sum(axis=0)) * kw[:, None]
        return np.array([p[(freqs >= c / 2 ** (1 / 6)) & (freqs < c * 2 ** (1 / 6))].sum(axis=0) for c in THIRD_CENTERS])
    ko, kf = k_cells(zo), k_cells(zf)
    gain_db = db(kf[mask].sum()) - db(ko[mask].sum())
    g = 10 ** (gain_db / 10)
    ours = ours * 10 ** (gain_db / 20)
    co = co * g
    centers = np.array(THIRD_CENTERS)

    def band_gap(lo, hi):
        rows = (centers >= lo) & (centers < hi)
        m = mask[rows]
        if not m.any():
            return None, 0.0
        return db(co[rows][m].sum()) - db(cf[rows][m].sum()), float(m.any(axis=0).sum()) * HOP_S

    bands, scored = {}, {}
    for c in OCTAVES:
        gap, secs = band_gap(c / 2 ** 0.5, c * 2 ** 0.5)
        bands[str(c)], scored[str(c)] = r2(gap), round(secs, 1)
    thirds = {str(c): r2(band_gap(c / 2 ** (1 / 6), c * 2 ** (1 / 6))[0]) for c in THIRDS}
    low_thirds = {str(c): r2(band_gap(c / 2 ** (1 / 6), c * 2 ** (1 / 6))[0]) for c in (40, 50, 63, 80, 100, 125, 160, 200, 250)}

    # Width from the side channel, which carries almost no vocal: side level per band, ours minus official.
    so, _, _ = cells((ours[0] - ours[1]) / 2, rate)
    sf, _, _ = cells((official[0] - official[1]) / 2, rate)
    mo, _, _ = cells((ours[0] + ours[1]) / 2, rate)
    mf, _, _ = cells((official[0] + official[1]) / 2, rate)
    width, width_abs = {}, {}
    for lo, hi in [(120, 16000)] + WIDTH_BANDS:
        rows = (centers >= lo) & (centers < hi)
        m = mask[rows]
        key = "all" if (lo, hi) == (120, 16000) else f"{lo}-{hi}"
        width[key] = r2(db(so[rows][m].sum()) - db(sf[rows][m].sum()))
        width_abs[key] = [r2(db(so[rows][m].sum()) - db(mo[rows][m].sum())), r2(db(sf[rows][m].sum()) - db(mf[rows][m].sum()))]

    def scored_hits(times, lo, hi):
        rows = (centers * 2 ** (1 / 6) > lo) & (centers / 2 ** (1 / 6) < hi)
        ok = mask[rows].all(axis=0)
        t = np.arange(frames) * HOP_S + NFFT / rate / 2
        keep = []
        for h in times:
            near = np.abs(t - h) <= 0.2
            if near.any() and ok[near].all() and 0.2 < h < length / rate - 0.2:
                keep.append(h)
        return np.array(keep)

    punch, raw = {}, {}
    for drum, bands_hz in (("kick", {"low": (40, 120), "click": (2000, 6000)}), ("snare", {"body": (150, 350), "crack": (2000, 8000)})):
        all_times = stem_hits(song, drum, rate, length)
        for name, (lo, hi) in bands_hz.items():
            times = scored_hits(all_times, lo, hi)
            eo, ef = env(bandpass(ours, rate, lo, hi), rate), env(bandpass(official, rate, lo, hi), rate)
            po_, pf_ = prominence(eo, times), prominence(ef, times)
            d = po_ - pf_
            ok = np.isfinite(d).sum() >= 4
            punch[f"{drum}_{name}"] = r2(np.nanmedian(d)) if ok else None
            raw[f"{drum}_{name}"] = [r2(np.nanmedian(po_)) if ok else None, r2(np.nanmedian(pf_)) if ok else None, int(np.isfinite(d).sum())]
            if drum == "kick" and name == "low" and ok:
                to, tf = tail(eo, times), tail(ef, times)
                punch["kick_low_tail"] = r2(np.nanmedian(to - tf))
                raw["kick_low_tail"] = [r2(np.nanmedian(to)), r2(np.nanmedian(tf))]

    lowmid = low_thirds["160"]
    def crest(x):
        frames_ = env(x, rate)
        return db(np.percentile(frames_, 99.5)) - db(frames_.mean())
    crest_gap = r2(crest(ours) - crest(official))
    punch_keys = [k for k in ("kick_low", "kick_click", "snare_body", "snare_crack") if punch.get(k) is not None]
    band_vals = [v for v in bands.values() if v is not None]
    gates = {"bands": max(abs(v) for v in band_vals) <= TOL["band"],
             "kick": all(abs(punch[k]) <= TOL["punch"] for k in punch_keys if k.startswith("kick")),
             "snare": all(abs(punch[k]) <= TOL["punch"] for k in punch_keys if k.startswith("snare")),
             "width": all(abs(v) <= TOL["width"] for v in width.values())}
    excess = (sum(max(0, abs(v) - TOL["band"]) for v in band_vals)
              + sum(max(0, abs(punch[k]) - TOL["punch"]) for k in punch_keys)
              + sum(max(0, abs(v) - TOL["width"]) for v in width.values()))
    total = (sum(abs(v) for v in band_vals) / TOL["band"] + sum(abs(punch[k]) for k in punch_keys) / TOL["punch"]
             + sum(abs(v) for v in width.values()) / TOL["width"])
    return {"song": song.name, "scored_s_by_octave": scored, "align_lag_samples": lag, "match_gain_db": r2(gain_db),
            "bands": bands, "thirds": thirds, "low_thirds": low_thirds,
            "punch": punch, "punch_raw_ours_official_n": raw, "width": width, "side_mid_ours_official": width_abs,
            "lowmid_160": lowmid, "crest": crest_gap, "gates": gates, "pass": all(gates.values()),
            "excess": r2(excess), "total": r2(total)}


def line(g):
    b = " ".join(f"{k}:{v:+.1f}" if v is not None else f"{k}:-" for k, v in g["bands"].items())
    p = " ".join(f"{k}:{v:+.1f}" for k, v in g["punch"].items() if v is not None)
    w = " ".join(f"{k}:{v:+.1f}" for k, v in g["width"].items())
    return (f"{g['song'][:24]:24s} pass={g['pass']!s:5s} excess={g['excess']:5.1f} total={g['total']:5.1f}\n"
            f"  bands {b}\n  scored_s {g['scored_s_by_octave']}\n  hi-thirds {g['thirds']}  lowmid160 {g['lowmid_160']}  crest {g['crest']}\n"
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
