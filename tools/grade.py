"""Grader: our master against the official excerpt, aligned and at matched loudness, on the graded spans only.

uv run tools/grade.py songs/<song> [more songs] [--master renders/master.wav] [--label text]

Gaps are ours minus official. The official is used only here, never by the pipeline.
"""
import argparse
import json
import time
from pathlib import Path

import numpy as np
import pyloudnorm
import soundfile
from scipy import signal

from punch import bandpass, db, drum_hits, env, prominence, tail

OCTAVES = [31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
THIRDS = [10000, 12500]
WIDTH_BANDS = [(120, 500), (500, 2000), (2000, 8000)]
MIN_SCORED_S = 8.0
MIN_HITS = 15
TOL = {"band": 2.0, "punch": 1.5, "width": 1.5}
# A hit is scored when adding the raw vocals to the raw instruments moves its prominence by at most this much, under a
# third of the punch tolerance. Prominence is tested itself, so a vocal in the attack peak or in the median around it counts.
HIT_VOCAL_BIAS_DB = 0.4


def r2(x):
    return None if x is None else round(float(x), 2)


def stem_hits(song, chain, rate, length):
    """Hit times (s) from the close-mic stems, found by name: the grader is test equipment, not the pipeline."""
    total = np.zeros(length)
    for path in sorted((song / "stems").glob("*.wav")):
        name = path.stem.lower()
        if chain in name and "sample" not in name:
            x = soundfile.read(path, always_2d=True, dtype="float64")[0]
            total[: len(x)] += x.mean(axis=1)[:length]
    return drum_hits(total, rate, chain)


def summed_mono(files, length):
    total = np.zeros(length)
    for path in files:
        x = soundfile.read(path, always_2d=True, dtype="float64")[0]
        total[: len(x)] += x.mean(axis=1)[:length]
    return total


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

    voice, _, _ = cells(summed_mono(vocal_files, length), rate)
    band, _, _ = cells(summed_mono(sorted((song / "stems").glob("*.wav")), length), rate)
    # No time margin for the official's vocal reverb and delays: a 0.3-1 s margin left 002 and 003 under 2 s scored
    # in every octave above 250 Hz. The cost is a bias: those effects count as instruments (night decisions N10).
    return 10 * np.log10(voice + 1e-20) <= 10 * np.log10(band + 1e-20) - 15


def grade(song, master_path=None):
    song = Path(song)
    notes = json.loads((song / "notes.json").read_text())
    path = master_path or song / "renders" / "master.wav"
    if not Path(path).exists():
        path = song / "renders" / "master-failed-gates.wav"
    ours, rate = soundfile.read(path, always_2d=True, dtype="float64")
    checks_file = song / "renders" / "checks.json"
    export_ok = json.loads(checks_file.read_text()).get("export_ok") if master_path is None and checks_file.exists() else None
    official, orate = soundfile.read(song / "official.wav", always_2d=True, dtype="float64")
    assert rate == orate
    length = min(len(ours), len(official))
    ours, official = ours[:length].T, official[:length].T
    # Aligned to the raw stems, not to our master, so the official's numbers never move with our mix.
    stems_sum = summed_mono(sorted((song / "stems").glob("*.wav")), length)
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
    width, width_abs, side_level, width_scored = {}, {}, {}, {}
    for lo, hi in [(120, 16000)] + WIDTH_BANDS:
        rows = (centers >= lo) & (centers < hi)
        m = mask[rows]
        key = "all" if (lo, hi) == (120, 16000) else f"{lo}-{hi}"
        width_scored[key] = round(float(m.any(axis=0).sum()) * HOP_S, 1)
        if width_scored[key] < 1:
            width[key], width_abs[key], side_level[key] = None, None, None
            continue
        ours_ratio = db(so[rows][m].sum()) - db(mo[rows][m].sum())
        off_ratio = db(sf[rows][m].sum()) - db(mf[rows][m].sum())
        # Width is side against mid in the same band, so a band that is only louder or quieter is not a width gap.
        width[key] = r2(ours_ratio - off_ratio)
        width_abs[key] = [r2(ours_ratio), r2(off_ratio)]
        side_level[key] = r2(db(so[rows][m].sum()) - db(sf[rows][m].sum()))

    vocal_files = sorted((song / "vocals").glob("*.wav")) if (song / "vocals").exists() else []
    vocals = summed_mono(vocal_files, length) if vocal_files else None
    spans = notes.get("graded_spans_s", [[0, length / rate]])

    def scored_hits(times, lo, hi):
        """Hits away from the edges; with vocals, only those whose prominence the raw vocals barely move in this band.
        The 0.1 s cells of the band mask are too coarse here: a sung line covers nearly every snare hit in some cell."""
        times = np.array([h for h in times if 0.2 < h < length / rate - 0.2])
        if vocals is None:
            return np.array([h for h in times if any(a <= h - 0.13 and h + 0.15 <= b for a, b in spans)])
        # The summed waveform, not summed powers: a vocal can also cancel an instrument for a moment.
        ei, eiv = env(bandpass(stems_sum[None], rate, lo, hi), rate), env(bandpass((stems_sum + vocals)[None], rate, lo, hi), rate)
        bias = prominence(eiv, times) - prominence(ei, times)
        return times[np.abs(bias) <= HIT_VOCAL_BIAS_DB]

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
    def judge(band_items, punch_items, width_items):
        def gate(values, tol):
            # No value scored well enough means unmeasured (None), never a pass.
            return all(abs(v) <= tol for v in values) if values else None

        gates = {"bands": gate(band_items, TOL["band"]),
                 "kick": gate([v for k, v in punch_items if k.startswith("kick")], TOL["punch"]),
                 "snare": gate([v for k, v in punch_items if k.startswith("snare")], TOL["punch"]),
                 "width": gate(width_items, TOL["width"])}
        excess = (sum(max(0, abs(v) - TOL["band"]) for v in band_items) + sum(max(0, abs(v) - TOL["punch"]) for _, v in punch_items)
                  + sum(max(0, abs(v) - TOL["width"]) for v in width_items))
        total = (sum(abs(v) for v in band_items) / TOL["band"] + sum(abs(v) for _, v in punch_items) / TOL["punch"]
                 + sum(abs(v) for v in width_items) / TOL["width"])
        return gates, excess, total

    punch_keys = [k for k in ("kick_low", "kick_click", "snare_body", "snare_crack") if punch.get(k) is not None]
    strict = judge([v for v in bands.values() if v is not None], [(k, punch[k]) for k in punch_keys],
                   [v for v in width.values() if v is not None])
    # A band scored on under 8 s, or a punch on under 15 hits, is reported but does not gate: a few hits can read several dB off.
    confident = judge([v for k, v in bands.items() if v is not None and scored[k] >= MIN_SCORED_S],
                      [(k, punch[k]) for k in punch_keys if raw[k][2] >= MIN_HITS],
                      [v for k, v in width.items() if v is not None and width_scored[k] >= MIN_SCORED_S])
    low_confidence = [k for k, v in bands.items() if v is not None and scored[k] < MIN_SCORED_S] + \
                     [k for k in punch_keys if raw[k][2] < MIN_HITS] + \
                     [f"width {k}" for k, v in width.items() if v is not None and width_scored[k] < MIN_SCORED_S]
    # A drum measure with too few scored hits is not a pass: it is unmeasured, and the grade says so.
    unmeasured = [k for k in ("kick_low", "kick_click", "snare_body", "snare_crack") if punch.get(k) is None]
    gates, excess, total = confident
    return {"song": song.name, "scored_s_by_octave": scored, "align_lag_samples": lag, "match_gain_db": r2(gain_db),
            "bands": bands, "thirds": thirds, "low_thirds": low_thirds,
            "punch": punch, "punch_raw_ours_official_n": raw, "width": width, "side_mid_ours_official": width_abs, "side_level_diagnostic": side_level, "width_scored_s": width_scored,
            "lowmid_160": lowmid, "crest": crest_gap, "gates": gates, "pass": all(v is True for v in gates.values()) and export_ok is not False,
            "pass_measured": all(v is not False for v in gates.values()) and export_ok is not False,
            "excess": r2(excess), "total": r2(total), "low_confidence": low_confidence, "unmeasured": unmeasured,
            "graded_file": str(Path(path).name), "export_ok": export_ok,
            "pass_strict": all(v is True for v in strict[0].values()) and export_ok is not False and not unmeasured, "excess_strict": r2(strict[1]), "total_strict": r2(strict[2])}


def line(g):
    b = " ".join(f"{k}:{v:+.1f}" if v is not None else f"{k}:-" for k, v in g["bands"].items())
    p = " ".join(f"{k}:{v:+.1f}" for k, v in g["punch"].items() if v is not None)
    w = " ".join(f"{k}:{v:+.1f}" if v is not None else f"{k}:-" for k, v in g["width"].items())
    return (f"{g['song'][:24]:24s} pass={g['pass']!s:5s} (measured-only {g['pass_measured']!s:5s}) excess={g['excess']:5.1f} total={g['total']:5.1f} "
            f"| strict pass={g['pass_strict']!s:5s} excess={g['excess_strict']:5.1f} low-confidence {g['low_confidence']} "
            f"unmeasured {g['unmeasured']} file {g['graded_file']} export_ok {g['export_ok']}\n"
            f"  bands {b}\n  scored_s {g['scored_s_by_octave']}\n  hi-thirds {g['thirds']}  lowmid160 {g['lowmid_160']}  crest {g['crest']}\n"
            f"  punch {p}\n  width {w}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("songs", nargs="+")
    parser.add_argument("--master")
    parser.add_argument("--label", default="")
    args = parser.parse_args()
    results = [grade(s, args.master) for s in args.songs]
    for g in results:
        print(line(g))
    summary = {"time": time.strftime("%Y-%m-%d %H:%M"), "label": args.label,
               "excess_sum": r2(sum(g["excess"] for g in results)), "total_sum": r2(sum(g["total"] for g in results)),
               "results": results}
    summary["excess_strict_sum"] = r2(sum(g["excess_strict"] for g in results))
    print(f"SUM excess={summary['excess_sum']} total={summary['total_sum']} all_pass={all(g['pass'] for g in results)} "
          f"| strict excess={summary['excess_strict_sum']} all_pass={all(g['pass_strict'] for g in results)}")


if __name__ == "__main__":
    main()
