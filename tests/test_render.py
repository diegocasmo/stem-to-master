import copy

import soundfile

import daw
from conftest import RATE, kicks, noise
from engineer import zl_eq

SHAPE = {"band_hz": [None, 150], "hold_ms": 30.0, "fade_ms": 40.0, "depth_db": 6.0, "floor_db": 30}
DUCK = {"key": "kick", "band_hz": [None, 150], "threshold_db": -30.0, "ratio": 4, "range_db": 3.0, "attack_ms": 1, "release_ms": 80}
RECIPE = {
    "sections": {},
    "tracks": [
        {"name": "Kick", "stem": "Kick.wav", "out": "kick", "fader_db": -2.0, "pan": 0.0,
         "inserts": [{"plugin": "Shape", "settings": SHAPE}]},
        {"name": "Gtr", "stem": "Gtr.wav", "out": "gtr", "fader_db": 1.5, "pan": -0.3, "sends": [{"bus": "verb", "gain_db": -12}],
         "inserts": [{"plugin": "HighpassFilter", "settings": {"cutoff_frequency_hz": 90.0}},
                     {"plugin": "ZL Equalizer 2", "settings": zl_eq({"type": "Peak", "freq": 2500, "gain": 2.0, "q": 1.0})}]},
    ],
    "buses": [
        {"name": "kick", "out": "mix", "inserts": []},
        {"name": "gtr", "out": "mix", "inserts": [{"plugin": "Duck", "settings": DUCK}]},
        {"name": "verb", "out": "mix", "inserts": [{"plugin": "DragonflyPlateReverb", "settings": {}}]},
    ],
    "mix": {"inserts": [{"plugin": "ZL Equalizer 2", "settings": zl_eq({"type": "High Shelf", "freq": 8000, "gain": 1.5})}]},
    "master": {"inserts": [{"plugin": "Gain", "settings": {"gain_db": 6.0}},
                           {"plugin": "ZL Limiter", "settings": {"output_ceiling_output_ceiling": -1.3, "true_peak_true_peak": True}}]},
}


def master(song, recipe, cache_folder):
    return daw.Mixer(song, recipe, daw.Cache(cache_folder)).render()[1].tobytes()


def test_cached_render_equals_fresh(tmp_path):
    (tmp_path / "stems").mkdir()
    soundfile.write(tmp_path / "stems" / "Kick.wav", kicks(3), RATE, subtype="PCM_24")
    soundfile.write(tmp_path / "stems" / "Gtr.wav", noise(3).T, RATE, subtype="PCM_24")
    cold = master(tmp_path, RECIPE, tmp_path / "a")
    assert master(tmp_path, RECIPE, tmp_path / "a") == cold

    # A changed insert resumes from the cached prefix and must still equal a render from nothing.
    changed = copy.deepcopy(RECIPE)
    changed["tracks"][1]["inserts"][1]["settings"] = zl_eq({"type": "Peak", "freq": 2500, "gain": -2.0, "q": 1.0})
    assert master(tmp_path, changed, tmp_path / "a") == master(tmp_path, changed, tmp_path / "b")
