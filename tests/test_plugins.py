import io

import pytest

import plugins

ENTRY = {"name": "Fake", "version": "1", "url": "https://example.invalid/Fake.dmg", "sha256": "0" * 64,
         "bundle_sha256": "1" * 64}


def test_a_download_with_another_hash_stops_the_install(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "PLUGINS", tmp_path)
    monkeypatch.setattr(plugins.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"not the locked file"))
    with pytest.raises(SystemExit, match="plugin Fake: download sha256 mismatch, expected 0+, actual [0-9a-f]{64}"):
        plugins.install(ENTRY)
    assert not (tmp_path / "Fake.vst3").exists()


def test_an_installed_bundle_with_another_hash_stops_the_install(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "PLUGINS", tmp_path)
    (tmp_path / "Fake.vst3" / "Contents").mkdir(parents=True)
    (tmp_path / "Fake.vst3" / "Contents" / "binary").write_bytes(b"other build")
    with pytest.raises(SystemExit, match="plugin Fake: installed bundle sha256 .* mismatch, expected 1+, actual"):
        plugins.install(ENTRY)
