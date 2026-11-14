import json

import numpy as np
import pytest
import soundfile

import grade
from conftest import RATE, kicks, noise

SECONDS, DELAY = 12, 40


def test_grade_aligns_the_official_and_masks_the_vocal(tmp_path):
    for folder in ("stems", "vocals", "renders"):
        (tmp_path / folder).mkdir()
    kick, gtr = kicks(SECONDS), noise(SECONDS)
    t = np.arange(SECONDS * RATE) / RATE
    vox = np.where((t > 4) & (t < 8), 0.5, 0.0) * np.sin(2 * np.pi * 1000 * t)
    soundfile.write(tmp_path / "stems" / "Kick.wav", kick, RATE, subtype="FLOAT")
    soundfile.write(tmp_path / "stems" / "Gtr.wav", gtr.T, RATE, subtype="FLOAT")
    soundfile.write(tmp_path / "vocals" / "Vox.wav", vox, RATE, subtype="FLOAT")
    instruments = kick + gtr
    official = np.roll(instruments + vox, DELAY, axis=1)
    soundfile.write(tmp_path / "official.wav", official.T, RATE, subtype="FLOAT")
    soundfile.write(tmp_path / "renders" / "master.wav", 0.5 * instruments.T, RATE, subtype="FLOAT")
    (tmp_path / "notes.json").write_text(json.dumps({"graded_spans_s": [[0, SECONDS]]}))

    g = grade.grade(tmp_path)
    assert g["align_lag_samples"] == DELAY
    # Timing-sensitive, so it also catches a lag found right but applied the wrong way.
    assert abs(g["punch"]["kick_low"]) < 0.3
    assert g["match_gain_db"] == pytest.approx(6.02, abs=0.1)
    # The vocal is far louder than the guitar at 1 kHz: unmasked, that band would read many dB short.
    assert all(abs(v) < 0.3 for v in g["bands"].values())
