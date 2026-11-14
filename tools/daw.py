"""The DAW: renders a recipe (tracks, buses, mix bus, master) with every insert's output cached by its fingerprint.

Audio is (channels, samples). Every insert takes and returns float32, as plugins do, so a cached output and a fresh
one are the same bytes. Faders, pans, sends and bus sums run in float64, in the recipe's fixed order.
"""
import ctypes
import hashlib
import json
import math
import os
import platform
import sys
import tomllib
from importlib import metadata
from pathlib import Path

import numpy as np
import pedalboard
import soundfile

import native

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ROOT / "plugins"
LOCKED = {entry["name"] for entry in tomllib.loads((ROOT / "plugins.lock").read_text())["plugin"]}
LIBC = ctypes.CDLL(None)
RAND_SEED = 1
NATIVE = {"Ride", "Duck", "Shape"}
TOOLS = Path(__file__).resolve().parent


def digest(*parts):
    # Settings keep their order: parameters are set in that order, and some plugins depend on it.
    return hashlib.sha256(json.dumps(parts, separators=(",", ":"), sort_keys=False).encode()).hexdigest()


def _code_tag():
    files = [[name, hashlib.sha256((TOOLS / name).read_bytes()).hexdigest()] for name in ("daw.py", "native.py", "hits.py")]
    # Cached files outlive this process, so the libraries and OS build that computed them are part of every key.
    libs = [metadata.version(name) for name in ("numpy", "scipy", "pedalboard", "numba", "soundfile")]
    return digest(files, libs, sys.version, platform.platform(), os.uname().version)


CODE = _code_tag()
_BUNDLES = {}


def bundle_hash(name):
    bundle = PLUGINS / f"{name}.vst3"
    if not bundle.exists():
        return "built-in"
    stats = tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(bundle.rglob("*")) if p.is_file())
    found = _BUNDLES.get(name)
    if found is None or found[0] != stats:
        content = hashlib.sha256()
        for path, size, _ in stats:
            content.update(f"{Path(path).relative_to(bundle)}:{size}".encode())
            content.update(Path(path).read_bytes())
        found = _BUNDLES[name] = stats, content.hexdigest()
    return found[1]


class Cache:
    """Audio as .npy and measurements as .json, named by key. Writes are atomic, so a reader never sees half a file."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.hits = self.misses = 0

    def _path(self, key, suffix):
        return self.folder / f"{key}{suffix}"

    def get(self, key):
        path = self._path(key, ".npy")
        try:
            audio = np.load(path)
        except (OSError, ValueError, EOFError):
            self.misses += 1
            return None
        os.utime(path)
        self.hits += 1
        return audio

    def put(self, key, audio):
        path = self._path(key, ".npy")
        temp = path.with_name(f"{key}.{os.getpid()}.tmp.npy")
        np.save(temp, np.ascontiguousarray(audio))
        os.replace(temp, path)

    def get_json(self, key):
        try:
            return json.loads(self._path(key, ".json").read_text())
        except (OSError, ValueError):
            return None

    def put_json(self, key, value):
        path = self._path(key, ".json")
        temp = path.with_name(f"{key}.{os.getpid()}.tmp.json")
        temp.write_text(json.dumps(value))
        os.replace(temp, path)

    def prune(self, cap_bytes):
        """Least recently used files go first until the folder fits the cap; a hit refreshes a file's mtime."""
        files = []
        for entry in os.scandir(self.folder):
            if entry.is_file(follow_symlinks=False):
                stat = entry.stat(follow_symlinks=False)
                files.append((stat.st_mtime_ns, stat.st_size, entry.path))
        total = sum(size for _, size, _ in files)
        for _, size, path in sorted(files):
            if total <= cap_bytes:
                break
            os.unlink(path)
            total -= size

    def measured(self, key, name, compute):
        """A measurement of the audio named by key; key None means the audio is uncached, so it is computed."""
        if key is None:
            return compute()
        mkey = digest("measure", key, name)
        found = self.get_json(mkey)
        if found is None:
            found = compute()
            self.put_json(mkey, found)
        return found


def load(plugin, settings):
    settings = dict(settings)
    bundle = PLUGINS / f"{plugin}.vst3"
    if not bundle.exists():
        return getattr(pedalboard, plugin)(**settings)
    # A bundle left in plugins/ but not locked would render here and fail on a fresh clone.
    if plugin not in LOCKED:
        raise ValueError(f"{plugin} is in plugins/ but not in plugins.lock")
    # Dragonfly draws on the process-wide C rand(), so its state is fixed before every load.
    LIBC.srand(RAND_SEED)
    instance = pedalboard.load_plugin(str(bundle))
    for name, value in settings.items():
        if name not in instance.parameters:
            raise ValueError(f"{plugin} has no parameter {name}")
        setattr(instance, name, value)
    return instance


def apply(audio, rate, insert, keys, sections=None):
    """One insert on float32 (channels, samples) audio; keys maps a Duck key's node name to its audio."""
    plugin, settings = insert["plugin"], insert["settings"]
    if plugin in NATIVE:
        context = _Context(keys, sections or {})
        out = {"Ride": native.ride, "Duck": native.duck, "Shape": native.shape}[plugin](audio.T.astype(np.float64), rate, settings, context)
        return np.ascontiguousarray(out.T, dtype=np.float32)
    instance = load(plugin, settings)
    mono = audio.shape[0] == 1 and isinstance(instance, pedalboard.VST3Plugin)
    # Some VST3s accept only stereo, so a mono track goes through them as two equal channels.
    signal = np.repeat(audio, 2, axis=0) if mono else audio
    LIBC.srand(RAND_SEED)
    out = instance(signal, rate)
    if mono:
        out = out.mean(axis=0, keepdims=True)
    return np.ascontiguousarray(out, dtype=np.float32)


class _Context:
    def __init__(self, keys, sections):
        self.keys, self.sections = keys, sections

    def key(self, name):
        return self.keys[name].T.astype(np.float64)


def insert_key(input_key, insert, key_signatures, sections=None):
    extra = [key_signatures[insert["settings"]["key"]]] if insert["plugin"] == "Duck" else []
    if insert["plugin"] == "Ride":
        extra = [sections or {}]
    return digest(input_key, CODE, insert["plugin"], bundle_hash(insert["plugin"]), insert["settings"], extra)


def chain(cache, rate, source_key, source, inserts, keys=None, key_signatures=None, steps=False, sections=None):
    """Runs inserts in order from the longest cached prefix. Returns the output and its key, plus each step's key if asked."""
    keys, key_signatures = keys or {}, key_signatures or {}
    step_keys, key = [], source_key
    for insert in inserts:
        key = insert_key(key, insert, key_signatures, sections)
        step_keys.append(key)
    start, audio = len(inserts), None
    while start > 0 and (audio := cache.get(step_keys[start - 1])) is None:
        start -= 1
    if audio is None:
        audio = source()
    for index in range(start, len(inserts)):
        audio = apply(audio, rate, inserts[index], keys, sections)
        cache.put(step_keys[index], audio)
    final = step_keys[-1] if step_keys else source_key
    return (audio, final, step_keys) if steps else (audio, final)


STEMS_DIGEST = {}


def stem_source(song, track, length):
    """The stem with polarity and timing applied, as float32, and its key from the samples read."""
    path = Path(song) / "stems" / track["stem"]
    stat = path.stat()
    sample_digest = STEMS_DIGEST.get((path, stat.st_size, stat.st_mtime_ns))
    if sample_digest is None:
        sample_digest = STEMS_DIGEST[(path, stat.st_size, stat.st_mtime_ns)] = hashlib.sha256(path.read_bytes()).hexdigest()
    settings = [track.get("invert", False), track.get("offset_samples", 0), track.get("trim_db", 0), length]
    key = digest("stem", CODE, sample_digest, settings)

    def source():
        audio = soundfile.read(path, always_2d=True, dtype="float64")[0].T
        audio = audio * (10 ** (track.get("trim_db", 0) / 20)) * (-1 if track.get("invert", False) else 1)
        offset, placed = track.get("offset_samples", 0), np.zeros((audio.shape[0], length))
        start, end = max(offset, 0), min(offset + audio.shape[1], length)
        if end > start:
            placed[:, start:end] = audio[:, start - offset : end - offset]
        return placed.astype(np.float32)

    return key, source


def pan_gain(audio, fader_db, position):
    """Fader and pan to stereo float64. Mono pans at constant power; stereo pans as a balance."""
    # Explicit float64: under NumPy 2 a float32 array times a NumPy float64 scalar is float64 but times a Python float is
    # float32, so the same fader read from JSON or computed in memory would round differently.
    audio = audio.astype(np.float64)
    gain = float(10 ** (float(fader_db) / 20))
    position = float(position)
    if audio.shape[0] == 1:
        angle = (position + 1) * math.pi / 4
        return np.vstack([audio[0] * (gain * math.cos(angle)), audio[0] * (gain * math.sin(angle))])
    return np.vstack([audio[0] * (gain * min(1, 1 - position)), audio[1] * (gain * min(1, 1 + position))])


def sum_key(parts):
    return digest("sum", CODE, parts)


def length_of(song, recipe):
    return max(soundfile.info(Path(song) / "stems" / t["stem"]).frames + max(t.get("offset_samples", 0), 0)
               for t in recipe["tracks"])


def _track_job(args):
    song, cache_folder, rate, length, track, sections = args
    cache = Cache(cache_folder)
    key, source = stem_source(song, track, length)
    _, final = chain(cache, rate, key, source, track.get("inserts", []), sections=sections)
    return track["name"], final


class Mixer:
    """Renders a recipe. Tracks run in a small process pool; sums, buses, mix and master run here, in recipe order."""

    def __init__(self, song, recipe, cache, workers=2):
        # Rendered as it would be read back from recipe.json, so numbers computed in memory (NumPy scalars) and numbers
        # read from the file give the same samples.
        recipe = json.loads(json.dumps(recipe, default=lambda o: o.item() if hasattr(o, "item") else str(o)))
        self.song, self.recipe, self.cache, self.workers = Path(song), recipe, cache, workers
        self.length = length_of(song, recipe)
        self.rate = soundfile.info(self.song / "stems" / recipe["tracks"][0]["stem"]).samplerate
        self.sections = recipe.get("sections", {})
        self.nodes = {}  # name -> (key, float32 audio after inserts, before fader)

    def ducked(self, node):
        return any(insert["plugin"] == "Duck" for insert in node.get("inserts", []))

    def track_keys(self, pool=None):
        jobs = [(str(self.song), str(self.cache.folder), self.rate, self.length, track, self.sections)
                for track in self.recipe["tracks"] if not self.ducked(track)]
        results = pool.map(_track_job, jobs) if pool is not None and len(jobs) > 1 else map(_track_job, jobs)
        return dict(results)

    def node_audio(self, name):
        key, audio = self.nodes[name]
        return audio

    def render(self, pool=None, stop_at_mix=False):
        finals = self.track_keys(pool)
        for track in self.recipe["tracks"]:
            if track["name"] in finals:
                key = finals[track["name"]]
                audio = self.cache.get(key)
                if audio is None:
                    # Evicted between the worker's write and this read: render it here.
                    key, source = stem_source(self.song, track, self.length)
                    audio, key = chain(self.cache, self.rate, key, source, track.get("inserts", []), sections=self.sections)
                self.nodes[track["name"]] = (key, audio)
        sources = self.recipe["tracks"] + self.recipe.get("buses", [])
        for bus in self.recipe.get("buses", []):
            self.bus(bus["name"], sources)
        for track in self.recipe["tracks"]:
            if track["name"] not in self.nodes:
                self.track_here(track)
        mix_in, mix_key = self.summed("mix", sources)
        mix, mix_key = chain(self.cache, self.rate, mix_key, lambda: mix_in.astype(np.float32),
                             self.recipe.get("mix", {}).get("inserts", []), sections=self.sections)
        self.nodes["mix"] = (mix_key, mix)
        if stop_at_mix:
            return mix, None
        master, master_key = chain(self.cache, self.rate, mix_key, lambda: mix, self.recipe.get("master", {}).get("inserts", []),
                                   sections=self.sections)
        self.nodes["master"] = (master_key, master)
        return mix, master

    def track_here(self, track):
        keys = {i["settings"]["key"]: self.node_audio(i["settings"]["key"]) for i in track["inserts"] if i["plugin"] == "Duck"}
        signatures = {name: self.nodes[name][0] for name in keys}
        key, source = stem_source(self.song, track, self.length)
        audio, key = chain(self.cache, self.rate, key, source, track["inserts"], keys, signatures, sections=self.sections)
        self.nodes[track["name"]] = (key, audio)

    def feeds(self, name, sources):
        """The sources that reach `name`, in recipe order, each with its send gain (None for the main output)."""
        found = []
        for source in sources:
            if source.get("out", "mix") == name:
                found.append((source, None))
            for send in source.get("sends", []):
                if send["bus"] == name:
                    found.append((source, send.get("gain_db", 0)))
        return found

    def summed(self, name, sources):
        total, parts = np.zeros((2, self.length)), []
        for source, send_db in self.feeds(name, sources):
            if source["name"] not in self.nodes:
                # A ducked track whose key is already rendered.
                self.track_here(source)
            key, audio = self.nodes[source["name"]]
            fader = source.get("fader_db", 0) + (send_db or 0)
            total += pan_gain(audio, fader, source.get("pan", 0))
            parts.append([key, source.get("fader_db", 0), send_db, source.get("pan", 0)])
        return total, sum_key(parts)

    def bus(self, name, sources):
        bus = next(b for b in self.recipe["buses"] if b["name"] == name)
        audio, key = self.summed(name, sources)
        keys = {i["settings"]["key"]: self.node_audio(i["settings"]["key"]) for i in bus.get("inserts", []) if i["plugin"] == "Duck"}
        out, key = chain(self.cache, self.rate, key, lambda: audio.astype(np.float32), bus.get("inserts", []),
                         keys, {name: self.nodes[name][0] for name in keys}, sections=self.sections)
        self.nodes[name] = (key, out)
