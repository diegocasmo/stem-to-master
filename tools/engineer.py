"""The engineer: decides the recipe from measurements, in signal-flow order, judging every move on its own before/after.

Each move follows docs/mixing-chains: its Needed-if decides whether it is tried, a rule computes its settings, and its
Worked-if (measured on the insert's own input and output) decides whether it is kept. Every decision is recorded.
"""
import json
import math
import multiprocessing
from pathlib import Path

import numpy as np
from scipy import signal

import daw
import meters
import measure as measurelib
import notes as notelib

HOLD_DB = 0.5
ZL_EQ = "ZL Equalizer 2"
ZL_COMP = "ZL Compressor"


def zl_eq(*bands):
    """ZL Equalizer 2 settings for bands given as dicts: type, freq, gain, q, order, mode."""
    settings = {}
    for index, band in enumerate(bands):
        settings |= {
            f"filter_status{index}_filter_status": "On",
            f"filter_type{index}_filter_type": band["type"],
            f"order{index}_order": band.get("order", "12 dB/oct"),
            f"lrmode{index}_lrmode": band.get("mode", "Stereo"),
            f"freq{index}_freq": float(band["freq"]),
            f"gain{index}_gain": float(band.get("gain", 0.0)),
            f"q{index}_q": float(band.get("q", 0.707)),
        }
    return settings


def zl_comp(threshold, ratio, attack, release, knee=6.0, style="Clean"):
    return {"compressor_style_compressor_style": style, "threshold_db_threshold_db": float(round(threshold, 1)),
            "ratio_ratio": float(ratio), "attack_attack": float(attack), "release_release": float(release),
            "knee_width_knee_width": float(knee)}


def r2(value):
    return None if value is None else round(float(value), 2)


class Node:
    """A signal being decided: its audio, key, the hit map found once on its input, and the inserts kept so far."""

    def __init__(self, name, audio, key, cache, rate, sections, hits=None):
        self.name, self.audio, self.key, self.cache, self.rate, self.sections = name, audio, key, cache, rate, sections
        self.hits = hits
        self.inserts, self.decisions = [], []

    def spec(self, audio=None, key=None):
        audio, key = (self.audio, self.key) if audio is None else (audio, key)
        found = self.cache.measured(key, "spectrum", lambda: [x.tolist() for x in meters.spectrum(audio, self.rate)])
        return np.array(found[0]), np.array(found[1])

    def band(self, low, high, audio=None, key=None):
        return meters.band_db(self.spec(audio, key), low, high)

    def hit_stats(self, audio=None, key=None):
        audio, key = (self.audio, self.key) if audio is None else (audio, key)
        times = self.hits
        return self.cache.measured(key, daw.digest("hits", times.tolist()), lambda: meters.hit_stats(audio, self.rate, times))

    def render(self, insert):
        out, key = daw.chain(self.cache, self.rate, self.key, lambda: self.audio, [insert], sections=self.sections)
        return out, key

    def record(self, move, outcome, numbers, insert=None):
        entry = {"move": move, "target": self.name, "outcome": outcome, "numbers": numbers}
        if insert is not None:
            entry["insert"] = insert["plugin"]
        self.decisions.append(entry)

    def attempt(self, move, needed, numbers, candidates, judge):
        """Tries candidate inserts in order and keeps the first whose judge passes. Returns True if one was kept."""
        if not needed:
            self.record(move, "dropped: not needed", numbers)
            return False
        tried = []
        for insert in candidates:
            insert = {"id": move, **insert}
            out, key = self.render(insert)
            ok, result = judge(out, key)
            tried.append({"settings": insert["settings"], **result, "kept": ok})
            if ok:
                self.audio, self.key = out, key
                self.inserts.append(insert)
                self.record(move, "kept", numbers | {"tried": tried}, insert)
                return True
        self.record(move, "dropped: did not work", numbers | {"tried": tried})
        return False


# ---------- per-move rules ----------

def hits_change(node, out, key):
    before, after = node.hit_stats(), node.hit_stats(out, key)
    if before["hits"] == 0:
        return {}
    level = np.array(after["level_db"]) - np.array(before["level_db"])
    attack = np.array(after["attack_db"]) - np.array(before["attack_db"])
    return {"level_change_min": r2(level.min()), "level_change_p50": r2(np.median(level)),
            "attack_change_p10": r2(np.percentile(attack, 10)), "attack_change_p50": r2(np.median(attack)),
            "bleed_change": r2(after["bleed_db"]["all"] - before["bleed_db"]["all"])}


def steep_hpf(node, move, cutoff_candidates, low_band, keep_band, order="24 dB/oct", fall_db=1.0, hold_db=HOLD_DB):
    """A steep high-pass, kept if the energy below falls and the band to keep holds."""
    below, keep = node.band(*low_band), node.band(*keep_band)

    def judge(out, key):
        b, k = node.band(*low_band, out, key), node.band(*keep_band, out, key)
        ok = below - b >= fall_db and keep - k <= hold_db
        return ok, {"below_change": r2(b - below), "keep_change": r2(k - keep)}

    return [{"plugin": ZL_EQ, "settings": zl_eq({"type": "High Pass", "freq": f, "order": order})} for f in cutoff_candidates], judge, \
        {"below_db": r2(below), "keep_db": r2(keep)}


def gate_from_hits(node, move, floor_rel_db=-25.0):
    """kick.gate / toms.clean: gate between hits when off-hit bleed is within 25 dB of the hits."""
    stats = node.hit_stats()
    if stats["hits"] == 0:
        node.record(move, "dropped: no hits", {})
        return
    bleed = stats["bleed_db"]["all"]
    levels = np.array(stats["level_db"])
    gaps = np.diff(node.hits) / node.rate * 1000
    fastest = float(np.percentile(gaps, 5)) if len(gaps) else 200.0
    numbers = {"bleed_db": r2(bleed), "softest_hit_db": r2(levels.min()), "fastest_gap_ms": r2(fastest)}
    candidates = [{"plugin": "NoiseGate", "settings": {"threshold_db": float(round(levels.min() - margin, 1)), "ratio": 10.0,
                                                        "attack_ms": 0.5, "release_ms": float(round(min(80, fastest * 0.5), 1))}}
                  for margin in (6, 9, 12)]

    def judge(out, key):
        change = hits_change(node, out, key)
        ok = change["bleed_change"] <= -1 and change["level_change_min"] >= -1 and change["attack_change_p10"] >= -1
        return ok, change

    node.attempt(move, bleed > floor_rel_db, numbers, candidates, judge)


def narrow_peaks(node, low, high, rise_db=6.0):
    """Peaks in the long-term spectrum more than rise_db over the 1/3-octave neighbourhood."""
    freqs, power = node.spec()
    level = 10 * np.log10(np.maximum(power, 1e-20))
    found = []
    for i in np.flatnonzero((freqs >= low) & (freqs < high)):
        f = freqs[i]
        around = (freqs >= f / 2 ** (1 / 3)) & (freqs <= f * 2 ** (1 / 3)) & (np.abs(freqs - f) > f * 0.06)
        if around.any() and level[i] == level[(freqs >= f / 1.06) & (freqs <= f * 1.06)].max():
            rise = level[i] - np.median(level[around])
            if rise > rise_db:
                found.append((float(f), float(rise)))
    return sorted(found, key=lambda x: -x[1])


def compress_hits(node, move, ratio, attack, release, spread_db=3.0, low_attack_db=None, targets=(4.0, 6.0)):
    """kick/snare.compress: needed if hit levels spread more than 3 dB in the dense section, or attack is low."""
    stats = node.hit_stats()
    if stats["hits"] < 4:
        node.record(move, "dropped: too few hits", {"hits": stats["hits"]})
        return
    levels, attacks = np.array(stats["level_db"]), np.array(stats["attack_db"])
    spread = float(np.percentile(levels, 90) - np.percentile(levels, 10))
    attack_p50 = float(np.median(attacks))
    for_attack = low_attack_db is not None and attack_p50 < low_attack_db
    needed = spread > spread_db or for_attack
    numbers = {"hit_spread_db": r2(spread), "attack_p50_db": r2(attack_p50)}
    peak_db = float(np.median(levels))
    candidates = [{"plugin": ZL_COMP, "settings": zl_comp(peak_db - t, ratio, attack, release)} for t in targets]
    if for_attack:
        candidates += [{"plugin": ZL_COMP, "settings": zl_comp(peak_db - t, ratio, attack * 1.5, release)} for t in targets]

    def judge(out, key):
        after = node.hit_stats(out, key)
        lv, at = np.array(after["level_db"]), np.array(after["attack_db"])
        new_spread = float(np.percentile(lv, 90) - np.percentile(lv, 10))
        result = {"hit_spread_db": r2(new_spread), "attack_p50_db": r2(np.median(at))}
        if for_attack:
            ok = np.median(at) > attack_p50 + HOLD_DB and new_spread <= spread + HOLD_DB
        else:
            ok = new_spread < spread - HOLD_DB and np.median(at) >= attack_p50 - HOLD_DB
        return ok, result

    node.attempt(move, needed, numbers, candidates, judge)


def eq_cut_if(node, move, needed, numbers, freq, gains, q, band, keep=None):
    """A bell cut, kept if its band falls and an optional band to keep holds."""
    before = node.band(*band)
    kept_before = node.band(*keep) if keep else None

    def judge(out, key):
        after = node.band(*band, out, key)
        result = {"band_change": r2(after - before)}
        ok = after < before - HOLD_DB
        if keep:
            k = node.band(*keep, out, key)
            result["keep_change"] = r2(k - kept_before)
            ok = ok and kept_before - k <= HOLD_DB
        return ok, result

    node.attempt(move, needed, numbers, [{"plugin": "PeakFilter", "settings": {"cutoff_frequency_hz": float(round(freq, 1)),
                                                                                 "gain_db": float(g), "q": float(q)}} for g in gains], judge)


def tom_edit(node, move):
    """toms.clean as an edit: keep each tom only around its own hits, with its decay, via a Ride (mute automation)."""
    stats = node.hit_stats()
    if stats["hits"] == 0:
        node.record(move, "dropped: no hits", {})
        return
    levels = np.array(stats["level_db"])
    own = node.hits[levels >= levels.max() - 20]
    bleed = stats["bleed_db"]["all"]
    numbers = {"bleed_db": r2(bleed), "own_hits": len(own), "detected": int(stats["hits"])}
    rate, length = node.rate, node.audio.shape[1]
    env = meters.filtered(node.audio, rate, 40, None)
    power = np.convolve((env**2).sum(axis=0), np.ones(441) / 441, mode="same")
    keep = []
    for t in own:
        peak = power[t : t + int(0.05 * rate)].max()
        tail = np.flatnonzero(power[t : t + int(2.0 * rate)] < peak * 10 ** (-40 / 10))
        end = t + (tail[tail > int(0.3 * rate)][0] if (tail > int(0.3 * rate)).any() else int(2.0 * rate))
        keep.append([max(0, t - int(0.01 * rate)), min(length, end)])
    merged = []
    for start, end in keep:
        if merged and start <= merged[-1][1] + int(0.05 * rate):
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    segments, last = [], 0
    for start, end in merged:
        if start - last > int(0.03 * rate):
            segments.append([round(last / rate, 4), round(start / rate, 4), -80])
        last = end
    if length - last > int(0.03 * rate):
        segments.append([round(last / rate, 4), round(length / rate, 4), -80])
    times_backup = node.hits
    node.hits = own

    def judge(out, key):
        change = hits_change(node, out, key)
        ok = change["bleed_change"] <= -1 and change["level_change_min"] >= -1 and change["attack_change_p10"] >= -1
        return ok, change

    node.attempt(move, bleed > -25 and segments, numbers, [{"plugin": "Ride", "settings": {"segments": segments, "ramp_ms": 10}}], judge)
    node.hits = times_backup


def measure_valley(audio, times, rate):
    found = measurelib.valley.__wrapped__(audio.T.astype(np.float64), rate, np.asarray(times))
    return found["valley_db"]["p50"] if found["valley_db"] else -200.0


def overlap_db(first, second, rate, band, inside=None):
    found = measurelib.overlap.__wrapped__(first.T.astype(np.float64), second.T.astype(np.float64), rate, [band], inside)
    value = next(iter(found["bands"].values()))["overlap_db"]
    return -200.0 if value is None else value


# ---------- per-track decisions (run in workers) ----------

def decide_track(args):
    song, cache_folder, rate, length, track, chain_name, sections, dense = args
    cache = daw.Cache(cache_folder)
    key, source = daw.stem_source(song, track, length)
    audio = cache.get(key)
    if audio is None:
        audio = source()
        cache.put(key, audio)
    node = Node(track["name"], audio, key, cache, rate, sections)
    stem = track["stem"]
    if chain_name == "toms":
        node.hits = meters.detect_hits(audio, rate)
        tom_edit(node, "toms.clean")
    elif chain_name == "snare":
        top = "Up" in stem
        cutoff = 70 if top else 90
        node.hits = meters.detect_hits(audio, rate)
        below, body = node.band(20, cutoff), node.band(150, 250)
        cands, judge, numbers = steep_hpf(node, "snare.hpf-lpf", [cutoff], (20, cutoff), (150, 250), order="12 dB/oct")
        node.attempt("snare.hpf-lpf", below > body - 20, numbers | {"body_db": r2(body)}, cands, judge)
    elif stem.startswith("15_Overheads"):
        low, cym = node.band(20, 250), node.band(2000, 8000)
        cands, judge, numbers = steep_hpf(node, "overheads.hpf", [150, 220], (20, 150), (2000, 8000), order="12 dB/oct", fall_db=3)
        node.attempt("overheads.hpf", low > cym - 10, numbers | {"cymbal_db": r2(cym)}, cands, judge)
    elif stem.startswith("13_HiHat"):
        cands, judge, numbers = steep_hpf(node, "overheads.hihat-blend", [400], (20, 300), (4000, 12000), order="12 dB/oct", fall_db=3)
        node.attempt("overheads.hihat-blend", True, numbers, cands, judge)
    elif stem.startswith("16_DrumsRoom"):
        decide_room(node)
    elif chain_name == "bass":
        decide_bass(node, dense)
    elif chain_name == "guitars":
        decide_guitar(node)
    elif chain_name == "synths":
        main = max(((lo, lo * 2) for lo in (125, 250, 500, 1000, 2000, 4000)), key=lambda b: node.band(*b))
        low = node.band(20, 150)
        total = node.band(20, 20000)
        cands, judge, numbers = steep_hpf(node, "synths.hpf", [150, 120], (20, 150), main, order="12 dB/oct", fall_db=3)
        node.attempt("synths.hpf", low > total - 20, numbers | {"total_db": r2(total), "main_band": list(main)}, cands, judge)
    return track["name"], node.inserts, node.decisions


def decide_room(node):
    """overheads.room-blend: low-pass the cymbal spill, cut the mud, then heavy compression with a fast release."""
    cym, total = node.band(6000, 20000), node.band(20, 20000)
    node.attempt("overheads.room-blend.lpf", cym > total - 20, {"above_6k_db": r2(cym), "total_db": r2(total)},
                 [{"plugin": ZL_EQ, "settings": zl_eq({"type": "Low Pass", "freq": 7000, "order": "12 dB/oct"})}],
                 lambda out, key: ((r := node.band(6000, 20000, out, key)) < cym - 3, {"above_6k_change": r2(r - cym)}))
    mud, body = node.band(200, 400), node.band(80, 160)
    eq_cut_if(node, "overheads.room-blend.cut", mud > body - 3, {"200_400_db": r2(mud), "80_160_db": r2(body)}, 300, [-4], 0.8,
              (200, 400))
    times = meters.detect_hits(node.audio, node.rate)
    node.hits = times
    stats = node.hit_stats()
    level = float(np.median(stats["level_db"])) if stats["hits"] else -20.0
    tail_before = float(np.median(stats["tail_db"])) if stats["hits"] else 0.0

    def judge(out, key):
        after = node.hit_stats(out, key)
        tail = float(np.median(after["tail_db"]))
        return tail > tail_before + 1, {"tail_p50_db": r2(tail), "tail_before_db": r2(tail_before)}

    node.attempt("overheads.room-blend.compress", True, {"hit_level_p50_db": r2(level)},
                 [{"plugin": ZL_COMP, "settings": zl_comp(level - 12, 8, 5, 40)}], judge)


def decide_bass(node, dense):
    rate = node.rate
    mono = node.audio.T.astype(np.float64)
    onsets = notelib.onsets(mono, rate)
    found = node.cache.measured(node.key, "notes", lambda: notelib.measure(mono, rate, onsets))
    f0s = np.array([n["f0_hz"] for n in found if "f0_hz" in n])
    lowest = float(np.percentile(f0s, 3)) if len(f0s) else 41.0
    below, fund = node.band(15, lowest * 0.85), node.band(lowest * 0.9, lowest * 1.12)
    cands, judge, numbers = steep_hpf(node, "bass.hpf", [round(lowest * 0.7, 1)], (15, lowest * 0.7), (lowest * 0.95, lowest * 1.6),
                                      order="24 dB/oct", fall_db=1, hold_db=1.0)
    node.attempt("bass.hpf", below > fund - 10, numbers | {"lowest_f0_hz": r2(lowest), "fundamental_db": r2(fund)}, cands, judge)
    # bass.rides and bass.compress read note levels in the dense passage.
    start, end = dense
    levels = np.array([n["level_db"] for n in found if "level_db" in n and start * rate <= n["start"] < end * rate])
    spread = float(np.percentile(levels, 90) - np.percentile(levels, 10)) if len(levels) > 4 else 0.0
    node.record("bass.rides", "dropped: compression evens the notes" if spread > 3 else "dropped: not needed",
                {"note_spread_db": r2(spread), "notes": int(len(levels)),
                 "why": "single-note rides are not built; the two compressors are judged on the same spread"})
    level = float(np.median(levels)) if len(levels) else -20.0
    note_starts = np.array([n["start"] for n in found if "level_db" in n and start * rate <= n["start"] < end * rate])

    def note_spread(audio):
        power = (audio.astype(np.float64) ** 2).sum(axis=0)
        cum = np.concatenate([[0.0], np.cumsum(power)])
        span = int(0.1 * rate)
        values = 10 * np.log10(np.maximum((cum[note_starts + span] - cum[note_starts]) / span, 1e-20))
        return float(np.percentile(values, 90) - np.percentile(values, 10))

    before = note_spread(node.audio)

    def judge(out, key):
        after = note_spread(out)
        return after < before - HOLD_DB, {"note_spread_db": r2(after)}

    node.attempt("bass.compress", spread > 3, {"note_spread_db": r2(before), "note_level_p50_db": r2(level)},
                 [{"plugin": ZL_COMP, "settings": zl_comp(level - 8, 4, 3, 60)}], judge)


def decide_guitar(node):
    palm, low = node.band(80, 120), node.band(20, 70)
    best = None
    for cutoff in (100, 80, 60):
        insert = {"id": "guitars.filters", "plugin": ZL_EQ, "settings": zl_eq({"type": "High Pass", "freq": cutoff, "order": "24 dB/oct"})}
        out, key = node.render(insert)
        if palm - node.band(80, 120, out, key) <= 1.0:
            best = cutoff
            break
    cands = [{"plugin": ZL_EQ, "settings": zl_eq({"type": "High Pass", "freq": best, "order": "24 dB/oct"})}] if best else []

    def judge(out, key):
        p, l = node.band(80, 120, out, key), node.band(20, 70, out, key)
        return l < low - HOLD_DB and palm - p <= 1.0, {"palm_change": r2(p - palm), "below_70_change": r2(l - low)}

    node.attempt("guitars.filters", True, {"80_120_db": r2(palm), "below_70_db": r2(low), "highest_cutoff_holding_palm": best},
                 cands, judge)
    peaks = [p for p in narrow_peaks(node, 100, 250) + narrow_peaks(node, 4000, 10000)]
    if peaks:
        f, rise = peaks[0]
        eq_cut_if(node, "guitars.resonance", True, {"peak_hz": r2(f), "rise_db": r2(rise)}, f, [-min(6, rise - 3)], 6.0,
                  (f / 1.06, f * 1.06), keep=(1000, 4000))
    else:
        node.record("guitars.resonance", "dropped: not needed", {"narrow_peaks_over_6db": 0})


# ---------- the engineer ----------

class Engineer:
    def __init__(self, song, workers=2):
        self.song = Path(song)
        self.facts = json.loads((self.song / "song.json").read_text())
        self.cache = daw.Cache(self.song / "cache")
        self.workers = workers
        self.profile = json.loads((daw.ROOT / "profiles" / self.facts["profile"] / "profile.json").read_text())
        self.sections = self.facts["sections"]
        dense = self.sections[self.facts["dense"]]
        self.dense = (dense["start_s"], dense["end_s"])
        self.decisions = []
        self.recipe = self.skeleton()
        self.length = daw.length_of(self.song, self.recipe)
        self.rate = 44100

    def skeleton(self):
        tracks, buses = [], []
        for group, source in self.facts["sources"].items():
            pans = source.get("pan", [0] * len(source["stems"]))
            for stem, pan in zip(source["stems"], pans):
                tracks.append({"name": stem[:-4], "stem": stem, "chain": source["chain"], "group": group, "inserts": [],
                               "fader_db": 0.0, "pan": pan, "out": group, "sends": []})
        drums = ["kick", "snare", "toms", "cymbals", "room"]
        for group in self.facts["sources"]:
            if group == "kick-sample":
                continue
            buses.append({"name": group, "inserts": [], "fader_db": 0.0, "pan": 0, "out": "drums" if group in drums else "mix", "sends": []})
        for track in tracks:
            if track["group"] == "kick-sample":
                track["out"] = "kick"
        buses.insert(len(drums), {"name": "drums", "inserts": [], "fader_db": 0.0, "pan": 0, "out": "mix", "sends": []})
        return {"profile": self.facts["profile"], "sections": self.sections, "tracks": tracks, "buses": buses,
                "mix": {"inserts": []}, "master": {"inserts": []}, "decisions": []}

    def track(self, name):
        return next(t for t in self.recipe["tracks"] if t["name"] == name)

    def bus(self, name):
        return next(b for b in self.recipe["buses"] if b["name"] == name)

    def log(self, move, target, outcome, numbers):
        self.recipe["decisions"].append({"move": move, "target": target, "outcome": outcome, "numbers": numbers})

    # Stage 1: polarity and timing of every multi-mic source.
    def align(self):
        for group, source in self.facts["sources"].items():
            if "align" not in source:
                continue
            bands = source["align"]
            reference = self.track(source["stems"][0][:-4])
            key, read = daw.stem_source(self.song, reference, self.length)
            ref = read()[0].astype(np.float64)
            times = meters.detect_hits(ref[None], self.rate)
            inside = meters.windows(len(ref), times, self.rate, (0, 100))
            running = ref.copy()
            for stem in source["stems"][1:]:
                track = self.track(stem[:-4])
                skey, sread = daw.stem_source(self.song, track, self.length)
                result = self.cache.measured(daw.digest(key, skey, bands, "align-v1"), "align",
                                             lambda: self.best_alignment(running, sread()[0].astype(np.float64), inside, bands))
                track["invert"], track["offset_samples"] = result["invert"], result["offset_samples"]
                self.log(f"{source['chain']}.blend" if source["chain"] != "bass" else "bass.align", track["name"],
                         "kept" if result["invert"] or result["offset_samples"] else "dropped: as recorded is best", result)
                _, aligned = daw.stem_source(self.song, track, self.length)
                running = running + aligned()[0].astype(np.float64)

    def best_alignment(self, ref, other, inside, bands):
        rate = self.rate
        a = np.where(inside, ref, 0)
        b = np.where(inside, other, 0)
        corr = signal.correlate(b, a, mode="full", method="fft")
        mid, reach = len(a) - 1, int(0.006 * rate)
        lag = int(np.argmax(np.abs(corr[mid - reach : mid + reach + 1]))) - reach
        filtered_ref = [meters.filtered(ref[None], rate, *band)[0][inside] for band in bands]

        def sum_gain(invert, lag):
            moved = np.roll(other, -lag) * (-1 if invert else 1)
            gains = []
            for band, fr in zip(bands, filtered_ref):
                fo = meters.filtered(moved[None], rate, *band)[0][inside]
                gains.append(10 * math.log10(((fr + fo) ** 2).sum() / ((fr**2).sum() + (fo**2).sum())))
            return gains

        options = [(inv, l, sum_gain(inv, l)) for inv in (False, True) for l in sorted({0, lag})]
        # The first band (the body) decides; the second only breaks near-ties.
        best = max(options, key=lambda o: (round(o[2][0], 1), o[2][1]))
        return {"invert": best[0], "offset_samples": -best[1],
                "sum_gain_db": {f"{inv}/{-l}": [r2(g) for g in gains] for inv, l, gains in options}}

    # Stage 2: per-track inserts, in workers.
    def tracks_stage(self, pool):
        jobs = [(str(self.song), str(self.cache.folder), self.rate, self.length, t, t["chain"], self.sections, self.dense)
                for t in self.recipe["tracks"]]
        results = pool.map(decide_track, jobs) if pool else map(decide_track, jobs)
        for name, inserts, decisions in results:
            self.track(name)["inserts"] = inserts
            self.recipe["decisions"].extend(decisions)

    def mixer(self):
        return daw.Mixer(self.song, self.recipe, self.cache, self.workers)

    def bus_input(self, mixer, name):
        sources = self.recipe["tracks"] + self.recipe["buses"]
        audio, key = mixer.summed(name, sources)
        return audio.astype(np.float32), key

    def node_for_bus(self, mixer, name, hits_from=None):
        audio, key = self.bus_input(mixer, name)
        hits = meters.detect_hits(audio if hits_from is None else hits_from, self.rate)
        return Node(name, audio, key, self.cache, self.rate, self.sections, hits)

    def finish(self, node):
        self.bus(node.name)["inserts"].extend(node.inserts)
        self.recipe["decisions"].extend(node.decisions)

    # Stage 3: within-group balance, then group-bus inserts, each on its own measured input.
    def groups_stage(self, pool):
        mixer = self.mixer()
        mixer.render(pool, stop_at_mix=True)
        self.within_groups(mixer)
        mixer = self.mixer()
        mixer.render(pool, stop_at_mix=True)
        kick = self.node_for_bus(mixer, "kick")
        gate_from_hits(kick, "kick.gate")
        cands, judge, numbers = steep_hpf(kick, "kick.hpf", [36], (20, 40), (50, 70))
        kick.attempt("kick.hpf", kick.band(20, 40) > kick.band(50, 70) - 6, numbers, cands, judge)
        mud, thump = kick.band(200, 400), kick.band(50, 70)
        peaks = narrow_peaks(kick, 200, 400, rise_db=3)
        eq_cut_if(kick, "kick.mud-cut", mud > thump - 6 or bool(peaks), {"200_400_db": r2(mud), "50_70_db": r2(thump), "peaks": peaks[:2]},
                  peaks[0][0] if peaks else 300, [-4], 1.0, (200, 400), keep=(50, 70))
        compress_hits(kick, "kick.compress", 6, 30, 60, low_attack_db=3)
        self.finish(kick)

        snare = self.node_for_bus(mixer, "snare")
        peaks = narrow_peaks(snare, 300, 900)
        body, low_mid = snare.band(150, 250), snare.band(200, 500)
        if peaks:
            eq_cut_if(snare, "snare.ring-cut", True, {"peak_hz": r2(peaks[0][0]), "rise_db": r2(peaks[0][1])}, peaks[0][0], [-5], 4.0,
                      (peaks[0][0] / 1.08, peaks[0][0] * 1.08), keep=(150, 250))
        else:
            eq_cut_if(snare, "snare.ring-cut", low_mid > body - 3, {"200_500_db": r2(low_mid), "150_250_db": r2(body)}, 350, [-3], 1.0,
                      (300, 450), keep=(150, 250))
        compress_hits(snare, "snare.compress", 4, 20, 80, low_attack_db=4)
        self.finish(snare)

        bass = self.node_for_bus(mixer, "bass")
        self.recipe["decisions"].append({"move": "bass.split", "target": "bass", "outcome": "deferred to context stage", "numbers": {
            "700_3k_db": r2(bass.band(700, 3000)), "40_250_db": r2(bass.band(40, 250))}})
        self.finish(bass)
        for name in ("toms", "cymbals", "room", "gtr", "synths"):
            self.finish(Node(name, None, None, self.cache, self.rate, self.sections))


    def align_stage(self, pool):
        self.align()

    def stages(self):
        return ["align_stage", "tracks_stage", "groups_stage", "balance_stage", "drums_stage", "context_stage",
                "profile_stage", "punch_stage", "mix_stage", "master_stage"]

    # ---------- helpers on rendered nodes ----------

    def dense_spans(self):
        return [(s["start_s"], s["end_s"]) for s in self.sections.values() if s.get("group") in ("riff", "drive")]

    def post_fader(self, mixer, name):
        node = next(n for n in self.recipe["tracks"] + self.recipe["buses"] if n["name"] == name)
        return daw.pan_gain(mixer.node_audio(name), node.get("fader_db", 0), node.get("pan", 0))

    def loud(self, audio, spans):
        return meters.span_lufs(audio, self.rate, spans)

    def within_groups(self, mixer):
        """kick.blend (sample level), guitars.pair, overheads.hihat-blend: balance inside each group before its bus."""
        rate = self.rate
        mics = sum(daw.pan_gain(mixer.node_audio(t["name"]), 0, t.get("pan", 0)) for t in self.recipe["tracks"] if t["group"] == "kick")
        sample = mixer.node_audio("04_KickSample")
        mic_hits = meters.hit_stats(mics.astype(np.float32), rate, meters.detect_hits(mics.astype(np.float32), rate))
        sample_hits = meters.hit_stats(sample, rate, meters.detect_hits(sample, rate))
        gain = float(np.median(mic_hits["level_db"]) - np.median(sample_hits["level_db"]))
        self.track("04_KickSample")["fader_db"] = round(gain, 2)
        self.log("kick.blend", "04_KickSample", "kept", {"mic_hit_p50_db": r2(np.median(mic_hits["level_db"])),
                                                          "sample_hit_p50_db": r2(np.median(sample_hits["level_db"])), "fader_db": r2(gain)})
        spans = self.dense_spans()
        for left, right in (("20_ElecGtr1", "21_ElecGtr1DT"), ("22_ElecGtr2", "23_ElecGtr2DT")):
            l, r = self.loud(mixer.node_audio(left), spans), self.loud(mixer.node_audio(right), spans)
            self.track(left)["fader_db"] = round((r - l) / 2, 2)
            self.track(right)["fader_db"] = round((l - r) / 2, 2)
            self.log("guitars.pair", f"{left}+{right}", "kept" if abs(l - r) >= 0.2 else "dropped: already equal",
                     {"left_lufs": r2(l), "right_lufs": r2(r)})
        main = self.loud(mixer.node_audio("20_ElecGtr1"), spans) + self.track("20_ElecGtr1")["fader_db"]
        layer = self.loud(mixer.node_audio("22_ElecGtr2"), spans) + self.track("22_ElecGtr2")["fader_db"]
        # Extra layer lower: 3 LU under the main pair.
        offset = round(min(0.0, main - 3 - layer), 2)
        for name in ("22_ElecGtr2", "23_ElecGtr2DT"):
            self.track(name)["fader_db"] = round(self.track(name)["fader_db"] + offset, 2)
        self.log("guitars.pair", "22_ElecGtr2+23_ElecGtr2DT", "kept", {"main_lufs": r2(main), "layer_lufs": r2(layer), "layer_offset_db": offset})
        # Hi-hat: placed where it sits in the overheads, blended 8 LU under them where it plays.
        hat, oh = mixer.node_audio("13_HiHat"), mixer.node_audio("15_Overheads")
        times = meters.detect_hits(hat, rate)
        inside = meters.windows(hat.shape[1], times, rate, (0, 60))
        hi = meters.filtered(oh, rate, 4000, 12000)[:, inside]
        left, right = float((hi[0] ** 2).sum()), float((hi[1] ** 2).sum())
        position = round(float(np.clip((math.sqrt(right) - math.sqrt(left)) / (math.sqrt(right) + math.sqrt(left)) * 2, -1, 1)), 2)
        active = [(s["start_s"], s["end_s"]) for s in self.sections.values() if s.get("group") == "break"]
        h, o = self.loud(hat, active), self.loud(oh, active)
        self.track("13_HiHat")["pan"] = position
        self.track("13_HiHat")["fader_db"] = round(o - 8 - h, 2)
        self.log("overheads.hihat-blend", "13_HiHat", "kept", {"oh_left_right_hat_band": [r2(meters.db(left)), r2(meters.db(right))],
                                                               "pan": position, "hat_lufs": r2(h), "oh_lufs": r2(o)})

    # Stage 4: group faders from loudness targets against the guitars, over the dense sections.
    TARGETS_LU = {"kick": -4.0, "snare": -4.0, "bass": -3.0, "cymbals": -9.0, "room": -13.0, "synths": -7.0}

    def balance_stage(self, pool):
        mixer = self.mixer()
        mixer.render(pool, stop_at_mix=True)
        spans = self.dense_spans()
        guitars = self.loud(self.post_fader(mixer, "gtr"), spans)
        for name, target in self.TARGETS_LU.items():
            level = self.loud(daw.pan_gain(mixer.node_audio(name), 0, 0), spans)
            self.bus(name)["fader_db"] = round(target + guitars - level, 2)
            self.log("balance.fader", name, "kept", {"bus_lufs": r2(level), "guitars_lufs": r2(guitars), "target_lu": target,
                                                    "fader_db": self.bus(name)["fader_db"]})
        toms = mixer.node_audio("toms")
        times = meters.detect_hits(toms, self.rate)
        fills = [(t / self.rate - 0.05, t / self.rate + 0.6) for t in times]
        t_level = self.loud(daw.pan_gain(toms, 0, 0), fills)
        g_level = self.loud(self.post_fader(mixer, "gtr"), fills)
        self.bus("toms")["fader_db"] = round(-3.0 + g_level - t_level, 2)
        self.log("balance.fader", "toms", "kept", {"toms_lufs_in_fills": r2(t_level), "guitars_lufs_in_fills": r2(g_level),
                                                   "target_lu": -3.0, "fader_db": self.bus("toms")["fader_db"]})

    # Stage 5: the drum bus: shared reverb, then glue.
    def drums_stage(self, pool):
        self.recipe["buses"].insert(self.recipe["buses"].index(self.bus("drums")),
                                    {"name": "drum-verb", "inserts": [], "fader_db": 0.0, "pan": 0, "out": "drums", "sends": []})
        verb = [{"id": "drum-bus.reverb", "plugin": "DragonflyPlateReverb", "settings": {
            "dry_level": 0.0, "wet_level": 100.0, "algorithm": "Nested", "width": 100.0, "predelay_ms": 15.0, "decay_s": 1.1,
            "low_cut_hz": 180.0, "high_cut_hz": 9000.0, "dampen_hz": 7000.0}}]
        self.bus("drum-verb")["inserts"] = verb
        mixer = self.mixer()
        mixer.render(pool, stop_at_mix=True)
        snare_hits = meters.detect_hits(mixer.node_audio("snare"), self.rate)
        kick_hits = meters.detect_hits(mixer.node_audio("kick"), self.rate)
        dry, dry_key = self.bus_input(mixer, "drums")

        def tail_and_valley(audio):
            snare = meters.hit_stats(audio, self.rate, snare_hits)
            valley = measure_valley(audio, kick_hits, self.rate)
            return float(np.median(snare["tail_db"])), float(np.median(snare["attack_db"])), valley

        tail0, attack0, valley0 = tail_and_valley(dry)
        tried, chosen = [], None
        for send_db in (-12.0, -8.0, -4.0):
            self.bus("snare")["sends"] = [{"bus": "drum-verb", "gain_db": send_db}]
            self.bus("toms")["sends"] = [{"bus": "drum-verb", "gain_db": send_db - 4}]
            mixer = self.mixer()
            mixer.render(pool, stop_at_mix=True)
            wet, _ = self.bus_input(mixer, "drums")
            tail, attack, valley = tail_and_valley(wet)
            ok = tail > tail0 + HOLD_DB and attack >= attack0 - HOLD_DB and valley <= valley0 + HOLD_DB and tail < tail0 + 4
            tried.append({"send_db": send_db, "tail_change": r2(tail - tail0), "attack_change": r2(attack - attack0),
                          "valley_change": r2(valley - valley0), "kept": ok})
            if ok:
                chosen = send_db
                break
        if chosen is None:
            self.bus("snare")["sends"], self.bus("toms")["sends"] = [], []
            self.recipe["buses"].remove(self.bus("drum-verb"))
        self.log("drum-bus.reverb", "snare+toms", "kept" if chosen is not None else "dropped: did not work",
                 {"tail_p50_db": r2(tail0), "tried": tried})
        mixer = self.mixer()
        mixer.render(pool, stop_at_mix=True)
        drums = self.node_for_bus(mixer, "drums", hits_from=mixer.node_audio("kick") + mixer.node_audio("snare"))
        stats = drums.hit_stats()
        spans_mask = np.zeros(len(drums.hits), bool)
        for a, b in self.dense_spans():
            spans_mask |= (drums.hits >= a * self.rate) & (drums.hits < b * self.rate)
        levels = np.array(stats["level_db"])[spans_mask]
        spread = float(np.percentile(levels, 90) - np.percentile(levels, 10))
        before_l = meters.lufs(drums.audio, self.rate)
        level = float(np.median(levels))
        attack_before = float(np.median(stats["attack_db"]))

        def judge(out, key):
            after = drums.hit_stats(out, key)
            lv = np.array(after["level_db"])[spans_mask]
            new_spread = float(np.percentile(lv, 90) - np.percentile(lv, 10))
            reduction = before_l - meters.lufs(out, self.rate)
            at = float(np.median(after["attack_db"]))
            ok = new_spread < spread - HOLD_DB and at >= attack_before - HOLD_DB and 1 <= reduction <= 3.5
            return ok, {"hit_spread_db": r2(new_spread), "attack_p50_db": r2(at), "loudness_drop_db": r2(reduction)}

        drums.attempt("drum-bus.compress", spread > 3, {"hit_spread_db": r2(spread), "attack_p50_db": r2(attack_before)},
                      [{"plugin": ZL_COMP, "settings": zl_comp(level - t, 2, 30, 120)} for t in (6, 9, 12)], judge)
        self.finish(drums)

    # Stage 6: in-context checks on the full mix: what each source needs against the others.
    def context_stage(self, pool):
        rate = self.rate
        mixer = self.mixer()
        mixer.render(pool, stop_at_mix=True)
        kick_hits = meters.detect_hits(mixer.node_audio("kick"), rate)
        snare_hits = meters.detect_hits(mixer.node_audio("snare"), rate)
        length = mixer.length
        on_kick = meters.windows(length, kick_hits, rate, (0, 25))
        on_snare = meters.windows(length, snare_hits, rate, (0, 25))
        off_hits = ~meters.windows(length, np.concatenate([kick_hits, snare_hits]), rate, (-10, 120))
        gtr = self.post_fader(mixer, "gtr")

        # kick.click, then guitars.lead-room if the click alone does not close the gap.
        def click_gap(kick_audio):
            return masked(kick_audio, 2000, 6000, on_kick) - masked(gtr, 2000, 6000, on_kick)

        def masked(audio, low, high, inside):
            return meters.masked_band_db(audio, rate, low, high, inside)

        kick_bus = self.bus("kick")
        gap = click_gap(self.post_fader(mixer, "kick"))
        node = Node("kick", mixer.node_audio("kick"), mixer.nodes["kick"][0], self.cache, rate, self.sections, kick_hits)

        def click_judge(out, key):
            new_gap = click_gap(daw.pan_gain(out, kick_bus["fader_db"], 0))
            return new_gap > gap + HOLD_DB, {"gap_db": r2(new_gap)}

        node.attempt("kick.click", gap < 0, {"kick_minus_guitars_2_6k_on_kick_hits_db": r2(gap)},
                     [{"plugin": "PeakFilter", "settings": {"cutoff_frequency_hz": 4000.0, "gain_db": g, "q": 0.8}} for g in (4.0, 6.0)],
                     click_judge)
        kick_bus["inserts"].extend(node.inserts)
        self.recipe["decisions"].extend(node.decisions)

        snare_bus = self.bus("snare")
        def crack_gap(snare_audio):
            return masked(snare_audio, 2000, 5000, on_snare) - masked(gtr, 2000, 5000, on_snare)
        sgap = crack_gap(self.post_fader(mixer, "snare"))
        node = Node("snare", mixer.node_audio("snare"), mixer.nodes["snare"][0], self.cache, rate, self.sections, snare_hits)
        node.attempt("snare.body-crack", sgap < 0, {"snare_minus_guitars_2_5k_on_snare_hits_db": r2(sgap)},
                     [{"plugin": "PeakFilter", "settings": {"cutoff_frequency_hz": 3000.0, "gain_db": g, "q": 0.8}} for g in (3.0, 5.0)],
                     lambda out, key: ((g := crack_gap(daw.pan_gain(out, snare_bus["fader_db"], 0))) > sgap + HOLD_DB, {"gap_db": r2(g)}))
        snare_bus["inserts"].extend(node.inserts)
        self.recipe["decisions"].extend(node.decisions)

        mixer = self.mixer()
        mixer.render(pool, stop_at_mix=True)
        gap, sgap = click_gap(self.post_fader(mixer, "kick")), crack_gap(self.post_fader(mixer, "snare"))
        gtr_node = Node("gtr", None, None, self.cache, rate, self.sections)
        gtr_node.record("guitars.lead-room", "dropped: not needed" if gap >= 0 and sgap >= 0 else "dropped: open",
                        {"kick_gap_db": r2(gap), "snare_gap_db": r2(sgap)})
        self.recipe["decisions"].extend(gtr_node.decisions)

        # bass.split: one bass track with no mid growl gets a distorted, band-limited copy on a send.
        bass_audio = mixer.node_audio("19_Bass")
        bass_spec = meters.spectrum(bass_audio, rate)
        growl, low = meters.band_db(bass_spec, 700, 3000), meters.band_db(bass_spec, 40, 250)
        tried, chosen = [], None
        if growl < low - 15:
            self.recipe["buses"].insert(self.recipe["buses"].index(self.bus("bass")),
                                        {"name": "bass-dirt", "fader_db": 0.0, "pan": 0, "out": "bass", "sends": [], "inserts": [
                {"id": "bass.split.pre", "plugin": ZL_EQ, "settings": zl_eq({"type": "High Pass", "freq": 400}, {"type": "Low Pass", "freq": 5000})},
                {"id": "bass.split.drive", "plugin": "Distortion", "settings": {"drive_db": 24.0}},
                {"id": "bass.split.post", "plugin": ZL_EQ, "settings": zl_eq({"type": "High Pass", "freq": 600}, {"type": "Low Pass", "freq": 4000})}]})
            before = mixer.node_audio("bass")
            b_growl = meters.band_db(meters.spectrum(before, rate), 700, 3000) - meters.lufs(before, rate)
            for send_db in (-18.0, -12.0, -6.0):
                self.track("19_Bass")["sends"] = [{"bus": "bass-dirt", "gain_db": send_db}]
                m = self.mixer()
                m.render(pool, stop_at_mix=True)
                after = m.node_audio("bass")
                a_growl = meters.band_db(meters.spectrum(after, rate), 700, 3000) - meters.lufs(after, rate)
                ok = a_growl - b_growl >= 6
                tried.append({"send_db": send_db, "growl_vs_loudness_change_db": r2(a_growl - b_growl), "kept": ok})
                if ok:
                    chosen = send_db
                    break
            if chosen is None:
                self.track("19_Bass")["sends"] = []
                self.recipe["buses"].remove(self.bus("bass-dirt"))
        self.log("bass.split", "19_Bass", "kept" if chosen is not None else "dropped", {"700_3k_db": r2(growl), "40_250_db": r2(low),
                 "tried": tried, "clean_lpf": "not added: the clean track has almost nothing above 300 Hz to remove"})

        # kick.duck-bass: the bass's lows dip on kick hits when the two still overlap below 120 Hz.
        mixer = self.mixer()
        mixer.render(pool, stop_at_mix=True)
        kick_pf, bass_pf = self.post_fader(mixer, "kick"), self.post_fader(mixer, "bass")
        ov = overlap_db(kick_pf, bass_pf, rate, (40, 120), meters.windows(length, kick_hits, rate, (0, 25)))
        between = ~meters.windows(length, kick_hits, rate, (-5, 150))
        bass_between = masked(bass_pf, 40, 120, between)
        node = Node("bass", mixer.node_audio("bass"), mixer.nodes["bass"][0], self.cache, rate, self.sections, kick_hits)
        kick_level = float(np.median(meters.hit_stats(mixer.node_audio("kick"), rate, kick_hits)["level_db"]))

        def duck_judge(out, key):
            o = daw.pan_gain(out, self.bus("bass")["fader_db"], 0)
            new_ov = overlap_db(kick_pf, o, rate, (40, 120), meters.windows(length, kick_hits, rate, (0, 25)))
            held = masked(o, 40, 120, between)
            return new_ov < ov - HOLD_DB and bass_between - held <= HOLD_DB, {"overlap_db": r2(new_ov), "between_change_db": r2(held - bass_between)}

        cands = [{"plugin": "Duck", "settings": {"key": "kick", "band_hz": [None, 120], "threshold_db": round(kick_level - 15, 1),
                                                  "ratio": 4, "range_db": r, "attack_ms": 1, "release_ms": 60}} for r in (3.0, 5.0)]
        # Duck inserts need the key's audio, so they render through the mixer rather than Node.render.
        if ov > -15:
            tried = []
            for cand in cands:
                insert = {"id": "kick.duck-bass", **cand}
                out, key = daw.chain(self.cache, rate, node.key, lambda: node.audio, [insert], {"kick": mixer.node_audio("kick")},
                                     {"kick": mixer.nodes["kick"][0]}, sections=self.sections)
                ok, result = duck_judge(out, key)
                tried.append({"settings": cand["settings"], **result, "kept": ok})
                if ok:
                    self.bus("bass")["inserts"].append(insert)
                    break
            self.log("kick.duck-bass", "bass", "kept" if tried[-1]["kept"] else "dropped: did not work", {"overlap_db": r2(ov), "tried": tried})
        else:
            self.log("kick.duck-bass", "bass", "dropped: not needed", {"overlap_db": r2(ov)})

        # synths.cut-in-context: a mid-only cut where the pads overlap the guitars.
        mixer = self.mixer()
        mixer.render(pool, stop_at_mix=True)
        syn_pf, gtr = self.post_fader(mixer, "synths"), self.post_fader(mixer, "gtr")
        dense = np.zeros(length, bool)
        for a, b in self.dense_spans():
            dense[round(a * rate) : round(b * rate)] = True
        scores = {c: overlap_db(syn_pf, gtr, rate, (c / 1.41, c * 1.41), dense) for c in (250, 500, 1000, 2000)}
        worst = max(scores, key=scores.get)
        intro = [(s["start_s"], s["end_s"]) for s in self.sections.values() if s.get("group") == "intro"]
        node = Node("synths", mixer.node_audio("synths"), mixer.nodes["synths"][0], self.cache, rate, self.sections)
        sparse0 = self.loud(node.audio, intro)

        def syn_judge(out, key):
            o = daw.pan_gain(out, self.bus("synths")["fader_db"], 0)
            new = overlap_db(o, gtr, rate, (worst / 1.41, worst * 1.41), dense)
            sparse = self.loud(out, intro)
            return new < scores[worst] - HOLD_DB and sparse0 - sparse <= 1.0, {"overlap_db": r2(new), "intro_change_lu": r2(sparse - sparse0)}

        node.attempt("synths.cut-in-context", scores[worst] > -15, {"overlap_db_by_band": {k: r2(v) for k, v in scores.items()}},
                     [{"plugin": ZL_EQ, "settings": zl_eq({"type": "Peak", "freq": worst, "gain": g, "q": 1.0, "mode": "Mid"})} for g in (-3.0, -2.0)],
                     syn_judge)
        self.finish(node)

    # ---------- trial master and profile ----------

    TARGET_TP = -1.0
    CEILING = -1.3

    def master_chain(self, gain_db, clip_db=None):
        chain = []
        if clip_db is not None:
            chain.append({"id": "master.clip", "plugin": "Clipping", "settings": {"threshold_db": float(clip_db)}})
        chain.append({"id": "master.drive", "plugin": "Gain", "settings": {"gain_db": float(round(gain_db, 4))}})
        chain.append({"id": "master.limit", "plugin": "ZL Limiter", "settings": {
            "output_ceiling_output_ceiling": self.CEILING, "true_peak_true_peak": True, "oversampling_oversampling": "4x",
            "lookahead_lookahead": 2.0, "recovery_recovery": 10.0, "attack_attack": 10.0, "release_release": 50.0}})
        return chain

    def trial(self, mix, mix_key, clip_db=None, extra=()):
        """The mix through a trial master at the profile's loudness: (limiter input, master, chain, loudness).

        The drive is found by a guarded secant search: a limiter flattens the loudness-per-dB slope, so a plain
        gain step undershoots. It fails loudly rather than return a master off the target."""
        target = self.profile["master_integrated_lufs"]
        pre = list(extra)

        def run(gain):
            chain = pre + self.master_chain(gain, clip_db)
            out, key, step_keys = daw.chain(self.cache, self.rate, mix_key, lambda: mix, chain, steps=True, sections=self.sections)
            level = self.cache.measured(key, "lufs", lambda: meters.lufs(out, self.rate))
            return chain, out, step_keys, level

        gain = getattr(self, "_last_drive", None) or target - meters.lufs(mix, self.rate)
        points = []
        for _ in range(12):
            chain, out, step_keys, level = run(round(gain, 4))
            points.append((gain, level))
            if abs(level - target) <= 0.003:
                break
            if len(points) >= 2 and abs(points[-1][1] - points[-2][1]) > 1e-6:
                slope = (points[-1][1] - points[-2][1]) / (points[-1][0] - points[-2][0])
                slope = min(max(slope, 0.05), 1.0)
            else:
                slope = 1.0
            gain = min(gain + (target - level) / slope, gain + 6)
        else:
            raise RuntimeError(f"trial master missed {target} LUFS: {points}")
        self._last_drive = gain
        limit_in = self.cache.get(step_keys[-2])
        return limit_in, out, chain, level

    def profile_check(self, audio):
        """The profile's ranges, except absolute loudness: the master's -9 LUFS target is below every reference, so
        the loudest short-term window is compared relative to the integrated loudness instead."""
        found = measurelib.profile_range.__wrapped__(audio.T.astype(np.float64), self.rate, self.facts["profile"])
        refs = [r["max_short_term_lufs"] - r["integrated_lufs"] for r in self.profile["references"]]
        value = found["max_short_term_lufs"]["value"] - found["integrated_lufs"]["value"]
        low, high = min(refs), max(refs)
        found["max_short_term_over_integrated_lu"] = {"value": r2(value), "min": r2(low), "max": r2(high),
                                                     "outside": r2(value - high if value > high else value - low if value < low else 0)}
        return {k: v for k, v in found.items() if k not in ("integrated_lufs", "max_short_term_lufs")}

    LEAVES = ["kick", "snare", "toms", "cymbals", "room", "bass", "gtr", "synths"]

    def leaf_out(self, mixer, name):
        # The leaf's own signal after its fader and its parent's fader (drums feeds mix at its own fader).
        audio = self.post_fader(mixer, name)
        node = self.bus(name)
        if node["out"] == "drums":
            audio = audio * 10 ** (self.bus("drums")["fader_db"] / 20)
        return audio

    # Stage 7: one correction pass toward the profile's octave bands, at the source that owns each band.
    PROFILE_PASSES = 3

    def profile_stage(self, pool):
        """Bounded: at most three band-correction passes, then the width through the guitar layer's pan."""
        for index in range(self.PROFILE_PASSES):
            mixer = self.mixer()
            mix, _ = mixer.render(pool, stop_at_mix=True)
            _, master, _, _ = self.trial(mix, mixer.nodes["mix"][0])
            check = self.profile_check(master)
            self.log("profile.measure", "mix", f"pass {index + 1}", check)
            if not any(v["outside"] for k, v in check.items() if k.startswith("bands_db.")):
                break
            self.correct_bands(mixer, mix, check)
        layer = [self.track("22_ElecGtr2"), self.track("23_ElecGtr2DT")]
        tried = []
        for _ in range(3):
            mixer = self.mixer()
            mix, _ = mixer.render(pool, stop_at_mix=True)
            _, master, _, _ = self.trial(mix, mixer.nodes["mix"][0])
            check = self.profile_check(master)
            width = check["side_mid_db"]
            tried.append({"layer_pan": layer[1]["pan"], "side_mid_db": width["value"], "outside": width["outside"]})
            if width["outside"] <= 0 or layer[1]["pan"] <= 0.3:
                break
            for track in layer:
                track["pan"] = round(track["pan"] - math.copysign(0.2, track["pan"]), 2)
        self.log("guitars.pair.width", "22_ElecGtr2+23_ElecGtr2DT", "kept" if len(tried) > 1 else "dropped: not needed", {"tried": tried})
        self.log("profile.after", "mix", "measured", check)

    MOVES = {("gtr", -1): "guitars.mud-cut", ("gtr", 1): "guitars.tone", ("bass", -1): "bass.mud-cut", ("bass", 1): "bass.growl",
             ("cymbals", -1): "overheads.harsh-cut", ("cymbals", 1): "overheads.air", ("kick", 1): "kick.thump", ("kick", -1): "kick.bass-room",
             ("synths", -1): "synths.cut-in-context", ("synths", 1): "synths.colour", ("snare", 1): "snare.body-crack", ("snare", -1): "snare.ring-cut"}

    def correct_bands(self, mixer, mix, check):
        """For each octave band outside the profile range: a bell at the third-octave where the band lives, on the
        source that owns most of it there, sized from that source's share so the mix band lands 0.5 dB inside."""
        mix_spec = meters.spectrum(mix, self.rate)
        leaves = {name: meters.spectrum(self.leaf_out(mixer, name), self.rate) for name in self.LEAVES}
        for field, entry in check.items():
            if not field.startswith("bands_db.") or entry["outside"] == 0:
                continue
            center = float(field.split(".", 1)[1])
            span = entry["max"] - entry["min"]
            goal = (entry["min"] + entry["max"]) / 2 if span < 1 else (entry["max"] - 0.5 if entry["outside"] > 0 else entry["min"] + 0.5)
            change = goal - entry["value"]
            thirds = [center / 2 ** (1 / 3), center, center * 2 ** (1 / 3)]
            third = lambda spec, c: meters.band_db(spec, c / 2 ** (1 / 6), c * 2 ** (1 / 6))
            if change > 0:
                owner = max(leaves, key=lambda n: meters.band_db(leaves[n], center / 2 ** 0.5, center * 2 ** 0.5))
                # A boost stays inside its octave, so it does not raise the neighbouring band.
                freq = max([center / 2 ** (1 / 6), center, center * 2 ** (1 / 6)], key=lambda c: third(leaves[owner], c))
            else:
                freq = max(thirds, key=lambda c: third(mix_spec, c))
                owner = max(leaves, key=lambda n: third(leaves[n], freq))
            low, high = freq / 2 ** 0.5, freq * 2 ** 0.5
            share = min(10 ** ((meters.band_db(leaves[owner], low, high) - meters.band_db(mix_spec, low, high)) / 10), 0.99)
            inner = (10 ** (change / 10) - 1 + share) / share
            gain = float(np.clip(10 * math.log10(inner) if inner > 0 else -6.0, -6, 6))
            move = self.MOVES.get((owner, 1 if gain > 0 else -1), f"{owner}.tone")
            insert = {"id": move, "plugin": "PeakFilter", "settings": {"cutoff_frequency_hz": round(freq, 1), "gain_db": round(gain, 2),
                                                                         "q": 2.0 if gain > 0 else 1.4}}
            self.bus(owner)["inserts"].append(insert)
            self.log(move, owner, "kept", {"band": field, "value": entry["value"], "range": [entry["min"], entry["max"]],
                                           "bell_hz": r2(freq), "owner_share": r2(share), "gain_db": r2(gain)})

    # Stage 9: the mix bus: section loudness, glue, and the fade at the cut.
    def mix_stage(self, pool):
        mixer = self.mixer()
        mix, _ = mixer.render(pool, stop_at_mix=True)
        mix_key = mixer.nodes["mix"][0]
        _, master, _, _ = self.trial(mix, mix_key)
        found = measurelib.sections.__wrapped__(master.T.astype(np.float64), self.rate, self.sections)
        errors = {n: e.get("error_lu") for n, e in found["sections"].items()}
        off = {n: e for n, e in errors.items() if e is not None and abs(e) > 1.5 and n != "outro"}
        if off:
            segments = [[n, round(-e, 2)] for n, e in off.items()]
            self.recipe["mix"]["inserts"].insert(0, {"id": "mix-bus.rides", "plugin": "Ride", "settings": {"segments": segments, "ramp_ms": 500}})
        self.log("mix-bus.rides", "mix", "kept" if off else "dropped: not needed", {"error_lu": errors})
        # mix-bus.compress: spread of short-term loudness inside each steady section.
        times, st = meters.short_term(mix, self.rate)
        spreads = {}
        for n, s in self.sections.items():
            if s.get("group") in ("intro", "outro"):
                continue
            inside = (times >= s["start_s"]) & (times + 3 <= s["end_s"]) & np.isfinite(st)
            if inside.sum() >= 5:
                spreads[n] = r2(np.percentile(st[inside], 90) - np.percentile(st[inside], 10))
        self.log("mix-bus.compress", "mix", "dropped: not needed" if max(spreads.values()) <= 3 else "dropped: open", {"spread_lu": spreads})
        self.log("mix-bus.console", "mix", "dropped: not tried", {"why": "no Airwindows console in the chain yet; the drum bus and limiter carry the glue"})
        fade = self.facts.get("fade_out_s")
        if fade:
            half = (fade[1] - fade[0]) / 2
            self.recipe["mix"]["inserts"].append({"id": "mix-bus.fade", "plugin": "Ride", "settings": {
                "segments": [[fade[0] + half, fade[1] + half, -60]], "ramp_ms": (fade[1] - fade[0]) * 1000}})
            self.log("mix-bus.fade", "mix", "kept", {"fade_s": fade})

    def master_view(self, mix, mix_key, hits, clip_db=None, extra=()):
        """How the trial master treats the drum hits: limiter dip (limiter alone) and attack in the master per drum,
        2-8 kHz against loudness, and the limiter's work beyond plain gain."""
        rate = self.rate
        limit_in, master, chain, level = self.trial(mix, mix_key, clip_db, extra)
        dips, attacks = {}, {}
        for name, times in hits.items():
            found = measurelib.dip.__wrapped__(limit_in.T.astype(np.float64), master.T.astype(np.float64), rate, times)["dip_db"]
            dips[name] = r2(found["p50"]) if found else 0.0
            attacks[name] = r2(np.median(meters.hit_stats(master, rate, times)["attack_db"]))
        hi = meters.band_db(meters.spectrum(master, rate), 2000, 8000) - level
        return {"dip_db": dips, "attack_db": attacks, "2_8k_rel_db": r2(hi),
                "limiter_work_db": r2(meters.lufs(limit_in, rate) - level), "drive_db": chain[-2]["settings"]["gain_db"], "lufs": r2(level)}, chain

    def hit_peaks_db(self, audio, times):
        span = int(0.01 * self.rate)
        return float(np.median([20 * math.log10(max(float(np.abs(audio[:, t : t + span]).max()), 1e-9)) for t in times]))

    # Stage 8: drum peaks judged through the trial master, after the tone is set: snare.clip, kick.limit, toms.limit.
    PUNCH = (("snare", "snare.clip", "Clipping"), ("kick", "kick.limit", "ZL Limiter"), ("toms", "toms.saturate-clip", "Clipping"))

    def punch_stage(self, pool):
        rate = self.rate
        mixer = self.mixer()
        mix, _ = mixer.render(pool, stop_at_mix=True)
        hits = {name: meters.detect_hits(mixer.node_audio(name), rate) for name in ("kick", "snare", "toms")}
        base, _ = self.master_view(mix, mixer.nodes["mix"][0], hits)
        for name, move, plugin in self.PUNCH:
            bus, times = self.bus(name), hits[name]
            audio = mixer.node_audio(name)
            peak = self.hit_peaks_db(audio, times)
            levels = np.array(meters.hit_stats(audio, rate, times)["level_db"])
            over = float(np.percentile(levels, 95) - np.median(levels))
            needed = base["dip_db"][name] > 2 or over > 3
            tried, best = [], None
            for depth in (2.0, 4.0, 6.0) if needed else ():
                if plugin == "Clipping":
                    insert = {"id": move, "plugin": "Clipping", "settings": {"threshold_db": round(peak - depth, 2)}}
                else:
                    # The ceiling's range ends at -12 dB, so a quiet bus is driven into it and brought back after.
                    lift = max(0.0, -12.0 - (peak - depth))
                    insert = {"id": move, "plugin": "ZL Limiter", "settings": {"input_gain_input_gain": round(lift, 2),
                              "output_ceiling_output_ceiling": round(peak - depth + lift, 2), "true_peak_true_peak": False,
                              "oversampling_oversampling": "2x", "lookahead_lookahead": 1.0, "recovery_recovery": 10.0,
                              "attack_attack": 5.0, "release_release": 40.0}}
                bus["inserts"].append(insert)
                m = self.mixer()
                mix2, _ = m.render(pool, stop_at_mix=True)
                result, _ = self.master_view(mix2, m.nodes["mix"][0], hits)
                bus["inserts"].pop()
                ok = (result["dip_db"][name] < base["dip_db"][name] - HOLD_DB
                      and result["attack_db"][name] >= base["attack_db"][name] - HOLD_DB)
                tried.append({"depth_db": depth, "settings": insert["settings"], **result, "kept": False, "ok": ok})
                if ok and (best is None or result["dip_db"][name] < best[1]["dip_db"][name]):
                    best = (insert, result, len(tried) - 1)
                if ok and result["dip_db"][name] <= 2:
                    break
            if best:
                bus["inserts"].append(best[0])
                tried[best[2]]["kept"] = True
            self.log(move, name, "dropped: not needed" if not needed else ("kept" if best else "dropped: did not work"),
                     {"median_hit_peak_db": r2(peak), "p95_over_median_db": r2(over), "before": base, "tried": tried})
            if best:
                base = best[1]

    def master_tone(self, mix, mix_key):
        """master.tone: wide bells of at most 1 dB for octave bands still outside the profile range."""
        _, master, _, _ = self.trial(mix, mix_key)
        check = self.profile_check(master)
        tone = []
        for field, entry in check.items():
            if not field.startswith("bands_db.") or entry["outside"] == 0:
                continue
            gain = float(np.clip(-entry["outside"] - math.copysign(0.3, entry["outside"]), -1, 1))
            tone.append({"id": "master.tone", "plugin": "PeakFilter", "settings": {"cutoff_frequency_hz": float(field.split(".", 1)[1]),
                                                                                   "gain_db": round(gain, 2), "q": 0.7}})
        tried = []
        if tone:
            _, after, _, _ = self.trial(mix, mix_key, extra=tone)
            new = self.profile_check(after)
            moved = {f: (check[f]["outside"], new[f]["outside"]) for f in check if f.startswith("bands_db.")}
            ok = all(abs(b) <= abs(a) - 0.5 or b == 0 for a, b in moved.values() if a != 0) and \
                all(b == 0 for a, b in moved.values() if a == 0)
            tried.append({"inserts": [t["settings"] for t in tone], "outside_before_after": moved, "kept": ok})
            if not ok:
                tone = []
        self.log("master.tone", "master", "kept" if tone else ("dropped: did not work" if tried else "dropped: not needed"), {"tried": tried})
        return tone

    # Stage 10: the master: clip only if the limiter still dips on hits, then limit to the profile loudness.
    def master_stage(self, pool):
        rate = self.rate
        mixer = self.mixer()
        mix, _ = mixer.render(pool, stop_at_mix=True)
        mix_key = mixer.nodes["mix"][0]
        hits = {name: meters.detect_hits(mixer.node_audio(name), rate) for name in ("kick", "snare")}
        tone = self.master_tone(mix, mix_key)
        base, chain = self.master_view(mix, mix_key, hits, extra=tone)
        tried, chosen = [], chain
        hit_peak = self.hit_peaks_db(mix, np.sort(np.concatenate(list(hits.values()))))
        worst = max(base["dip_db"].values())
        if worst > 2:
            # A few dB off the median hit's peak; the limiter still does the rest.
            for depth in (1.5, 3.0):
                clip = round(hit_peak - depth, 2)
                result, c = self.master_view(mix, mix_key, hits, clip, extra=tone)
                ok = (max(result["dip_db"].values()) < worst - HOLD_DB
                      and all(result["attack_db"][n] >= base["attack_db"][n] - HOLD_DB for n in hits)
                      and result["2_8k_rel_db"] <= base["2_8k_rel_db"] + HOLD_DB and result["limiter_work_db"] >= 1)
                tried.append({"clip_threshold_db": clip, "depth_under_median_hit_db": depth, **result, "kept": ok})
                if ok:
                    chosen = c
                    break
        self.log("master.clip", "master", "kept" if chosen is not chain else ("dropped: did not work" if tried else "dropped: not needed"),
                 {"limiter_only": base, "median_hit_peak_db": r2(hit_peak), "tried": tried})
        self.log("master.compress", "master", "dropped: not needed" if base["limiter_work_db"] <= 4 else "dropped: open",
                 {"limiter_work_db": base["limiter_work_db"]})
        self.recipe["master"]["inserts"] = chosen
        self.log("master.limit", "master", "kept", {"settings": chosen[-1]["settings"], "drive_db": chosen[-2]["settings"]["gain_db"]})

    # Export: render the recipe as written and check the files actually written.
    def export(self, pool):
        import soundfile
        mixer = self.mixer()
        mix, master = mixer.render(pool)
        out = self.song / "renders"
        out.mkdir(exist_ok=True)
        soundfile.write(out / "mix.wav", mix.T, self.rate, subtype="FLOAT")
        temp = out / "master.tmp.wav"
        soundfile.write(temp, master.T, self.rate, subtype="PCM_24")
        written = soundfile.read(temp, always_2d=True, dtype="float64")[0].T.astype(np.float32)
        level, peak = meters.lufs(written, self.rate), meters.true_peak_db(written)
        target = self.profile["master_integrated_lufs"]
        checks = {"master_lufs": r2(level), "master_true_peak_dbtp": r2(peak), "target_lufs": target,
                  "loudness_ok": bool(abs(level - target) <= 0.005), "true_peak_ok": bool(peak <= self.TARGET_TP),
                  "profile": self.profile_check(written)}
        checks["profile_outside"] = {k: v["outside"] for k, v in checks["profile"].items() if v["outside"] != 0}
        if checks["loudness_ok"] and checks["true_peak_ok"]:
            temp.replace(out / "master.wav")
        else:
            temp.unlink()
        (out / "checks.json").write_text(json.dumps(checks, indent=1) + "\n")
        return checks
