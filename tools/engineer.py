"""The engineer: decides the recipe from measurements, in signal-flow order, judging every move on its own before/after.

Stems are classified by name hint and content (stems.py). Drums come first (mic blends, the kick's tail and weight),
then the static balance and the faders that set kick and snare punch, then context moves on the full mix (kick/bass
ownership, a low-mid build-up cut on its owner, air), drum-bus glue kept only if punch holds, a second punch pass,
and the master. References are a printed comparison only; nothing here reads them back.
"""
import itertools
import json
import math
import os
import re
from pathlib import Path

import numpy as np
from scipy import signal

import daw
import meters
import measure as measurelib
import notes as notelib
import punch as punchlib
import soundfile
import stems as stemlib

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
        starts = note_starts[note_starts + span < len(cum)].astype(int)
        if len(starts) < 5:
            return 0.0
        values = 10 * np.log10(np.maximum((cum[starts + span] - cum[starts]) / span, 1e-20))
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
    # Resonance notches are not tried: a notch from the long-term spectrum is not validated across notes.
    node.record("guitars.resonance", "dropped: not tried", {"why": "needs a per-note check"})


# ---------- per-track decisions (run in workers) ----------

def decide_track(args):
    song, cache_folder, rate, length, track = args
    cache = daw.Cache(cache_folder)
    key, source = daw.stem_source(song, track, length)
    audio = cache.get(key)
    if audio is None:
        audio = source()
        cache.put(key, audio)
    node = Node(track["name"], audio, key, cache, rate, {})
    cls = track["class"]
    if track.get("sample"):
        pass
    elif cls == "toms":
        node.hits = meters.detect_hits(audio, rate)
        tom_edit(node, "toms.clean")
    elif cls == "snare":
        node.hits = meters.detect_hits(audio, rate)
        below, body = node.band(20, 80), node.band(150, 250)
        cands, judge, numbers = steep_hpf(node, "snare.hpf-lpf", [80], (20, 80), (150, 250), order="12 dB/oct")
        node.attempt("snare.hpf-lpf", below > body - 20, numbers | {"body_db": r2(body)}, cands, judge)
    elif cls == "overheads":
        low, cym = node.band(20, 250), node.band(2000, 8000)
        cands, judge, numbers = steep_hpf(node, "overheads.hpf", [150, 220], (20, 150), (2000, 8000), order="12 dB/oct", fall_db=3)
        node.attempt("overheads.hpf", low > cym - 10, numbers | {"cymbal_db": r2(cym)}, cands, judge)
    elif cls in ("hat", "ride", "cymbal"):
        cands, judge, numbers = steep_hpf(node, "overheads.close-hpf", [400], (20, 300), (4000, 12000), order="12 dB/oct", fall_db=3)
        node.attempt("overheads.close-hpf", True, numbers, cands, judge)
    elif cls == "room":
        decide_room(node)
    elif cls == "bass":
        decide_bass(node, (0, length / rate))
    elif cls == "guitar":
        decide_guitar(node)
    elif cls == "synth":
        main = max(((lo, lo * 2) for lo in (125, 250, 500, 1000, 2000, 4000)), key=lambda b: node.band(*b))
        low, total = node.band(20, 150), node.band(20, 20000)
        # The highest cutoff that keeps the part's own main band within 1 dB; even a low one takes the sub rumble out.
        cands, judge, numbers = steep_hpf(node, "synths.hpf", [150, 120, 80, 50], (20, 45), main, order="12 dB/oct", fall_db=3,
                                          hold_db=1.0)
        node.attempt("synths.hpf", low > total - 20, numbers | {"total_db": r2(total), "main_band": list(main)}, cands, judge)
    return track["name"], node.inserts, node.decisions


# ---------- punch measures on a whole mix ----------

def punch_of(audio, rate, kick_s, snare_s):
    """Median hit prominence (dB) of kick low/click and snare body/crack, and the kick's low tail, in this audio."""
    out = {}
    for name, times, (lo, hi) in (("kick_low", kick_s, (40, 120)), ("kick_click", kick_s, (2000, 6000)),
                                  ("snare_body", snare_s, (150, 350)), ("snare_crack", snare_s, (2000, 8000))):
        if len(times) < 4:
            out[name] = None
            continue
        e = punchlib.env(punchlib.bandpass(audio.astype(np.float64), rate, lo, hi), rate)
        out[name] = r2(np.nanmedian(punchlib.prominence(e, times)))
        if name == "kick_low":
            out["kick_tail"] = r2(np.nanmedian(punchlib.tail(e, times)))
    return out


def third_levels(audio, rate, centers, inside=None):
    x = audio.astype(np.float64)
    if inside is not None:
        x = x[:, inside]
    f, p = signal.welch(x, rate, nperseg=16384, axis=-1)
    p = p.sum(axis=0)
    return {c: meters.db(float(p[(f >= c / 2 ** (1 / 6)) & (f < c * 2 ** (1 / 6))].sum())) for c in centers}


# ---------- the engineer ----------

class Engineer:
    # The release choice (research decision 2): -7 LUFS integrated, true peak at most -1 dBTP.
    TARGET_LUFS = -7.0
    TARGET_TP = -1.0
    CEILING = -1.3
    # Group loudness against the guitars over the full-band frames (LU).
    TARGETS_LU = {"kick": -4.0, "snare": -4.0, "bass": -3.0, "cymbals": -9.0, "room": -13.0, "synths": -7.0}
    # Genre rules, the same for every song: hit prominence in the mix (dB) the faders aim for, the kick's low decay,
    # the low-mid build-up and the air tilt that trigger a move, and the master's limits on punch loss.
    KICK_LOW_PROMINENCE = 12.5
    MASTER_KICK_PROMINENCE = 10.0
    MASTER_SNARE_PROMINENCE = 12.5
    SNARE_BODY_PROMINENCE = 14.0
    KICK_TAIL_DB = -14.0
    LOWMID_BUILDUP_DB = 2.5
    AIR_TILT_DB = -10.5
    PRESENCE_TILT_DB = -2.0
    TOP_TILT_DB = -10.3
    CYMBAL_AIR_REF_DB = -4.3
    CYMBAL_AIR_SLOPE = 1.0
    KICK_CLICK_PROMINENCE = 5.0
    SNARE_CRACK_PROMINENCE = 7.0
    SUB_SOURCE_DB = -4.0
    SUB_CUT_DEPTH_DB = 4.0
    CREST_LOSS_DB = 3.0
    # Export gates: the master's kick and snare must still stand out (the tuning songs' officials: kick 9.4-10.6 dB,
    # snare 10.1-12.9 dB by this measure).
    KICK_GATE_DB = 8.5
    SNARE_GATE_DB = 9.0
    MIN_SIDE_MID_DB = -12.0

    def __init__(self, song, workers=2):
        self.song = Path(song)
        self.cache = daw.Cache(os.environ.get("S2M_CACHE") or self.song / "cache")
        self.workers = workers
        self.profile_name = "instrumental-prog-metal"
        self.found, self.left_out = stemlib.classify(self.song / "stems")
        self.rate = soundfile.info(self.song / "stems" / self.found[0]["stem"]).samplerate
        self.recipe = self.skeleton()
        self.length = daw.length_of(self.song, self.recipe)
        self.recipe["decisions"].append({"move": "stems.classify", "target": "song", "outcome": "kept", "numbers": {
            "stems": {s["stem"]: [s["class"], s["by"]] + (["sample"] if s["sample"] else []) for s in self.found},
            "left_out": self.left_out}})

    def stages(self):
        return ["setup_stage", "tracks_stage", "drums_stage", "balance_stage", "context_stage", "glue_stage",
                "rebalance_stage", "master_stage"]

    # ---------- recipe helpers ----------

    def skeleton(self):
        tracks, groups = [], []
        for s in self.found:
            tracks.append({"name": s["name"], "stem": s["stem"], "class": s["class"], "group": s["group"], "sample": s["sample"],
                           "double": s["double"], "channels": s["channels"], "inserts": [], "fader_db": 0.0, "pan": 0.0,
                           "out": s["group"], "sends": []})
            if s["group"] not in groups:
                groups.append(s["group"])
        order = ["kick", "snare", "toms", "cymbals", "room", "bass", "gtr", "synths"]
        groups.sort(key=order.index)
        buses = [{"name": g, "inserts": [], "fader_db": 0.0, "pan": 0, "out": "drums" if g in stemlib.DRUM_GROUPS else "mix",
                  "sends": []} for g in groups]
        drums_at = sum(1 for g in groups if g in stemlib.DRUM_GROUPS)
        buses.insert(drums_at, {"name": "drums", "inserts": [], "fader_db": 0.0, "pan": 0, "out": "mix", "sends": []})
        return {"profile": self.profile_name, "sections": {}, "tracks": tracks, "buses": buses,
                "mix": {"inserts": []}, "master": {"inserts": []}, "decisions": []}

    def tracks_of(self, group=None, cls=None):
        return [t for t in self.recipe["tracks"] if (group is None or t["group"] == group) and (cls is None or t["class"] == cls)]

    def track(self, name):
        return next(t for t in self.recipe["tracks"] if t["name"] == name)

    def bus(self, name):
        return next((b for b in self.recipe["buses"] if b["name"] == name), None)

    def has(self, group):
        return self.bus(group) is not None

    def log(self, move, target, outcome, numbers):
        self.recipe["decisions"].append({"move": move, "target": target, "outcome": outcome, "numbers": numbers})

    def mixer(self):
        return daw.Mixer(self.song, self.recipe, self.cache, self.workers)

    def rendered(self, pool):
        mixer = self.mixer()
        mixer.render(pool, stop_at_mix=True)
        return mixer

    def raw(self, track):
        key, read = daw.stem_source(self.song, track, self.length)
        audio = self.cache.get(key)
        if audio is None:
            audio = read()
            self.cache.put(key, audio)
        return audio

    def post_fader(self, mixer, name):
        node = next(n for n in self.recipe["tracks"] + self.recipe["buses"] if n["name"] == name)
        return daw.pan_gain(mixer.node_audio(name), node.get("fader_db", 0), node.get("pan", 0))

    def leaf_out(self, mixer, name, faders=None):
        """A group's signal after its fader (or an override), before any parent bus."""
        bus = self.bus(name)
        fader = (faders or {}).get(name, bus["fader_db"])
        return daw.pan_gain(mixer.node_audio(name), fader, bus["pan"])

    def leaves(self):
        return [b["name"] for b in self.recipe["buses"] if b["name"] != "drums"]

    def mix_of(self, mixer, faders=None, replace=None):
        """The mix with fader overrides and replaced group audio, the drum bus rendered through its own inserts
        (cached by the same key the mixer would use), so a decision sees what the render will produce."""
        replace = replace or {}

        def out(name):
            bus = self.bus(name)
            if name in replace:
                return daw.pan_gain(replace[name][0], (faders or {}).get(name, bus["fader_db"]), bus["pan"]), replace[name][1]
            return self.leaf_out(mixer, name, faders), mixer.nodes[name][0]

        total = np.zeros((2, mixer.length))
        drums, parts = np.zeros((2, mixer.length)), []
        for name in self.leaves():
            audio, key = out(name)
            bus = self.bus(name)
            if bus["out"] == "drums":
                drums += audio
                parts.append([key, (faders or {}).get(name, bus["fader_db"]), None, bus["pan"]])
            else:
                total += audio
        if self.has("drums") and parts:
            inserts = self.bus("drums")["inserts"]
            if inserts:
                drums, _ = daw.chain(self.cache, self.rate, daw.sum_key(parts), lambda: drums.astype(np.float32), inserts)
            total += daw.pan_gain(drums, self.bus("drums")["fader_db"], 0)
        return total

    def loud(self, audio, inside=None):
        x = audio if inside is None else audio[:, inside]
        return meters.lufs(x, self.rate)

    # ---------- stage 1: classify, pan, align ----------

    def setup_stage(self, pool):
        self.pan_guitars()
        self.pan_toms()
        self.pan_close_cymbals()
        self.align()
        # A positive alignment offset lengthens the song; every later measure uses the mixer's length.
        self.length = daw.length_of(self.song, self.recipe)
        self.dense = self.full_band_frames()

    def envelope(self, audio, hop_s=0.05):
        hop = int(hop_s * self.rate)
        power = (audio.astype(np.float64) ** 2).sum(axis=0)
        count = len(power) // hop
        return 10 * np.log10(power[: count * hop].reshape(count, hop).mean(axis=1) + 1e-12)

    def pan_guitars(self):
        """Doubled rhythm parts go hard left and right; a part with no double stays in the middle."""
        mono = [t for t in self.tracks_of("gtr") if t["channels"] == 1]
        envs = {t["name"]: self.envelope(self.raw(t)) for t in mono}
        pairs, used, scores = [], set(), {}
        for a in mono:
            for b in mono:
                if a["name"] < b["name"]:
                    ea, eb = envs[a["name"]], envs[b["name"]]
                    active = (ea > ea.max() - 40) | (eb > eb.max() - 40)
                    scores[(a["name"], b["name"])] = float(np.corrcoef(ea[active], eb[active])[0, 1]) if active.sum() > 10 else 0.0
        # A name that marks a double-track pairs first; then the parts whose level envelopes move together.
        def name_pair(a, b):
            base = lambda n: re.sub(r"dt\d*$", "", stemlib.words(n))
            return (a["double"] or b["double"]) and re.sub(r"^\d+", "", base(a["name"])) == re.sub(r"^\d+", "", base(b["name"]))
        ranked = sorted(scores, key=lambda k: (not name_pair(self.track(k[0]), self.track(k[1])), -scores[k]))
        for a, b in ranked:
            if a in used or b in used:
                continue
            if name_pair(self.track(a), self.track(b)) or scores[(a, b)] >= 0.75:
                pairs.append((a, b))
                used |= {a, b}
        for a, b in pairs:
            self.track(a)["pan"], self.track(b)["pan"] = -1.0, 1.0
        self.log("guitars.pan", "gtr", "kept", {"pairs": pairs, "envelope_correlation": {f"{a}+{b}": r2(v) for (a, b), v in scores.items()},
                                                "centred": [t["name"] for t in mono if t["name"] not in used]})

    def pan_toms(self):
        toms = [t for t in self.tracks_of("toms")]
        if len(toms) < 2:
            return
        pitch = {}
        for t in toms:
            f, p = signal.welch(self.raw(t).astype(np.float64).mean(axis=0), self.rate, nperseg=8192)
            band = (f > 50) & (f < 400)
            pitch[t["name"]] = float(f[band][np.argmax(p[band])])
        order = sorted(toms, key=lambda t: -pitch[t["name"]])
        for i, t in enumerate(order):
            t["pan"] = round(-0.4 + 0.8 * i / (len(order) - 1), 2) if t["channels"] == 1 else 0.0
        self.log("toms.pan", "toms", "kept", {"pitch_hz": {k: r2(v) for k, v in pitch.items()}, "high_to_low": [t["name"] for t in order]})

    def pan_close_cymbals(self):
        """Hat, ride and cymbal close mics sit where the overheads hear them."""
        oh = [t for t in self.tracks_of(cls="overheads") if t["channels"] == 2]
        if not oh:
            return
        over = sum(self.raw(t).astype(np.float64) for t in oh)
        for t in self.tracks_of("cymbals"):
            if t["class"] == "overheads" or t["channels"] != 1:
                continue
            close = self.raw(t)
            times = meters.detect_hits(close, self.rate)
            inside = meters.windows(close.shape[1], times, self.rate, (0, 60))
            if inside.sum() < self.rate * 0.2:
                continue
            hi = meters.filtered(over, self.rate, 4000, 12000)[:, inside]
            left, right = math.sqrt(float((hi[0] ** 2).sum())), math.sqrt(float((hi[1] ** 2).sum()))
            t["pan"] = round(float(np.clip((right - left) / (right + left) * 2, -1, 1)), 2)
            self.log("overheads.close-pan", t["name"], "kept", {"pan": t["pan"]})

    def align(self):
        """Polarity and timing of every multi-mic drum: each mic against the sum of the ones before it."""
        for group, band_hz in (("kick", [[40, 150], [2000, 6000]]), ("snare", [[150, 400], [2000, 6000]])):
            mics = [t for t in self.tracks_of(group) if not t["sample"]]
            if len(mics) < 2:
                continue
            low = lambda t: meters.band_db(meters.spectrum(self.raw(t), self.rate), *band_hz[0])
            mics.sort(key=lambda t: -low(t))
            ref = self.raw(mics[0])[0].astype(np.float64)
            rkey, _ = daw.stem_source(self.song, mics[0], self.length)
            times = (punchlib.drum_hits(ref[None], self.rate, group) * self.rate).astype(int)
            inside = meters.windows(len(ref), times, self.rate, (0, 100))
            running = ref.copy()
            for t in mics[1:]:
                skey, sread = daw.stem_source(self.song, t, self.length)
                result = self.cache.measured(daw.digest(rkey, skey, band_hz, "align-v2"), "align",
                                             lambda: self.best_alignment(running, sread()[0].astype(np.float64), inside, band_hz))
                t["invert"], t["offset_samples"] = result["invert"], result["offset_samples"]
                self.log(f"{group}.align", t["name"], "kept" if result["invert"] or result["offset_samples"] else "dropped: as recorded is best", result)
                _, aligned = daw.stem_source(self.song, t, self.length)
                running = running + aligned()[0].astype(np.float64)

    def best_alignment(self, ref, other, inside, bands):
        rate = self.rate
        a, b = np.where(inside, ref, 0), np.where(inside, other, 0)
        corr = signal.correlate(b, a, mode="full", method="fft")
        mid, reach = len(a) - 1, int(0.006 * rate)
        lag = int(np.argmax(np.abs(corr[mid - reach : mid + reach + 1]))) - reach
        filtered_ref = [meters.filtered(ref[None], rate, *band)[0][inside] for band in bands]

        def sum_gain(invert, lag):
            moved = np.roll(other, -lag) * (-1 if invert else 1)
            gains = []
            for band, fr in zip(bands, filtered_ref):
                fo = meters.filtered(moved[None], rate, *band)[0][inside]
                gains.append(10 * math.log10(((fr + fo) ** 2).sum() / ((fr**2).sum() + (fo**2).sum() + 1e-20) + 1e-20))
            return gains

        options = [(inv, l, sum_gain(inv, l)) for inv in (False, True) for l in sorted({0, lag})]
        best = max(options, key=lambda o: (round(o[2][0], 1), o[2][1]))
        return {"invert": best[0], "offset_samples": -best[1],
                "sum_gain_db": {f"{inv}/{-l}": [r2(g) for g in gains] for inv, l, gains in options}}

    def full_band_frames(self):
        """Samples where drums, bass and guitars all play: the frames every balance decision reads."""
        hop = int(0.1 * self.rate)
        count = self.length // hop
        active = np.ones(count, bool)
        for group in ("kick", "bass", "gtr"):
            members = self.tracks_of(group)
            if not members:
                continue
            e = None
            for t in members:
                x = self.raw(t).astype(np.float64)
                p = (x**2).sum(axis=0)[: count * hop].reshape(count, hop).mean(axis=1)
                e = p if e is None else e + p
            level = 10 * np.log10(e + 1e-20)
            on = level > np.percentile(level, 95) - 20
            on = np.convolve(on, np.ones(11), mode="same") > 0
            active &= on
        if active.mean() < 0.2:
            active[:] = True
        mask = np.repeat(active, hop)
        mask = np.concatenate([mask, np.zeros(self.length - len(mask), bool)])
        self.log("song.full-band", "song", "measured", {"seconds": r2(mask.sum() / self.rate), "of": r2(self.length / self.rate)})
        return mask

    # ---------- stage 2: per-track inserts ----------

    def tracks_stage(self, pool):
        jobs = [(str(self.song), str(self.cache.folder), self.rate, self.length, t) for t in self.recipe["tracks"]]
        results = pool.map(decide_track, jobs) if pool else map(decide_track, jobs)
        for name, inserts, decisions in results:
            self.track(name)["inserts"] = inserts
            self.recipe["decisions"].extend(decisions)

    # ---------- stage 3: drums first ----------

    def drums_stage(self, pool):
        mixer = self.rendered(pool)
        self.kick_blend(mixer)
        self.snare_blend(mixer)
        self.close_cymbal_blend(mixer)
        self.guitar_pairs(mixer)
        mixer = self.rendered(pool)
        self.kick_shape(mixer)
        self.snare_ring(mixer)

    def hit_level(self, audio, times):
        return float(np.median(meters.hit_stats(audio.astype(np.float32), self.rate, times)["level_db"]))

    def match_samples(self, mixer, group):
        """A drum sample sits at the close mics' hit level, so it reinforces the hits it doubles."""
        mics = [t for t in self.tracks_of(group) if not t["sample"]]
        for s in [t for t in self.tracks_of(group) if t["sample"]]:
            sample = mixer.node_audio(s["name"])
            st = (punchlib.drum_hits(sample, self.rate, group) * self.rate).astype(int)
            if not mics or len(st) < 2:
                continue
            total = sum(daw.pan_gain(mixer.node_audio(t["name"]), t["fader_db"], t["pan"]) for t in mics).astype(np.float32)
            mt = (punchlib.drum_hits(total, self.rate, group) * self.rate).astype(int)
            if len(mt) < 2:
                continue
            gain = round(self.hit_level(total, mt) - self.hit_level(sample, st), 2)
            s["fader_db"] = gain
            self.log(f"{group}.sample-blend", s["name"], "kept", {"fader_db": gain})

    def blend_grid(self, mixer, mics, score, steps=(0.0, -4.0, -8.0, -12.0, None)):
        """Faders for every mic but the first: a full grid for up to two more mics, else one mic at a time in order
        (each settled with the ones before it). score(sum) -> (ok, value, numbers); the highest value wins."""
        audio = {t["name"]: daw.pan_gain(mixer.node_audio(t["name"]), 0, t["pan"]) for t in mics}
        tried = []

        def run(faders):
            total = sum(audio[t["name"]] * (0 if f is None else 10 ** (f / 20)) for t, f in zip(mics, faders))
            ok, value, numbers = score(total)
            tried.append({"faders": list(faders), **numbers, "ok": ok})
            return ok, value

        if len(mics) <= 3:
            combos = [[0.0] + list(c) for c in itertools.product(steps, repeat=len(mics) - 1)]
            best = None
            for faders in combos:
                ok, value = run(faders)
                if ok and (best is None or value > best[1]):
                    best = (faders, value)
            return best, tried
        faders = [0.0] + [None] * (len(mics) - 1)
        run(faders)
        best = None
        for index in range(1, len(mics)):
            options = []
            for step in steps:
                trial = faders[:index] + [step] + faders[index + 1 :]
                ok, value = run(trial)
                if ok:
                    options.append((value, trial))
            if options:
                value, faders = max(options, key=lambda o: o[0])
                best = (faders, value)
        return best, tried

    def kick_blend(self, mixer):
        mics = [t for t in self.tracks_of("kick") if not t["sample"]]
        if len(mics) >= 2:
            mics.sort(key=lambda t: -meters.band_db(meters.spectrum(mixer.node_audio(t["name"]), self.rate), 40, 120))
            ref = mixer.node_audio(mics[0]["name"]).astype(np.float64)
            times = punchlib.drum_hits(ref, self.rate, "kick")
            base = None

            def score(total):
                nonlocal base
                e = punchlib.env(punchlib.bandpass(total, self.rate, 40, 120), self.rate)
                peak = float(np.median([punchlib.db(e[max(0, int(t / punchlib.ENV_S) - 2): int(t / punchlib.ENV_S) + 8].max()) for t in times]))
                decay = -float(np.nanmedian(punchlib.tail(e, times)))
                click = punch_of(total, self.rate, times, [])["kick_click"]
                if base is None:
                    base = peak
                # The low body must stay within 3 dB of the loudest-low mic alone; then the fastest low decay wins.
                return peak >= base - 3, decay + 0.25 * (click or 0), {"low_peak_db": r2(peak), "low_decay_db": r2(decay), "click_prominence": click}

            best, tried = self.blend_grid(mixer, mics, score)
            if best:
                for t, f in zip(mics, best[0]):
                    t["fader_db"] = -60.0 if f is None else f
            self.log("kick.blend", "kick", "kept" if best and any(best[0][1:]) else "dropped: all at 0 is best",
                     {"mics": [t["name"] for t in mics], "chosen": best[0] if best else None, "tried": tried})
        self.match_samples(mixer, "kick")

    def snare_blend(self, mixer):
        mics = [t for t in self.tracks_of("snare") if not t["sample"]]
        if len(mics) >= 2:
            mics.sort(key=lambda t: -meters.band_db(meters.spectrum(mixer.node_audio(t["name"]), self.rate), 150, 350))
            ref = mixer.node_audio(mics[0]["name"]).astype(np.float64)
            times = punchlib.drum_hits(ref, self.rate, "snare")
            base = {}

            def score(total):
                found = punch_of(total, self.rate, [], times)
                if not base:
                    base.update(found)
                body, crack = found["snare_body"] or 0, found["snare_crack"] or 0
                # Body against wires: the most body whose crack stays within 1 dB of the body mic alone.
                return crack >= (base["snare_crack"] or 0) - 1, body + crack, {"body": body, "crack": crack}

            best, tried = self.blend_grid(mixer, mics, score)
            if best:
                for t, f in zip(mics, best[0]):
                    t["fader_db"] = -60.0 if f is None else f
            self.log("snare.blend", "snare", "kept" if best and any(best[0][1:]) else "dropped: all at 0 is best",
                     {"mics": [t["name"] for t in mics], "chosen": best[0] if best else None, "tried": tried})
        self.match_samples(mixer, "snare")

    def close_cymbal_blend(self, mixer):
        oh = [t for t in self.tracks_of(cls="overheads")]
        if not oh:
            return
        over = sum(daw.pan_gain(mixer.node_audio(t["name"]), 0, t["pan"]) for t in oh)
        for t in self.tracks_of("cymbals"):
            if t["class"] == "overheads":
                continue
            close = mixer.node_audio(t["name"])
            inside = meters.windows(close.shape[1], meters.detect_hits(close, self.rate), self.rate, (0, 300))
            if inside.sum() < self.rate * 0.5:
                continue
            c = self.loud(daw.pan_gain(close, 0, t["pan"]), inside)
            o = self.loud(over, inside)
            t["fader_db"] = round(float(np.clip(o - 8 - c, -30, 6)), 2)
            self.log("overheads.close-blend", t["name"], "kept", {"close_lufs": r2(c), "oh_lufs": r2(o), "fader_db": t["fader_db"]})

    def guitar_pairs(self, mixer):
        for t in self.tracks_of("gtr"):
            if t["pan"] != -1.0:
                continue
            partner = next((u for u in self.tracks_of("gtr") if u["pan"] == 1.0 and u is not t and
                            abs(self.loud(mixer.node_audio(u["name"]), self.dense) - self.loud(mixer.node_audio(t["name"]), self.dense)) < 20), None)
            if partner is None:
                continue
            l, r = self.loud(mixer.node_audio(t["name"]), self.dense), self.loud(mixer.node_audio(partner["name"]), self.dense)
            t["fader_db"], partner["fader_db"] = round((r - l) / 2, 2), round((l - r) / 2, 2)
            self.log("guitars.pair", f"{t['name']}+{partner['name']}", "kept", {"left_lufs": r2(l), "right_lufs": r2(r)})

    def snare_ring(self, mixer):
        """snare.ring-cut: a narrow resonance 300-900 Hz standing 6 dB over its neighbourhood in the blended snare."""
        if not self.has("snare"):
            return
        audio, key = mixer.summed("snare", self.recipe["tracks"] + self.recipe["buses"])
        node = Node("snare", audio.astype(np.float32), key, self.cache, self.rate, {})
        peaks = narrow_peaks(node, 300, 900)
        if peaks:
            f, rise = peaks[0]
            eq_cut_if(node, "snare.ring-cut", True, {"peak_hz": r2(f), "rise_db": r2(rise)}, f, [-4.0, -6.0], 4.0,
                      (f / 1.08, f * 1.08), keep=(150, 250))
        else:
            node.record("snare.ring-cut", "dropped: not needed", {"narrow_peaks_over_6db": 0})
        self.bus("snare")["inserts"].extend(node.inserts)
        self.recipe["decisions"].extend(node.decisions)

    def kick_shape(self, mixer):
        """The kick's low tail: a hit-gated decay below 150 Hz, the smallest depth that reaches the decay target."""
        bus = self.bus("kick")
        if bus is None:
            return
        audio, key = mixer.summed("kick", self.recipe["tracks"] + self.recipe["buses"])
        audio = audio.astype(np.float32)
        node = Node("kick", audio, key, self.cache, self.rate, {})
        times = punchlib.drum_hits(audio, self.rate, "kick")
        before = punch_of(audio, self.rate, times, [])
        numbers = {"kick_tail_db": before.get("kick_tail"), "target_db": self.KICK_TAIL_DB}
        cands = [{"plugin": "Shape", "settings": {"band_hz": [None, 150], "hold_ms": 30.0, "fade_ms": 40.0, "depth_db": d, "floor_db": 30}}
                 for d in (4.0, 8.0, 12.0, 18.0)]

        def judge(out, key):
            after = punch_of(out, self.rate, times, [])
            return after["kick_tail"] <= self.KICK_TAIL_DB, {"kick_tail_db": after["kick_tail"], "kick_low": after["kick_low"]}

        needed = before.get("kick_tail") is not None and before["kick_tail"] > self.KICK_TAIL_DB
        if not node.attempt("kick.tail", needed, numbers, cands, judge) and needed:
            # None reached the target: keep the deepest if it shortened the tail by at least 2 dB.
            out, k = node.render({"id": "kick.tail", **cands[-1]})
            after = punch_of(out, self.rate, times, [])
            if after["kick_tail"] <= before["kick_tail"] - 2:
                node.inserts.append({"id": "kick.tail", **cands[-1]})
                node.audio, node.key = out, k
                node.record("kick.tail", "kept: deepest, short of target", {"kick_tail_db": after["kick_tail"]})
        # Weight: a bell at the kick's own low peak, kept if it lifts the low body against its 200-400 Hz.
        f, p = signal.welch(node.audio.astype(np.float64).sum(axis=0), self.rate, nperseg=16384)
        band = (f >= 50) & (f <= 100)
        peak_hz = float(f[band][np.argmax(p[band])])
        low0, mud0 = node.band(45, 110), node.band(200, 400)

        def weight_judge(out, key):
            low, mud = node.band(45, 110, out, key), node.band(200, 400, out, key)
            return (low - mud) - (low0 - mud0) >= 1.0, {"low_minus_mud_change": r2((low - mud) - (low0 - mud0))}

        node.attempt("kick.weight", True, {"peak_hz": r2(peak_hz)},
                     [{"plugin": "PeakFilter", "settings": {"cutoff_frequency_hz": round(peak_hz, 1), "gain_db": g, "q": 1.2}} for g in (2.0, 3.0)],
                     weight_judge)
        mud, thump = node.band(200, 400), node.band(50, 100)
        eq_cut_if(node, "kick.mud-cut", mud > thump - 10, {"200_400_db": r2(mud), "50_100_db": r2(thump)}, 300, [-3], 1.0, (200, 400), keep=(50, 100))
        bus["inserts"].extend(node.inserts)
        self.recipe["decisions"].extend(node.decisions)

    # ---------- stage 4: balance ----------

    def balance_stage(self, pool):
        mixer = self.rendered(pool)
        self.static_balance(mixer)
        self.punch_faders(self.rendered(pool))

    def reference_group(self):
        return next(g for g in ("gtr", "bass", "synths", "kick") if self.has(g))

    def static_balance(self, mixer):
        ref = self.reference_group()
        level_ref = self.loud(self.post_fader(mixer, ref), self.dense)
        for name, target in self.TARGETS_LU.items():
            if not self.has(name) or name == ref:
                continue
            level = self.loud(daw.pan_gain(mixer.node_audio(name), 0, 0), self.dense)
            if level < -100:
                continue
            self.bus(name)["fader_db"] = round(target + level_ref - level, 2)
            self.log("balance.fader", name, "kept", {"bus_lufs": r2(level), "reference": ref, "reference_lufs": r2(level_ref),
                                                    "target_lu": target, "fader_db": self.bus(name)["fader_db"]})
        self.static_faders = {b["name"]: b["fader_db"] for b in self.recipe["buses"]}
        if self.has("toms"):
            toms = mixer.node_audio("toms")
            times = meters.detect_hits(toms, self.rate)
            fills = meters.windows(toms.shape[1], times, self.rate, (-50, 600))
            if fills.sum() > self.rate * 0.3:
                t_level, g_level = self.loud(toms, fills), self.loud(self.post_fader(mixer, ref), fills)
                # Toms that barely play (bleed found as hits) are not pushed up to the guitars.
                self.bus("toms")["fader_db"] = round(float(np.clip(-3.0 + g_level - t_level, -12, 6)), 2)
                self.log("balance.fader", "toms", "kept", {"toms_lufs_in_fills": r2(t_level), "reference_lufs_in_fills": r2(g_level),
                                                           "fader_db": self.bus("toms")["fader_db"]})

    def drum_times(self, mixer):
        kick = punchlib.drum_hits(mixer.node_audio("kick"), self.rate, "kick") if self.has("kick") else np.array([])
        snare = punchlib.drum_hits(mixer.node_audio("snare"), self.rate, "snare") if self.has("snare") else np.array([])
        return kick, snare

    def punch_faders(self, mixer, targets=None):
        """Kick and snare faders move (at most 6 dB either side of the static balance) until their hits stand out of
        the mix by the target. A fader whose travel does not move the measure (wrong hits) stays where it is."""
        kick_s, snare_s = self.drum_times(mixer)
        self.kick_s, self.snare_s = kick_s, snare_s
        targets = targets or {"kick_low": self.KICK_LOW_PROMINENCE, "snare_body": self.SNARE_BODY_PROMINENCE}
        for name, key, target, times in (("kick", "kick_low", targets["kick_low"], kick_s),
                                         ("snare", "snare_body", targets["snare_body"], snare_s)):
            if not self.has(name) or len(times) < 4:
                continue
            start = self.bus(name)["fader_db"]
            base = self.static_faders.get(name, start)
            tried = []
            chosen = start
            for step in [0, 1, 2, 3, 4, 5, 6, -1, -2, -3, -4, -5, -6]:
                # At most 6 dB over and 3 dB under the static balance, however many passes run.
                if not -6 <= start + step - base <= 6:
                    continue
                faders = {name: start + step}
                found = punch_of(self.mix_of(mixer, faders), self.rate, kick_s, snare_s)[key]
                tried.append({"step_db": step, key: found})
            ups = [t for t in tried if t["step_db"] >= 0]
            if ups[-1][key] - tried[0][key] < 1.0 and tried[0][key] < target:
                reach = tried[0]
            else:
                reach = min(tried, key=lambda t: (abs(t[key] - target), abs(t["step_db"])))
            chosen = start + reach["step_db"]
            self.bus(name)["fader_db"] = round(chosen, 2)
            self.log("balance.punch", name, "kept" if reach["step_db"] else "dropped: on target",
                     {"target_db": target, "tried": tried, "fader_db": round(chosen, 2)})

    # ---------- stage 5: context moves on the full mix ----------

    def context_stage(self, pool):
        self.duck_bass(self.rendered(pool))
        self.lowmid_owner(self.rendered(pool))
        self.source_sub(self.rendered(pool))
        self.tilt(self.rendered(pool), "presence", (2000, 4000), (125, 250), self.PRESENCE_TILT_DB, 1,
                  lambda g: {"plugin": "PeakFilter", "settings": {"cutoff_frequency_hz": 3000.0, "gain_db": g, "q": 0.7}}, (1.5, 3.0, 4.5, 6.0))
        # Air and top follow the cymbals as recorded: a bright cymbal source is left bright, a dull one gets lifted.
        shift = -self.CYMBAL_AIR_SLOPE * (self.cymbal_air(self.rendered(pool)) - self.CYMBAL_AIR_REF_DB)
        self.tilt(self.rendered(pool), "air", (8000, 16000), (1000, 4000), self.AIR_TILT_DB + shift, 1,
                  lambda g: {"plugin": "HighShelfFilter", "settings": {"cutoff_frequency_hz": 7000.0, "gain_db": g, "q": 0.707}}, (2.0, 4.0, 6.0))
        self.tilt(self.rendered(pool), "top", (11300, 16000), (5600, 11300), self.TOP_TILT_DB + shift, 1,
                  lambda g: {"plugin": "HighShelfFilter", "settings": {"cutoff_frequency_hz": 12000.0, "gain_db": g, "q": 0.707}}, (2.0, 4.0, 6.0))
        self.lead_space(pool)
        self.attack_eq(self.rendered(pool), "kick", "kick.click", "kick_click", 4000.0, self.KICK_CLICK_PROMINENCE)
        self.attack_eq(self.rendered(pool), "snare", "snare.crack", "snare_crack", 5000.0, self.SNARE_CRACK_PROMINENCE)

    def source_sub(self, mixer):
        """Sub as the sources carry it: a kick or bass whose 25-45 Hz stands within SUB_SOURCE_DB of its own 60-100 Hz
        (boom under the note or the hit) gets a low shelf until it sits SUB_CUT_DEPTH_DB under that. Nothing is boosted: a source with no sub
        stays without it."""
        for name in ("kick", "bass"):
            if not self.has(name):
                continue
            audio = mixer.node_audio(name)

            def tilt(x):
                spec = meters.spectrum(x[:, self.dense], self.rate)
                return meters.band_db(spec, 25, 45) - meters.band_db(spec, 60, 100)

            t0 = tilt(audio)
            numbers = {"sub_tilt_db": r2(t0), "rule_db": self.SUB_SOURCE_DB}
            if t0 <= self.SUB_SOURCE_DB:
                self.log("sub.cut", name, "dropped: not needed", numbers)
                continue
            tried, kept = [], None
            for gain in (-2.0, -4.0, -6.0, -8.0, -10.0):
                insert = {"id": "sub.cut", "plugin": "LowShelfFilter", "settings": {"cutoff_frequency_hz": 45.0, "gain_db": gain, "q": 0.707}}
                out, key = daw.chain(self.cache, self.rate, mixer.nodes[name][0], lambda: audio, [insert])
                t = tilt(out)
                tried.append({"gain_db": gain, "sub_tilt_db": r2(t)})
                kept = insert
                if t <= self.SUB_SOURCE_DB - self.SUB_CUT_DEPTH_DB:
                    break
            self.bus(name)["inserts"].append(kept)
            self.log("sub.cut", name, "kept", numbers | {"tried": tried})

    def lead_space(self, pool):
        """A guitar that plays in the middle through the song (a melody or lead, no double) gets a stereo plate on a
        send, the smallest that widens the 500 Hz-2 kHz side by 1.5 dB without lifting that band by more than 1 dB."""
        mixer = self.rendered(pool)
        centred = []
        for t in self.tracks_of("gtr"):
            if t["pan"] != 0.0:
                continue
            audio = mixer.node_audio(t["name"])
            if t["channels"] == 2 and meters.side_mid_db(audio) > -10:
                continue
            x = audio[:, self.dense].astype(np.float64)
            hop = int(0.1 * self.rate)
            count = x.shape[1] // hop
            level = 10 * np.log10((x[:, : count * hop] ** 2).sum(axis=0).reshape(count, hop).mean(axis=1) + 1e-20)
            if count and (level > level.max() - 30).mean() >= 0.5:
                centred.append(t)
        if not centred:
            self.log("guitars.lead-space", "gtr", "dropped: not needed", {"why": "no guitar plays alone in the middle"})
            return

        def width(audio):
            x = meters.filtered(audio[:, self.dense], self.rate, 500, 2000)
            return meters.side_mid_db(x), meters.db(float((x**2).sum()))

        w0, l0 = width(self.mix_of(mixer))
        self.recipe["buses"].append({"name": "gtr-space", "inserts": [{"id": "guitars.lead-space", "plugin": "DragonflyPlateReverb", "settings": {
            "dry_level": 0.0, "wet_level": 100.0, "algorithm": "Nested", "width": 100.0, "predelay_ms": 30.0, "decay_s": 1.4,
            "low_cut_hz": 200.0, "high_cut_hz": 8000.0, "dampen_hz": 6000.0}}], "fader_db": 0.0, "pan": 0, "out": "mix", "sends": []})
        tried, kept = [], None
        for send in (-16.0, -12.0, -8.0, -4.0):
            for t in centred:
                t["sends"] = [{"bus": "gtr-space", "gain_db": send}]
            m = self.rendered(pool)
            w, l = width(self.mix_of(m))
            ok = w - w0 >= 0.5 and l - l0 <= 1.0
            tried.append({"send_db": send, "side_mid_change_db": r2(w - w0), "band_change_db": r2(l - l0), "ok": ok})
            if ok:
                kept = send
                if w - w0 >= 1.5:
                    break
            elif kept is not None:
                break
        if kept is None:
            for t in centred:
                t["sends"] = []
            self.recipe["buses"].remove(self.bus("gtr-space"))
        else:
            for t in centred:
                t["sends"] = [{"bus": "gtr-space", "gain_db": kept}]
        self.log("guitars.lead-space", "gtr", "kept" if kept is not None else "dropped: did not work",
                 {"centred": [t["name"] for t in centred], "side_mid_db": r2(w0), "tried": tried})

    def cymbal_air(self, mixer):
        """The cymbal bus's own 8-16 kHz against its 1-4 kHz, before any tone move: how bright the cymbals were recorded."""
        if not self.has("cymbals"):
            return self.CYMBAL_AIR_REF_DB
        audio, _ = mixer.summed("cymbals", self.recipe["tracks"] + self.recipe["buses"])
        spec = meters.spectrum(audio[:, self.dense], self.rate)
        found = meters.band_db(spec, 8000, 16000) - meters.band_db(spec, 1000, 4000)
        self.log("overheads.brightness", "cymbals", "measured", {"air_db": r2(found), "reference_db": self.CYMBAL_AIR_REF_DB})
        return found

    def steady(self, mixer, name):
        """True if the group plays through most of the full-band frames (toms that only play fills do not)."""
        audio = mixer.node_audio(name)[:, self.dense].astype(np.float64)
        hop = int(0.1 * self.rate)
        count = audio.shape[1] // hop
        if count == 0:
            return False
        level = 10 * np.log10((audio[:, : count * hop] ** 2).sum(axis=0).reshape(count, hop).mean(axis=1) + 1e-20)
        return float((level > np.percentile(level, 95) - 30).mean()) >= 0.5

    def attack_eq(self, mixer, name, move, key, hz, target):
        """Click and crack: after the tone moves, a bell on the drum's attack band until its hits stand out of the
        full mix by the rule (the guitars' presence is what masks them)."""
        if not self.has(name):
            return
        kick_s, snare_s = self.drum_times(mixer)
        before = punch_of(self.mix_of(mixer), self.rate, kick_s, snare_s)[key]
        numbers = {key: before, "rule_db": target}
        if before is None or before >= target:
            self.log(move, name, "dropped: not needed", numbers)
            return
        tried, kept = [], None
        for gain in (2.0, 4.0, 6.0):
            insert = {"id": move, "plugin": "PeakFilter", "settings": {"cutoff_frequency_hz": hz, "gain_db": gain, "q": 0.8}}
            out, k = daw.chain(self.cache, self.rate, mixer.nodes[name][0], lambda: mixer.node_audio(name), [insert])
            found = punch_of(self.mix_of(mixer, replace={name: (out, k)}), self.rate, kick_s, snare_s)[key]
            ok = found >= before + 0.5
            tried.append({"gain_db": gain, key: found, "ok": ok})
            if ok:
                kept = insert
                if found >= target:
                    break
        if kept:
            self.bus(name)["inserts"].append(kept)
        self.log(move, name, "kept" if kept else "dropped: did not work", numbers | {"tried": tried})

    def duck_bass(self, mixer, pool=None):
        """Low-end ownership: the bass, then the guitars, dip below 150 Hz on kick hits, kept only if the kick's low
        punch in the full mix rises by 0.5 dB and the source keeps its lows between hits (within 1 dB)."""
        if not self.has("kick"):
            return
        rate, length = self.rate, mixer.length
        kick_hits = (punchlib.drum_hits(mixer.node_audio("kick"), rate, "kick") * rate).astype(int)
        kick_level = float(np.median(meters.hit_stats(mixer.node_audio("kick"), rate, kick_hits)["level_db"]))
        between = ~meters.windows(length, kick_hits, rate, (-5, 150))
        replaced = {}
        for name in ("bass", "gtr"):
            if not self.has(name):
                continue
            base_punch = punch_of(self.mix_of(mixer, replace=replaced), rate, kick_hits / rate, [])["kick_low"]
            if base_punch is not None and base_punch >= self.KICK_LOW_PROMINENCE:
                self.log(f"kick.duck-{name}", name, "dropped: not needed", {"kick_low": base_punch, "rule_db": self.KICK_LOW_PROMINENCE})
                continue
            pf = self.post_fader(mixer, name)
            held0 = meters.masked_band_db(pf, rate, 40, 150, between)
            tried, kept = [], None
            for r in (3.0, 6.0):
                insert = {"id": f"kick.duck-{name}", "plugin": "Duck", "settings": {"key": "kick", "band_hz": [None, 150],
                          "threshold_db": round(kick_level - 15, 1), "ratio": 4, "range_db": r, "attack_ms": 1, "release_ms": 80}}
                out, key = daw.chain(self.cache, rate, mixer.nodes[name][0], lambda: mixer.node_audio(name), [insert],
                                     {"kick": mixer.node_audio("kick")}, {"kick": mixer.nodes["kick"][0]})
                held = meters.masked_band_db(daw.pan_gain(out, self.bus(name)["fader_db"], self.bus(name)["pan"]), rate, 40, 150, between)
                punch = punch_of(self.mix_of(mixer, replace=replaced | {name: (out, key)}), rate, kick_hits / rate, [])["kick_low"]
                ok = punch >= base_punch + 0.5 and held0 - held <= 1.0
                tried.append({"range_db": r, "kick_low_prominence": punch, "between_change_db": r2(held - held0), "kept": ok})
                if ok:
                    kept, kept_audio = insert, (out, key)
            if kept:
                self.bus(name)["inserts"].append(kept)
                replaced[name] = kept_audio
            self.log(f"kick.duck-{name}", name, "kept" if kept else "dropped: did not work", {"kick_low_before": base_punch, "tried": tried})

    LOWMID = [125, 160, 200]

    def lowmid_owner(self, mixer):
        """A low-mid build-up (125-200 Hz over the mean of 63 and 500 Hz) is cut on the source that owns it."""
        mix = self.mix_of(mixer)
        centers = [63, 125, 160, 200, 500]
        levels = third_levels(mix, self.rate, centers, self.dense)
        build = max(levels[c] for c in self.LOWMID) - (levels[63] + levels[500]) / 2
        numbers = {"thirds_db": {c: r2(v) for c, v in levels.items()}, "buildup_db": r2(build), "trigger_db": self.LOWMID_BUILDUP_DB}
        if build <= self.LOWMID_BUILDUP_DB:
            self.log("lowmid.cut", "mix", "dropped: not needed", numbers)
            return
        peak = max(self.LOWMID, key=lambda c: levels[c])
        shares = {n: third_levels(self.leaf_out(mixer, n), self.rate, [peak], self.dense)[peak] for n in self.leaves()}
        owner = max(shares, key=shares.get)
        numbers |= {"peak_hz": peak, "owner_db": {n: r2(v) for n, v in shares.items()}, "owner": owner}
        tried, kept = [], None
        for gain in (-2.0, -3.0, -4.5, -6.0):
            insert = {"id": "lowmid.cut", "plugin": "PeakFilter", "settings": {"cutoff_frequency_hz": float(peak), "gain_db": gain, "q": 1.4}}
            out, key = daw.chain(self.cache, self.rate, mixer.nodes[owner][0], lambda: mixer.node_audio(owner), [insert])
            bus = self.bus(owner)
            new = self.mix_of(mixer, replace={owner: (out, key)})
            lv = third_levels(new, self.rate, centers, self.dense)
            b = max(lv[c] for c in self.LOWMID) - (lv[63] + lv[500]) / 2
            # Each owner keeps the band that is its job below the cut: kick thump, bass fundamentals, guitar chug.
            keep = {"kick": 63, "bass": 80}.get(owner, 100)
            chug_before = third_levels(self.leaf_out(mixer, owner), self.rate, [keep], self.dense)[keep]
            chug_after = third_levels(daw.pan_gain(out, bus["fader_db"], bus["pan"]), self.rate, [keep], self.dense)[keep]
            ok = b < build - 0.5 and chug_before - chug_after <= 1.5
            tried.append({"gain_db": gain, "buildup_db": r2(b), "owner_100hz_change": r2(chug_after - chug_before), "ok": ok})
            if ok:
                kept = insert
                if b <= self.LOWMID_BUILDUP_DB:
                    break
        if kept:
            self.bus(owner)["inserts"].append(kept)
        self.log("lowmid.cut", owner, "kept" if kept else "dropped: did not work", numbers | {"tried": tried})

    def tilt(self, mixer, move, upper, lower, target, direction, make, gains):
        """A broad tonal balance: band `upper` against band `lower` over the full-band frames. When it is off the genre
        rule by more than 1 dB in `direction`, the source that owns the moved band gets a broad filter, the smallest that
        reaches the rule; what one source cannot do, the mix bus does, up to 3 dB."""
        moved = upper if direction > 0 or move == "sub.cut" else lower

        def measure(audio):
            spec = meters.spectrum(audio[:, self.dense], self.rate)
            return meters.band_db(spec, *upper) - meters.band_db(spec, *lower)

        t0 = measure(self.mix_of(mixer))
        numbers = {"tilt_db": r2(t0), "rule_db": target, "bands": [upper, lower]}
        off = (target - t0) * direction
        if off <= 1.0:
            self.log(move, "mix", "dropped: not needed", numbers)
            return
        # Only a source that plays through the song can own a broad tone move; fill-only toms carry cymbal bleed.
        shares = {n: meters.band_db(meters.spectrum(self.leaf_out(mixer, n)[:, self.dense], self.rate), *moved)
                  for n in self.leaves() if self.steady(mixer, n)}
        numbers |= {"owner_db": {n: r2(v) for n, v in shares.items()}}
        reached, replaced = t0, {}
        # The two sources that own most of the moved band, in turn: the second only if the first cannot reach the rule.
        for owner in sorted(shares, key=shares.get, reverse=True)[:2]:
            if (target - reached) * direction <= 0.5:
                break
            tried, kept, start = [], None, reached
            for gain in gains:
                insert = {"id": move, **make(gain)}
                out, key = daw.chain(self.cache, self.rate, mixer.nodes[owner][0], lambda: mixer.node_audio(owner), [insert])
                t = measure(self.mix_of(mixer, replace=replaced | {owner: (out, key)}))
                ok = (t - start) * direction >= 0.5 and (t - target) * direction <= 1.0
                tried.append({"gain_db": gain, "tilt_db": r2(t), "ok": ok})
                if ok:
                    kept, reached, kept_audio = insert, t, (out, key)
                    if (target - t) * direction <= 0.5:
                        break
            if kept:
                self.bus(owner)["inserts"].append(kept)
                replaced[owner] = kept_audio
            self.log(move, owner, "kept" if kept else "dropped: did not work", numbers | {"tried": tried})
        rest = (target - reached) * direction
        if rest > 1.0 and move != "sub.cut":
            gain = round(float(min(3.0, rest)) * direction, 2)
            insert = make(gain)
            self.recipe["mix"]["inserts"].append({"id": f"mix-bus.{move}", **insert})
            self.log(f"mix-bus.{move}", "mix", "kept", {"rest_db": r2(rest), "gain_db": gain})

    # ---------- stage 6: drum bus glue, judged at matched loudness ----------

    def glue_stage(self, pool):
        mixer = self.rendered(pool)
        kick_s, snare_s = self.drum_times(mixer)
        base_mix = self.mix_of(mixer)
        base = punch_of(base_mix, self.rate, kick_s, snare_s)
        drums_in = mixer.node_audio("drums")
        level = self.hit_level(drums_in, (np.concatenate([kick_s, snare_s]) * self.rate).astype(int))
        tried, kept = [], None
        for depth in (4.0, 7.0):
            insert = {"id": "drum-bus.glue", "plugin": ZL_COMP, "settings": zl_comp(level - depth, 4, 10, 100)}
            self.bus("drums")["inserts"] = [insert]
            m = self.rendered(pool)
            mix = self.mix_of(m)
            mix = mix * 10 ** ((meters.lufs(base_mix, self.rate) - meters.lufs(mix, self.rate)) / 20)
            found = punch_of(mix, self.rate, kick_s, snare_s)
            drop = meters.lufs(drums_in, self.rate) - meters.lufs(m.node_audio("drums"), self.rate)
            ok = all(found[k] >= base[k] - 0.5 for k in ("kick_low", "snare_body", "snare_crack") if base.get(k) is not None) and 0.5 <= drop <= 4
            tried.append({"settings": insert["settings"], **found, "loudness_drop_db": r2(drop), "kept": ok})
            if ok:
                kept = insert
        self.bus("drums")["inserts"] = [kept] if kept else []
        self.log("drum-bus.glue", "drums", "kept" if kept else "dropped: punch did not hold", {"before": base, "tried": tried})

    def rebalance_stage(self, pool):
        """Downstream re-decided once after the upstream changes. The punch that counts is the master's: a trial
        master at the release loudness shows how much each drum loses there, and the mix targets add that back."""
        mixer = self.rendered(pool)
        best, _ = self.master_select(mixer, mixer.nodes["mix"][1])
        loss = best[2] if best else {}
        targets = {"kick_low": self.MASTER_KICK_PROMINENCE + max(0.0, loss.get("kick_low", 0.0)),
                   "snare_body": self.MASTER_SNARE_PROMINENCE + max(0.0, loss.get("snare_body", 0.0))}
        self.log("balance.punch-targets", "mix", "measured", {"trial_master_loss_db": loss, "mix_targets_db": {k: r2(v) for k, v in targets.items()}})
        self.punch_faders(mixer, targets)

    # ---------- stage 7: the master ----------

    def master_chain(self, gain_db, clip_db=None):
        # Drive, then a clipper at a fixed level takes the hits' peaks, then the limiter holds the true peak.
        chain = [{"id": "master.drive", "plugin": "Gain", "settings": {"gain_db": float(round(gain_db, 4))}}]
        if clip_db is not None:
            kind, level = clip_db if isinstance(clip_db, tuple) else ("Clipping", clip_db)
            chain.append({"id": "master.clip", "plugin": kind, "settings": {"threshold_db": float(level)}})
        chain.append({"id": "master.limit", "plugin": "ZL Limiter", "settings": {
            "output_ceiling_output_ceiling": self.CEILING, "true_peak_true_peak": True, "oversampling_oversampling": "4x",
            "lookahead_lookahead": 2.0, "recovery_recovery": 10.0, "attack_attack": 10.0, "release_release": 50.0}})
        return chain

    def trial(self, mix, mix_key, clip_db=None):
        """The mix through the master chain at the release loudness, by a guarded secant search on the drive."""
        target = self.TARGET_LUFS

        def run(gain):
            chain = self.master_chain(gain, clip_db)
            out, key = daw.chain(self.cache, self.rate, mix_key, lambda: mix, chain)
            level = self.cache.measured(key, "lufs", lambda: meters.lufs(out, self.rate))
            return chain, out, level

        gain = getattr(self, "_last_drive", None) or target - meters.lufs(mix, self.rate)
        points = []
        for _ in range(12):
            if gain > 40:
                break
            chain, out, level = run(round(gain, 4))
            points.append((gain, level))
            if abs(level - target) <= 0.003:
                break
            if len(points) >= 2 and abs(points[-1][1] - points[-2][1]) > 1e-6:
                slope = min(max((points[-1][1] - points[-2][1]) / (points[-1][0] - points[-2][0]), 0.05), 1.0)
            else:
                slope = 1.0
            gain = min(gain + (target - level) / slope, gain + 6)
        if abs(points[-1][1] - target) > 0.003:
            raise RuntimeError(f"master missed {target} LUFS: {[(round(float(g), 1), round(float(l), 2)) for g, l in points]}")
        self._last_drive = gain
        return out, chain, level

    def crest_loss(self, mix, master, kick_s, snare_s):
        a, b = punch_of(mix, self.rate, kick_s, snare_s), punch_of(master, self.rate, kick_s, snare_s)
        return {k: r2(a[k] - b[k]) for k in ("kick_low", "kick_click", "snare_body", "snare_crack") if a.get(k) is not None and b.get(k) is not None}

    def master_select(self, mixer, mix):
        """The master chain at the release loudness that loses the least punch: a clipper at -3 or -5 dBFS after the
        drive, then the limiter. Returns (chain, crest loss, master punch) or None."""
        mix_key = mixer.nodes["mix"][0]
        kick_s, snare_s = self.drum_times(mixer)
        best, tried = None, []
        for clip in (-2.0, -3.0):
            self._last_drive = None
            try:
                out, chain, level = self.trial(mix, mix_key, clip_db=clip)
            except RuntimeError as error:
                tried.append({"clip_dbfs": clip, "reached": False, "why": str(error)[:120]})
                continue
            loss = self.crest_loss(mix, out, kick_s, snare_s)
            worst = max(loss.values(), default=0)
            tried.append({"clip_dbfs": clip, "crest_loss_db": loss, "drive_db": chain[0]["settings"]["gain_db"]})
            if best is None or worst < best[0]:
                best = (worst, chain, loss, punch_of(out, self.rate, kick_s, snare_s))
        return best, tried

    LOUDNESS_STEPS = (-7.0, -7.5, -8.0)

    def master_stage(self, pool):
        """-7 LUFS is accepted only if kick and snare lose at most 3 dB of punch; otherwise the release loudness
        steps down by 0.5 LU (to -8 at most) and the quietest step is kept with its gate failing if none passes."""
        mixer = self.rendered(pool)
        tried_all, chosen = [], None
        for target in self.LOUDNESS_STEPS:
            self.TARGET_LUFS = target
            best, tried = self.master_select(mixer, mixer.nodes["mix"][1])
            tried_all.append({"target_lufs": target, "tried": tried})
            if best is None:
                continue
            chosen = (target, best)
            if best[0] <= self.CREST_LOSS_DB:
                break
        if chosen is None:
            raise RuntimeError(f"no master chain reached the release loudness: {tried_all}")
        self.TARGET_LUFS = chosen[0]
        self.recipe["master"]["target_lufs"] = chosen[0]
        self.recipe["master"]["inserts"] = chosen[1][1]
        self.log("master.limit", "master", "kept", {"target_lufs": chosen[0], "tried": tried_all, "crest_loss_db": chosen[1][2],
                                                   "master_punch": chosen[1][3]})

    # ---------- export: render the recipe as written, check the file written ----------

    def export(self, pool):
        mixer = self.mixer()
        mix, master = mixer.render(pool)
        out = self.song / "renders"
        out.mkdir(exist_ok=True)
        soundfile.write(out / "mix.wav", mix.T, self.rate, subtype="FLOAT")
        temp = out / "master.tmp.wav"
        soundfile.write(temp, master.T, self.rate, subtype="PCM_24")
        written = soundfile.read(temp, always_2d=True, dtype="float64")[0].T.astype(np.float32)
        level, peak = meters.lufs(written, self.rate), meters.true_peak_db(written)
        kick_s, snare_s = self.drum_times(mixer)
        loss = self.crest_loss(mix, written, kick_s, snare_s)
        punch = punch_of(written, self.rate, kick_s, snare_s)
        spec = written[:, self.dense].astype(np.float64)
        side_mid = meters.side_mid_db(meters.filtered(spec, self.rate, 500, 8000))
        self.TARGET_LUFS = self.recipe["master"].get("target_lufs", self.TARGET_LUFS)
        checks = {"master_lufs": r2(level), "master_true_peak_dbtp": r2(peak), "target_lufs": self.TARGET_LUFS,
                  "release_default_lufs": self.LOUDNESS_STEPS[0],
                  "crest_loss_db": loss, "punch_db": punch, "side_mid_500_8k_db": r2(side_mid)}
        gates = {"loudness": abs(level - self.TARGET_LUFS) <= 0.05, "true_peak": peak <= self.TARGET_TP,
                 "crest_loss": max(loss.values(), default=0) <= self.CREST_LOSS_DB,
                 "kick_punch": punch.get("kick_low") is None or punch["kick_low"] >= self.KICK_GATE_DB,
                 "snare_punch": punch.get("snare_body") is None or punch["snare_body"] >= self.SNARE_GATE_DB,
                 "width": side_mid >= self.MIN_SIDE_MID_DB}
        checks["gates"] = {k: bool(v) for k, v in gates.items()}
        checks["export_ok"] = all(gates.values())
        # The references are a printed comparison only: nothing in the run reads it back.
        found = measurelib.profile_range.__wrapped__(written.T.astype(np.float64), self.rate, self.profile_name)
        checks["references_comparison"] = {k: v["outside"] for k, v in found.items() if v["outside"] != 0}
        for stale in ("master.wav", "master-failed-gates.wav"):
            if (out / stale).exists():
                (out / stale).unlink()
        temp.replace(out / ("master.wav" if checks["export_ok"] else "master-failed-gates.wav"))
        (out / "checks.json").write_text(json.dumps(checks, indent=1) + "\n")
        return checks
