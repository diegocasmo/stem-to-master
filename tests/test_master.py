import soundfile

import engineer
from conftest import RATE, kicks, noise


def test_export_releases_only_a_master_at_profile_loudness_under_the_true_peak_ceiling(tmp_path, monkeypatch):
    (tmp_path / "stems").mkdir()
    soundfile.write(tmp_path / "stems" / "Kick.wav", kicks(8), RATE, subtype="PCM_24")
    soundfile.write(tmp_path / "stems" / "Gtr.wav", noise(8, level=0.03).T, RATE, subtype="PCM_24")
    eng = engineer.Engineer(tmp_path, workers=1)
    eng.dense = eng.full_band_frames()
    # Punch gates are left out: a synthetic kick cannot keep real punch at release loudness, and this test is about
    # loudness and true peak deciding the release.
    monkeypatch.setattr(engineer, "punch_of", lambda *args: {})
    monkeypatch.setattr(eng, "crest_loss", lambda *args: {})
    mixer = eng.rendered(None)
    _, chain, _ = eng.trial(mixer.nodes["mix"][1], mixer.nodes["mix"][0], clip_db=-2.0)
    eng.recipe["master"] |= {"target_lufs": eng.TARGET_LUFS, "inserts": chain}
    renders = tmp_path / "renders"
    checks = eng.export(None)
    assert checks["export_ok"] and (renders / "master.wav").exists()
    assert abs(checks["master_lufs"] - eng.LOUDNESS_STEPS[0]) <= 0.05

    # The same drive without the limiter is too loud and peaks over the ceiling: it must not be released.
    eng.recipe["master"]["inserts"] = chain[:1]
    checks = eng.export(None)
    assert not checks["gates"]["loudness"] and not checks["gates"]["true_peak"]
    assert (renders / "master-failed-gates.wav").exists() and not (renders / "master.wav").exists()
