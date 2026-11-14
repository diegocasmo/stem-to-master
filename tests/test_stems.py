import numpy as np
import pytest
import soundfile

import stems
from conftest import RATE, kicks


# Rule order: a sample is still a snare, a bass drum is not a bass, a two-letter "OH" token is an overhead.
@pytest.mark.parametrize("name, cls", [("Bass Drum", "kick"), ("SnareSample", "snare"), ("OH L", "overheads")])
def test_name_hint(name, cls):
    assert stems.hint(name) == cls


def test_classify_leaves_out_vocals_di_and_silence_and_reads_unnamed_audio(tmp_path):
    t = np.arange(4 * RATE) / RATE
    files = {"Lead Vox": 0.1 * np.sin(2 * np.pi * 300 * t), "BassDI": 0.1 * np.sin(2 * np.pi * 50 * t),
             "Track 7": np.zeros_like(t), "Track 8": 0.3 * np.sin(2 * np.pi * 50 * t), "Track 9": kicks(4, every_s=1.0)}
    for name, x in files.items():
        soundfile.write(tmp_path / f"{name}.wav", x, RATE, subtype="FLOAT")
    found, left_out = stems.classify(tmp_path)
    assert {s["stem"]: (s["class"], s["by"]) for s in found} == {"Track 8.wav": ("bass", "content"), "Track 9.wav": ("kick", "content")}
    assert {s["stem"]: s["why"] for s in left_out} == {"Lead Vox.wav": "vocal", "BassDI.wav": "di", "Track 7.wav": "silent"}
