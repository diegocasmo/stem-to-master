"""Benchmark set: cut a song's stems and its official master to the same window, sample-exact.

uv run tools/benchmark.py <stems dir> <official.mp3> <song dir> --core kick,bass,guitar [--start S] [--length 45]

Vocal and DI stems are left out. The window is the 45 s with the most time where every vocal stem is silent and the
core stems (kick, bass, a main guitar) all play; those seconds are written to notes.json as the graded spans, since no
45 s of these songs is free of vocals. The official is used only by the grader.
"""
import argparse
import json
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import soundfile
from scipy import signal

HOP = 4410
SILENT_DB = -60.0
VOCAL_MARGIN_S = 1.5


def frames_db(audio):
    power = (audio.astype(np.float64) ** 2).mean(axis=1)
    count = len(power) // HOP
    return 10 * np.log10(power[: count * HOP].reshape(count, HOP).mean(axis=1) + 1e-20)


def dilate(mask, reach):
    padded = np.concatenate([np.zeros(reach, bool), mask, np.zeros(reach, bool)])
    return np.lib.stride_tricks.sliding_window_view(padded, 2 * reach + 1).any(axis=1)


def runs(mask, rate_hz=10):
    found, start = [], None
    for index, value in enumerate(np.append(mask, False)):
        if value and start is None:
            start = index
        if not value and start is not None:
            found.append([round(start / rate_hz, 1), round(index / rate_hz, 1)])
            start = None
    return found


def decode(path, rate):
    with tempfile.TemporaryDirectory() as folder:
        out = Path(folder) / "official.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ar", str(rate), "-c:a", "pcm_f32le", str(out)], check=True)
        return soundfile.read(out, always_2d=True, dtype="float32")[0]


def lag_of(stems_mono, official_mono):
    """Samples to add to a stem time to get the official time, to within the decimation step."""
    step = 8
    a = signal.decimate(stems_mono, step, ftype="fir")
    b = signal.decimate(official_mono, step, ftype="fir")
    corr = signal.correlate(b, a, mode="full", method="fft")
    return (int(np.argmax(np.abs(corr))) - (len(a) - 1)) * step


def _fine(a, b, around, reach):
    """The correction to lag `around` within +-reach samples, at full rate."""
    start = max(reach, -around + reach)
    end = min(len(a), len(b) - around) - reach
    seg = a[start:end]
    ref = b[start + around - reach : end + around + reach]
    corr = signal.correlate(ref, seg, mode="valid", method="fft")
    return int(np.argmax(np.abs(corr))) - reach


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stems")
    parser.add_argument("official")
    parser.add_argument("song")
    parser.add_argument("--core", required=True)
    parser.add_argument("--length", type=float, default=45.0)
    parser.add_argument("--start", type=float)
    args = parser.parse_args()
    files = sorted(Path(args.stems).glob("*.wav"))
    vocals = [f for f in files if "Vox" in f.stem]
    dis = [f for f in files if f.stem.endswith("DI") or "DI" in f.stem.split("_", 1)[-1]]
    kept = [f for f in files if f not in vocals and f not in dis]
    audio = {f: soundfile.read(f, always_2d=True, dtype="float32") for f in kept + vocals}
    rate = audio[kept[0]][1]
    total = max(len(a) for a, _ in audio.values())
    frames = total // HOP

    def level(f):
        found = frames_db(audio[f][0])
        return np.concatenate([found, np.full(frames - len(found), -200.0)])[:frames]

    voice = np.zeros(frames, bool)
    for f in vocals:
        voice |= level(f) > SILENT_DB
    voice = dilate(voice, int(VOCAL_MARGIN_S * 10))
    band = np.ones(frames, bool)
    core = {}
    for prefix in args.core.split(","):
        f = next(f for f in kept if f.stem.startswith(prefix))
        lv = level(f)
        playing = dilate(lv > np.percentile(lv, 99) - 25, 5)
        core[f.name] = playing
        band &= playing
    mix = np.zeros(total)
    for f in kept:
        x = audio[f][0].mean(axis=1)
        mix[: len(x)] += x
    official = decode(args.official, rate)
    off_mono = official.mean(axis=1).astype(np.float64)
    coarse = lag_of(mix, off_mono)
    covered = np.zeros(frames, bool)
    covered[max(0, -coarse // HOP + 1) : max(0, (len(off_mono) - coarse) // HOP - 1)] = True
    graded = band & ~voice & covered
    span = int(args.length * 10)
    if args.start is None:
        # Best full-band stretch, vocals allowed: the grader masks them per band and frame.
        counts = np.convolve((band & covered).astype(int), np.ones(span, int), mode="valid")
        start_frame = int(np.argmax(counts))
    else:
        start_frame = int(round(args.start * 10))
    start = start_frame * HOP
    length = int(round(args.length * rate))
    # Alignment and its drift check use the vocal-free full-band samples of the whole song: elsewhere the official carries vocals.
    sample_mask = np.repeat(graded, HOP)[:total]
    sample_mask = np.concatenate([sample_mask, np.zeros(total - len(sample_mask), bool)])
    inside = np.zeros(total, bool)
    inside[start : start + length] = True
    use = sample_mask
    fine = coarse + _fine(np.where(use, mix, 0), off_mono, coarse, int(0.05 * rate))
    where = np.flatnonzero(use)
    thirds = []
    for part in np.array_split(where, 3):
        piece = np.zeros(total)
        piece[part] = mix[part]
        thirds.append(fine + _fine(piece, off_mono, fine, int(0.005 * rate)))

    song = Path(args.song)
    (song / "stems").mkdir(parents=True, exist_ok=True)
    written, silent = [], []
    for f in kept:
        x, _ = audio[f]
        cut = np.zeros((length, x.shape[1]), np.float32)
        piece = x[start : start + length]
        cut[: len(piece)] = piece
        if np.abs(cut).max() < 10 ** (SILENT_DB / 20):
            silent.append(f.name)
            continue
        soundfile.write(song / "stems" / f.name, cut, rate, subtype="PCM_24")
        written.append(f.name)
    (song / "vocals").mkdir(exist_ok=True)
    for f in vocals:
        x, _ = audio[f]
        cut = np.zeros((length, x.shape[1]), np.float32)
        piece = x[start : start + length]
        cut[: len(piece)] = piece
        if np.abs(cut).max() >= 10 ** (SILENT_DB / 20):
            soundfile.write(song / "vocals" / f.name, cut, rate, subtype="PCM_24")
    o_start = start + fine
    cut = np.zeros((length, official.shape[1]), np.float32)
    piece = official[max(o_start, 0) : o_start + length]
    cut[max(-o_start, 0) : max(-o_start, 0) + len(piece)] = piece
    soundfile.write(song / "official.wav", cut, rate, subtype="FLOAT")

    window = slice(start_frame, start_frame + span)
    notes = {
        "source_stems": str(Path(args.stems).name),
        "official": Path(args.official).name,
        "window_s": [round(start / rate, 3), round((start + length) / rate, 3)],
        "official_offset_samples": int(fine),
        "official_offset_s": round(fine / rate, 4),
        "drift_check_samples": [int(t - fine) for t in thirds],
        "excluded_vocal": [f.name for f in vocals],
        "excluded_di": [f.name for f in dis],
        "silent_in_window": silent,
        "stems": written,
        "core": {k: round(float(v[window].mean()), 2) for k, v in core.items()},
        "full_band_fraction": round(float(band[window].mean()), 2),
        "graded_spans_s": runs(graded[window]),
        "graded_s": round(float(graded[window].sum()) / 10, 1),
        "why_graded_spans": "no 45 s of the song has every vocal stem silent; the grader scores only the spans where all "
                            "vocal stems are silent (with 1.5 s margin) and kick, bass and the main guitar play",
    }
    (song / "notes.json").write_text(json.dumps(notes, indent=1) + "\n")
    print(json.dumps({k: v for k, v in notes.items() if k not in ("excluded_vocal", "stems")}, indent=1))


if __name__ == "__main__":
    main()
