import json

import numpy as np
import soundfile

import daw
import engineer
import meters
from conftest import RATE, kicks, noise


def test_master_reaches_profile_loudness_under_the_true_peak_ceiling(tmp_path):
    profile = json.loads((daw.ROOT / "profiles" / "instrumental-prog-metal" / "profile.json").read_text())
    eng = object.__new__(engineer.Engineer)
    eng.cache, eng.rate, eng.TARGET_LUFS = daw.Cache(tmp_path / "cache"), RATE, float(profile["master_integrated_lufs"])
    mix = (kicks(8) + noise(8, level=0.03)).astype(np.float32)
    out, _, _ = eng.trial(mix, "mix", clip_db=-2.0)

    # Checked on the 24-bit file as written, as the export gates are.
    soundfile.write(tmp_path / "master.wav", out.T, RATE, subtype="PCM_24")
    written = soundfile.read(tmp_path / "master.wav", always_2d=True, dtype="float64")[0].T
    assert abs(meters.lufs(written, RATE) - eng.TARGET_LUFS) <= 0.05
    assert meters.true_peak_db(written) <= engineer.Engineer.TARGET_TP
